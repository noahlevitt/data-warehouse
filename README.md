# Customer Records

Internal web app that keeps every customer row of every imported file as a searchable record. Each import is for one
**location** of a **merchant** of the main app (both with the main app's IDs) and one **service**. Records are found with FileMaker-style
find requests, collected into **tags**, and a tag exports its unique phone numbers in the file converter's import
format, ready to import into the main system.

Built on the file converter (file-converter 0.21.2): files are read, matched and cleaned by the same engine, with the
same rules. **Prototype**; the version is in the app's menu and in [docs/CHANGELOG.md](docs/CHANGELOG.md).

**Quick start (Mac):**

```
brew install postgresql@17
brew services start postgresql@17
./run_local.sh
```

Then open http://localhost:8502 (port 8502, so the file converter can keep running on 8501). The app creates its
database on first start. On one computer there is no login; the server setup has one (*On a server*).
**Tests:** `.venv/bin/python -m pytest -q` (PostgreSQL must be installed; the tests start their own server).

## Documentation

| Document | For | What's in it |
|---|---|---|
| This README | everyone | what the tool does, finding records, tags and exports, running it, settings |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | people using it | step by step: merchants and services, importing, finding, tagging, exporting |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | developers | how it is put together, the database, design decisions, tests, connecting to the main system |
| [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) | developers | limits and quirks worth knowing before changing code |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | everyone | what changed in each version |
| [docs/HANDOFF.md](docs/HANDOFF.md) | whoever continues the work | how changes are made and delivered, the business's decisions, open questions |
| [docs/BUCKET_FILES.md](docs/BUCKET_FILES.md) | the main app's developers | the files sent to the main app's bucket: folders, names, columns, deletions, tag snapshots, and ClickHouse tables that load them |
| [docs/converter/](docs/converter/README.md) | everyone | the file converter's documents (0.21.2): every rule for phone numbers, dates and column matching used by each import |

```
Merchants / Services:  merchants and locations loaded from the main app; the list of services set up once
New import:  merchant + location + service → upload → columns matched (template / rules / Claude on request) → review
             → Start import (background) → one record per file row, all its numbers, every column kept
Find:        find requests (AND within a request, OR between requests, Omit) → found set → constrain / extend /
             omit → add to a tag, or download as CSV
Tags:        one merchant per tag → export: one row per unique phone number, in the converter's import format
Main app:    every completed import, its deletion, and each tag exported or sent → CSV files in the main app's
             bucket (Cloudflare R2), for its ClickHouse; sent in the background, retried until they go
```

## Pages

| Page | What it's for |
|---|---|
| **Find** | Find records with requests (see [Finding records](#finding-records)); limit to merchants, locations and services; see the found set, one record's details (every field, its numbers, the row as in the file, its tags); omit records; add the found set to a tag or make a new tag from it; download the found set as CSV. |
| **Tags** | Every tag with its merchant, records and unique numbers. One tag: export (download and preview), *Send to the main app*, its records (remove some), rename, notes, delete, and its history (who added or removed how many records, exports). |
| **New import** | Choose the merchant, its location (filled in when it has one) and the service, upload a file, check how it was read, review the column mapping (the converter's page), start the import. Warns when the same file was already imported for that merchant. |
| **Imports** | Every import with its merchant, location, service, status, progress and records. One import: records stored, numbers, rows without a usable number, side by side (original vs cleaned), what changed, the mapping used, the mistake check, *Show this import's records in Find*, *Delete this import's records*, and whether it was sent to the main app. |
| **Merchants** | The main app's merchants and their locations, with their IDs, imports, records and tags; loaded from the main app (automatically from its ClickHouse, or from a CSV exported there and uploaded here), never typed in. See [Merchants and locations](#merchants-and-locations). Also *Sending to the main app*: what was sent, what waits and why. |
| **Services** | The list every import picks from: add, rename, deactivate (no longer offered; its records keep it), delete one no import uses. |
| **Templates** | Saved column mappings per file layout (as in the converter). The merchant, location and service are chosen for each import, not saved. |
| **Fields** | The record fields and the export format, read-only. |

## Merchants and locations

Customer Records doesn't have businesses of its own: every import is for a **location** of a **merchant** of the
main app, with the main app's IDs, so records and tags line up with it. A merchant is either one business with one
location, or a **corporate account** with several locations (Flakowitz Corporate, Denny's CORPORATE…); a chain can
also be several one-location merchants (the KPOTs). Merchant IDs are the main app's numbers (`63`); location IDs are
long codes nobody types, so pages show names and addresses.

They are read from the main app's ClickHouse (`pg.merchants`, `pg.locations`), one of two ways:

- **Automatically**, when `.env` has `CLICKHOUSE_URL`, `CLICKHOUSE_USER` and `CLICKHOUSE_PASSWORD` (an app user
  that may read those two tables): at start, then every hour (`MERCHANTS_REFRESH_MINUTES`), and with *Load from the
  main app now* on the Merchants page.
- **From a file**, until then: on the Merchants page, copy the query under *Load from a file*, run it in the
  ClickHouse console, export the result as CSV and upload it.

A merchant or location that leaves the main app's list is kept (its imports, records and tags refer to it) but no
longer offered; it comes back if it reappears. Only active merchants are offered (not archived ones). A merchant without any
location can't be imported for until the main app has one for it (the page says so).

**Coming from 1.5 or earlier**, a business typed in by hand whose ID is a main app merchant ID becomes that
merchant (with the main app's name); the others are marked *no longer in the main app* by the first load. Either
way they keep their imports and records, without a location. To start clean instead, stop the app and run
`.venv/bin/python scripts/reset_records.py --yes` (Docker: `docker compose exec app python scripts/reset_records.py
--yes`): it deletes every merchant, location, import, record and tag, and the import jobs with their files; services,
users and templates stay. Without `--yes` it only says what it would delete. Files already sent to the main app's
bucket stay there (it says how many).

## Importing

The column matching, the review page and every conversion rule are the converter's: see
[docs/converter/README.md](docs/converter/README.md) (phone numbers, approved typo repairs, dates, birthdays with
1600, files without a header row, Claude). What Customer Records does differently:

- **One record per file row**, never merged with other imports. A customer with several phone numbers (several
  columns, or several numbers in one cell) keeps all of them on one record; each is searchable, and each goes once
  into a tag's export (the converter makes one row per number instead).
- **Rows without a usable phone number are kept** as records, with the reason (*no phone number*, *too few digits
  (3)*, *not a US number — Canada…*). They can be found and tagged, but are never in an export.
- **Every column is kept as read** on the record, also columns that feed no field (*Points*, *Notes*,
  *Customer ID*), searchable as *Column: Points*: the cell as the converter's reader gives it (spaces around it
  trimmed; an Excel date cell as `2026-03-15 00:00:00`). A column ticked **Skip** on the mapping page is left out
  completely.
- **Repaired numbers** (the converter's approved repairs) are stored marked *repaired*, with the number as written.
- **No duplicate removal** at import: the same customer in two files is two records. Duplicates are merged when a
  tag is exported.
- **The same file twice** for the same merchant: the page warns before importing it again.
- **One location per import.** A file that mixes several locations (a corporate export with a *Store* column) has to
  be split before importing, for now.
- **Re-running** a completed import (after fixing the mapping): delete its records first (*Delete this import's
  records* on its page), which also takes them out of their tags; then *Re-run this import*.

### Record fields

`schemas/records_schema.yaml`. Typed and cleaned, so they can be searched by value and range:

| Field | Type | From columns such as |
|---|---|---|
| Phone | 10-digit US numbers (all of a row's) | Phone, Mobile, Cell, Home Phone, phone2… |
| First name, Last name | text | First Name, Last Name; a whole-name column (*Name*, *Full Name*, *Customer Name*, *Guest*) is split by the converter into first and last word ("Ray, Lisa" too); a whole name in a *First Name* column stays there, and is found by First and Last name (see Finding records) |
| Gender | `F` / `M` as the file says (Female, Woman, Girl, W… → F; Male, Man, Boy… → M); `Fb` / `Mb` likely female / male, **guessed** from the first name (or the email) | Gender, Sex; guessed for everyone else (see below); not in the import format, so never exported |
| Email | email (lower-cased) | Email, E-mail |
| Address, Address 2, City, State, ZIP | text, 2-letter state, 5-digit ZIP | Address, City, State, Zip; a one-line address is split |
| Total visits | whole number | Visits, Visit Count, Total Orders |
| Total spent | amount | Total Spend, Lifetime Spend, LTV, Total Sales |
| First visit / sign-up | date and time | Created At, Sign Up Date, Member Since, First Visit |
| Last visit / order | **date only** (a time in the file is dropped, after converting a written zone to the client's) | as in the converter |
| Last call, Last text received, Last redemption, Last spin | date and time | as in the converter |
| **Date** (searched, not stored) | date only | the later of Last visit / order and Last call: what Find and the found set show as the record's date |
| Last contacted | date and time | **mapped by hand only** (as in the converter) |
| Birthday, Anniversary | date; 1600 = no year | as in the converter |
| Consent | true / false | **mapped by hand only** (as in the converter) |

To add a field, add it to the YAML file and restart the app: its database column is added on start.

## Finding records

A find is one or more **requests**. **Nothing is required**: fill in only what you're looking for, and a Find with
everything blank shows every record. Each request shows what is searched most: **Merchants**, **Locations** and
**Services** pickers (one or more of each; empty = any; Locations lists the locations of corporate accounts, the
merchants with several) and **Date**, then **Phone**, **First name** and **Last name**. Every
other field is one click away under **＋ Add a field** (and taken off again with its ✕): Full name, Gender, Email, address,
City, State, ZIP, Total visits, Total spent, Record created (the import date), Birthday, Anniversary, the other
dates (first visit, last text, redemption, spin, contacted), Any field, Consent, Phone problem, Tag, Import #, File
name, Record ID, File row and the files' own columns. In one request a record must match **everything filled in** (AND); several requests **add
up** (OR); an
**Omit** request removes what it matches from what the requests above it found (FileMaker's rules: requests are
applied top to bottom; an Omit as the first request starts from all records; a blank request is ignored). Enter in
any box is **Find**. Then:

- **Constrain** — keep only found records that also match new requests; **Extend** — add what new requests match.
- **Omit selected** — take selected records out; **Show omitted only** — every record not in the found set.
- **Show all** — every record (the same as a blank Find).

Every record has its own **Record ID** and its **Record created** date and time (when its import stored it), shown
in the found set and searchable like any number and date. **Date** is a record's last visit or, from a call log,
its last call: one field for both (the later of the two when a record has both), a day only, searched by days
(`1/1/2026...1/20/2026`) and by **day of the week** (`fri` finds every Friday). The found set shows it first of the
chosen columns; Last visit / order and Last call stay as they were in the records and the exports. **On the screen,
dates show their day of the week**: `Fri, 10/9/2026`, `Fri, 10/9/2026 3:45 PM` (birthdays and anniversaries
without it: `9/12/1985`); a click on a date column's heading sorts by date. **Tag exports keep the import format**
(`10/09/2026 12:00 AM`), and the found-set CSV keeps `10/09/2026` with the day in a column of its own.
**Names**: a file whose *First Name* column holds whole names leaves them in First name
with no Last name ("Lisa Marie Ray"). First name and Last name also read such a name as split at its first space
(Lisa / Marie Ray), or at its comma ("Ray, Lisa": Ray is the last name), so First name `lisa` and Last name `ray`
both find it; `=` and `*` look at the stored names only. *Full name* searches first and last together (`lisa ray`).

**Gender** is `F` or `M` when the file said so (a column whose header says gender or sex: Female, Woman, Girl,
F - Female, ♀… become F) and `Fb` or `Mb` when it is likely: guessed for every record whose file said neither, from
the first name (its first word; a title such as Mr, Mrs, Frau, Señora decides first; two people, "John and Mary",
get none), or from the email when there is no known first name. The guess uses US Social Security baby names since
1930 (`records/names/`, public domain): a name at least 90% of whose people were girls gives `Fb`, boys `Mb`; names
given to both (Jordan, Riley) give none. Imports are guessed as they are stored; on the first start of 1.4.0,
records imported before get what their files' Gender column said, then guesses. A guess never replaces F or M, nor
a file's own other answer (Other, Non-binary, Mx: the gender stays empty); Unknown, N/A and number codes count as
nothing said. Search it as a
word: `f` finds F and Fb, `=f` only F, `fb` only the guesses.

**`*` alone** in any box finds the records where that field has a value, **`=` alone** those where it is blank
(Phone `=`: no usable number; Tag `=`: in no tag; a file's column `=`: also records from files without it; a value
the import couldn't read counts as blank). With anything else typed, `*` is a wildcard and `=` means the whole
word.

| Type | Criterion | Finds |
|---|---|---|
| Text | `smith` | a word starting with smith (Smith, Smithson — not Goldsmith); upper/lower case never matters, also for Å, Ó, Ñ |
| | `john smith` | both words, any order |
| | `=smith` / `==john smith` | the whole word / exactly the whole field |
| | `"main st"` | this text anywhere, also inside words and with its punctuation (`"c++"`) |
| | `sm*th` | `*` = any characters (`@` and `#` are ordinary characters, so emails and "Apt #4" can be typed as they are) |
| | `=` / `*` | empty / any value |
| | `a...m`, `...m`, `a...`, `<m`, `>=m` | A through M (Miller included: the upper end takes in everything starting with it), up to M, from A on; comparisons |
| Phone | `(516) 555-0100`, `+1 516.555.0100` | that number, however it is typed; any of the record's numbers |
| | `5550100` / `516*`, `+1 516*` | numbers containing these digits / starting with 516 |
| | `=` / `*` | no usable number / has a number |
| Number | `5`, `>5`, `<=100`, `10...20`, `$1,200`, `#12` | values, comparisons, ranges (inclusive, the smaller first) |
| | `...20` / `10...` | up to and including 20 / 10 or more |
| Date | `10/9/2026`, `10/2026`, `2026`, `Oct 9 2026`, `//` | that day (the whole day), month, year, today (in the app's time zone) |
| | `10/9/2026 3:45 PM`, `>10/9/2026 9:00 AM` | that minute, after it (date-and-time fields such as Record created; a date-only field like Date takes the day) |
| | `fri`, `Friday`, `fridays` / `sat sun`, `sat/sun` / `mon...fri`, `mon-fri` | every Friday / Saturdays and Sundays / Monday through Friday (`fri...mon` goes over the weekend), in any date box |
| | `fri 10/2026`, `sat sun 1/1/2026...3/31/2026`, `fri >10/1/2026` | days of the week within a date, range or comparison typed after them (a day as one end of a range, `1/1/2026...fri`, or after `<` / `>` is refused) |
| | `1/1/2025...1/1/2026`, `<10/1/2026`, `>=10/1/2026` | ranges (inclusive, the earlier first), before, on or after |
| | `...1/1/2026` / `1/1/2025...` | up to and including 1/1/2026 / from 1/1/2025 on (also `...2025`, `10/2026...`) |
| | `10/9`, `Oct 9`, `10/*`, `October`, `12/15...1/15` | month and day in **any year** (birthdays), a month, a span over the new year |
| True/false | `yes` / `no` / `=` / `*` | consent true, false, empty, any |

Punctuation at the start or end of a word is ignored, as in FileMaker (`c++` searches for words starting with
"c"): put text in quotes to search for it exactly. A curly apostrophe (’) and a straight one (') are the same. The
range operator is `...`; `…` (one character, as phones and Macs type it) and `..` are read the same, and four or more
dots are refused. A range written backwards, a year after 9998 or a number too large for the database says so
instead of finding nothing. A criterion that can't be read leaves the found set as it was. **Constrain** and
**Extend** with nothing filled in change nothing and say so (only a blank **Find** means every record). Phone
numbers have no ranges (`516*` finds numbers starting with 516).

Not supported yet: `!` (duplicates), `?` (invalid dates), `~` (relaxed search): they say so. Birthdays without a
year are stored with year 1600: month/day criteria (`9/12`, `10/*`) and the year 1600 itself (`1600`,
`9/12/1600`) find them; another year, a range or before/after with a year never does (`<1/1/1950` isn't "year
unknown").

Typical finds: **a merchant** — pick it under Merchants, Find (a corporate account: all its locations). **One
location of a corporate account** — pick it under Locations. **A merchant and one or more of its services** — pick
the merchant and the services in one request. **A merchant without one service** — pick the merchant, click
*+ Omit request* and pick the service there. **Visited in a date range** — Date `1/1/2026...1/20/2026`. **Friday
guests** — Date `fri` (in October 2026: `fri 10/2026`). **A phone number anywhere** — Phone `516-555-0100` alone.
**Lapsed guests of a merchant** — the merchant and Date `...6/30/2026`. **Imported this month** — Add a field → Record created,
`10/2026`. **Everyone in Bayside or Flushing** — two requests, City `bayside` and City `flushing` (Add a field →
City). **October birthdays** — Add a field → Birthday, `10/*`.

## Tags and exports

- A tag belongs to **one merchant** (the main app imports per merchant), and can hold records of any of its
  locations. Adding a found set adds only that merchant's records and says how many of other merchants were left
  out.
- A tag holds records, not a search: records added later to the database don't join it by themselves. Add or remove
  found sets at any time; the tag's history lists every change.
- **Export**: one row per **unique phone number** of the tag's records, in the converter's import format
  (`schemas/target_schema.yaml`: same headers, order, formats, CSV settings), written by the converter's writer.
  Dates are written as that format says (`10/09/2026 12:00 AM`), never as the screen shows them (`Fri, 10/9/2026`).
  Records without a usable number are left out and counted. The file is named `<merchant ID>_<tag>_<date-time>.csv`.

When one number is in several records:

| Export column | Value |
|---|---|
| phone_number | the number (10 digits) |
| consent | **always blank for now** — blank changes nobody's consent; the main system handles opt-outs per merchant (decided 2026-10-14). Records keep the file's consent and can be searched by it. |
| calls_last, transactions_last, in_sms_last, redemptions_last, spin_last, last_contacted | the latest of the records' values |
| birthday, anniversary | the date with a real year most of the records give (the latest record's on a tie); else the month/day most give, with 1600 |

For an import whose file had **no last-activity column at all**, the converter writes the file's creation date
(*Date Created*, *Sign Up Date*) as the last transaction (the last call, in a call log). Customer Records keeps it as
*First visit / sign-up* and uses it the same way in exports of that import's records. A tag made of one file's
records therefore exports exactly what the converter makes of that file — apart from consent and repeated numbers
being merged — which `scripts/regression/parity.py` checks on 406 files.

Checking that numbers are cell phones and removing numbers on the Do Not Call list (unless within the established
business relationship) are done by the main app when it builds a segment, not here.

## Sending to the main app

Customer Records runs beside the main app and hands its data over through the main app's **bucket** (Cloudflare
R2), as CSV files the main app loads into its ClickHouse (decided 2026-10-17: a separate app first, connected
through the bucket; built into the main app later). With `BUCKET_ENDPOINT`, `BUCKET_NAME`, `BUCKET_ACCESS_KEY_ID`
and `BUCKET_SECRET_ACCESS_KEY` in `.env`, a background sender puts there:

| When | File (under `data/`) |
|---|---|
| an import completes | `customer-records/<ULID>-<merchant>-import-<uid>.csv`: one row per record — IDs (record, import, merchant, location, service), every field, all its numbers, the row's cells as in the file |
| the records of an import that was sent (or tried) are deleted | `customer-records/<ULID>-<merchant>-import-<uid>-deleted.csv`: the same records, marked deleted; it waits for the import's own send |
| a tag is exported, or sent with *Send to the main app* | `customer-records-tags/<ULID>-<merchant>-tag-<uid>.csv`: all its numbers, in the import format (a snapshot: the newest replaces the earlier ones) |
| a tag that was sent is deleted | the same name, saying the tag was deleted |

Files go one at a time, in order; one that fails (bucket unreachable, key refused) waits 1, 2, 4… minutes (at most
an hour) and is tried again until it goes. Each import's and tag's page says whether it was sent, when and as which
file, or why not yet (with *Try again now*); the Merchants page lists the latest sends, and the sidebar warns while
something can't be sent. Imports completed before the bucket was set up are sent once it is. Only merchants of the
main app are sent (an ID that is a number). Columns, formats, deletions and suggested ClickHouse tables:
[docs/BUCKET_FILES.md](docs/BUCKET_FILES.md). The main app stopped picking files up from its bucket by itself on
2026-10-15, so its developer loads these folders (HANDOFF → Open items).

## Running it

### Database

PostgreSQL (the main system uses it too, which keeps connecting the two simple). `DATABASE_URL` says where it is;
the app creates the database and its tables on first start when it may (Homebrew's PostgreSQL allows it).

| Where | Set up PostgreSQL | `DATABASE_URL` |
|---|---|---|
| Mac | `brew install postgresql@17` then `brew services start postgresql@17` (starts with the Mac) | not needed (default `postgresql://localhost:5432/customer_records`) |
| Windows | installer from postgresql.org (note the password chosen for the `postgres` user) | `postgresql://postgres:PASSWORD@localhost:5432/customer_records` in `.env` |
| Docker | included in `docker-compose.yml` | set by docker-compose; set `POSTGRES_PASSWORD` in `.env` |

When PostgreSQL isn't running, the app says so and how to start it.

### On one computer (no Docker)

Install Python 3.11+ and PostgreSQL, then run `./run_local.sh` (Mac/Linux) or `run_local.bat` (Windows). It opens at
http://localhost:8502, next to the file converter on 8501 (`PORT=8600 ./run_local.sh` for another port). The first
run installs the packages (a minute or two); after an update, `run_local.sh` installs them again by itself when they
changed.

### On a server (for the team)

One shared copy that everyone opens in the browser: the same records, tags and imports for all, and up to five
imports converting at the same time (`IMPORT_WORKERS`, each in its own worker process).

1. A Linux server with Docker (size below), and a name for it (e.g. `records.example.com`) pointing at it. Get the
   code onto it (`git clone`, or the bundle).
2. `cp .env.example .env`, then set `POSTGRES_PASSWORD` (long and random), `ADMIN_PASSWORD` (8+ characters, for the
   first admin, `admin`; don't leave it empty on a server, or whoever opens the app first creates the admin) and
   optionally `ANTHROPIC_API_KEY`.
3. `mkdir -p data && sudo chown 1000 data`, then `docker compose up -d --build`. The app is then reachable on the
   server itself only (`127.0.0.1:8502`).
4. Give it an HTTPS address with Caddy on the server, which gets the certificate by itself: in its `Caddyfile`,
   `records.example.com { reverse_proxy localhost:8502 }`; open ports 80 and 443 in the firewall. Over a VPN
   instead: change the line under `ports:` in `docker-compose.yml` to `"8502:8502"` and allow only the VPN to reach
   the server (Docker's published ports bypass `ufw`). Caddy, backups and troubleshooting are in
   [docs/converter/OPERATIONS.md](docs/converter/OPERATIONS.md), written for the file converter: where it differs
   from this README (port 8501, server size, logins off), this README is right.
5. Sign in as `admin`, delete `ADMIN_PASSWORD` from `.env`, and add an account for each person on **Setup → Users**.
6. Load the merchants (**Setup → Merchants**; see [Merchants and locations](#merchants-and-locations)) and add the
   services.

Logins are always on in the Docker setup (`docker-compose.yml` sets `AUTH_MODE=password`, whatever `.env` says):
each person signs in with their own account, and imports, tags and services show who made them. A
deleted account is signed out at its next click. Stopping or updating the server stops the imports running or
waiting at that moment: they show *Interrupted*, with nothing stored, and are started again with *Re-run*.

Server size for 5–20 people with five imports at once:

| | Minimum | Recommended |
|---|---|---|
| CPU | 4 cores (five imports share them, each takes longer) | 6 cores (each import its own core, plus PostgreSQL and the pages) |
| Memory | 8 GB | 16 GB |
| Disk | 40 GB SSD | 100 GB SSD |

What it is based on (measured, 1.5.0): a running import uses about one core and 0.4 GB of memory per 20,000 rows
(1 GB per 100,000) until it ends; PostgreSQL keeps about 2 KB per record with its indexes (200 MB per 100,000
records); each import's files are kept 30 days (`FILE_RETENTION_DAYS`). With less memory, lower `IMPORT_WORKERS`.

### Backups

- **The records** (merchants, locations, services, imports, records, tags): `pg_dump customer_records > backup.sql`
  (Docker: `docker compose exec db pg_dump -U records customer_records > backup.sql`). Restore into an empty
  database with `psql customer_records < backup.sql`.
- **The app's own data** (`data/`: import jobs, templates, each import's files for 30 days): copy the folder.

## Data stored

| Where | What |
|---|---|
| PostgreSQL | merchants and locations (copies of the main app's), services, imports (merchant, location, service, file, who, when, counts), records (every field, the row as in the file, why there's no usable number), their phone numbers, tags, tag records and tag history. Kept until deleted (an import's records can be deleted on its page). |
| `data/converter.db` (SQLite) | the app's own state, as in the converter: import jobs and progress, templates, drafts, users, display preferences |
| `data/imports/<id>/` | each import's original file and side-by-side data; **deleted after `FILE_RETENTION_DAYS` (30)** — the records stay |
| the main app's bucket | the files sent there (see [Sending to the main app](#sending-to-the-main-app)); Customer Records never deletes them, and `reset_records.py` doesn't either |

The records hold customers' phone numbers, names, emails and addresses: keep the database on an encrypted disk and
the app on a trusted network (the server setup has logins on).

## Settings (`.env`)

Customer Records' own:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql://localhost:5432/customer_records` | the PostgreSQL database |
| `POSTGRES_PASSWORD` | — | Docker only: the database password docker-compose uses |
| `RECORDS_SCHEMA_PATH` | `schemas/records_schema.yaml` | the record fields |
| `EXPORT_SCHEMA_PATH` | `SCHEMA_PATH` (`schemas/target_schema.yaml`) | the format tags export into |
| `IMPORT_WORKERS` | 5 | imports that run at the same time, each in its own worker process; more wait as *queued* (the converter alone defaults to 2) |
| `DATABASE_POOL_SIZE` | 16 | the most PostgreSQL connections the app opens for the pages; each running import uses up to 2 more |
| `AUTH_MODE` | `none` | `password`: everyone signs in (accounts on the Users page). Docker always uses `password` |
| `CLICKHOUSE_URL` | — | the main app's ClickHouse (`https://<host>:8443`, or just the host), where merchants and locations are read |
| `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD` | — | its app user (may read `pg.merchants` and `pg.locations`); without these, merchants are loaded from a file on the Merchants page |
| `MERCHANTS_REFRESH_MINUTES` | 60 | minutes between automatic loads of the merchants (0 = only with the Merchants page's button) |
| `BUCKET_ENDPOINT` | — | the main app's bucket, its S3 API address (R2: `https://<account id>.r2.cloudflarestorage.com`); see [Sending to the main app](#sending-to-the-main-app) |
| `BUCKET_NAME` | — | the bucket (`master-bucket`) |
| `BUCKET_ACCESS_KEY_ID`, `BUCKET_SECRET_ACCESS_KEY` | — | a key that may write to it; without the four, nothing is sent |
| `BUCKET_PREFIX` | `data/` | put before the folders `customer-records/` and `customer-records-tags/` |
| `BUCKET_REGION` | `auto` | the region (R2: `auto`) |
| `DEFAULT_PAGE` | `new` | first page: `new`, `imports` or `find` |
| `APP_TITLE` | Customer Records | browser tab title |
| `PORT` | 8502 | `run_local.sh` only: the port (the file converter keeps 8501); not read from `.env` |

The converter's other settings work as before (`ANTHROPIC_API_KEY`, `AI_*`, `MAPPING_MODEL`,
`FILE_RETENTION_DAYS`, `APP_TIMEZONE`, `ADMIN_*`, `DATA_DIR`): see
[docs/converter/README.md](docs/converter/README.md) → Settings. `NUMBER_RECORD_DAYS` has no effect here (the
converter's number history isn't used; the records are the history).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                                   # all tests (needs PostgreSQL installed)
.venv/bin/python scripts/regression/regression.py check v1.0.0   # the engine's output vs a release
.venv/bin/python scripts/regression/parity.py                    # tag exports = converter output, 406 files
.venv/bin/streamlit run app.py
```

```
app.py                     Streamlit web app (the converter's pages with Customer Records' changes)
records/                   Customer Records: config, db (tables), store, importer (records from an import),
                           fields + criteria + find (FileMaker-style finds), tags (exports), ui (new pages)
converter/                 the file converter's engine (0.21.2, with two small additions: see ARCHITECTURE)
schemas/records_schema.yaml   the record fields
schemas/target_schema.yaml    the export format (the converter's import format)
scripts/regression/        regression.py (converter output vs a version), parity.py (tags vs converter)
tests/                     pytest; test_records_*.py for Customer Records
docs/                      Customer Records' documents; docs/converter/ the converter's
```
