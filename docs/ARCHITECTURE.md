# Architecture

How Customer Records is put together, the database, the design decisions, how it is tested, and how it can be
connected to the main system. The conversion engine it is built on is described in
[converter/ARCHITECTURE.md](converter/ARCHITECTURE.md).

## Design principles

1. **The converter decides every value.** Files are read, matched and cleaned by the file converter's engine
   (`converter/`, 0.23.0), unchanged except for two small additions (below). Every rule, approved repair and
   business decision of the converter applies to records; fixes made to the converter can be ported in.
2. **Keep everything, decide later.** Every file row is a record, with its cleaned fields *and* the row as written.
   Rows without a usable number are kept. Nothing is merged at import; merging happens when a tag is exported.
3. **Same output as the converter.** A tag of one file's records exports what the converter makes of that file
   (consent and repeated numbers apart). `scripts/regression/parity.py` checks it on 406 files.
4. **Schema-driven.** The record fields are `schemas/records_schema.yaml`; their database columns are created from
   it. The export format is the converter's `schemas/target_schema.yaml`; export columns are filled by name.
5. **PostgreSQL for the business data**, the same database as the main system, so connecting the two later is a
   matter of schemas and IDs, not of moving data between engines.

## The path of the data

```
 Browser (app.py, records/ui.py)                     packages
 ───────────────────────────────                     ────────
 New import: merchant, location, service,  ─────────►  converter.reader / mapping / services (as in the converter)
   review mapping
 Start import ────────────────────────────────────►  records.store.create_import (PostgreSQL, status queued)
                                                      converter Store.create_import (SQLite job, read_options
                                                        carry records_import_id) → records.runner submit
 worker process (one per running import) ─────────►  converter.jobs.run_import → engine.convert (record fields)
                                                      → audit/source parquet (side by side)
                                                      → records.importer.RecordSink.save: group the engine's
                                                        rows per file row, type the values, COPY into records
                                                        and record_phones, complete the import
 Find: requests, found-set steps ────────────────►  records.find (node tree) → records.criteria (one SQL
                                                      condition per criterion) → counts / page / CSV
 Tag the found set ───────────────────────────────►  records.store.add_to_tag / remove_from_tag (+ tag_events)
 Tag export ──────────────────────────────────────►  records.tags.export_table (one row per number, merge rules)
                                                      → converter.output.write_output (import format)
 (background, 1.7.0) ─────────────────────────────►  records.bucket.BucketSender: completed imports, deletions,
                                                      tags exported / sent → CSV files in the main app's bucket
                                                      (R2, S3 API, boto3) for its ClickHouse
```

## Two databases

| | Holds | Why |
|---|---|---|
| **PostgreSQL** (`DATABASE_URL`) | businesses (the main app's merchants), locations, services, imports, records, record_phones, tags, tag_records, tag_events, bucket_sends | the business data: shared, searched, eventually connected to the main system |
| **SQLite** `data/converter.db` | import jobs (status, progress, mapping and schema snapshots, summary), templates, drafts, users, preferences | the converter's own app state, unchanged, so its pages (Imports, side by side, templates) work as they are |

The link: a PostgreSQL import has `job_id` (the SQLite import's id, the number shown on the Imports page), and the
job's `read_options` has `records_import_id`. See KNOWN_ISSUES #1 for what that implies.

## Modules (`records/`)

| Module | Responsibility |
|---|---|
| `config.py` | `DATABASE_URL`, `RECORDS_SCHEMA_PATH`, `EXPORT_SCHEMA_PATH`, `IMPORT_WORKERS` (5 here; the converter's default is 2), `DATABASE_POOL_SIZE` (16) (`.env` is loaded by `converter.config`). |
| `db.py` | Connection pool (psycopg 3 + psycopg_pool); `ensure_database` (creates the database when allowed, with ICU case rules so Å = å); `Database.setup`: `pg_trgm`, the fixed tables, one column per record field (added when missing; a retyped field only warns, except a date-and-time field made `date_only`, converted to a date), indexes (trigram for text and the split parts of whole names, b-tree for the rest and for the Date expression), and `collation` (None when the database folds accented letters; otherwise an explicit collation for finds, `_case_collation`); `check_fields` (names must be valid columns, one phone field); `date_fields` / `date_expr` (the Date: the later of the last visit and last call, as a day). |
| `store.py` | `RecordStore`: merchants (table `businesses`, its 1.0 name; "business" in the code means a merchant) and locations (`load_merchants`: the main app's lists, missing ones marked removed, never deleted; `list_locations`), services, imports (`create_import`: one current location of its merchant, the only one taken by itself), records, tags; the rules of one merchant per tag, unique names, deletions only when unused, tag history. All SQL except finds and exports. |
| `bucket.py` | Sending to the main app's bucket (1.7.0): the files (`write_records_file`: one row per record; `write_delete_file`: the same keys, `is_deleted` 1; `write_tag_file`: a tag snapshot in the import format plus the tag's columns, a first row naming the tag), their names (`object_key`, `new_ulid`: the main app's `<ULID>-<merchant>-<name>.csv`), `BucketClient` (boto3, path-style, checksums only when required; errors without the key), `BucketSender` (a background thread over the queue `bucket_sends`: queues completed imports whose run finished, sends in order, retries 1, 2, 4… minutes up to an hour, recovers a send cut off by a restart; nothing without `BUCKET_*`) and what the pages read (`import_sends`, `tag_sends`, `summary`, `retry_now`). The file format: [BUCKET_FILES.md](BUCKET_FILES.md). |
| `merchants.py` | The main app's merchants and locations (1.6.0): `QUERY` (one row per location, plus one per merchant without one, from ClickHouse's `pg.merchants` / `pg.locations`), `parse_rows`, `rows_from_file` (the console's CSV/TSV export, NULL as `\N`), `fetch_clickhouse` (ClickHouse's HTTP interface; user and password in its headers), `MerchantRefresher` (at start, every `MERCHANTS_REFRESH_MINUTES` in the background, and the Merchants page's button; nothing without `CLICKHOUSE_*`). |
| `importer.py` | `RecordSink` (the runner's sink): `build_records` (engine audit → one record per file row, typed values, all numbers, the row as written minus skipped columns), `save` (one transaction: COPY records and numbers, complete the import), `discard`; `creation_fallback` (see Tag export). |
| `fields.py` | What a find can search: the record fields plus Date (an expression, `FindField.expr`), Any field, Full name, Business, Service, Tag, Phone problem, Import #, File name, Record created, Record ID, File row, Column: …; which have pickers instead (`PICKER_KEYS`). First and Last name are searched through `criteria.name_condition` (also the parts of a whole name kept in First name). |
| `criteria.py` | One criterion → one SQL condition, per kind of field (text, phone, number, date, true/false, business, …), FileMaker syntax; values always as parameters; every condition `COALESCE(…, false)`. The module docstring is the syntax reference (also shown on the page). |
| `find.py` | Requests (merchants, locations, services, criteria; all optional) → found-set nodes (find, constrain, extend, omit records, show omitted, all, tag) → one SQL condition; counts, pages (sorts, the Date as `activity_date`), CSV (with Date and Day), `record_date` / `day_name`; how the screen shows dates (`shown`, `date_columns`, `DAY_FORMAT`: "Fri, 10/9/2026"). |
| `gender.py` | Gender: `stated` (a file's Female, Woman, F - Female, ♀… → F / M), `unknown` (Unknown, N/A, codes, Dr), `other_answer`, `guess` (one SQL UPDATE: no guess for two people; Fb / Mb from a title, the first name in `first_names` (≥ 90% of one sex, ≥ 10 people; accents off, also in C-locale databases; a hyphenated name's first part) or the email's first word; never over F / M or a file's other answer); `read_stored_files` (upgrading: what the stored rows' Gender column said, in the column `header_column` picks as the rules would); `load_names` / `names_version` (the shipped `names/first_names.csv.gz`, built by `scripts/names/build_first_names.py` from US SSA baby names 1930–2017, CC0). |
| `runner.py` | `ProcessImportRunner` (1.5.0): the converter's `ImportRunner` (slots, queue, cancel flag, "interrupted" at start-up, purge) with each import run in a new worker process, `python -m records.runner <job database> <job id> <server pid>` (started with `subprocess` in its own process group; `multiprocessing` would run app.py again in each one). The process first connects to PostgreSQL (failing the import with PostgreSQL's reason if it can't), runs `converter.jobs.run_import` with its own `RecordSink` and up to two PostgreSQL connections, and exits. A process that ends without an outcome fails its import with the reason (`exit_reason`: signal 9 = out of memory) and `RecordSink.discard` runs. When the server shuts down (`server_stopping`: a `threading._register_atexit` hook, which runs before Python joins the slots' threads), running worker processes are terminated and their imports, and the queued ones, end *interrupted* with what they stored removed. A new runner removes what imports left running by the previous server stored. A process whose server is gone exits within a second (`_stop_with_server`, macOS and Linux). |
| `tags.py` | A tag's export: one row per unique number, filled by name from the record fields with the merge rules, written with the converter's writer; the file name. |
| `ui.py` | The Find, Tags, Businesses and Services pages (Streamlit), given a `Ctx` by `app.py`. |

`app.py` is the converter's app with Customer Records' changes: the merchant/location/service step and the
duplicate-file warning on New import, `start_import` / `rerun_import` registering imports in PostgreSQL, `can_rerun`
(no re-run while records are stored, or once the location left the main app), merchant/location/service/records on
the Imports pages, the merchant loader (`get_merchant_refresher`), *Delete this import's records*, the Fields page,
the account check after sign-in (1.5.0: a deleted account is signed out, a changed admin right applies at once), the
navigation (Number history not shown), the sidebar, and both mapping tables drawn at full height
(`height="content"`, 1.0.2: they get a new key on every edit, so a table scrolled inside itself jumped back to
its first row).

### Additions to the converter (`converter/`)

Kept small so fixes from file-converter apply cleanly ([HANDOFF.md](HANDOFF.md), section 5):

| File | Change |
|---|---|
| `schema.py` | `FieldSpec.creation_date` (validated: date/date-time fields without `generic_date`); `FieldSpec.header_words` / `header_not_words` (1.4.0: the rules map a column to such a field only when its header has one of the words, or is an exact name, and never with one of the not-words). |
| `heuristics.py` | Step 7 (`_map_generic_dates`) ignores `creation_date` fields when deciding whether a file already has a last date. The converter's format has none, so its results are unchanged (regression check: identical). |
| `heuristics.py` (1.4.0) | `score` returns 0 for a field with `header_words` when the header has none of them (unless it is an exact name), and for a header with one of its `header_not_words`. The converter's format uses neither, so its results are unchanged (regression check: identical). |
| `jobs.py` | `run_import(..., sink=None)` and `ImportRunner(..., sink=None)`: with a sink, `sink.save` replaces the number history step and adds to the summary; `sink.discard` runs when the import then ends cancelled or failed. |

## Tables (PostgreSQL)

| Table | Columns (main) |
|---|---|
| `businesses` | the main app's merchants: `id` (text: the merchant ID's digits), `name`, `status` (active / archived), `removed_at` (no longer in the main app's list), `synced_at`, `notes` (unused since 1.6.0), created |
| `locations` | the main app's locations: `id` (its UUID, lower-case), `business_id` (its merchant), `name`, `address`, `kind` (store / office), `removed_at`, `synced_at` |
| `services` | `id`, `name` (unique, any case), `active` |
| `imports` | `id`, `uid` (a UUID naming it in the main app's bucket, 1.7.0), `job_id`, `business_id`, `location_id` (NULL before 1.6.0), `service_id`, `file_name`, `file_sha1`, `sheet`, `username`, `status` (queued / completed / not completed / deleted), counts (`records`, `phones`, `no_phone`), `first_visit_as`, `columns` (the file's column names) |
| `records` | `id`, `import_id` (cascade), `business_id`, `location_id` (the import's), `service_id`, `file_row`, `phone_issue`, `issues` (jsonb), `source` (jsonb: the row as written), `search_text`, and one column per record field |
| `record_phones` | `record_id` (cascade), `phone` (10 digits), `position` (the order of the phone field's columns, mobile first), `repaired`, `original`; b-tree and trigram indexes |
| `tags` | `id`, `uid` (a UUID, 1.7.0), `business_id` (its merchant), `name` (unique per merchant, any case), `notes`, created, updated |
| `tag_records` | `tag_id`, `record_id` (both cascade), added at / by |
| `tag_events` | `tag_id`, at, username, action (created / added / removed / renamed / exported), count, detail |
| `bucket_sends` | the queue of files for the main app's bucket (1.7.0): `kind` (import / delete / tag), `import_id` (cascade) or `tag_id`, `tag_uid`, `tag_name`, `business_id`, `file_rows` (a deletion's records), `status` (pending / sending / sent / cancelled), `attempts`, `next_try_at`, `claimed_at`, `error`, `object_key`, `rows`, `bytes`, created / by, `sent_at`; one import send and one deletion per import (unique), one pending snapshot per tag |
| `app_meta` | small facts (pg_trgm available, the version of the loaded name table) |
| `first_names` | `name` (lower case), `female`, `male`: US first names with how many girls and boys got them, for gender guesses |

Foreign keys to `businesses` and `locations` are `ON UPDATE CASCADE`. Merchants and locations are never deleted
(imports, records and tags refer to them); one the main app no longer lists gets `removed_at`. A location moved to
another merchant in the main app keeps its records under the merchant they were imported for.

## Key behaviours and where they live

| Behaviour | Where |
|---|---|
| One record per file row; all its numbers; empty rows skipped; rejected rows (only with a required field) not stored | `importer.build_records` |
| Rows without a usable number kept, with the reason | `importer.build_records` (`phone_issue`, `NO_PHONE`) |
| Every column kept as written, except Skip columns | `importer.build_records` (`source`, `skip`); `imports.columns` |
| Records stored in one transaction; cancelled/failed imports keep nothing | `RecordSink.save` / `discard`; `converter.jobs.run_import` |
| A completed import can't be re-run until its records are deleted | `app.can_rerun`, `store.delete_import` |
| FileMaker order of requests (Omit removes from the requests above; Omit first = from all) | `find.Compiler.node` ("find") |
| NULL-safe conditions (an Omit never drops records whose field is empty): `… AND value IS NOT NULL` for text, number and date fields, the Date, Any field and each part of First / Last name (so their indexes are used); `COALESCE(…, false)` for the rest | `criteria.condition`, `_guard`, `_coalesce`, `name_condition` |
| Upper/lower case of accented letters; ’ = ' | `db._case_collation`, `criteria.Params.collation`, `criteria._regex` |
| `//` is today in the app's time zone | `find.Compiler` (`today`) |
| A business ID wins over names starting with it; ranges are about IDs | `criteria.business_condition` |
| A typed time means that minute; backward ranges refused | `criteria.date_token(with_time)`, `_backwards` |
| Birthdays without a year (1600) only found by month/day or `1600` | `criteria.date_condition` (`no_year`) |
| A prepared CSV belongs to one found set and sort | `ui._found_set` (`found_csv["sig"]`) |
| Birthday month/day in any year; 1600 = no year | `criteria.date_token`, `_md_expr` |
| Nothing required: a blank request is ignored, an all-blank find is every record | `find.Compiler.request` / `node` |
| Ranges with an open end (`...X` up to and including X, `X...` from X on) | `criteria._range` and each kind's condition |
| The requests are one form: typing doesn't rerun the page; Enter = Find (the form's first button); the Add a field menu and ✕ are form buttons too, so typed values are kept | `ui.page_find`, `ui.find_layout` |
| Which boxes a request shows (Business, Services, Date; Phone, First and Last name) and the Add a field menu's groups (without Last visit and Last call, which the Date covers) | `ui.find_layout` (from the record fields) |
| Date: one searched field for the last visit and the last call (`generic_date` default / call_log), the later of the two as a day; both stay stored and exported as they are | `db.date_expr` (and its index), `fields.find_fields`, `find.page` / `SORTS` / `found_set_csv` / `record_date` |
| Days of the week in every date criterion (`fri`, `sat sun`, `mon...fri`, `fri 10/2026`), on the day as stored; never for a no-year birthday; refused as a range's end or after `<` / `>` | `criteria.weekdays`, `criteria.date_condition` (`ISODOW`) |
| A date column is compared with dates, not timestamps, and a whole number is passed as an integer (otherwise the column would be cast and its index unused) | `criteria.date_condition` (`at`), `criteria.number_condition` (`num`) |
| Last visit is a date only: a date-and-time field whose format has no time; the converter maps, checks and converts it as a date and time, records keep the day; an old timestamp column is converted on start | `db.date_only`, `db.column_type`, `db.Database.setup`, `importer.typed_value`, `fields.find_fields` (`datetime`) |
| First / Last name also read a whole name kept in First name (comma, else first space); indexes on both parts | `criteria.name_expr`, `criteria.name_condition`, `db.Database.setup` |
| One merchant per tag; counts of left-out records | `store.add_to_tag` |
| Export: one row per number; latest dates; birthday with a year; consent blank; creation date per import | `tags.export_table`, `ALWAYS_BLANK`, `_pick_date`; `importer.creation_fallback` |
| No live formulas in the found-set CSV | `find.found_set_csv` (`neutralise_formula`) |
| Gender: F / M from the file; Fb / Mb guessed on import and, once, for records stored before (or when the name table changes); never exported | `gender.stated` in `importer.build_records`, `gender.guess` in `RecordSink.save` and `db.Database.setup` |
| Dates on the screen "Fri, 10/9/2026" (birthdays without the day), still dates in the table so headings sort by date; the found-set CSV and tag exports keep their own formats | `find.shown`, `find.date_columns`, `ui._date_column_config`; `find.found_set_csv`; `tags.export_table` |

## Why it is built this way

- **Records per file row, numbers in their own table.** The converter's "one row per number" is right for an
  import file; for records it would duplicate names, dates and addresses per number and make "how many customers"
  wrong. A separate `record_phones` table makes any-number search one index lookup and the tag export a join.
- **The row as written in `source` (jsonb).** "Store nearly every field" without guessing every column a client
  might send: typed fields for what is searched by value and range, the rest kept verbatim and still searchable.
- **Found sets as a tree of steps, not id lists.** A found set of a million records costs nothing to keep, and
  every step (constrain, extend, omit, show omitted) is one more node; the database does the work.
- **Regular expressions for word searches.** FileMaker's "a word starting with" is `~* '\mword'`, which `pg_trgm`
  indexes; plain LIKE couldn't tell Smith from Goldsmith.
- **Static tags.** A tag is what was decided to send, kept as records with a history, not a search that changes as
  imports arrive.
- **Consent blank in exports.** The main system decides opt-outs per merchant; sending a file's consent could
  re-subscribe or opt out someone across merchants.
- **Merchants and locations are copies of the main app's**, not a live join: imports, records and tags keep
  working when ClickHouse can't be reached, and a merchant the main app drops stays attached to its records.
- **Locations only for corporate accounts in Find.** A one-location merchant is the same as its location, and the
  requests are one form (a list can't follow the merchants picked), so the Locations picker lists only the
  locations of merchants with several.

## Connecting to the main system

Merchants and locations are read from the main app's ClickHouse (`records.merchants`, 1.6.0); completed imports,
their deletions and tags are sent to its bucket as CSV files (`records.bucket`, 1.7.0; decided 2026-10-17: a
separate app connected through the bucket first, built into the main app later). What its ClickHouse holds (seen
with read access, 2026-10-16): `pg.*` are the main app's PostgreSQL tables seen through ClickHouse;
`default.generic_csv_import` takes files in the converter's import format, per merchant; `default.audience_members`
(segment, merchant, phone as a 10-digit number) holds the evaluated segments; `audiences.*` has per-number
activity, consent, Do Not Call (`ndnc`) and mobile checks (`is_mobile`). Planned, from least to most change
(HANDOFF, decisions):

1. **Files in the bucket** (1.7.0): every completed import (one row per record), its deletion, each tag's numbers
   (snapshots), under `data/customer-records/` and `data/customer-records-tags/` (BUCKET_FILES.md). The main app
   loads them (since 2026-10-15 nothing picks files up from the bucket by itself).
2. **Tags as segments**: the main app gets a segment source that reads each tag's latest snapshot (its segments
   are re-evaluated, so rows written into `audience_members` directly would be lost). The main app's side.
3. **Into the main app** (later): Customer Records' importer, finds and tags rebuilt in sendsational (TypeScript),
   the converter ported with the 406-file check as its test (HANDOFF, decisions).
4. **Records in ClickHouse** (2.0; 80M+ records expected): records and record_phones there, the rest in
   PostgreSQL; finds compile to ClickHouse SQL instead (`criteria.py` and `find.py` are where that changes); a load
   test of 80M records first.

The checks on numbers (cell phones, opt-outs, Do Not Call with the 18-month established business relationship) are
done by the main app when it evaluates a segment (its `audiences.eligible` view and the evaluation's gates), not
here.

## Tests

`tests/test_records_*.py` (about 95 tests) and `tests/test_drafts.py` run against a real PostgreSQL: `conftest.py`
starts a private server for the session (from `initdb`/`pg_ctl` found on the PATH, Homebrew, Postgres.app or
Debian/Ubuntu; as the `postgres` user when run as root), or uses `RECORDS_TEST_DATABASE_URL`. Each test gets a fresh
database. Without PostgreSQL those tests are skipped with a message; the converter's ~1,000 tests don't need it.
`test_consent_choice.py` is the converter's (0.23.0) with its app tests adapted: they run New import and an
import's page with a records database, and the re-run of an import from before 0.22.0 checks the records it
stores.

| Test module | Covers |
|---|---|
| `test_records_import.py` | one record per row, all numbers, rows without numbers, every column, Skip, typed values (the last visit as a day, mapped and checked as the converter does), consent by hand only and as each column's consent setting says, repaired numbers, creation-date fallback, cancel/fail cleanup, same file, delete import, 20,000 rows |
| `test_records_find.py` | every criterion kind and operator, half ranges, FileMaker request order, Omit with empty fields, business and service pickers, blank requests and finds, Record created, constrain/extend/omit/show omitted, tags as found sets, messages, counts, CSV, sorting |
| `test_records_find_edges.py` | the cases the independent test found: accented letters (ICU and C-locale databases), business ID vs names, times in dates, today's time zone, `+1 516*`, no-year birthdays, apostrophes and punctuation, backward ranges, `#12`; 1.1.0 and 1.2.0 reviews (the date-only last visit, whole names in First name) |
| `test_records_star_equals.py` | `*` alone finds the records with a value and `=` alone the blank ones, for every field a find can search (also unreadable values); with other text they keep their meanings; an Omit keeps the records whose field is blank, for every field made NULL-safe without COALESCE |
| `test_records_date.py` | the Date (last visit or last call, the later), days of the week alone, in lists and ranges, with a date, in Omit requests and other dates (Record created on the app's clock, no-year birthdays), messages, the found set's Date column, sort and CSV, dates on the screen vs in tag exports, the Find layout, the Date index (replaced when it changes), and that finds and the Date sort use indexes |
| `test_records_gender.py` | what a file's gender cell becomes, guesses on import (titles, whole names, accents, hyphens, the email, ambiguous names, a file's other answer), guesses never over F / M, finding by gender, no gender in exports, guesses for records stored before and after a new name table, the name table |
| `test_records_runner.py` | imports in worker processes (1.5.0): several at once up to `IMPORT_WORKERS`, the same records as an import run in the server, cancel, a worker killed (signal 9), ending without a result or not starting, Ctrl+C left to the server, a worker stopping when its server is gone, stopping the server (running and queued imports interrupted, also in a real server process), a new start removing what interrupted imports stored, no database, a relative data folder, the settings |
| `test_server_setup.py` | the Docker setup: logins always on, the port published on the server itself only, no admin password shipped, five imports at once |
| `test_records_tags.py` | export format, one row per number, merge rules, consent blank, no-number records, creation date per import, file name, parity on the sample files |
| `test_records_store.py` | merchants and locations (loading the main app's lists: new, changed, removed, back; bad lists change nothing), one current location per import, services, tags (one merchant, names, history), setup (repeatable, new fields, retyped fields, field names, a last visit from 1.1.0 converted to a date, a database from before 1.6.0), `scripts/reset_records.py` |
| `test_records_bucket.py` | sending to the main app's bucket: ULIDs and names (read back by the main app's expressions), an import's file (every record, IDs, numbers, typed values, the row as in the file), waiting for the run to finish, deletion files (sent, or never sent when deleted first), tag snapshots (first row, numbers, one waiting at a time, deleted tags), retries and their waits, a send cut off by a restart, nothing without settings, only main app merchants, the background thread, boto3 against a stand-in S3 server (signature, refusal, unreachable, never the secret) |
| `test_records_merchants.py` | the merchant and location lists: rows from ClickHouse and from the console's CSV/TSV export (NULL as `\N`, 63.0), the main app's JSON addresses on one line, mistakes explained, ClickHouse addresses, reading a stand-in ClickHouse (headers, errors without the password), the background refresher |
| `test_records_app.py` | the real app.py with AppTest: database errors, Merchants/Services, New import with merchant, location (filled in, chosen, none) and service, import page, Find (the shown boxes, Add a field and ✕, `*` and `=` alone, a day of the week in Date and the Date column, pickers, an Omit request, blank find, errors, omit selected), tag creation, tag export, Fields, logins (sign-in, who added a service, Users for admins only, a deleted account signed out), finds by location of a corporate account, sending to the main app (off without settings; imports, a tag, a refusal with Try again now and the sidebar warning, against a stand-in bucket) |

Checks before a release: [HANDOFF.md](HANDOFF.md) → Checks.
