# Handoff

For whoever picks Customer Records up next: a person, or Claude in a new chat. It records what the code and the
other documents don't: how changes are made and delivered, the decisions the business made, and what is still open.

**State:** version 1.0.2, the first prototype (see [CHANGELOG.md](CHANGELOG.md)), based on file-converter 0.21.2.
About 1,100 tests. Repository: github.com/noahlevitt/data-warehouse, with its own git history: it started from the
converter's files, not from its history (section 5).
Owner: Noah Levitt. Last updated 2026-10-14. Update this file with every release (section 4 in particular).

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
1. The whole test suite: `.venv/bin/python -m pytest -q` (about 1,100 tests, about 1½ minutes). PostgreSQL must be
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
  came as `data-warehouse-1.0.0.bundle` (complete, no prerequisites); 1.0.1 came as `data-warehouse-update1.bundle`
  and 1.0.2 as `data-warehouse-update2.bundle`, so the next is `data-warehouse-update3.bundle`. Update this line
  with each bundle.

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
| Businesses and services | Every import is for one business and one service. Businesses are created by hand with the **same ID as in the main system** (the systems will be connected later). Services are a predetermined list. |
| Database | PostgreSQL (the main system uses PostgreSQL and DuckDB; ClickHouse is being considered for everything later). |
| Rows without a usable number | Stored as records (searchable), with the reason; never exported. |
| Tags | One business per tag. Deduplicated (one row per number) when exported. |
| Export format | The file converter's import format, until the systems are connected. |
| Consent in exports | Not sent (blank): opt-outs are per business and the main system handles them; once connected, it will know who is opted in or out. Records keep the file's consent. |
| Before tagging (later) | Deduplicate, check that numbers are cell phones, and remove numbers on the Do Not Call list unless within an 18-month established business relationship. Depends on the main system; not built. |
| Platform | A web app or something launched on the computer, Mac first, Windows too. Built as the converter: a local web app (`./run_local.sh`), Docker for a server. |
| Repository | A brand-new project and repository, github.com/noahlevitt/data-warehouse, with its own commit history (not file-converter's). |
| Port | 8502. The file converter keeps running on 8501 on the same computer, so the two must never share a port (1.0.1). |

**Carried over from the converter, not yet confirmed for this project** (stated in the plan on 2026-10-14 and not
objected to; the converter's handoff asks to confirm each one for a new project): US numbers only (+1 assumed); the
five approved phone repairs (imported and marked); fax numbers never used; consent and last_contacted mapped by a
person only, per file; year 1600 only for birthday and anniversary; no templates for files without a header row;
logins off; the "approve guessing features one by one" rule.

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

## 4. Open items

- **Questions for Noah**:
  - The main system: its business IDs (numbers or text?), how opt-outs, last messages and transactions are stored
    (needed for the Do Not Call / 18-month rule), and whether it should read tags directly instead of files.
  - The Do Not Call list: which list (national, state, internal), where it comes from, how often it changes.
  - The 18-month established business relationship: counted from which date (last transaction, last visit, last
    inquiry?) and per business or across businesses.
  - Cell-phone check: which provider (e.g. Twilio Lookup, paid per number), and whether landlines are removed or
    only flagged.
  - Should a tag export also have the business ID or the service in the file, or one file per business is right?
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
