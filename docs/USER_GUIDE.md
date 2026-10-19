# User guide

How to load merchants and set up services, import customer files, find records, put them into tags and export a tag
for the main system, and how they reach the main app. No technical knowledge needed. The rules for reading files (phone numbers, dates, birthdays,
column matching) are the file converter's, described in [converter/USER_GUIDE.md](converter/USER_GUIDE.md).

## Opening it

Open the address in a browser (on one computer: http://localhost:8502). On the team's server, sign in with your own
account: an admin adds accounts on **Setup → Users**, and each person changes their password under **My account** in
the menu (*Sign out* is there too). Imports, tags and services show who made them. On one computer there is no
login. The menu on the left has **Records** (Find, Tags, New import, Imports) and **Setup** (Merchants, Services,
Templates, Fields, and Users for admins on the server).

## 1 · Merchants and services

- **Merchants** come from the main app, with its merchant and location IDs; nobody types them in. A merchant is one
  business with one location, or a corporate account with several locations. When the app can read the main app's
  ClickHouse, they are loaded by themselves (every hour, or with *Load from the main app now*). Until then, on
  **Merchants** → *Load from a file*: copy the query, run it in the ClickHouse console, export the result as CSV and
  upload it. Do that again when the main app gets a new merchant or location.
- A merchant without a location in the main app can't be imported for: add its location there first, then load the
  merchants again.
- **Services**: add each service imports can be for. One that is no longer used can be made inactive (untick
  *Active*): it is then not offered on New import, and records imported with it keep it.

## 2 · Import a file

1. **New import**: choose the **Merchant**, its **Location** (filled in by itself when the merchant has one) and
   the **Service**. They stay chosen for the next file. One file is for one location; a file that mixes locations
   has to be split first, for now.
2. Upload the file (CSV, Excel and the other formats the converter reads).
3. Check how it was read and how its columns map to the record fields, exactly as in the converter (field grid,
   *Columns in your file*, notes, *Check for mistakes*). Columns that feed no field are still kept on each record
   as written; tick **Skip** to leave a column out completely. **Consent** is only filled when you send a column to
   it, and you say what each such column records: *opt-ins only* (a no is left blank, e.g. an *Email Opt In*
   column: not opting into email isn't opting out of texts), *opt-ins and opt-outs* (a no is an opt-out), or
   *opt-outs only* (e.g. *Unsubscribed*: a yes is an opt-out, a no is left blank). Pick it in *Goes to*
   ("consent — opt-ins only") or under the field table; **Start import** waits until every consent column has one.
   Several consent columns are read together: an opt-out in any of them wins.
4. **Start import**. It runs in the background; its page shows the progress, then how many records were stored,
   how many different phone numbers, and how many rows have no usable number. Up to five imports run at the same
   time (everyone's together); more wait as *Queued* and start by themselves.

Good to know:

- Every row becomes a record, also rows without a usable phone number (they show the reason, and are never
  exported). Empty rows are skipped.
- A customer with several numbers keeps them all on one record.
- If the same file was already imported for that merchant, the page warns you: importing it again stores its rows a
  second time.
- **A mistake in the mapping?** Open the import, *Delete this import's records* (tick the box first), then *Re-run
  this import*, or upload the file again. Deleting also takes the records out of any tag they were in, and the
  tag's history says so. If the import had already been sent to the main app, its deletion is sent too (section 6).
- **Consent from imports before 1.8.0** was read without the settings above: a no in an opt-in column was stored as
  *false*. Re-run such an import (as above) to read its consent again; the re-run first asks what each of its
  consent columns records.

## 3 · Find records

On **Find**, fill in only what you're looking for: **nothing is required**, and a **Find** with everything blank
shows every record. Each **request** has:

- **Merchants**, **Locations** and **Services**: pick one or more of each (type to search the list). Empty = any.
  A merchant means all its locations. **Locations** lists the locations of corporate accounts (merchants with
  several), to look at one or some of them.
- **Date**: the record's last visit or, from a call log, its last call (one field for both: the later of the two
  when a record has both). Type a day or a range of days, e.g. `1/1/2026...1/20/2026` (a date only — no times), or
  a **day of the week**: `fri` or `Friday` finds every Friday, `sat sun` (or `sat/sun`) the weekends, `mon...fri`
  (or `mon-fri`) the weekdays, and `fri 10/2026` the Fridays of October 2026. Days of the week work in every date
  box.
- **Phone**, **First name** and **Last name**.
- **＋ Add a field**: a menu of every other field — Full name, Gender, Email, Address, City, State, ZIP, Total visits, Total
  spent, Record created (the day the record was imported), Birthday, Anniversary, the other dates (first visit,
  last text, redemption, spin, contacted), Any field,
  Consent, Phone problem, Tag, Import #, File name, Record ID, File row, and the files' own columns. A click adds
  its box to the request; the box's ✕ takes it off. What you typed elsewhere stays.

Leave a box empty to ignore it. Type **`*`** alone to find the records where that field has a value, or **`=`**
alone for the records where it is blank — in any box (Phone `=`: no usable number; Tag `=`: in no tag). Typed
with anything else they keep their usual meaning (`sm*th`, `=smith`).

Then:

- A record must match **everything filled in** in one request.
- **+ Request** adds another request: records matching **either** request are found.
- **+ Omit request** adds a request that **removes** what it matches from the requests above it.
- **Find** (or Enter in any box) makes a new found set. **Constrain** keeps only found records that also match the
  requests on screen; **Extend** adds the records they match (with nothing filled in, both leave the found set as
  it is). **Show all** finds every record. **Clear** empties the requests.
- A criterion that can't be read (a typo, a date that doesn't exist) is explained in red, and the found set stays
  as it was: correct it and click again.
- Ranges use three dots: `...`. Typing `…` (a phone or a Mac may turn three dots into one character) or `..` works
  the same.
- In **Merchants**, **Locations** and **Services**, type an ID or part of a name and press Enter to pick the first
  match; click to pick more.
- **Names**: a file with one *Name* (or *Full Name*, *Customer Name*…) column is split into first and last name when
  it is imported. A file whose *First Name* column holds the whole name keeps it there, with no Last name ("Lisa
  Marie Ray"). First name and Last name also read such a name as split at its first space (Lisa / Marie Ray), or at
  its comma ("Ray, Lisa": Ray is the last name), so First name `lisa` and Last name `ray` both find it. `=` (empty)
  and `*` (any value) look at the stored names only. **Full name** (Add a field) searches first and last together:
  `lisa ray`.

**Gender**: `F` or `M` when the imported file said so (Female, Woman, Girl… become F); `Fb` or `Mb` when it is only
likely, guessed from the first name (Mary → Fb, John → Mb; names used for both, like Jordan, get nothing). Type `f`
for F and Fb together, `=f` for only those the file said, `fb` for only the guesses.

Examples:

| To find | Do |
|---|---|
| Women, including the likely ones / only those the file said | Add a field → Gender: `f` / `=f` |
| A merchant (all its locations) | Merchants: pick it, **Find** |
| One location of a corporate account | Locations: pick it, **Find** |
| A merchant and one or more of its services | Merchants and Services: pick them in one request |
| A merchant without one of its services | Merchants: pick it; **+ Omit request**; Services: pick the service there |
| A phone number, any merchant | Phone `516-555-0100` (nothing else) |
| A merchant's records with a certain phone number | one request: Merchants picked, Phone `516-555-0100` |
| Smiths who live in Queens | one request: Last name `smith`, City `queens` (Add a field → City) |
| Guests of Bayside or Flushing | two requests, each with City added: `bayside` / `flushing` |
| Who visited between two days | Date `1/1/2026...1/20/2026` |
| Who visited in 2025 | Date `1/1/2025...12/31/2025` (or `2025`) |
| Who hasn't visited since June | Date `...6/30/2026` (up to and including June 30) |
| Who visited since September | Date `9/1/2026...` (September 1 on) |
| Friday guests / Friday guests in October | Date `fri` / `fri 10/2026` |
| Weekend guests this year / October guests except weekend ones | Date `sat sun 2026` / Date `10/2026`, then **+ Omit request** with Date `sat sun` |
| Records imported today / this month | Add a field → Record created: `//` / `10/2026` |
| October birthdays, any year | Add a field → Birthday: `10/*` (one day: `10/15`; a span: `10/1...10/15`) |
| Records with no usable phone number | Phone `=` |
| Records already in a tag | Add a field → Tag: `promo` (in any tag: `*`; in no tag: `=`) |
| A value of a column that feeds no field | Add a field → Columns of the files → Points: `>=100` (compares text), or Notes: `vip` |

How to type criteria is listed under **How to search** on the page and in the [README](../README.md#finding-records).

The **found set** shows how many records, how many unique phone numbers and how many without a usable number, and
for each record its **Record ID**, when it was **created** and its **Date**. Dates on the screen show their day of
the week: **Fri, 10/9/2026** (with the time when there is one: **Fri, 10/9/2026 3:45 PM**; birthdays and
anniversaries without the day). Click a date column's heading to sort the page by it. Pick the columns to show, the
sort (e.g. **Date (newest first)**) and the page. Select rows to:

- see **one record** (select one row): every field, its phone numbers (with the number as written and whether it
  was repaired — check those), values that couldn't be read, its tags, the row exactly as in the file, and a button
  to open its import;
- **Omit selected** from the found set.

**Show omitted only** swaps the found set for every other record. **Prepare CSV of the found set** gives every
field of every found record, for Excel, with dates as `10/09/2026` (which Excel reads as dates) and the Date's day
of the week in a column of its own. Tag exports write dates in the main system's import format
(`10/09/2026 12:00 AM`), never with the day of the week.

## 4 · Put records into a tag

Under the found set, **Tag the found set**:

- choose a tag and **Add found set** (or **Remove found set**), or
- type a name, choose the merchant and **Create tag with found set**.

A tag belongs to **one merchant** (any of its locations): only that merchant's records go in, and the page says how
many of other merchants were left out. **Constrain** with one merchant to keep a found set to one merchant.

## 5 · Export a tag

**Tags** → select a tag:

- **Prepare export**, then download it. The file has **one row per unique phone number**, in the import format of
  the main system (the converter's), named `<merchant ID>_<tag>_<date-time>.csv`.
- **Export preview** shows the rows exactly as exported.
- **Records** lists the tag's records; select some and *Remove selected from tag*.
- **Edit**: rename, notes, delete the tag (the records stay).
- **History**: who created it, added or removed records (and how many), exported it.

Exporting a tag also sends its numbers to the main app (section 6); so does **Send to the main app** on the tag's
page, without downloading anything.

What the export contains when a number is in several records: the latest date of each activity, the birthday or
anniversary with a real year, and **consent left blank** (the main system handles opt-outs per merchant; blank
changes nobody's consent). Records without a usable number aren't in it.

## 6 · Sending to the main app

Once the app has the main app's bucket set up (by whoever runs it, in `.env`), it sends there by itself:

- every **import** once it has completed (all its records, with their merchant, location and service);
- the **deletion** of an import's records, when they are deleted after the import was sent;
- a **tag**, each time it is exported or sent with *Send to the main app* (all of its numbers, as they are then);
  and once more if it is deleted.

Each import's page and each tag's page has a **Main app** line: when it was sent and as which file, *waiting to be
sent*, or *not sent yet* with the reason and the time of the next try (it keeps trying by itself; **Try again now**
doesn't wait). The **Imports** list has a *Main app* column. **Merchants** → *Sending to the main app* lists the
latest files and what is waiting, and the menu shows a warning while something can't be sent. Without the bucket set
up, the lines say sending is off; nothing is lost: what waits is sent once it is set up.

The main app decides what to do with the files: segments built from tags check opt-outs, the Do Not Call list and
mobile numbers there.

## Common questions

**The app says it can't connect to PostgreSQL.** The database isn't running. On a Mac: `brew services start
postgresql@17`, then reload the page.

**A record shows a phone number marked "repaired — check it".** The converter fixed a likely typo (e.g. filled in
the file's main area code). Check it against the file; a wrong guess texts a stranger.

**A birthday shows 1600.** Month and day are known, the year isn't (as in the converter).

**The same customer appears twice.** Each import keeps its rows; the tag export has each number once.
