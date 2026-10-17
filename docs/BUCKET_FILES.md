# Files Customer Records sends to the main app's bucket

For the main app's developers. Customer Records (this repository) runs beside the main app and hands its data over
through the main app's bucket, as CSV files the main app loads into its ClickHouse. Customer Records only **writes**
the files; loading them is the main app's side. Since 2026-10-15 nothing picks up files from the bucket by itself
(the S3 ClickPipes were retired; the dashboard's import runs `imports.ingest`), so these two folders need their own
loading: a ClickPipe or S3Queue table on each, or a job that runs the same `INSERT … SELECT … FROM s3()/url()` as
`imports.ingest` for each new file.

Sent since Customer Records 1.7.0 (2026-10-17), by `records/bucket.py`.

## Where and what

`BUCKET_PREFIX` (default `data/`) of the bucket in `BUCKET_NAME` (`master-bucket`):

| File | Written when |
|---|---|
| `customer-records/<ULID>-<merchant>-import-<import uid>.csv` | an import completes (its run in Customer Records finished) |
| `customer-records/<ULID>-<merchant>-import-<import uid>-deleted.csv` | the records of an import that was sent are deleted in Customer Records (to fix its mapping and run it again, usually) |
| `customer-records-tags/<ULID>-<merchant>-tag-<tag uid>.csv` | a tag is exported, or sent with *Send to the main app*; and once more when a tag that was sent is deleted |

* **Names** follow the main app's import files: `<ULID>-<merchant id>-<name>.csv`. The ULID is when the file was
  written (`ULIDStringToDateTime(substring(_file, 1, 26))`); a later file of the same import or tag always has a later
  ULID, so it can be the `version` of a ReplacingMergeTree. The merchant ID is the main app's (a number), read with the
  same expression as the import tables: `extract(_file, '^[0-9A-HJKMNP-TV-Z]{26}-(\\d+)-')`.
* **uid**: the import's or tag's UUID in Customer Records. Unique for good (Customer Records' own numeric IDs start
  again after its reset script; the UUIDs don't).
* **Format**: UTF-8 CSV, comma, `"` quoting, `\n` line ends, a header row (`CSVWithNames`). Empty = no value.
* **Order**: files go one at a time in the order they were queued; one that fails is retried (1, 2, 4… minutes, at most
  an hour apart) until it goes, so a file can arrive late. An import's deletion waits until the import's own send is
  settled, so it is never written before an import file that arrives. An import whose records were deleted before its
  file went is never sent; when a try had been made (a failed or cut-off upload can still have landed), its deletion
  is sent anyway, so a deletion can arrive for an import file that never did (harmless: it matches nothing).
* Only merchants of the main app are sent (their ID is a number). Customer Records never deletes files from the
  bucket.

## An import's file

One row per **record**, i.e. per row of the file that was imported (a row without a usable phone number is a record
too). Customer Records keeps one record per file row with all its numbers, unlike the converter's one row per
number.

| Column | Value |
|---|---|
| `record_key` | `<import uid>:<file row>`: the record's key, for good (also in the deletion file) |
| `import_uid` | the import's UUID |
| `is_deleted` | `0` here; `1` in a deletion file |
| `merchant_id` | the main app's merchant ID (also in the file name) |
| `location_id` | the main app's location (a UUID, lower-case) the file was imported for; empty for imports made before Customer Records 1.6.0 |
| `service_id`, `service` | the service chosen for the import in Customer Records (its ID there, its name) |
| `import_id`, `record_id` | Customer Records' own numbers for the import and the record (for people; use the uid and key as keys) |
| `file_row` | the row in the imported file, counted as a spreadsheet does (row 1 is the header) |
| `file_name`, `imported_at`, `imported_by` | the imported file's name, when the import completed (UTC, `2026-10-17T18:14:18Z`), who imported it |
| `phone_number` | the record's first usable number, 10 digits, no leading 1; empty when it has none |
| `phone_numbers` | all its usable numbers, `|`-separated (`5165550100|6315550101`) |
| `phone_issue` | why there is no usable number (`no phone number`, `too few digits (7)`…); empty otherwise |
| `first_name`, `last_name`, `email`, `address1`, `address2`, `city`, `state` (2 letters), `zip` (5 digits) | as cleaned by the converter's rules |
| `gender` | `F`, `M` (the file said so), `Fb`, `Mb` (guessed from the first name), or empty |
| `total_visits`, `total_spent` | whole number; decimal with a dot (`12.5`) |
| `first_visit` | the sign-up / creation date, `YYYY-MM-DD HH:MM:SS` |
| `transactions_last` | the last visit or purchase, `YYYY-MM-DD` (day only) |
| `calls_last`, `in_sms_last`, `redemptions_last`, `spin_last`, `last_contacted` | `YYYY-MM-DD HH:MM:SS` |
| `birthday`, `anniversary` | `YYYY-MM-DD`; the year **1600** means the year is unknown (month and day are right) |
| `consent` | `true` / `false` as the file said; empty when it didn't (tag exports leave consent blank: the main app's opt-outs decide) |
| `first_visit_as` | when the imported file had **no** last-activity column at all, the activity its sign-up date stands for (`transactions_last`, or `calls_last` for a call log), as the converter does; else empty |
| `source` | the imported row's cells that aren't empty, each exactly as read from the file, as a JSON object (`{"First Name":"Ann",…}`); empty cells are left out, and the columns' order isn't kept |

Date-times are the client's local time as written in the file (no zone); `imported_at` is UTC. Values were cleaned
by the converter's rules (docs/converter/): the same phone, date and name handling as the generic CSV import.

**A deletion file** has the same columns; each row has `record_key`, `import_uid`, `is_deleted` = `1`,
`merchant_id`, `location_id`, `service_id`, `service`, `import_id` and `file_row`, and nothing else. With
`ReplacingMergeTree(version, is_deleted)` ordered by the key, the deleted records disappear from `FINAL` reads.

## A tag's file

A **snapshot** of the tag: all its numbers at the time the file was written. The newest file of a tag replaces all
earlier ones (a number removed from the tag is simply not in the next file).

The columns are the import format's (`schemas/target_schema.yaml`, the generic CSV import's: `phone_number,
consent, calls_last, transactions_last, last_contacted, in_sms_last, redemptions_last, spin_last, birthday,
anniversary`, with its formats: `10/01/2026 12:00 AM`, `03/04/1980`), so the file is also a valid generic CSV import,
then:

| Column | Value |
|---|---|
| `tag_uid`, `tag_id`, `tag_name` | the tag's UUID, its number and name in Customer Records |
| `merchant_id` | the main app's merchant ID (a tag holds one merchant's records, of any of its locations) |
| `row_kind` | `tag` on the **first row**, which has no number and says which tag this is (so even an emptied or deleted tag replaces its earlier snapshot); `number` on every other row |
| `tag_deleted` | `1` on the first row of the last file of a tag deleted in Customer Records (then there are no number rows); else `0` |

One row per unique number of the tag's records; when a number is in several records, its dates are merged (the
latest of each activity; the birthday with a real year) and consent is left blank. Records without a usable number
aren't in it.

## ClickHouse tables that load them

Checked 2026-10-17: files written by Customer Records 1.7.0 loaded into these tables in ClickHouse 26.9 (deleted
records gone with `FINAL`; each tag's latest snapshot correct), and the expressions run on the `sstxt` service
(26.6) against a file read with `format()`. Written in the style of `deploy/clickhouse/*.sql`: text columns as
received, typed ones `MATERIALIZED`.

```sql
CREATE TABLE default.customer_records
(
    `record_key`        String,
    `import_uid`        UUID,
    `is_deleted`        UInt8,
    `location_id`       String,
    `service_id`        UInt32,
    `service`           LowCardinality(String),
    `import_id`         UInt64,
    `file_row`          UInt32,
    `file_name`         String,
    `imported_at`       String,
    `imported_by`       LowCardinality(String),
    `phone_number`      String,
    `phone_numbers`     String,
    `phone_issue`       String,
    `first_name`        String,
    `last_name`         String,
    `gender`            LowCardinality(String),
    `email`             String,
    `address1`          String,
    `address2`          String,
    `city`              String,
    `state`             LowCardinality(String),
    `zip`               String,
    `total_visits`      String,
    `total_spent`       String,
    `first_visit`       String,
    `transactions_last` String,
    `calls_last`        String,
    `in_sms_last`       String,
    `redemptions_last`  String,
    `spin_last`         String,
    `last_contacted`    String,
    `birthday`          String,
    `anniversary`       String,
    `consent`           String,
    `first_visit_as`    LowCardinality(String),
    `source`            String,
    `_path`             String,
    `_file`             String,
    `merchant_id`       UInt16             MATERIALIZED toUInt16(extract(_file, '^[0-9A-HJKMNP-TV-Z]{26}-(\\d+)-')),
    `version`           DateTime64(3)      MATERIALIZED ULIDStringToDateTime(substring(_file, 1, 26)),
    `phone`             UInt64             MATERIALIZED toUInt64OrZero(phone_number),
    `phones`            Array(UInt64)      MATERIALIZED arrayFilter(x -> x > 0, arrayMap(x -> toUInt64OrZero(x), splitByChar('|', phone_numbers))),
    `transaction_at`    Nullable(DateTime) MATERIALIZED parseDateTimeBestEffortOrNull(transactions_last),
    `call_at`           Nullable(DateTime) MATERIALIZED parseDateTimeBestEffortOrNull(calls_last),
    `in_sms_at`         Nullable(DateTime) MATERIALIZED parseDateTimeBestEffortOrNull(in_sms_last),
    `redemption_at`     Nullable(DateTime) MATERIALIZED parseDateTimeBestEffortOrNull(redemptions_last),
    `spin_at`           Nullable(DateTime) MATERIALIZED parseDateTimeBestEffortOrNull(spin_last),
    `contacted_at`      Nullable(DateTime) MATERIALIZED parseDateTimeBestEffortOrNull(last_contacted),
    `first_visit_at`    Nullable(DateTime) MATERIALIZED parseDateTimeBestEffortOrNull(first_visit),
    `consent_flag`      Nullable(Bool)     MATERIALIZED if(consent = '', NULL, consent = 'true')
)
ENGINE = ReplacingMergeTree(version, is_deleted)
PARTITION BY merchant_id
ORDER BY (merchant_id, record_key);

CREATE TABLE default.customer_records_tags
(
    `phone_number`      String,
    `tag_uid`           UUID,
    `tag_id`            UInt32,
    `tag_name`          String,
    `row_kind`          LowCardinality(String),
    `tag_deleted`       UInt8,
    `_path`             String,
    `_file`             String,
    `merchant_id`       UInt16        MATERIALIZED toUInt16(extract(_file, '^[0-9A-HJKMNP-TV-Z]{26}-(\\d+)-')),
    `version`           DateTime64(3) MATERIALIZED ULIDStringToDateTime(substring(_file, 1, 26)),
    `phone`             UInt64        MATERIALIZED toUInt64OrZero(phone_number)
)
ENGINE = MergeTree
PARTITION BY merchant_id
ORDER BY (merchant_id, tag_uid, version, phone);
```

Loading a file: as `imports.ingest` does, e.g.

```sql
INSERT INTO default.customer_records (record_key, import_uid, …, source, _path, _file)
SELECT record_key, import_uid, …, source, '<bucket>/<key>', '<file name>'
FROM s3('<file URL>', '<key id>', '<secret>', 'CSVWithNames')
SETTINGS input_format_skip_unknown_fields = 1;
```

(`merchant_id` and `record_id` in the file aren't columns there: the merchant comes from the file name, and
`input_format_skip_unknown_fields` skips them. A tag file's import-format columns are skipped the same way.)

Reading:

```sql
-- The records as they are now (deleted ones left out):
SELECT * FROM default.customer_records FINAL WHERE merchant_id = 63;

-- Each tag's numbers, from its latest snapshot (an emptied or deleted tag has none):
SELECT merchant_id, tag_uid, groupArrayIf(phone, row_kind = 'number') AS numbers, max(tag_deleted) AS deleted
FROM default.customer_records_tags
WHERE (tag_uid, version) IN (SELECT tag_uid, max(version) FROM default.customer_records_tags GROUP BY tag_uid)
GROUP BY merchant_id, tag_uid;
```

Into the audiences fold, like the generic CSV import (`m_<kind>_generic_csv_import`, `mv_sources_from_…`), e.g.

```sql
CREATE MATERIALIZED VIEW audiences.m_transactions_customer_records TO audiences.transactions AS
SELECT merchant_id, phone AS phone_number,
       if(transaction_at IS NULL AND first_visit_as = 'transactions_last', first_visit_at, transaction_at) AS timestamp,
       'customer-records' AS source
FROM default.customer_records
WHERE phone > 0 AND is_deleted = 0
  AND (transaction_at IS NOT NULL OR (first_visit_as = 'transactions_last' AND first_visit_at IS NOT NULL));
```

Two things the fold can't do by itself, to decide on the main app's side:

* **Deletions**: the fold only adds, so a deleted import's activity and sources stay in `audiences.*` unless they are
  rebuilt (the records table itself drops them with `FINAL`).
* **Tags as segments**: a tag's numbers come and go between snapshots, which `audiences.sources` (it only adds labels)
  can't follow; a segment source "Customer Records tag" should read the latest snapshot (the query above) and keep
  the evaluation's gates (opt-outs, Do Not Call with the EBR windows, mobile only).
