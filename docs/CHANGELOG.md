# Changelog

What changed in each version of Customer Records. The running version is shown in the app's sidebar. Each version
is tagged in git (`vX.Y.Z`). The history of the file converter this project is based on is in
[converter/CHANGELOG.md](converter/CHANGELOG.md).

## 1.1.0 — finds without anything required

- **Nothing is required in a find.** Fill in only what you're looking for; a **Find** with everything blank shows
  every record. The request no longer starts with Phone, First name and Last name rows: each request is a form with
  a box for every field, all optional, and a blank request is ignored.
- **Business and Services pickers in every request** (one or more of each, empty = any) for the most common finds:
  a business; a business and one or more of its services; a business without a service (pick the service in an
  **Omit** request). They replace *Limit to* at the top of the page. A phone number (or anything else) can be
  searched without a business.
- **Ranges with an open end**: `...1/1/2026` finds up to and including that day, `1/1/2025...` from that day on;
  also for numbers (`...20`, `10...`), text and business IDs. `1/1/2025...1/1/2026` works as before.
- **Record ID and Record created**: every record's own number and when it was stored, shown in the found set and the
  record's details, in the CSV, and searchable (*Record created* replaces *Imported on*). New sort: *Record created
  (newest first)*.
- Enter in any box is **Find**; typing no longer reloads the page. Requests after the first keep their field boxes
  folded unless something is typed in them; the rarer fields (Any field, Full name, Tag, Import #, File name, file
  columns…) are under **More criteria**.
- `…` and `..` work as the range operator; text ranges take in everything starting with their upper end (`a...m`
  is A through M, Miller included). A criterion that can't be read leaves the found set as it was, and a blank
  Constrain or Extend changes nothing. Out-of-range dates and numbers give a message instead of an error page.
- **How to search** on the Find page is rewritten for people (it showed the developers' notes).

## 1.0.2

- **The mapping tables on New import no longer jump back to the top.** Ticking *Skip*, changing *Goes to* or
  changing a field's source column(s) redrew the table scrolled back to its first row, so after each click you had
  to scroll down again. Both tables (*Review column mapping* and *Columns in your file*) now show every row and the
  page scrolls instead, so the row you changed stays where it was.

## 1.0.1

- **Opens at http://localhost:8502** instead of 8501, so it runs next to the file converter, which keeps 8501 on
  the same computer. Docker publishes 8502 too. `PORT=8600 ./run_local.sh` still picks another port.

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
