# Customer Records

Internal web app that keeps every customer row of every imported file as a searchable record. Each import is for one
**business** (with the same ID as in the main system) and one **service**. Records are found with FileMaker-style
find requests, collected into **tags**, and a tag exports its unique phone numbers in the file converter's import
format, ready to import into the main system.

Built on the file converter (file-converter 0.21.2): files are read, matched and cleaned by the same engine, with the
same rules. **Version 1.0.0 — prototype.**

**Quick start (Mac):**

```
brew install postgresql@17
brew services start postgresql@17
./run_local.sh
```

Then open http://localhost:8502 (port 8502, so the file converter can keep running on 8501). The app creates its
database on first start. There is no login for now.
**Tests:** `.venv/bin/python -m pytest -q` (PostgreSQL must be installed; the tests start their own server).

## Documentation

| Document | For | What's in it |
|---|---|---|
| This README | everyone | what the tool does, finding records, tags and exports, running it, settings |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | people using it | step by step: businesses and services, importing, finding, tagging, exporting |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | developers | how it is put together, the database, design decisions, tests, connecting to the main system |
| [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) | developers | limits and quirks worth knowing before changing code |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | everyone | what changed in each version |
| [docs/HANDOFF.md](docs/HANDOFF.md) | whoever continues the work | how changes are made and delivered, the business's decisions, open questions |
| [docs/converter/](docs/converter/README.md) | everyone | the file converter's documents (0.21.2): every rule for phone numbers, dates and column matching used by each import |

```
Businesses / Services:  set up once: each business with the main system's ID; the list of services
New import:  business + service → upload → columns matched (template / rules / Claude on request) → review
             → Start import (background) → one record per file row, all its numbers, every column kept
Find:        find requests (AND within a request, OR between requests, Omit) → found set → constrain / extend /
             omit → add to a tag, or download as CSV
Tags:        one business per tag → export: one row per unique phone number, in the converter's import format
```

## Pages

| Page | What it's for |
|---|---|
| **Find** | Find records with requests (see [Finding records](#finding-records)); limit to businesses and services; see the found set, one record's details (every field, its numbers, the row as in the file, its tags); omit records; add the found set to a tag or make a new tag from it; download the found set as CSV. |
| **Tags** | Every tag with its business, records and unique numbers. One tag: export (download and preview), its records (remove some), rename, notes, delete, and its history (who added or removed how many records, exports). |
| **New import** | Choose the business and service, upload a file, check how it was read, review the column mapping (the converter's page), start the import. Warns when the same file was already imported for that business. |
| **Imports** | Every import with its business, service, status, progress and records. One import: records stored, numbers, rows without a usable number, side by side (original vs cleaned), what changed, the mapping used, the mistake check, *Show this import's records in Find*, *Delete this import's records*. |
| **Businesses** | Add a business (ID as in the main system, name, notes), correct one (a corrected ID follows into its imports, records and tags), delete one that has no imports or tags. |
| **Services** | The list every import picks from: add, rename, deactivate (no longer offered; its records keep it), delete one no import uses. |
| **Templates** | Saved column mappings per file layout (as in the converter). The business and service are chosen for each import, not saved. |
| **Fields** | The record fields and the export format, read-only. |

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
- **The same file twice** for the same business: the page warns before importing it again.
- **Re-running** a completed import (after fixing the mapping): delete its records first (*Delete this import's
  records* on its page), which also takes them out of their tags; then *Re-run this import*.

### Record fields

`schemas/records_schema.yaml`. Typed and cleaned, so they can be searched by value and range:

| Field | Type | From columns such as |
|---|---|---|
| Phone | 10-digit US numbers (all of a row's) | Phone, Mobile, Cell, Home Phone, phone2… |
| First name, Last name | text | First Name, Last Name; a whole-name column (*Name*, *Full Name*, *Customer Name*, *Guest*) is split by the converter into first and last word ("Ray, Lisa" too); a whole name in a *First Name* column stays there, and is found by First and Last name (see Finding records) |
| Email | email (lower-cased) | Email, E-mail |
| Address, Address 2, City, State, ZIP | text, 2-letter state, 5-digit ZIP | Address, City, State, Zip; a one-line address is split |
| Total visits | whole number | Visits, Visit Count, Total Orders |
| Total spent | amount | Total Spend, Lifetime Spend, LTV, Total Sales |
| First visit / sign-up | date and time | Created At, Sign Up Date, Member Since, First Visit |
| Last visit / order | **date only** (a time in the file is dropped, after converting a written zone to the client's) | as in the converter |
| Last call, Last text received, Last redemption, Last spin | date and time | as in the converter |
| Last contacted | date and time | **mapped by hand only** (as in the converter) |
| Birthday, Anniversary | date; 1600 = no year | as in the converter |
| Consent | true / false | **mapped by hand only** (as in the converter) |

To add a field, add it to the YAML file and restart the app: its database column is added on start.

## Finding records

A find is one or more **requests**. **Nothing is required**: fill in only what you're looking for, and a Find with
everything blank shows every record. Each request shows what is searched most: **Business** and **Services**
pickers (one or more of each; empty = any) and **Last visit / order**, then **Phone**, **First name** and **Last
name**. Every other field is one click away under **＋ Add a field** (and taken off again with its ✕): Full name,
Email, address, City, State, ZIP, Total visits, Total spent, Record created (the import date), Birthday,
Anniversary, the other dates, Any field, Consent, Phone problem, Tag, Import #, File name, Record ID, File row and
the files' own columns. In one request a record must match **everything filled in** (AND); several requests **add
up** (OR); an
**Omit** request removes what it matches from what the requests above it found (FileMaker's rules: requests are
applied top to bottom; an Omit as the first request starts from all records; a blank request is ignored). Enter in
any box is **Find**. Then:

- **Constrain** — keep only found records that also match new requests; **Extend** — add what new requests match.
- **Omit selected** — take selected records out; **Show omitted only** — every record not in the found set.
- **Show all** — every record (the same as a blank Find).

Every record has its own **Record ID** and its **Record created** date and time (when its import stored it), shown
in the found set and searchable like any number and date. **Last visit / order is a date only**: search it by days
(`1/1/2026...1/20/2026`). **Names**: a file whose *First Name* column holds whole names leaves them in First name
with no Last name ("Lisa Marie Ray"). First name and Last name also read such a name as split at its first space
(Lisa / Marie Ray), or at its comma ("Ray, Lisa": Ray is the last name), so First name `lisa` and Last name `ray`
both find it; `=` and `*` look at the stored names only. *Full name* searches first and last together (`lisa ray`).

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
| | `10/9/2026 3:45 PM`, `>10/9/2026 9:00 AM` | that minute, after it (date-and-time fields such as Record created; a date-only field like Last visit takes the day) |
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

Typical finds: **a business** — pick it under Business, Find. **A business and one or more of its services** — pick
the business and the services in one request. **A business without one service** — pick the business, click
*+ Omit request* and pick the service there. **Visited in a date range** — Last visit / order
`1/1/2026...1/20/2026`. **A phone number anywhere** — Phone `516-555-0100` alone. **Lapsed guests of a business** —
the business and Last visit / order `...6/30/2026`. **Imported this month** — Add a field → Record created,
`10/2026`. **Everyone in Bayside or Flushing** — two requests, City `bayside` and City `flushing` (Add a field →
City). **October birthdays** — Add a field → Birthday, `10/*`.

## Tags and exports

- A tag belongs to **one business** (the main system imports per business). Adding a found set adds only that
  business's records and says how many of other businesses were left out.
- A tag holds records, not a search: records added later to the database don't join it by themselves. Add or remove
  found sets at any time; the tag's history lists every change.
- **Export**: one row per **unique phone number** of the tag's records, in the converter's import format
  (`schemas/target_schema.yaml`: same headers, order, formats, CSV settings), written by the converter's writer.
  Records without a usable number are left out and counted. The file is named `<business ID>_<tag>_<date-time>.csv`.

When one number is in several records:

| Export column | Value |
|---|---|
| phone_number | the number (10 digits) |
| consent | **always blank for now** — blank changes nobody's consent; the main system handles opt-outs per business (decided 2026-10-14). Records keep the file's consent and can be searched by it. |
| calls_last, transactions_last, in_sms_last, redemptions_last, spin_last, last_contacted | the latest of the records' values |
| birthday, anniversary | the date with a real year most of the records give (the latest record's on a tie); else the month/day most give, with 1600 |

For an import whose file had **no last-activity column at all**, the converter writes the file's creation date
(*Date Created*, *Sign Up Date*) as the last transaction (the last call, in a call log). Customer Records keeps it as
*First visit / sign-up* and uses it the same way in exports of that import's records. A tag made of one file's
records therefore exports exactly what the converter makes of that file — apart from consent and repeated numbers
being merged — which `scripts/regression/parity.py` checks on 406 files.

**Not built yet** (planned before tagging, and partly dependent on the main system): checking that numbers are cell
phones, removing numbers on the Do Not Call list unless within an 18-month established business relationship, and
connecting to the main system. See [docs/HANDOFF.md](docs/HANDOFF.md) → Open items.

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

Docker: `cp .env.example .env` (set `POSTGRES_PASSWORD`, optionally `ANTHROPIC_API_KEY`), `mkdir -p data && sudo
chown 1000 data`, `docker compose up -d --build`. Put it behind HTTPS (Caddy) or a VPN, and turn logins on
(`AUTH_MODE=password`): the records hold customers' phone numbers. See
[docs/converter/OPERATIONS.md](docs/converter/OPERATIONS.md) for Caddy, users and troubleshooting; this app's port
is 8502 where that document says 8501 (e.g. `reverse_proxy localhost:8502`).

### Backups

- **The records** (businesses, services, imports, records, tags): `pg_dump customer_records > backup.sql`
  (Docker: `docker compose exec db pg_dump -U records customer_records > backup.sql`). Restore into an empty
  database with `psql customer_records < backup.sql`.
- **The app's own data** (`data/`: import jobs, templates, each import's files for 30 days): copy the folder.

## Data stored

| Where | What |
|---|---|
| PostgreSQL | businesses, services, imports (business, service, file, who, when, counts), records (every field, the row as in the file, why there's no usable number), their phone numbers, tags, tag records and tag history. Kept until deleted (an import's records can be deleted on its page). |
| `data/converter.db` (SQLite) | the app's own state, as in the converter: import jobs and progress, templates, drafts, users, display preferences |
| `data/imports/<id>/` | each import's original file and side-by-side data; **deleted after `FILE_RETENTION_DAYS` (30)** — the records stay |

The records hold customers' phone numbers, names, emails and addresses: keep the database on an encrypted disk and
the app on a trusted network, and turn logins on for a shared server.

## Settings (`.env`)

Customer Records' own:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql://localhost:5432/customer_records` | the PostgreSQL database |
| `POSTGRES_PASSWORD` | — | Docker only: the database password docker-compose uses |
| `RECORDS_SCHEMA_PATH` | `schemas/records_schema.yaml` | the record fields |
| `EXPORT_SCHEMA_PATH` | `SCHEMA_PATH` (`schemas/target_schema.yaml`) | the format tags export into |
| `DEFAULT_PAGE` | `new` | first page: `new`, `imports` or `find` |
| `APP_TITLE` | Customer Records | browser tab title |
| `PORT` | 8502 | `run_local.sh` only: the port (the file converter keeps 8501); not read from `.env` |

The converter's settings work as before (`ANTHROPIC_API_KEY`, `AI_*`, `MAPPING_MODEL`, `IMPORT_WORKERS`,
`FILE_RETENTION_DAYS`, `APP_TIMEZONE`, `AUTH_MODE`, `ADMIN_*`, `DATA_DIR`): see
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
