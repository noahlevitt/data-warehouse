# User guide

How to set up businesses and services, import customer files, find records, put them into tags and export a tag
for the main system. No technical knowledge needed. The rules for reading files (phone numbers, dates, birthdays,
column matching) are the file converter's, described in [converter/USER_GUIDE.md](converter/USER_GUIDE.md).

## Opening it

Open the address in a browser (on one computer: http://localhost:8502). There is no login for now. The menu on the
left has **Records** (Find, Tags, New import, Imports) and **Setup** (Businesses, Services, Templates, Fields).

## 1 · Set up businesses and services (once)

- **Businesses**: *Add a business* with its **ID exactly as in the main system** (that is how the two will be
  connected), its name and optional notes. A mistyped ID can be corrected under *Change a business*: everything
  of that business follows. A business can only be deleted while it has no imports and no tags.
- **Services**: add each service imports can be for. One that is no longer used can be made inactive (untick
  *Active*): it is then not offered on New import, and records imported with it keep it.

## 2 · Import a file

1. **New import**: choose the **Business** and the **Service**. They stay chosen for the next file.
2. Upload the file (CSV, Excel and the other formats the converter reads).
3. Check how it was read and how its columns map to the record fields, exactly as in the converter (field grid,
   *Columns in your file*, notes, *Check for mistakes*). Columns that feed no field are still kept on each record
   as written; tick **Skip** to leave a column out completely.
4. **Start import**. It runs in the background; its page shows the progress, then how many records were stored,
   how many different phone numbers, and how many rows have no usable number.

Good to know:

- Every row becomes a record, also rows without a usable phone number (they show the reason, and are never
  exported). Empty rows are skipped.
- A customer with several numbers keeps them all on one record.
- If the same file was already imported for that business, the page warns you: importing it again stores its rows a
  second time.
- **A mistake in the mapping?** Open the import, *Delete this import's records* (tick the box first), then *Re-run
  this import*, or upload the file again. Deleting also takes the records out of any tag they were in, and the
  tag's history says so.

## 3 · Find records

On **Find**, each **request** is a small table: pick a **Field** and type a **Criterion** in as many rows as you
need. A new request starts with Phone, First name, Last name and Any field; change any row's field, and leave a
criterion empty to ignore its row.

- A record must match **every** criterion of one request.
- **+ Request** adds another request: records matching **either** request are found.
- **+ Omit request** adds a request that **removes** what it matches from the requests above it.
- **Find** makes a new found set. **Constrain** keeps only found records that also match the requests on screen;
  **Extend** adds the records they match. **Show all** finds every record. **Clear** empties the requests.
- **Limit to businesses / services** at the top applies to everything, always.

Examples:

| To find | Do |
|---|---|
| A business's records with a certain phone number | one request: Business `1001`, Phone `516-555-0100` |
| A service within a business | Limit to that business and service, click **Show all** |
| Smiths who live in Queens | one request: Last name `smith`, City `queens` |
| Guests of Bayside or Flushing | two requests: City `bayside` / City `flushing` |
| Everyone of a business except one service | request Business `1001`, then an Omit request: Service `square` |
| Who visited in September 2026 | Last visit / order `9/2026` (or `9/1/2026...9/30/2026`) |
| Who hasn't visited since July | Last visit / order `<7/1/2026` |
| October birthdays, any year | Birthday `10/*` (one day: `10/15`; a span: `10/1...10/15`) |
| Records with no usable phone number | Phone `=` |
| Records already in a tag | Tag `promo` (in any tag: `*`; in no tag: `=`) |
| A value of a column that feeds no field | Column: Points `>=100` (compares text) or Column: Notes `vip` |

How to type criteria is listed under **How to search** on the page and in the [README](../README.md#finding-records).

The **found set** shows how many records, how many unique phone numbers and how many without a usable number. Pick
the columns to show, the sort and the page. Select rows to:

- see **one record** (select one row): every field, its phone numbers (with the number as written and whether it
  was repaired — check those), values that couldn't be read, its tags, the row exactly as in the file, and a button
  to open its import;
- **Omit selected** from the found set.

**Show omitted only** swaps the found set for every other record. **Prepare CSV of the found set** gives every
field of every found record, for Excel.

## 4 · Put records into a tag

Under the found set, **Tag the found set**:

- choose a tag and **Add found set** (or **Remove found set**), or
- type a name, choose the business and **Create tag with found set**.

A tag belongs to **one business**: only that business's records go in, and the page says how many of other
businesses were left out. Use *Limit to* to keep a found set to one business.

## 5 · Export a tag

**Tags** → select a tag:

- **Prepare export**, then download it. The file has **one row per unique phone number**, in the import format of
  the main system (the converter's), named `<business ID>_<tag>_<date-time>.csv`.
- **Export preview** shows the rows exactly as exported.
- **Records** lists the tag's records; select some and *Remove selected from tag*.
- **Edit**: rename, notes, delete the tag (the records stay).
- **History**: who created it, added or removed records (and how many), exported it.

What the export contains when a number is in several records: the latest date of each activity, the birthday or
anniversary with a real year, and **consent left blank** (the main system handles opt-outs per business; blank
changes nobody's consent). Records without a usable number aren't in it.

## Common questions

**The app says it can't connect to PostgreSQL.** The database isn't running. On a Mac: `brew services start
postgresql@17`, then reload the page.

**A record shows a phone number marked "repaired — check it".** The converter fixed a likely typo (e.g. filled in
the file's main area code). Check it against the file; a wrong guess texts a stranger.

**A birthday shows 1600.** Month and day are known, the year isn't (as in the converter).

**The same customer appears twice.** Each import keeps its rows; the tag export has each number once.
