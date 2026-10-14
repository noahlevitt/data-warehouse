# Known issues and limits

Behaviour that is intentional but surprising, and limits of the prototype. Each entry says where it lives. The
converter's own list (conversion, mapping, Claude, app) still applies to every import:
[converter/KNOWN_ISSUES.md](converter/KNOWN_ISSUES.md).

## Data and imports

| # | Issue | Effect | Where |
|---|---|---|---|
| 1 | **Two databases.** Import jobs, templates and drafts are in `data/converter.db` (SQLite); records are in PostgreSQL. An import is linked by `job_id` (the number on the Imports page). | If `data/` is deleted or a second app install uses the same PostgreSQL, job numbers start again at 1 and the "Open the import" link of old records can open the wrong job (the records themselves are unaffected: they point to their PostgreSQL import). Keep one `data/` per database. | `app.start_import`, `imports.job_id` |
| 2 | **A record field can be added, not renamed or retyped automatically.** Adding a field adds its column on the next start. A renamed field gets a new, empty column (the old one stays); a retyped field keeps its old column type and the app warns in the sidebar. | Retyping a field with data is a decision for a person: convert the column by hand (`ALTER TABLE records ALTER COLUMN …`) or add a new field. An import into a mismatched column can fail. | `db.Database.setup` |
| 3 | **An import is stored in one transaction, and the whole file is held in memory** (as in the converter). | 20,000 rows take a few seconds; files of millions of rows need more memory and time. | `importer.RecordSink.save` |
| 4 | **No duplicate check across imports.** The same customer in two files is two records; the same file imported twice for a business is only warned about. | Tag exports merge numbers, so the main system receives each number once; counts of records include repeats. | `store.same_file_imports` |
| 5 | **Re-running a completed import needs its records deleted first**, which also takes them out of tags. | Tags must be filled again from the new records. | `app.can_rerun`, `store.delete_import` |
| 6 | **Records can only be deleted per import.** There is no "delete this record". | Delete the import's records and re-import, or leave the record and keep it out of tags. | — |
| 7 | **Business IDs are text**, compared exactly (spaces trimmed). | `1001` and `01001` are different businesses. | `store.add_business` |
| 8 | **The converter's command line (`converter.cli convert`) doesn't store records**: it has no business or service, and runs the converter's import (number history, converted file). | Import through the web app. | `converter/cli.py` |
| 23 | **"Kept as written" means as the converter's reader reads the cell**: spaces around a value are trimmed, and an Excel date cell is text like `1990-05-06 00:00:00`. | Search *Column: …* for the trimmed value. | `converter.reader`, `importer.build_records` |
| 24 | **A record's numbers are in the order of the phone field's columns**, which the converter's rules put mobile columns first in (Mobile before Phone), not always the file's column order. | Only the order shown on the record; every number is stored and exported. | `converter.heuristics` step 2, `importer.build_records` |
| 9 | **The converter's Number history page isn't shown**, and `NUMBER_RECORD_DAYS` has no effect: the runner's sink replaces the number recording. | Use Find (Phone, then the record's import) instead. | `app.get_runner` |

## Find

| # | Issue | Effect | Where |
|---|---|---|---|
| 10 | **FileMaker syntax with differences**: `@` and `#` are ordinary characters (so emails and "Apt #4" can be typed), `!` (duplicates), `?` (invalid dates) and `~` aren't supported and say so; month/day without a year (`10/9`) means "any year". | Expected by design; listed under *How to search*. | `criteria.py` |
| 11 | **Two-digit numbers after a month are days, not years**: `10/25` is October 25 in any year, not October 2025 (`10/2025`). | Type the year with four digits. | `criteria.date_token` |
| 12 | **`Column: …` compares text**, as written in the file: `>=100` compares alphabetically ("50" > "100"). | Map the column to a typed field to search it by value. | `criteria._condition` ("column") |
| 13 | **Any field searches the cleaned values and the row as written**, lower-cased together: a criterion can match across neighbouring values. | Rare; use the specific field when it matters. | `importer.build_records` (`search_text`) |
| 25 | **Upper/lower case of accented letters depends on the database's locale.** The app creates its database with ICU rules (Å = å). A database made by hand with the C locale (Homebrew's default) only folds A-Z: finds then add an explicit collation (correct; the sidebar says so), which keeps text searches from using their index. | On a large database made by hand, re-create it with `CREATE DATABASE … LOCALE_PROVIDER icu ICU_LOCALE 'und' …` (or let the app create it) and restore a backup. | `db.ensure_database`, `db._case_collation`, `criteria.text_condition` |
| 14 | **Text searches need `pg_trgm`** to be fast on large databases. Without it (a server that doesn't allow the extension) they still work, slower; the sidebar says so. | Ask for the extension on a shared server. | `db.Database.setup` |
| 15 | **Selecting rows works per page** of the found-set table; *Omit selected* omits the selected rows of the page shown. | Omit page by page, or narrow the find. | `ui._found_set` |
| 16 | **Dates and times are as written in the client's time zone** (converted only when the file gave a zone, as in the converter); *Imported on* uses `APP_TIMEZONE`. | Comparing activity dates across businesses in different zones compares local clock times. | `criteria`, `importer.typed_value` |

## Tags and exports

| # | Issue | Effect | Where |
|---|---|---|---|
| 17 | **Consent is always blank in exports** (decision 2026-10-14). | The main system's opt-outs decide; a file's opt-out isn't sent. The records keep it (searchable). | `tags.ALWAYS_BLANK` |
| 18 | **Merging a number's records** takes the latest date of each activity and the birthday/anniversary with a real year (most common, then the latest record's). Values the export format doesn't have (names, emails) aren't exported. | A wrong date in one file can become a number's latest date. | `tags.export_table`, `_pick_date` |
| 19 | **A tag holds records, not a search**: new imports don't join it. | Add found sets again after new imports. | `store.add_to_tag` |
| 20 | **The Do Not Call list, the 18-month established business relationship and the cell-phone check aren't built.** | Exports can include landlines and numbers on the Do Not Call list; the main system must still apply its own checks. | HANDOFF → Open items |

## App

| # | Issue | Effect | Where |
|---|---|---|---|
| 21 | **Logins are off** (as in the converter). Anyone who can open the address sees every record. | Keep it on a trusted computer/network, or set `AUTH_MODE=password`. | `converter/auth.py` |
| 22 | **The app tests set the find requests through the page's state**: Streamlit's test tool can't type into the request tables. The tables themselves were checked in a real browser for this release. | A change to the request editor needs a manual check in the browser. | `tests/test_records_app.py` |
