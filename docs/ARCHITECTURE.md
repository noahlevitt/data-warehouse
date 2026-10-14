# Architecture

How Customer Records is put together, the database, the design decisions, how it is tested, and how it can be
connected to the main system. The conversion engine it is built on is described in
[converter/ARCHITECTURE.md](converter/ARCHITECTURE.md).

## Design principles

1. **The converter decides every value.** Files are read, matched and cleaned by the file converter's engine
   (`converter/`, 0.21.2), unchanged except for two small additions (below). Every rule, approved repair and
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
 New import: business + service, upload,  ─────────►  converter.reader / mapping / services (as in the converter)
   review mapping
 Start import ────────────────────────────────────►  records.store.create_import (PostgreSQL, status queued)
                                                      converter Store.create_import (SQLite job, read_options
                                                        carry records_import_id) → ImportRunner.submit
 background ──────────────────────────────────────►  converter.jobs.run_import → engine.convert (record fields)
                                                      → audit/source parquet (side by side)
                                                      → records.importer.RecordSink.save: group the engine's
                                                        rows per file row, type the values, COPY into records
                                                        and record_phones, complete the import
 Find: requests, found-set steps ────────────────►  records.find (node tree) → records.criteria (one SQL
                                                      condition per criterion) → counts / page / CSV
 Tag the found set ───────────────────────────────►  records.store.add_to_tag / remove_from_tag (+ tag_events)
 Tag export ──────────────────────────────────────►  records.tags.export_table (one row per number, merge rules)
                                                      → converter.output.write_output (import format)
```

## Two databases

| | Holds | Why |
|---|---|---|
| **PostgreSQL** (`DATABASE_URL`) | businesses, services, imports, records, record_phones, tags, tag_records, tag_events | the business data: shared, searched, eventually connected to the main system |
| **SQLite** `data/converter.db` | import jobs (status, progress, mapping and schema snapshots, summary), templates, drafts, users, preferences | the converter's own app state, unchanged, so its pages (Imports, side by side, templates) work as they are |

The link: a PostgreSQL import has `job_id` (the SQLite import's id, the number shown on the Imports page), and the
job's `read_options` has `records_import_id`. See KNOWN_ISSUES #1 for what that implies.

## Modules (`records/`)

| Module | Responsibility |
|---|---|
| `config.py` | `DATABASE_URL`, `RECORDS_SCHEMA_PATH`, `EXPORT_SCHEMA_PATH` (`.env` is loaded by `converter.config`). |
| `db.py` | Connection pool (psycopg 3 + psycopg_pool); `ensure_database` (creates the database when allowed, with ICU case rules so Å = å); `Database.setup`: `pg_trgm`, the fixed tables, one column per record field (added when missing; a retyped field only warns), indexes (trigram for text, b-tree for the rest), and `collation` (None when the database folds accented letters; otherwise an explicit collation for finds, `_case_collation`); `check_fields` (names must be valid columns, one phone field). |
| `store.py` | `RecordStore`: businesses, services, imports, records, tags; the rules of one business per tag, unique names, deletions only when unused, tag history. All SQL except finds and exports. |
| `importer.py` | `RecordSink` (the runner's sink): `build_records` (engine audit → one record per file row, typed values, all numbers, the row as written minus skipped columns), `save` (one transaction: COPY records and numbers, complete the import), `discard`; `creation_fallback` (see Tag export). |
| `fields.py` | What a find can search: the record fields plus Any field, Full name, Business, Service, Tag, Phone problem, Import #, File name, Record created, Record ID, File row, Column: …; which of them get a box on the Find page (`FORM_KEYS`) and which have pickers (`PICKER_KEYS`). |
| `criteria.py` | One criterion → one SQL condition, per kind of field (text, phone, number, date, true/false, business, …), FileMaker syntax; values always as parameters; every condition `COALESCE(…, false)`. The module docstring is the syntax reference (also shown on the page). |
| `find.py` | Requests (businesses, services, criteria; all optional) → found-set nodes (find, constrain, extend, omit records, show omitted, all, tag) → one SQL condition; counts, pages (sorts), CSV. |
| `tags.py` | A tag's export: one row per unique number, filled by name from the record fields with the merge rules, written with the converter's writer; the file name. |
| `ui.py` | The Find, Tags, Businesses and Services pages (Streamlit), given a `Ctx` by `app.py`. |

`app.py` is the converter's app with Customer Records' changes: the business/service step and the duplicate-file
warning on New import, `start_import` / `rerun_import` registering imports in PostgreSQL, `can_rerun` (no re-run while
records are stored), business/service/records on the Imports pages, *Delete this import's records*, the Fields page,
the navigation (Number history not shown), the sidebar, and both mapping tables drawn at full height
(`height="content"`, 1.0.2: they get a new key on every edit, so a table scrolled inside itself jumped back to
its first row).

### Additions to the converter (`converter/`)

Kept small so fixes from file-converter apply cleanly ([HANDOFF.md](HANDOFF.md), section 5):

| File | Change |
|---|---|
| `schema.py` | `FieldSpec.creation_date` (validated: date/date-time fields without `generic_date`). |
| `heuristics.py` | Step 7 (`_map_generic_dates`) ignores `creation_date` fields when deciding whether a file already has a last date. The converter's format has none, so its results are unchanged (regression check: identical). |
| `jobs.py` | `run_import(..., sink=None)` and `ImportRunner(..., sink=None)`: with a sink, `sink.save` replaces the number history step and adds to the summary; `sink.discard` runs when the import then ends cancelled or failed. |

## Tables (PostgreSQL)

| Table | Columns (main) |
|---|---|
| `businesses` | `id` (text, the main system's ID), `name`, `notes`, created |
| `services` | `id`, `name` (unique, any case), `active` |
| `imports` | `id`, `job_id`, `business_id`, `service_id`, `file_name`, `file_sha1`, `sheet`, `username`, `status` (queued / completed / not completed / deleted), counts (`records`, `phones`, `no_phone`), `first_visit_as`, `columns` (the file's column names) |
| `records` | `id`, `import_id` (cascade), `business_id`, `service_id`, `file_row`, `phone_issue`, `issues` (jsonb), `source` (jsonb: the row as written), `search_text`, and one column per record field |
| `record_phones` | `record_id` (cascade), `phone` (10 digits), `position` (the order of the phone field's columns, mobile first), `repaired`, `original`; b-tree and trigram indexes |
| `tags` | `id`, `business_id`, `name` (unique per business, any case), `notes`, created, updated |
| `tag_records` | `tag_id`, `record_id` (both cascade), added at / by |
| `tag_events` | `tag_id`, at, username, action (created / added / removed / renamed / exported), count, detail |
| `app_meta` | small facts (pg_trgm available) |

Foreign keys to `businesses` are `ON UPDATE CASCADE`, so a corrected business ID follows everywhere.

## Key behaviours and where they live

| Behaviour | Where |
|---|---|
| One record per file row; all its numbers; empty rows skipped; rejected rows (only with a required field) not stored | `importer.build_records` |
| Rows without a usable number kept, with the reason | `importer.build_records` (`phone_issue`, `NO_PHONE`) |
| Every column kept as written, except Skip columns | `importer.build_records` (`source`, `skip`); `imports.columns` |
| Records stored in one transaction; cancelled/failed imports keep nothing | `RecordSink.save` / `discard`; `converter.jobs.run_import` |
| A completed import can't be re-run until its records are deleted | `app.can_rerun`, `store.delete_import` |
| FileMaker order of requests (Omit removes from the requests above; Omit first = from all) | `find.Compiler.node` ("find") |
| NULL-safe conditions (an Omit never drops records whose field is empty) | `criteria.condition` (`COALESCE`) |
| Upper/lower case of accented letters; ’ = ' | `db._case_collation`, `criteria.Params.collation`, `criteria._regex` |
| `//` is today in the app's time zone | `find.Compiler` (`today`) |
| A business ID wins over names starting with it; ranges are about IDs | `criteria.business_condition` |
| A typed time means that minute; backward ranges refused | `criteria.date_token(with_time)`, `_backwards` |
| Birthdays without a year (1600) only found by month/day or `1600` | `criteria.date_condition` (`no_year`) |
| A prepared CSV belongs to one found set and sort | `ui._found_set` (`found_csv["sig"]`) |
| Birthday month/day in any year; 1600 = no year | `criteria.date_token`, `_md_expr` |
| Nothing required: a blank request is ignored, an all-blank find is every record | `find.Compiler.request` / `node` |
| Ranges with an open end (`...X` up to and including X, `X...` from X on) | `criteria._range` and each kind's condition |
| The requests are one form: typing doesn't rerun the page; Enter = Find (the form's first button) | `ui.page_find` |
| One business per tag; counts of left-out records | `store.add_to_tag` |
| Export: one row per number; latest dates; birthday with a year; consent blank; creation date per import | `tags.export_table`, `ALWAYS_BLANK`, `_pick_date`; `importer.creation_fallback` |
| No live formulas in the found-set CSV | `find.found_set_csv` (`neutralise_formula`) |

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
- **Consent blank in exports.** The main system decides opt-outs per business; sending a file's consent could
  re-subscribe or opt out someone across businesses.

## Connecting to the main system (later)

Ready for it: business IDs are the main system's; the database is PostgreSQL; numbers are 10-digit text. Options,
from least to most change:

1. **Export files** (now): tags export the import format.
2. **Read the main system's data from here** (`postgres_fdw` or a read-only connection): business list instead of
   typing it, opt-outs per business, last messages sent — needed for the Do Not Call / 18-month rule.
3. **Write tags into the main system** directly instead of a file.
4. **One warehouse** (e.g. ClickHouse, as considered): `records` and `record_phones` are flat and append-mostly, which
   suits it; finds would then compile to ClickHouse SQL instead (`criteria.py` is the one place to change).

The checks planned before tagging (cell phones, Do Not Call with the 18-month established business relationship)
belong between the found set and the tag: a step that removes numbers from what `add_to_tag` adds (or from the
export), with the reason kept per number, like the converter's bad rows.

## Tests

`tests/test_records_*.py` (about 95 tests) and `tests/test_drafts.py` run against a real PostgreSQL: `conftest.py`
starts a private server for the session (from `initdb`/`pg_ctl` found on the PATH, Homebrew, Postgres.app or
Debian/Ubuntu; as the `postgres` user when run as root), or uses `RECORDS_TEST_DATABASE_URL`. Each test gets a fresh
database. Without PostgreSQL those tests are skipped with a message; the converter's ~1,000 tests don't need it.

| Test module | Covers |
|---|---|
| `test_records_import.py` | one record per row, all numbers, rows without numbers, every column, Skip, typed values, consent by hand only, repaired numbers, creation-date fallback, cancel/fail cleanup, same file, delete import, 20,000 rows |
| `test_records_find.py` | every criterion kind and operator, half ranges, FileMaker request order, Omit with empty fields, business and service pickers, blank requests and finds, Record created, constrain/extend/omit/show omitted, tags as found sets, messages, counts, CSV, sorting |
| `test_records_find_edges.py` | the cases the independent test found: accented letters (ICU and C-locale databases), business ID vs names, times in dates, today's time zone, `+1 516*`, no-year birthdays, apostrophes and punctuation, backward ranges, `#12` |
| `test_records_tags.py` | export format, one row per number, merge rules, consent blank, no-number records, creation date per import, file name, parity on the sample files |
| `test_records_store.py` | businesses (IDs, corrections cascade), services, tags (one business, names, history), setup (repeatable, new fields, retyped fields, field names) |
| `test_records_app.py` | the real app.py with AppTest: database errors, Businesses/Services, New import with business and service, import page, Find (boxes, pickers, an Omit request, More criteria, blank find, errors, omit selected), tag creation, tag export, Fields |

Checks before a release: [HANDOFF.md](HANDOFF.md) → Checks.
