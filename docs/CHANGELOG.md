# Changelog

What changed in each version of Customer Records. The running version is shown in the app's sidebar. Each version
is tagged in git (`vX.Y.Z`). The history of the file converter this project is based on is in
[converter/CHANGELOG.md](converter/CHANGELOG.md).

## 1.3.1 — dates shown with their day of the week

- **Dates on the Find page show their day of the week**: `Fri, 10/9/2026`, and `Fri, 10/9/2026 3:45 PM` for those
  with a time (Record created, Last call…), in the found set and in one record's details. Birthdays and anniversaries
  are shown without it (`9/12/1985`; often there is no year). The found set's date columns are real dates now, so a
  click on a column heading sorts them by date (it sorted them as text).
- **Tag exports are unchanged**: dates in the main system's import format (`10/09/2026 12:00 AM`). The found-set
  CSV also keeps `10/09/2026` (Excel reads it as a date), with the Date's day in its own column.

## 1.3.0 — one Date, and days of the week

- **Date** replaces Last visit / order on the Find page: one field for a record's last visit and, from a call log,
  its last call (the later of the two when a record has both), as a day. It is searched exactly as before
  (`1/1/2026...1/20/2026`, `...6/30/2026`, `=`, `*`). Last visit / order and Last call are no longer separate
  boxes under Add a field; they are still stored on every record and exported as before (the converter's mapping,
  checks and tag exports are unchanged).
- **Days of the week** in every date box: `fri`, `Friday` or `fridays` finds every Friday; `sat sun` (or `sat/sun`)
  the weekends; `mon...fri` (or `mon-fri`, `mon to fri`) Monday through Friday (`fri...mon` goes over the weekend);
  `fri 10/2026` the Fridays of October 2026, `sat sun 1/1/2026...3/31/2026` the weekends in that range and
  `fri >10/1/2026` the Fridays after October 1. Birthdays without a year never match a day of the week. A day of the
  week as one end of a range (`1/1/2026...fri`, `oct...mon`) or right after `<` / `>` (`>fri 10/1/2026`) says it
  can't be read.
- The found set shows the **Date** with its day ("10/09/2026 Fri") as the first of the chosen columns, sorts by
  **Date (newest first)** (instead of Last visit), and its CSV has **Date** and **Day** columns. One record's details
  start with its Date.
- **Finds use the database's indexes** for text, number and date fields, the Date, Any field and First / Last name.
  Every criterion used to be wrapped in `COALESCE(…, false)` (so an empty field never breaks an Omit), which kept
  every index unused and made each find read the whole table. These criteria are now made safe for empty fields with
  `… AND value IS NOT NULL` instead, which finds exactly the same records; dates are compared as dates and whole
  numbers as whole numbers, so the columns aren't converted on every row. The other fields still read every record
  (KNOWN_ISSUES #14).
- The three boxes next to each other at the top of a request are now the same width as the row below.

## 1.2.1 — `*` and `=` alone in any box

- **`*` alone** in a box finds the records where that field has a value, **`=` alone** the records where it is
  blank, in every box (Phone `=`: no usable number; Tag `=`: in no tag; a file's column `=`: also records from
  files without that column; a value the import couldn't read counts as blank). This already worked; it is now the
  first rule under **How to search** and is tested for every field. Typed with anything else, `*` and `=` keep
  their meanings (`sm*th`, `=smith`).

## 1.2.0 — the Find page laid out by what you search most

- **Each request shows Business, Services and Last visit / order**, then **Phone, First name and Last name**. Every
  other field is one click away: **＋ Add a field** opens a menu (name, contact & address; visits & spend; dates,
  with Record created — the import date — first; other; the files' own columns); a click adds its box to the
  request, and the box's ✕ takes it off. What you typed elsewhere stays. This replaces the long list of boxes and
  the More criteria table.
- **Last visit / order is a date only**: search it by days, e.g. `1/1/2026...1/20/2026`, and it shows as
  `01/10/2026`. A time in the file is dropped (after converting a written time zone to the client's, so the day is
  the client's). The import is mapped and reviewed exactly as before (the same columns are picked; future dates, old
  dates and converted time zones are still flagged). Records already stored keep their day: the database column is
  converted on the first start. Tag exports write the last visit at 12:00 AM.
- **Whole names in First name**: a file whose *First Name* column holds the whole name ("Lisa Marie Ray", no last
  name) is found by First name `lisa` and Last name `ray` as well as by the name as stored: First and Last name also
  read such a name as split at its first space, or at its comma ("Ray, Lisa"). **Full name** (in Add a field)
  searches first and last together.

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
