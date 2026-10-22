# Changelog

What changed in each version of Customer Records. The running version is shown in the app's sidebar. Each version
is tagged in git (`vX.Y.Z`). The history of the file converter this project is based on is in
[converter/CHANGELOG.md](converter/CHANGELOG.md).

## 1.9.0-sendsational — 1.9.0 on the `sendsational` branch

1.9.0 (Find without File row, Any field and Phone problem) merged into 1.8.0-sendsational, tagged
`sendsational-1.9.0`. Nothing changes in what is sent to the main app: the files still carry each record's file row
and phone problem.

## 1.9.0 — Find without File row, Any field and Phone problem

- **Find no longer offers File row, Any field or Phone problem** (under *Other* in **＋ Add a field**). A record's
  file row and phone problem are still kept, and still shown in the found set, its CSV and the record's details;
  they just can't be searched. Search a specific field instead of Any field.
- On `main` first (update15); on the `sendsational` branch as 1.9.0-sendsational.

## 1.8.0-sendsational — 1.8.0 on the `sendsational` branch

1.8.0 (consent settings per column) merged into 1.7.0-sendsational, tagged `sendsational-1.8.0`.

- **The records sent to the main app carry consent as the settings give it**: an *Email Opt In* = N with
  *opt-ins only* is sent empty, an *Unsubscribed* = Yes with *opt-outs only* as `false` (BUCKET_FILES.md, `consent`).
- Records sent before with consent mapped carry the old reading (a no as `false`). Deleting such an import's records
  sends their deletion; re-running it (asked what each consent column records) sends the records again, read with
  the settings.

## 1.8.0 — consent settings per column (file-converter 0.23.0)

1.7.0 is the `sendsational` branch's version (sending to the main app's bucket), so `main` goes from 1.6.1 to 1.8.0.

- **Each consent column now says what it records**, chosen when it is mapped (the converter's 0.22.0 and 0.23.0):
  - **Opt-ins only** (e.g. *Email Opt In*): yes → `true`; no → blank. Before, a no was stored as `false` (opted
    out), though not opting into email isn't opting out of texts.
  - **Opt-ins and opt-outs** (a column of text opt-ins and opt-outs): yes → `true`; no → `false`.
  - **Opt-outs only** (new; e.g. *Unsubscribed*, *Opt Out*, *Do Not Text*): yes → `false`; no → blank.

  Pick it with the column in *Goes to* under *Columns in your file* ("consent — opt-outs only") or under the field
  table; **Start import** stays disabled until every consent column has one. Values that name a status keep their
  meaning (*Subscribed* and *Active* are opt-ins, *Opted out* an opt-out). Several consent columns are read
  together: an opt-out in any of them wins, otherwise an opt-in, otherwise blank. Templates never keep a setting.
- **Translate values on consent** takes opt-in, opt-out or nothing (`DNT=opt-out; OK=`), since true / false and yes /
  no could be read either way.
- **More spellings of yes and no**: *Opt-Out*, *Opted_Out*, *Un-subscribed*, `1.0` and full-width letters are read;
  `-1` is no longer a yes.
- **The import's page counts consent** under the records stored ("Consent: 120 true (opted in), 15 false (opted
  out), 300 left blank by the consent settings"), and so does its summary file; *Side by side* shows a value left
  blank by its setting in teal and an opt-out from an opt-outs-only column in pink, with the column that decided;
  *What changed* and *Mapping used* show each column's setting.
- **Imports from before 1.8.0 with consent mapped** were read without settings (a no stored as `false`). Re-running
  one (after deleting its records) first asks what each of its consent columns records (KNOWN_ISSUES #53).
- *Suggest fixes* no longer proposes values for consent, and the AI suggestions never map consent or choose its
  settings (only a person maps consent, as before).
- Tag exports still leave consent blank. The regression check is identical on all 406 files (consent is only ever
  mapped by a person), and tag exports still match the converter (parity check).

## 1.7.0-sendsational — sending to the main app through its bucket (branch `sendsational`)

On the `sendsational` branch only, tagged `sendsational-1.7.0` (Noah, 2026-10-17: it needs the main app's side, which
doesn't exist yet, so `main` stays the standalone app). Built on 1.6.1.


- **Customer Records sends its data to the main app**, as CSV files in the main app's bucket (Cloudflare R2), for
  its ClickHouse: every completed import (one row per record, with the merchant, location, service, every field,
  all its numbers and the row as in the file), the deletion of an import's records once it was sent, and a tag's
  numbers each time it is exported or sent with the new **Send to the main app** button (a snapshot; a deleted tag
  sends a last one saying so). Files go in the background, one at a time, and are tried again after a failure (1, 2,
  4… minutes, at most an hour apart) until they go. Set up with `BUCKET_ENDPOINT`, `BUCKET_NAME`,
  `BUCKET_ACCESS_KEY_ID` and `BUCKET_SECRET_ACCESS_KEY` in `.env`; without them nothing is sent, and what waits
  goes once they are set. The file format, for the main app's developers: `docs/BUCKET_FILES.md`.
- **Where each one stands**: a *Main app* line on each import's and tag's page (sent when and as which file, waiting,
  or not sent yet with the reason and *Try again now*), a *Main app* column on the Imports list, *Sending to the
  main app* on the Merchants page (the latest files, what waits), and a warning in the menu while something can't
  be sent.
- Found by the independent test and fixed before release: a deletion could go while its import's send was still
  being tried (it now waits until that send is settled, and still goes when a failed try may have landed); a send
  cut off by a restart waited 30 minutes (now taken over at once); a deleted tag's last file could carry an earlier
  name; a merchant ID of non-ASCII digits was accepted for sending; the file description promised every cell of the
  row in `source` (empty cells aren't kept).
- `scripts/reset_records.py` also empties the list of what was sent, and says how many files already went (they
  stay in the bucket).
- New package: boto3 (installed by `run_local.sh` and the Docker build).

## 1.6.1 — locations' addresses on one line

- **A location's address shows as one line**, "25 North Newbridge Road, Hicksville, NY 11801", in the Location
  pickers and on the Merchants page. The main app keeps addresses as JSON (street, city, state, ZIP; a city may be
  empty), which 1.6.0 showed as it was. Loading the merchants again (by hand or the hourly load) rewrites the
  addresses already loaded.
- **Merchant files with Mac line endings** (Excel's *CSV (Macintosh)*) load, and a file that can't be read as CSV
  is explained on the page instead of showing an error trace.

## 1.6.0 — merchants and locations from the main app

- **Businesses are now the main app's merchants and locations**, with the main app's IDs, instead of being typed
  in. A merchant is one business with one location, or a corporate account with several. The **Merchants** page
  (was Businesses) lists them; they are loaded from the main app's ClickHouse, automatically once the app has its
  own user there (`CLICKHOUSE_URL`, `_USER`, `_PASSWORD`; every hour and with *Load from the main app now*), and
  until then from a file: copy the query shown on the page, run it in the ClickHouse console, export the result as
  CSV and upload it. Merchants and locations that leave the main app's list are kept for their records but no
  longer offered; only active merchants are offered.
- **New import: Merchant, Location and Service.** A merchant's only location is filled in by itself; a corporate
  account's location is chosen. A merchant without a location in the main app can't be imported for, and the page
  says to add one there first. A location that leaves the main app after it was chosen is never swapped for
  another: the page says so and waits for a location to be chosen. Every record keeps its import's location.
- **Find: Merchants and Locations** replace Business. A merchant finds all its locations; the Locations picker
  (shown when there are corporate accounts) finds one or some of them. The found set, its CSV, one record's details
  and the Imports pages show the location.
- **Tags are per merchant** (any of its locations), as before per business; exports are named with the merchant ID.
- The businesses typed in by hand before: one whose ID is a main app merchant ID becomes that merchant (with the main
  app's name), the others are marked *no longer in the main app*; either way their imports and records have no
  location. To delete all test data instead, stop the app and run `scripts/reset_records.py --yes` (services,
  users and templates stay).

## 1.5.0 — five imports at once, and the shared server

- **Five imports run at the same time** (`IMPORT_WORKERS`, now 5 by default), everyone's together; more wait as
  *Queued* and start by themselves. Each import now runs in its own worker process, so imports really run side by
  side, one per processor core. Before, they took turns inside the app: five started together all finished at about
  the time five in a row would have. Each import takes about 1.5 seconds longer to start. The records stored are
  exactly the same.
- If the server runs out of memory during an import, only that import fails, and it says so (*stopped by the
  system*); the others carry on. An import that can't reach the database says why at once. Cancel works as before.
- **Stopping or updating the server stops its imports** within seconds: those running or waiting show
  *Interrupted*, with nothing stored, and are started again with *Re-run*. Before, the server first finished all of
  them, running and waiting, with the pages already gone.
- **The server setup has logins**: the Docker setup always asks everyone to sign in with their own account (accounts
  on Setup → Users), and imports, tags, businesses and services show who made them (Businesses and Services have a
  new *Added by* column). A deleted account is signed out at its next click, and a changed admin right applies at
  once. On one computer there is still no login.
- **The server setup is reachable through HTTPS only**: `docker-compose.yml` publishes the app on the server itself
  (`127.0.0.1:8502`), for Caddy to serve. `.env.example` no longer ships an admin password (`change-me-now` was
  known to everyone with the code): set `ADMIN_PASSWORD` before the server's first start.
- The README's *On a server* section has the steps for one shared copy and the server size (4 cores and 8 GB at the
  least; 6 cores and 16 GB recommended for five imports at once).
- New setting `DATABASE_POOL_SIZE` (16): the most database connections the app opens for the pages (was 8, fixed).

## 1.4.0 — gender

- **Gender**, a new record field: `F` or `M` when the file says so (a column whose header says gender or sex, not
  someone else's such as Child Gender: Female, Woman, Girl, W, F - Female, ♀, Féminin… become F; Male, Man, Boy…
  become M), `Fb` or `Mb` when it is likely: guessed from the first name for every record whose file said neither.
  The guess gives nothing for two people ("John and Mary"), looks for a title first (Mr, Mrs, Frau, Señora…), then
  the first name in the US Social Security baby names since 1930 (shipped with the app, public domain): 90% or more
  of the people with that name female gives Fb, male Mb; names used for both (Jordan, Riley) give nothing. Accents
  don't matter (José, ANDRÉ); a hyphenated name needs both parts to agree ("Hans-Peter" → Mb; "Jean-François",
  "Jean-Noël" → none: Jean is mostly a girl's name in the US); "Sr." and "Dr." say nothing. With no known first name, the email's first word is
  used (`maria.lopez@…`).
- Guesses are automatic and fast: an import's records are guessed as it is stored (about 1.5 seconds per 20,000
  rows); the records already stored first get what their files' Gender column said (from the rows as written) and
  are then guessed, once, on the first start of this version (about 20 seconds per 300,000 records). A guess never
  replaces F or M, and a file's own other answer (Other, Non-binary, Mx) is never guessed over; Unknown, N/A, number
  codes (1/2) and spreadsheet errors count as nothing said.
- **The converter's rules** have a new field option, `header_words` / `header_not_words`: a column is mapped to such a
  field only when its header names it ("Tender" and "General" look like "Gender" but aren't). Only Customer Records'
  gender uses it; the file converter's own results are unchanged.
- Find: **Gender** under Add a field: `f` finds F and Fb, `=f` only F, `fb` only the guesses. The found set shows
  Gender by default. Gender isn't in the main system's import format, so tag exports are unchanged.
- An empty date in the found set is blank again (1.3.1 showed "None").

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
