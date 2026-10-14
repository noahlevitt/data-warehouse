# Changelog

What changed in each version of Customer Records. The running version is shown in the app's sidebar. Each version
is tagged in git (`vX.Y.Z`). The history of the file converter this project is based on is in
[converter/CHANGELOG.md](converter/CHANGELOG.md).

## 1.0.0 — first prototype

Based on file-converter 0.21.2.

- **Every imported row is kept as a record**, in PostgreSQL, for one **business** (with the main system's ID) and
  one **service**, chosen on New import. Files are read, matched and cleaned exactly as by the converter. A row
  keeps all its phone numbers, every typed field (names, email, address, visits, spend, first visit / sign-up, the
  activity dates, birthday, anniversary, consent when mapped by hand) and every column as written. Rows without a
  usable phone number are kept, with the reason. Columns ticked *Skip* are left out completely.
- **Find** with FileMaker-style requests: AND within a request, OR between requests, Omit requests, Constrain,
  Extend, Omit selected, Show omitted only, Show all; Limit to businesses and services. Every field can be searched
  (also any column of the files, any field at once, business, service, tag, import, file name, import date), with
  word, exact, phrase, wildcard, empty / any value, comparisons and ranges; phone numbers in any formatting or
  partly; dates by day, month, year, range, before / after, today, and month/day in any year (birthdays).
- **Tags**: lists of records for one business, filled from found sets, with a history of every change. A tag
  **exports** one row per unique phone number in the converter's import format: the latest activity dates, the
  birthday with a real year, consent left blank (the main system handles opt-outs per business). A tag of one
  file's records exports exactly what the converter makes of that file (checked on 406 files).
- **Businesses** and **Services** pages; the New import page warns when the same file was already imported for the
  business; an import's records can be deleted (and the import re-run).
- Runs on a Mac with Homebrew's PostgreSQL (`./run_local.sh` creates the database), on Windows, or with Docker
  (app + database).
- Engine additions (in `converter/`, small, so later converter fixes can be ported): a `creation_date` field attribute
  and a sink for the background import runner. The converter's own results are unchanged: all 406 regression files
  give identical results against 0.21.2.
