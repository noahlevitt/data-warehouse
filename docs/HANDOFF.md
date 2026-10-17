# Handoff

For whoever picks Customer Records up next: a person, or Claude in a new chat. It records what the code and the
other documents don't: how changes are made and delivered, the decisions the business made, and what is still open.

**State:** version 1.6.0, the first prototype (see [CHANGELOG.md](CHANGELOG.md)), based on file-converter 0.21.2.
About 1,270 tests. Repository: github.com/noahlevitt/data-warehouse, with its own git history: it started from the
converter's files, not from its history (section 5).
Owner: Noah Levitt. Last updated 2026-10-15. Update this file with every release (section 4 in particular).

What the tool does, and how, is in the [README](../README.md) and the documents it lists. Read
[ARCHITECTURE.md](ARCHITECTURE.md) before changing code and [KNOWN_ISSUES.md](KNOWN_ISSUES.md) before calling
something a bug. The conversion engine's own handoff, with the converter project's rules and history, is
[converter/HANDOFF.md](converter/HANDOFF.md).

## 1. How changes are made

**Commits** (the same rules as the converter project)
- Author and committer: **Noah Levitt <noah@thelevittfamily.com>**, for every commit.
- No AI attribution of any kind: no `Co-Authored-By` lines, no session links, and no "Claude" or "Joey" in commit
  messages. Describe the app's Claude features in commit messages as "the AI suggestions". (The code and the
  documents do name Claude, the optional AI the app uses; that is fine.) Check before delivering:
  `git log vPREVIOUS..main --format='%B' | grep -ci "claude\|joey\|co-authored"` must print 0.
- One commit per changed file, message in the imperative ("Add …", "Fix …", "Document …").
- Setting the identity for a session:

  ```bash
  export GIT_AUTHOR_NAME="Noah Levitt" GIT_AUTHOR_EMAIL="noah@thelevittfamily.com"
  export GIT_COMMITTER_NAME="Noah Levitt" GIT_COMMITTER_EMAIL="noah@thelevittfamily.com"
  ```

**Versions**
- `records/__init__.py` → `__version__`, shown in the sidebar next to the engine's version (`converter/__init__.py`,
  which stays file-converter's number). Patch (1.0.0 → 1.0.1) for fixes, minor (1.0 → 1.1) for new behaviour.
  Changes to documents or development scripts only: no new version.
- Every version gets a [CHANGELOG.md](CHANGELOG.md) entry written for the people using the app, and a git tag
  `vX.Y.Z` on its last commit (the "Version X.Y.Z" commit), pushed with the branch (`git push origin vX.Y.Z`).

**Checks before a release** (all of them, every time)
1. The whole test suite: `.venv/bin/python -m pytest -q` (about 1,270 tests, about 3 minutes). PostgreSQL must be
   installed: the tests start a private server (see `tests/conftest.py`), or set `RECORDS_TEST_DATABASE_URL`.
   Records tests that are *skipped* mean PostgreSQL wasn't found: that is not a pass.
2. New tests fail on the previous version: copy them into `git worktree add -f /tmp/old vPREVIOUS`, run them there,
   then `git worktree remove --force /tmp/old`.
3. The converter's regression check: `python scripts/regression/regression.py check vPREVIOUS` (after 1.0.0:
   `check v1.0.0`). Expected: `identical`; it shows the engine's own output didn't change. When a converter fix was
   ported in (section 5), the differences must be exactly the ones that fix explains. 1.0.0 itself was compared
   with file-converter 0.21.2: identical on all 406 files.
4. The parity check: `DATABASE_URL=<a server where databases may be created> python scripts/regression/parity.py`.
   Expected: `identical`. A tag of one file's records must export what the converter makes of that file.
5. Changes to the web pages: run the real `app.py` with Streamlit's AppTest (see `tests/test_records_app.py`,
   `run_page`). AppTest can't type into data editors: a change to the Find request tables also needs a check in a
   real browser (for 1.0.0: three requests typed, one changed to another field, one an Omit, with Playwright).
6. A feature that decides data (which records, which number, which date, which consent goes out): have it tested
   by someone, or a separate Claude agent, that did not build it, working only from the user-facing documents and
   trying to break it. 1.0.0 was tested that way (see section 4).

**Documents** — every change in behaviour updates, as they apply: README, docs/USER_GUIDE, docs/ARCHITECTURE
(modules, tables, key behaviours), docs/KNOWN_ISSUES, `.env.example` (settings), docs/CHANGELOG, this file, and the
overview at the top of each changed source file. `docs/converter/` holds the converter's documents as of 0.21.2:
change them only when a converter fix is ported in (section 5).

**Delivery** (when the changes can't be pushed directly)
- Changes go to Noah as a git bundle, `data-warehouse-updateN.bundle`, numbered in order. The repository itself
  came as `data-warehouse-1.0.0.bundle` (complete, no prerequisites); 1.0.1 came as update1, 1.0.2 as update2,
  1.1.0 as update3, 1.2.0 as update4, 1.2.1 as update5, 1.3.0 as update6, 1.3.1 as update7, 1.4.0 as update8,
  1.5.0 as update9, 1.6.0 as update10, 1.6.1 as update11 (with 1.6.0, for a copy still on 1.5.0) and 1.7.0 as
  update12, so the next is `data-warehouse-update13.bundle`. Update this line with each bundle.

  ```bash
  git bundle create ../data-warehouse-updateN.bundle main vX.Y.Z ^vPREVIOUS
  ```
- Before sending it, rehearse: clone GitHub into a temporary folder, `git pull --ff-only` the bundle, fetch its
  tags, compare the files with the working copy and run the tests there.
- Noah installs it on a Mac (zsh); his clone is `~/Downloads/data-warehouse`. Give him complete commands that he
  can paste as they are: start with the `cd`, put **no `#` comments** in them (zsh stops at a `#` followed by an
  apostrophe), and say which line `git log -1` should print. `./run_local.sh` reinstalls the packages by itself
  when requirements.txt changed.

  ```
  cd ~/Downloads/data-warehouse
  git pull --ff-only ~/Downloads/data-warehouse-updateN.bundle main
  git fetch ~/Downloads/data-warehouse-updateN.bundle 'refs/tags/*:refs/tags/*'
  git log -1 --format='%h %s'
  git push origin main
  git push origin vX.Y.Z
  ./run_local.sh
  ```
- The first install (1.0.0) was different: PostgreSQL installed with Homebrew, the GitHub repository
  `data-warehouse` created empty on github.com, then `git clone` of `data-warehouse-1.0.0.bundle` into
  `~/Downloads/data-warehouse`, `origin` pointed at GitHub, `main` and `v1.0.0` pushed. The commands are in the
  delivery message of 1.0.0.
- Per the converter's handoff, this project should have its own claude.ai project (not "Noah", which is the
  converter's), with a copy of this README.

## 2. Decisions the business made (this project)

Decided by Noah on 2026-10-14 unless marked otherwise. Don't reverse one without asking.

| Area | Decision |
|---|---|
| What it is | Keep nearly every field of every import as a record; each file row is its own record. Search every field, alone and combined (AND, OR, Omit, as in FileMaker). Put chosen records into tags; a tag is the list of unique numbers imported into the main system. |
| Businesses and services | Every import is for one business and one service. Services are a predetermined list. **Businesses (1.6.0) are the main app's merchants and locations**: a merchant is one business with one location, or a corporate account with several locations (one merchant ID, several location IDs); sometimes one business is two merchant IDs. Every import is for one **location**, so a find can look at one location; one import is one location for now (files with a location column later: split by AI or by code, the developer's call; planned: map the column's values to locations). They come from the main app's ClickHouse, where the merchants and locations live (Noah, 2026-10-16). Built in 1.6.0: copies loaded automatically with the app's own ClickHouse user, or from a CSV exported in the console; never typed in. No real data was in Customer Records yet, so starting fresh was fine. |
| Database | PostgreSQL (the main system uses PostgreSQL and DuckDB, and ClickHouse). **80 million records or more are expected** (Noah, 2026-10-16), so the records move to ClickHouse (option 1 chosen 2026-10-16: records and their numbers in ClickHouse, everything else in PostgreSQL), after a load test of 80M records against PostgreSQL (2.0). Since 1.7.0 the records reach the main app's ClickHouse through its bucket (the main app loads them), so no write access is needed; 2.0 is then finds reading them there. Noah has read access to the main app's ClickHouse (Cloud, 26.6). |
| Rows without a usable number | Stored as records (searchable), with the reason; never exported. |
| Tags | One merchant per tag (Noah, 2026-10-16; was one business per tag). Deduplicated (one row per number) when exported. **Tags will be the merchant's segments in the main app**, directly if possible, because the main app can no longer make a segment from a single import (Noah, 2026-10-16; the main app dropped its single-import segment source on 2026-10-14). Built in 1.7.0: each export, and *Send to the main app*, sends a snapshot of the tag to the main app's bucket (the newest replaces the earlier ones; a deleted tag sends a last one saying so; agreed by Noah 2026-10-16). The segment source that reads them is the main app's side. |
| Export format | The file converter's import format, until the systems are connected. |
| Consent in exports | Not sent (blank): opt-outs are per business and the main system handles them; once connected, it will know who is opted in or out. Records keep the file's consent. |
| Before tagging (later) | Deduplicate, check that numbers are cell phones, and remove numbers on the Do Not Call list unless within an 18-month established business relationship. The counts that matter come after a find, once these are applied (Noah, 2026-10-16). The main app already does them when it evaluates a segment (seen in its code 2026-10-17: mobile only, VoIP excluded; opt-outs; the Do Not Call list with an established-business-relationship window per segment on the last transaction, call and incoming text, 18 months or 3 for calls), so a tag sent there gets them. |
| Connecting to the main app | **A separate app first, connected through the main app's bucket, then built into the main app** (Noah, 2026-10-17: "get this up and running first and then work on the new features actually within the app"). Built in 1.7.0: completed imports (Customer Records' cleaned records with record, import, merchant and location IDs, not the original files; agreed 2026-10-16), deletions (a file with the same keys marked deleted; agreed 2026-10-16) and tag snapshots go to the bucket as CSV files (docs/BUCKET_FILES.md); the main app loads them. Finds are to read the main app's ClickHouse later (Customer Records' imports there plus its other data, the old FileMaker exports `sodb_export` and `client_users_export` included), not both databases. Later, in the main app (sendsational, a TypeScript monorepo; the lead developer allows work on a branch): its importer rebuilt around **services with saved column mappings** so new file types need no code, one ClickHouse table for all imports, the converter ported to TypeScript with the 406-file check as its test, lists (tags) as a segment source, and Find and Tags pages there; then this app retires. |

| Platform | A web app or something launched on the computer, Mac first, Windows too. Built as the converter: a local web app (`./run_local.sh`), Docker for a server. |
| Finds | Nothing is ever required; a blank Find shows every record. The common finds are a business, a business and one or more services, and a business without a service (an Omit request), so each request has Business and Services pickers; a business isn't required either (e.g. a phone number alone). Dates by day or range, also open-ended: `...1/1/2026` up to that day, `1/1/2025...` from it. Every record has its own ID and a creation date, both searchable. (Noah, 2026-10-14, after trying 1.0.2.) |
| What finds look like | Searched most: Business, Last visit and Service; then a phone number; sometimes first name or first and last name; sometimes ZIP, city, state, address, total spent, birthday, visits, the record's creation (import) date; rarely the record ID. So a request shows Business, Services and Last visit, then Phone, First and Last name, and every other field is added with a click (Add a field). (Noah, 2026-10-14, after 1.1.0.) |
| Last visit | A date only, so searches are by days (`1/1/2026...1/20/2026`) and no time is involved. (Noah, 2026-10-14.) Consequence built in 1.2.0: the time in a file is dropped, exports write 12:00 AM. |
| Names | First name must also find files that put the whole name in First name ("just give a name"); the logic was left to the developer ("figure it out"): a whole name kept in First name (no last name) is also searched as split at a comma, else at its first space; the stored value still matches; see README → Finding records. (Files with a *Name* / *Full Name* column are split by the converter at import, as before.) |
| Date and days of the week | The last visit and the last call are one field, **Date**, searched as the last visit was; a day of the week can be searched in it (`Fri`, `Friday`: all Fridays), as part of the field or a separate one if necessary (Noah, 2026-10-15). Built in 1.3.0 in the field itself, in every date box: `fri`, `sat sun`, `mon...fri`, `fri 10/2026`. A record with both dates has the later as its Date; both stay stored and exported. |
| Dates on the screen | Shown with the day of the week, `Fri, 1/1/2026`; tag exports keep the original format the main system imports (Noah, 2026-10-15). Built in 1.3.1: found set and record details (with the time: `Fri, 10/9/2026 3:45 PM`); birthdays and anniversaries without the day; the found-set CSV keeps `10/09/2026` plus a Day column. |
| Gender | F / M when an import says so (Female, Woman, Girl… = F); Fb / Mb = likely female / male, updated from other information, mostly the first name; automatic if fast, otherwise run by hand on a found set or the whole database (Noah, 2026-10-15). Built in 1.4.0, automatic (fast enough: ~1.5 s per 20,000 imported rows): US SSA baby names since 1930 (≥ 90% one sex, ≥ 10 people), a title first, the email when there is no known first name; never over F / M or a file's other answer (Other, Non-binary stay empty: a choice made while building, easy to change). Not exported (not in the import format). |
| Repository | A brand-new project and repository, github.com/noahlevitt/data-warehouse, with its own commit history (not file-converter's). |
| Port | 8502. The file converter keeps running on 8501 on the same computer, so the two must never share a port (1.0.1). |
| Shared server | One shared copy on a server for the staff who import and review mappings: a few now, 5–20 users expected, possibly more later. Logins on. The front end stays Streamlit for now; a React version (the main app is React) is postponed, possibly tried on a branch (Noah, 2026-10-15). Built in 1.5.0: the Docker setup always has logins (`AUTH_MODE=password` in `docker-compose.yml`), is published on the server itself only (`127.0.0.1:8502`) for Caddy's HTTPS, ships no admin password, and the README gives the steps and the server size (choices made while building). |
| Imports at once | Five imports at the same time (Noah, 2026-10-15). Built in 1.5.0: `IMPORT_WORKERS` defaults to 5 and each import runs in its own worker process (`records/runner.py`); in threads they took turns and five took as long as five in a row. Stopping the server now interrupts running and queued imports instead of finishing them (a choice made while building). |

**Carried over from the converter, not yet confirmed for this project** (stated in the plan on 2026-10-14 and not
objected to; the converter's handoff asks to confirm each one for a new project): US numbers only (+1 assumed); the
five approved phone repairs (imported and marked); fax numbers never used; consent and last_contacted mapped by a
person only, per file; year 1600 only for birthday and anniversary; no templates for files without a header row;
the "approve guessing features one by one" rule. (Logins off was carried over too, until Noah asked for logins on the
shared server, 2026-10-15.)

**Made while building 1.0.0, to confirm** (each is easy to change): when a number is in several records of a tag,
its export takes the latest date of each activity and the birthday/anniversary with a real year; the creation date
stands in for the last transaction only for imports with no last-activity column (as the converter); columns ticked
Skip are not kept; a completed import's records must be deleted before it is re-run; business IDs are text; services
are one list for all businesses; tags hold records, not saved searches; Find's differences from FileMaker (`@` and
`#` ordinary characters, month/day without a year matches any year, `!`/`?`/`~` not supported); the app's name,
Customer Records (the repository is `data-warehouse`), and version 1.0.0.

## 3. How work was planned

- 1.0.0 was planned in one conversation (2026-10-14): Noah described the tool; four questions were asked (database,
  consent in exports, rows without numbers, tags per business) and answered; everything else was built on the
  plan in section 2, as a prototype ("there will be plenty of changes later").
- Then Noah tried it and asked for changes, one at a time, each its own version: port 8502 (1.0.1), the mapping
  tables jumping to the top (1.0.2), finds with nothing required, business/service pickers, open-ended ranges and
  Record ID / created (1.1.0); the Find page laid out by what is searched most, Last visit as a date only and names
  given whole in First name (1.2.0); one Date for the last visit and last call, days of the week (1.3.0). The Add a
  field menu (form buttons in a popover, so typing is kept and Enter stays Find) and the name rule were chosen while
  building 1.2.0; the Date as the later of the two and the day-of-week syntax while building 1.3.0. All are easy to
  change.

## 4. Open items

- **Waiting on the main app's lead developer**:
  - For 1.7.0: the bucket settings for Customer Records (`BUCKET_ENDPOINT`: R2's S3 address for the account,
    `BUCKET_NAME`: `master-bucket`, and a key that may write to it); loading `data/customer-records/` and
    `data/customer-records-tags/` into ClickHouse (nothing picks files up from the bucket by itself since
    2026-10-15; BUCKET_FILES.md has tables and queries); a segment source reading each tag's latest snapshot; how
    deletions reach the audiences fold (it only adds).
  - An app user for Customer Records on the main app's ClickHouse (SELECT on `pg.merchants` / `pg.locations`), for
    the automatic merchant loads; with the bucket, no write access to ClickHouse is needed any more.
  - Found in the main app (2026-10-16/17), to tell him: credentials written in plain text where they can be read: the
    R2 key and secret in the `audiences.ndnc` dictionary's definition (visible to read-only ClickHouse users), the
    Kafka password in `workflows/f/sendsational/kafka.resource.yaml` (line 23), the R2 access key ID in
    `workflows/f/sendsational/master_bucket.resource.yaml` (line 3), and a staging test user's password in
    `frontend/.claude/skills/verify/SKILL.md` (line 35); the ClickHouse service accepts connections from any address
    (`0.0.0.0/0`). Reported earlier: 183 segment memberships with numbers that break US numbering rules, and that
    the console's read-only role may kill queries and write to outside systems.
  - Known: the service is `sstxt` (AWS us-east-1, ClickHouse 26.6), host `m7qi5e0gq5.us-east-1.aws.clickhouse.cloud`
    (`CLICKHOUSE_URL` is the host name alone). Location addresses there are JSON (`{"zip", "city", "state",
    "street"}`, city sometimes null), shown on one line since 1.6.1.
- **The main app (sendsational), read 2026-10-17** (a copy of its repository, master `c4beb0dd0`): a Next.js 16 /
  React 19 dashboard (shadcn, Prisma, better-auth) on PostgreSQL, which reaches ClickHouse only through PostgreSQL
  functions (pg_clickhouse); a Rust API (actix); Windmill jobs in TypeScript; no Python. Its import takes a file in
  one of a fixed list of presets (exact column names, no mapping, no location, no delete): the dashboard puts it on
  R2 and `imports.ingest` loads it into one ClickHouse table per preset, which feeds `audiences.*`. Segments are
  built from all the merchant's numbers or a preset's, filtered by recent activity, and gated as in section 2.
  Only `master` deploys (the dashboard, the API and Windmill, which applies every push to master); database changes
  are applied by hand; nothing is tested automatically there. Changing a preset or a segment source touches about a
  dozen files (Prisma migrations, `frontend/lib/importer/presets.ts` and `landing.ts`, `lib/actions/segments.ts`, the
  segment canvas, a ClickHouse table file).
- **Questions for Noah**:
  - Should Customer Records' finds also cover the main app's other data (the old FileMaker exports, Toast, the
    customer list), and which fields of those count (only some have names, emails or addresses)?
  - VoIP numbers: the main app treats them as not mobile (left out of segments); keep it so?
  - Section 2's carried-over and to-confirm lists.
- **Independent test of 1.0.0** (check 6, a separate Claude agent working from the README and user guide): it
  found nine defects, all fixed before release and covered by `tests/test_records_find_edges.py` and
  `test_records_app.py`: a prepared CSV kept after Limit to changed; accented names not found in any case (database
  locale); Business `1001` also finding "1001 Nights Cafe"; times in date criteria ignored; `//` on the server's
  clock; `+1 516*` finding nothing; no-year birthdays in "born before" finds; ’ vs '; backward ranges finding
  nothing silently. Smaller points it raised are documented (KNOWN_ISSUES #23–25, punctuation in the README).
- **The same jump is in the file converter.** Its New import page has the same two mapping tables, which jump back
  to their first row after every edit (reported here on 1.0.1, fixed in 1.0.2). The fix is `height="content"` on
  both `st.data_editor` calls in `app.py` (they get a new key on every edit, so a table scrolled inside itself
  loses its position); `tests/test_mapping_tables.py` checks it. Not yet done in file-converter.
- **Independent test of 1.1.0** (check 6, a separate Claude agent, from the README, user guide and the syntax): it
  found eight defects, all fixed before release and covered by `tests/test_records_find_edges.py` (section "found by
  the independent test of 1.1.0"), `test_records_find.py` and `test_records_app.py`: typing a business ID and Enter
  picked every fuzzy match ("1001" also took "1001 Nights Cafe"); clicking the chosen Omit again turned the request
  into a Find; `…` and `..` read as something else without a word; a typo in Constrain/Extend replaced the found set
  for good; years after 9998 and huge numbers gave the error page; a More criteria row without a Field said
  "'nan'"; ranges within one day or month called backwards; Phone problem ranges found records without a problem.
  Also from it: the How to search panel now has its own text for people (`criteria.SYNTAX`), text ranges take in
  everything starting with their upper end (`a...m` includes Miller), blank Constrain/Extend change nothing.
  It also confirmed 300 random multi-request finds against FileMaker's rules.
- **Independent test of 1.2.0** (check 6, a separate Claude agent): it found four defects, all fixed before release.
  The first design made the last visit a `date` field, which changed the converter's behaviour for it: headers with
  words like Rewards or Points ("Rewards Last Visit") were refused, so another column became the last visit, and
  the review's future-date, old-date and time-zone checks no longer ran. 1.2.0 keeps it a `datetime` field whose
  format has no time (`db.date_only`), so the converter sees exactly what it saw before
  (`test_the_last_visit_is_mapped_and_checked_as_the_converter_does`). First name had stopped finding the name as
  stored ("Mary Ann" by `mary ann`): it now matches the stored value or the split part, and a comma splits "Smith,
  John" (`test_records_find_edges.py`). The Add a field menu stayed open from the second add on, covering the new
  box: it now closes after every add (checked in a browser). Smaller points are documented (KNOWN_ISSUES #2, #22,
  #27–30).
- **Independent test of 1.3.0** (check 6, a separate Claude agent, twice): it found that no find used any index
  (every criterion was wrapped in `COALESCE`, since 1.0.0; date columns were compared with timestamps and whole-number
  columns with decimals, which casts the column), that a day of the week after a word and the dots (`oct...mon`) or
  after `<` / `>` with a date (`>fri 10/1/2026`) was read instead of refused, and four doc inaccuracies. All fixed
  before release: text, number and date criteria, the Date, Any field and First / Last name are now NULL-safe with
  `value IS NOT NULL` and use their indexes (`test_records_date.py`, `test_records_star_equals.py`); its battery of
  9,530 finds gave the same records as 1.2.1 on an ICU and a C-locale database. Fields still read in full are listed in
  KNOWN_ISSUES #14.
- **Independent test of 1.4.0** (check 6, a separate Claude agent): it found nine defects, all fixed before release
  and covered by `tests/test_records_gender.py` and `test_records_app.py`: the converter's fuzzy header matching
  took "Tender", "Sender", "General" and "Child Gender" for Gender (fixed with `header_words` / `header_not_words`,
  a new field option of the converter's rules); records from before 1.4.0 ignored their files' Gender column on the
  upgrade (`gender.read_stored_files`); "N / A", "Not known", "1.0" read as other answers and "F - Female", "♀",
  "Féminin" not read as female; a spreadsheet error in the cell blocked the guess; Customer Sex / Member Sex
  headers weren't found; letters outside a short accent list were deleted ("Šárka" → "arka" → Mb), and capital
  accents weren't folded in C-locale databases; placeholder names (Unknown, Baby) blocked the email; the Date and
  Record created boxes lost their help text. Also from it: no guess for two people in First name, Mx gives none,
  more titles (Frau, Herr, Señora…), "Hans-Peter" looked up as Hans. Its second run found that the fixes made the
  guess 3–5 times slower (PostgreSQL pulled the folding into every join: now computed once per record in
  materialized steps; ASCII names skip the accent map), that "Jean-François" became Fb (now: no guess when the
  hyphenated parts disagree), that the upgrade read cells without the converter's cleaning and could pick the other
  of two gender columns (both fixed), and smaller value, title and header cases (Sr., U - Unknown, M/F, Masc.,
  Género, Sexe, Stylist Gender): all fixed and tested. Its third run found that answers naming both sexes in other
  words ("Male to Female", "Intersex (male/female)") were taken for unfilled forms and guessed over (now only
  "M/F"-style cells are), and "Jean-Noël" → Fb (a hyphenated name now needs both parts to agree): fixed and tested.
- **Independent test of 1.5.0** (check 6, a separate Claude agent, on the runner, the server setup and logins): the
  runner held up (14 mixed files at once gave the same records as one at a time; cancels and kill -9 at every stage,
  and a killed server, left nothing behind). It found ten points; fixed before release and tested
  (`tests/test_records_runner.py`, `test_records_app.py`, `test_server_setup.py`): stopping the server ran every
  queued import before exiting, with the pages already gone (now running and queued imports end *interrupted*, within
  seconds); an import left interrupted with its records stored, which could be neither re-run nor deleted (a new
  start removes them); a worker that couldn't reach PostgreSQL converted the whole file and then reported a pool
  timeout (now PostgreSQL's reason, at once); a relative `DATA_DIR` broke every import when the server was started
  from another folder; a deleted account kept working in a tab already signed in; Businesses and Services didn't show
  who added them; `docker-compose.yml` published the port on every address (Docker bypasses `ufw`: plain HTTP to the
  sign-in page) and `.env.example` allowed an empty admin password without saying what that means on a server; the
  converter's OPERATIONS.md (linked for Caddy) contradicts the README on size and logins (the README now says it
  wins). Not fixed, low: a job store locked for over a minute leaves an import queued until a restart;
  `IMPORT_WORKERS=` (empty) stops the app (the converter's settings parse it) and a wrong setting shows only
  "ValueError" on the page; signing in lasts as long as the browser tab and a link opens the first page after
  signing in (KNOWN_ISSUES #21).
- **Independent test of 1.6.0** (check 6, a separate Claude agent, limited at Noah's request to merchants and
  locations, imports and finds by merchant or location; about 10 minutes): it found one serious defect, fixed and
  tested before release (`test_a_location_that_leaves_the_main_app_is_never_swapped_for_another`): a location
  chosen on New import that then left the main app's list was replaced by the merchant's only remaining location, so
  the file went to the wrong location without a word. Also fixed and tested (`test_records_merchants.py`,
  `test_records_store.py`): an unexpected ClickHouse answer (rows that aren't rows, an error after the first rows)
  wasn't said on the page; a spreadsheet's empty trailing row, UTF-16, Excel's `sep=` line and `63.00` were
  refused; "None"/"Null" were read as empty; location IDs weren't checked as UUIDs and conflicting duplicates
  weren't refused; error rows weren't the spreadsheet's row numbers; returning merchants and locations were counted
  as new; statuses other than active/archived were importable; the reset script emptied the database even with a
  wrong `DATA_DIR`; and two doc errors (hand-typed IDs equal to main app IDs are taken over, not marked removed;
  imports from before 1.6.0 can be re-run when their merchant has one location). Not covered in its time box:
  draft restore with a removed location (same code path as the fix), Constrain/Extend with locations in a browser.
- **Independent test of 1.6.1** (limited: the address change and reading the merchant file; about 15 minutes, with
  the main app's real list of 2026-10-16, which loads with no JSON left in any address): no serious defect. Fixed
  and tested (`test_unusual_json_addresses`, `test_a_file_as_a_spreadsheet_saves_it`,
  `test_a_wrong_file_says_what_to_do`): a ZIP given as a number lost its leading zero; true/false, decimals and
  coordinates were written into the address; line breaks stayed in it; escapes the database can't store (`\u0000`,
  `\ud800`) and absurdly nested JSON broke the load; and, from before 1.6.1, a file with Mac line endings (a lone
  `\r`) or a 130,000-character cell showed a traceback instead of a message. Not fixed (none occur in the real data):
  a street given as a list is left out; a file saved in Mac Roman is read as Windows encoding; ClickHouse's own TSV
  export escapes an apostrophe as `\'`, which leaves such an address as raw JSON (the page says to export CSV).
- **Independent test of 1.7.0** (limited: sending to the bucket, about 25 minutes, with a stand-in S3 server, the
  real merchant list and a 100,000-row file): no serious defect. Fixed and tested before release
  (`test_records_bucket.py`): a deletion could go while its import's send was still being tried, against the
  documented order (now held until that send is settled; when a failed or cut-off try may have landed, the deletion
  still goes, after it); a send cut off by a restart waited 30 minutes with *Try again now* doing nothing (now taken
  over at the sender's first run); a deleted tag's last file could carry a name from before a rename; a merchant ID
  of non-ASCII digits was accepted for sending; BUCKET_FILES.md promised every cell in `source` (empty cells were
  never kept; the doc now says so). Confirmed: the files match BUCKET_FILES.md (odd cells, a record without a number,
  several numbers, 1600 birthdays, UTC times), 100,000 rows sent in about 10 s, retry waits, settings added later,
  the secret never in pages, the database or logs, the reset script with sends present.
- Limits known and accepted: [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## 5. Taking fixes from the file converter

The converter keeps being updated (github.com/noahlevitt/file-converter). This repository has its own history, with
no link to the converter's: it starts with the converter's files as of file-converter commit `57b8c9c` (version
0.21.2 and its regression check), added unchanged, one commit per file, each message ending
`(file-converter 0.21.2)`. Every commit after them is Customer Records' own. Converter fixes are therefore **ported
as patches**, not merged or cherry-picked:

1. In a clone of file-converter, write the changes since the converter commit last ported (`57b8c9c` for 1.0.0;
   update this line after every port) up to the new version, in three patches (the converter's documents live in
   `docs/converter/` here):

   ```
   cd ~/Downloads/file-converter
   git diff 57b8c9c vNEW -- . ':(exclude)README.md' ':(exclude)docs' > /tmp/converter-code.patch
   git diff 57b8c9c vNEW -- README.md > /tmp/converter-readme.patch
   git diff --relative=docs 57b8c9c vNEW -- docs > /tmp/converter-docs.patch
   ```
2. Apply them here, with three-way merging (the converter's original files are in this history, so it works even
   for files Customer Records changed):

   ```
   git apply --3way /tmp/converter-code.patch
   git apply --3way --directory=docs/converter /tmp/converter-readme.patch /tmp/converter-docs.patch
   ```
   Conflicts are marked in the files as in a merge. Expect them where Customer Records changed the converter's
   files: `app.py`, `requirements.txt`, `run_local.*`, `Dockerfile`, `docker-compose.yml`, `.env.example`,
   `.streamlit/config.toml` (keep port 8502 everywhere), `converter/jobs.py`, `schema.py`, `heuristics.py`
   (ARCHITECTURE.md → Additions to the converter), `tests/conftest.py`, `tests/test_drafts.py`,
   `scripts/regression/regression.py` (its example), `docs/converter/README.md` (the note at the top).
   The exact changes: `git diff $(git log -1 --format=%h --grep='(file-converter 0.21.2)') HEAD -- converter`.
3. Commit as always (one commit per file, e.g. "Port the area code fix from file-converter 0.22.0"), then the
   regression check (the differences are the fix's), the parity check and the whole suite (section 1). Note the
   converter version in CHANGELOG.md and in the overviews that name 0.21.2 (README, ARCHITECTURE,
   `records/__init__.py`, `app.py`).

## 6. Where things live

| What | Where |
|---|---|
| The record fields | `schemas/records_schema.yaml` |
| The export format (the converter's import format) | `schemas/target_schema.yaml` |
| Settings | `.env` (from `.env.example`); README → Settings |
| Records, businesses, services, tags | PostgreSQL (`DATABASE_URL`, default `customer_records` on localhost) |
| The app's own data (import jobs, templates, each import's files) | `data/` — never in git |
| Find syntax (the reference) | `records/criteria.py` (module docstring, also shown on the Find page) |
| Regression and parity checks | `scripts/regression/` |
| The converter's documents (0.21.2) | `docs/converter/` |
| The converter's files as Customer Records started from them | the first commits, messages ending `(file-converter 0.21.2)` |
| Python | 3.11 or newer; the Docker image uses 3.12 |
