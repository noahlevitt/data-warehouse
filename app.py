"""Customer Records — internal web app (the Streamlit entry point). Built on the file converter (0.21.2).

Pages: New import · Imports (tracker, progress, results, side-by-side comparison) · Find · Tags · Businesses ·
Services · Templates · Fields · Users (admins only, when logins are on)

Run locally:   ./run_local.sh   (needs PostgreSQL; see README.md → Database)
Run on server: docker compose up -d   (see README.md)

What it does
------------
A user picks the business and service, uploads a customer/contact file (CSV, TSV, TAB, TXT, XLSX, XLSM, XLSB,
XLS, ODS) and reviews how its columns map to the record fields of schemas/records_schema.yaml. The import is
converted exactly as the file converter does (10-digit US phone numbers, dates in any format, 1600 for birthdays
without a year, consent only when a person maps it) and every file row is stored as a record in PostgreSQL
(records.importer). Records are found with FileMaker-style requests (Find), collected in tags (one business per
tag), and a tag exports its unique phone numbers in the converter's import format (schemas/target_schema.yaml).
The conversion rules live in the `converter` package, the records in the `records` package; this file is the web
UI on top of both, and the new pages are drawn by records/ui.py. Claude is optional and only called when someone
clicks a Claude button (or when AI_AUTO_MAP is on).

How this file is organised (top to bottom)
------------------------------------------
1. Imports, settings and small constants (US time zones, column labels of the review grids).
2. Shared resources, built once per server process with st.cache_resource: the SQLite Store, the background
   import runner (records.runner, one worker process per running import), the target schema (reloaded when the YAML file changes) and the optional Claude
   assistant. Then module-level setup: load the schema, sign the user in and seed session-state counters.
3. New import: state helpers (load_and_map, start_import, rerun_import, data-editor callbacks), the page itself
   (page_new_import) and its "Columns in your file" / notes / mistake-check panel (_notes_and_checks).
4. Imports: the list page with live progress (page_imports / _import_list) and the page for one import
   (page_import_detail → _live_progress while running, _completed_view when done, with the side-by-side view,
   the mistake check and Claude value fixes).
5. The Templates, Fields and Users pages (Number history is the converter's and not shown: records replace it).
6. Navigation: st.navigation picks the page from the URL, the sidebar is drawn, and nav.run() runs the page.
   Find, Tags, Businesses and Services are records/ui.py functions, given a records.ui.Ctx.

How a request flows
-------------------
Streamlit runs this whole script from the top on every click or widget change ("a rerun"). On each run the
cached resources are reused, require_login() either returns the signed-in user or draws the login form and stops
the run, the pages are registered, and nav.run() calls the one page function for the current URL. Anything that
must survive between runs lives in st.session_state (aliased `ss` below): the uploaded file and its read result,
the mapping being reviewed, Claude results, and the "version" counters described next.

New import: business + service → upload → load_and_map() reads the file, profiles the columns and suggests a
mapping (saved template, then rules, then optionally Claude) → the reviewer edits it in two data editors (the field
grid, and "Columns in your file"), optionally applies notes with Claude or runs a mistake check → start_import()
saves the template, registers the import in PostgreSQL (business, service, file), creates the job record plus a
stored copy of the file, and hands the id to the runner → the browser is sent to that import's page. The
conversion itself runs in a worker process (records.runner runs converter.jobs.run_import there, with
records.importer.RecordSink storing the records), which writes progress to data/converter.db; the Imports pages poll it with st.fragment(run_every=...)
until it finishes.

Two databases: data/converter.db (SQLite, as in the converter) holds the app's own state — import jobs and their
progress, templates, drafts, users, display preferences. PostgreSQL holds the business data — businesses,
services, imports (linked to their job by job_id), records, tags. An import's job keeps the PostgreSQL import id in
its read options ("records_import_id").

Widget-key versioning (why some keys end in a number)
----------------------------------------------------
st.data_editor keeps the user's edits (its "edited_rows") in session state under its key. After we change
ss.mapping ourselves (in a callback, after accepting Claude's changes, after re-reading the file) the grid is
rebuilt from the new mapping, and old edits replayed on top of it would be wrong. So we bump ss.editor_version
(and ss.notes_version / ss.uploader_version) to give the widget a new key, which gives it a clean slate. Keys of
per-file widgets (time zone, template name, general note) include a short hash of the uploaded file, because a
keyed widget ignores its `value=` once it has state; a new file therefore starts with its own defaults instead
of whatever was typed for the previous file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from converter import __version__ as ENGINE_VERSION
from converter.ai import AIError
from converter.auth import logout, require_login
from converter.compare import (FILTERS, build_compare_html, column_plan, comparison_xlsx, field_change_summary,
                               filter_rows, status_counts)
from converter.config import get_settings
from converter.checks import column_warnings
from converter.editor import (FROM_LABEL, LABELS, OPTION_HELP, apply_column_edit, apply_row_edit, option_text,
                              row_problem)
from converter.engine import convert, count_multi_number_cells, preview_values, source_rows
from converter.mapping import Mapping, area_code_pairs, has_generic_columns, profile_columns
from converter.numberlog import lookup as number_lookup
from converter.normalizers import sheet_error
from converter.output import neutralise_csv
from converter.reader import SUPPORTED_EXTENSIONS, ReadError, read_file
from converter.schema import TargetSchema, load_schema
from converter.services import (check_for_mistakes, initial_mapping, make_assistant, merge,
                                validate_repairs)
from converter.storage import ImportRecord, Store
from converter.ui_format import fmt_dt, fmt_duration, fmt_eta, fmt_size, status_color, status_text
from records import __version__
from records import ui as records_ui
from records.config import get_records_settings
from records.db import Database, DatabaseError, ensure_database
from records.importer import RecordSink
from records.runner import ProcessImportRunner
from records.store import RecordStore

log = logging.getLogger("app")

# Browser tab title and the heading of the login form; override with APP_TITLE in .env.
APP_TITLE = os.getenv("APP_TITLE", "Customer Records")
# All server settings (paths, worker count, retention, Claude options…) come from environment variables / .env.
settings = get_settings()
# Customer Records' own: DATABASE_URL, the record fields file and the export format file (records/config.py).
rsettings = get_records_settings()
# Zone used to *display* timestamps in the app (uploaded, started, last sign-in…). Not the same thing as the
# per-import "client time zone" the reviewer picks, which controls how times inside the file are converted.
TZ = settings.timezone

# Must be the first Streamlit call that draws anything (Streamlit raises otherwise), so it runs before the
# login form and before any page.
st.set_page_config(page_title=APP_TITLE, page_icon="🗂️", layout="wide")


# ---------------------------------------------------------------------------
# Light / dark mode
# ---------------------------------------------------------------------------
# .streamlit/config.toml defines a light and a dark theme ([theme.light] / [theme.dark]), so Streamlit's own
# "Custom Theme Auto" follows the browser's light/dark setting. Streamlit has no per-user API to switch theme, and a
# page reload would end the session (signing the user out and losing a mapping in progress). So the toggle makes
# this browser tab *report* the chosen mode: the script below wraps window.matchMedia so
# "(prefers-color-scheme: dark)" answers with the user's choice, then fires the "afterprint" event, on which
# Streamlit re-evaluates its automatic theme. The switch is instant, keeps the session, and uses Streamlit's real
# dark theme for every element (including the data grids). The choice is saved per user (users' preferences
# table) and in the browser's localStorage ("fcTheme"), so it also applies on the login page and after a refresh.
# If a future Streamlit version stops re-evaluating on "afterprint", the toggle simply has no effect; nothing breaks.
_THEME_SCRIPT = r"""<script>
(function () {
  var W = window, want = "__WANT__";
  try {
    if (want) { W.localStorage.setItem("fcTheme", want); } else { want = W.localStorage.getItem("fcTheme") || ""; }
  } catch (e) {}
  if (want !== "light" && want !== "dark") { return; }
  if (!W.__fcOrigMatchMedia) { W.__fcOrigMatchMedia = W.matchMedia.bind(W); }
  W.__fcTheme = want;
  W.matchMedia = function (query) {
    var real = W.__fcOrigMatchMedia(query), key = String(query).replace(/\s/g, "");
    if (key !== "(prefers-color-scheme:dark)" && key !== "(prefers-color-scheme:light)") { return real; }
    var isDark = W.__fcTheme === "dark";
    return {matches: key.indexOf("dark") >= 0 ? isDark : !isDark, media: query, onchange: null,
            addEventListener: real.addEventListener.bind(real),
            removeEventListener: real.removeEventListener.bind(real),
            addListener: real.addListener.bind(real), removeListener: real.removeListener.bind(real),
            dispatchEvent: real.dispatchEvent.bind(real)};
  };
  W.dispatchEvent(new Event("afterprint"));
})();
</script>"""
# The script's container is taken out of the page flow so it adds no blank space; the toggle is a small square
# button fixed at the top right of the page, beside Streamlit's own toolbar.
_THEME_CSS = """<style>
[class*="st-key-fc_theme_assets"]{position:fixed;width:0;height:0;overflow:hidden}
.st-key-fc_theme_toggle{position:fixed;top:0.6rem;right:1rem;z-index:999990;width:auto}
.st-key-fc_theme_toggle button{width:2.25rem;height:2.25rem;min-height:2.25rem;padding:0;border-radius:0.5rem}
.st-key-fc_theme_toggle button span[role="img"]{font-size:1.25rem}
</style>"""


def apply_theme(want: str | None, slot: str = "app") -> None:
    """Make this browser tab show light or dark mode: "light", "dark", or ""/None = the choice saved in the browser.

    Draws an invisible element holding _THEME_SCRIPT (see the comment above it). Streamlit re-runs the script only
    when the element's content changes, i.e. when the choice changes or the element is drawn anew (page change),
    so calling this on every rerun is cheap. `slot` keeps the two calls of one run (before sign-in and with the
    toggle) apart, because Streamlit needs a unique key per element.
    """
    with st.container(key=f"fc_theme_assets_{slot}"):
        st.html(_THEME_CSS + _THEME_SCRIPT.replace("__WANT__", want if want in ("light", "dark") else ""),
                unsafe_allow_javascript=True)


def current_theme() -> str:
    """"dark" or "light": the signed-in user's choice, else what the browser currently shows (its own setting)."""
    pref = st.session_state.get("theme_pref")
    if pref in ("light", "dark"):
        return pref
    return "dark" if st.context.theme.type == "dark" else "light"


def _toggle_theme() -> None:
    """Button callback: switch to the other mode and remember it for this user (a failed save never blocks)."""
    new = "light" if current_theme() == "dark" else "dark"
    st.session_state.theme_pref = new
    try:
        store.set_user_pref(user["username"], "theme", new)
    except Exception:  # the preference is a convenience; the switch still works for this session
        pass


def theme_toggle() -> None:
    """Draw the light/dark toggle: a small square with a moon (go dark) or a sun (go light), top right.

    The user's saved choice is loaded once per session; then the current choice is applied (apply_theme).
    """
    ss_ = st.session_state
    if "theme_pref" not in ss_:
        ss_.theme_pref = store.get_user_pref(user["username"], "theme")
    dark = current_theme() == "dark"
    with st.container(key="fc_theme_toggle"):
        st.button(":material/light_mode:" if dark else ":material/dark_mode:", on_click=_toggle_theme,
                  key="fc_theme_button", help="Switch to light mode" if dark else "Switch to dark mode")
    apply_theme(ss_.theme_pref)


# Choices for the "Client time zone" box: label shown to the user → IANA name stored in ss.timezone and in each
# import's read_options. US zones only, because the target system only takes US customers.
US_ZONES = {"Eastern": "America/New_York", "Central": "America/Chicago", "Mountain": "America/Denver",
            "Arizona": "America/Phoenix", "Pacific": "America/Los_Angeles", "Alaska": "America/Anchorage",
            "Hawaii": "Pacific/Honolulu"}
# Reverse lookup (IANA name → label). Also used as "is this one of our zones?" when a template or the server
# setting supplies a zone.
ZONE_LABEL = {v: k for k, v in US_ZONES.items()}
# Column headers of the mapping-review grid on New import (one row per target field). They are passed to
# converter.editor.apply_row_edit so it knows which edited cell means what; change them here, not inline.
COL_FIELD, COL_TYPE, COL_HOW, COL_SRC, COL_OPT = "Field", "Type", "How", "Source column(s)", "Option"
COL_PREVIEW, COL_CHECK, COL_BY, COL_WHY = "Sample output", "Check", "Mapped by", "Why"


# ---------------------------------------------------------------------------
# Shared resources (one per server process)
# ---------------------------------------------------------------------------

# Everything in this section is created once and shared by every user and browser tab. st.cache_resource returns
# the same object on every rerun instead of rebuilding it, which matters here: a second import runner would start a
# second set of import slots (and mark running imports as interrupted again), and a second Store would re-run its setup.

@st.cache_resource
def get_store() -> Store:
    """Return the one SQLite Store (users, templates, imports, preferences) shared by all sessions.

    The database file is settings.db_path (converter.db in the data folder); import files are stored next to it
    under imports/<id>/. Sharing one object is safe because the Store opens a short-lived connection per call.
    """
    return Store(settings.db_path)


@st.cache_resource
def get_records_db() -> tuple[Database, list[str]]:
    """Return the one PostgreSQL connection pool (records.db.Database) and the warnings of its setup.

    On the first call the database is created if it doesn't exist (when the server allows it) and its tables and
    record-field columns are created or extended from the record fields file (Database.setup). Adding a field to
    schemas/records_schema.yaml therefore needs a restart of the app to get its column. Raises DatabaseError (with
    what to do) when PostgreSQL can't be reached; the module-level setup shows it on the page.
    """
    ensure_database(rsettings.database_url)
    db = Database(rsettings.database_url, max_size=rsettings.database_pool_size)
    return db, db.setup(load_schema(rsettings.records_schema_path))


@st.cache_resource
def get_runner() -> ProcessImportRunner:
    """Return the one background import runner for this server process.

    Creating it has side effects, which is why it must only happen once: imports that were queued or running when
    the server last stopped are marked "interrupted" (they can't resume) and stored files older than
    FILE_RETENTION_DAYS are purged. IMPORT_WORKERS (default 5) sets how many imports convert at the same time, each
    in its own worker process (records.runner); the rest wait in the queue. Each worker process stores its import's
    rows as records in PostgreSQL (records.importer.RecordSink), instead of the converter's number history (not used
    here: the records themselves are the history). The sink given here only cleans up after a worker process that
    ended without a result.
    """
    db, _ = get_records_db()
    return ProcessImportRunner(get_store(), rsettings.database_url, workers=rsettings.import_workers,
                               retention_days=settings.file_retention_days, record_days=settings.number_record_days,
                               sink=RecordSink(RecordStore(db), get_store()))


@st.cache_resource
def _schema_cache(path: str, mtime: float) -> TargetSchema:
    """Load and validate the schema YAML at `path` (the record fields, or the export format).

    `mtime` is not used in the body: it is only part of the cache key, so saving the YAML file (new modification
    time) produces a cache miss and the new format is loaded without restarting the server.
    """
    return load_schema(path)


def get_schema() -> TargetSchema:
    """Return the record fields (schemas/records_schema.yaml), reloading them when the YAML file has changed.

    This is what files are mapped to on New import and what the import converts to. A new field also needs a
    restart for its database column (get_records_db). Raises whatever load_schema raises for a missing or invalid
    file; the module-level setup below turns that into a readable error on the page.
    """
    path = rsettings.records_schema_path
    return _schema_cache(str(path), path.stat().st_mtime)  # reloads automatically when the YAML changes


def get_export_schema() -> TargetSchema:
    """Return the import format tags export into (EXPORT_SCHEMA_PATH, default schemas/target_schema.yaml)."""
    path = rsettings.export_schema_path
    return _schema_cache(str(path), path.stat().st_mtime)


@st.cache_resource
def get_assistant():
    """Return the shared Claude assistant, or None when Claude isn't configured (no ANTHROPIC_API_KEY).

    Every Claude button checks `assistant` for None and is disabled or hidden without it, so the app works fully
    without a key.
    """
    return make_assistant(settings)


@st.cache_data(max_entries=6, show_spinner=False)
def load_import_tables(import_id: int, folder: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load a finished import's stored tables for its detail page.

    Inputs: the import id and its folder (data/imports/<id>, passed as a str so it can be hashed for the cache).
    Returns (source, audit): `audit` is audit.parquet written by the background runner (one row per converted row,
    with _row, _src, _status, _reason and out./raw./st. columns per field); `source` is source.parquet, the file as
    read (all text), lined up so that row i of source is the original of audit row i. They only differ in length
    when "one row per phone number" made several output rows from one file row.

    Cached (last 6 imports) because the detail page reruns on every click and these files can be large. The files
    never change after an import completes, so caching by id is safe; `import_id` is only part of the cache key.
    st.cache_data hands each caller its own copy, so callers may modify the frames.
    """
    d = Path(folder)
    audit = pd.read_parquet(d / "audit.parquet")
    return source_rows(pd.read_parquet(d / "source.parquet"), audit), audit


@st.cache_data(max_entries=6, show_spinner=False)
def rejects_download(path: str, mtime: float) -> bytes:
    """The rejected-rows file as offered for download, with would-be spreadsheet formulas neutralised.

    Files written since 0.15.0 are already safe and come back byte for byte; older ones (kept until the retention
    purge) are fixed here, so no download can carry a live formula from a client's file (see
    output.neutralise_csv). `mtime` is only part of the cache key, so a re-written file is read again.
    """
    return neutralise_csv(Path(path).read_bytes())


# Module-level setup, run on every rerun (cheap: the resources above are cached).
store = get_store()
try:
    schema = get_schema()
    export_schema = get_export_schema()
except Exception as exc:  # bad YAML should be obvious, not a stack trace
    st.error(f"A schema file could not be loaded ({rsettings.records_schema_path} / {rsettings.export_schema_path}):"
             f" {exc}")
    st.stop()
try:
    rdb, db_warnings = get_records_db()
except (DatabaseError, ValueError) as exc:  # PostgreSQL not running, or a record field that can't be a column
    st.error(str(exc))
    st.stop()
records = RecordStore(rdb)
runner = get_runner()
assistant = get_assistant()
# Before sign-in there is no user preference yet: apply the mode last chosen in this browser (if any), so the login
# page already matches.
apply_theme(st.session_state.get("theme_pref"), slot="login")
# Draws the login form (or the first-run "create admin" form) and stops the run until someone is signed in.
# With AUTH_MODE=none it returns a local admin user straight away. `user` is a dict: username, is_admin.
user = require_login(store, settings, APP_TITLE)
if settings.auth_mode != "none":
    # The account as it is now (1.5.0): an account an admin deleted signs out of a tab already signed in, and a
    # changed admin flag applies on the next click instead of at the next sign-in.
    account = next((u for u in store.list_users() if u["username"] == user["username"]), None)
    if account is None:
        logout()
        st.rerun()
    user = st.session_state["user"] = {"username": account["username"], "is_admin": bool(account["is_admin"])}

ss = st.session_state
# Counters appended to widget keys; bumping one gives that widget a fresh key and therefore fresh state
# (see "Widget-key versioning" in the module docstring). setdefault keeps the current value on later reruns.
ss.setdefault("editor_version", 0)
ss.setdefault("uploader_version", 0)


# ---------------------------------------------------------------------------
# New import: state helpers
# ---------------------------------------------------------------------------

# Session-state keys that belong to the file currently on the New import page. They are all dropped together
# when a different file is uploaded, on "Start over" and after "Start import", so nothing from one file leaks
# into the next. Per-file *widget* keys (time zone, template name, general note) aren't listed: they include a
# hash of the file in their key instead, so a new file simply gets new widgets.
#   file_sig      (file name, SHA-1 of its bytes): tells a new upload apart from the same file on a rerun
#   file_name / file_bytes   the upload itself (also what gets stored with the import)
#   read          converter.reader.ReadResult (DataFrame of text cells, sheet, header row, encoding, warnings…)
#   read_error    message when the file couldn't be read (read is then None)
#   profiles      per-column profiles (sample values, type guesses) used by the mapper, checks and Claude
#   mapping       the Mapping being reviewed; map_meta says where it came from (template / rules / rules+ai)
#   edit_errors   messages from the last field-grid edit (e.g. an Option value that couldn't be parsed)
#   ai_used       True once Claude contributed to the mapping (stored on the import for the record)
#   template_name suggested template name; timezone  the client time zone (IANA name)
#   notes_result / check_result   Claude's notes result and the last mistake check, kept so Accept works later
#   pending       set to None by load_and_map but not read anywhere in this file (left over; harmless)
#   multi_cells   {phone source columns: number of cells holding several numbers} (see multi_number_cells)
#   read_args     how the file was read: {"sheet", "header_row"} as requested ("auto" = detected), for a restore
#   draft_id      the draft kept on disk for this file (see "Drafts" below); draft_saved  the state last saved
#   remember_pref the "Remember this mapping" box as restored from a draft (its starting value)
FILE_KEYS = ["file_sig", "file_name", "file_bytes", "read", "profiles", "mapping", "map_meta", "pending",
             "edit_errors", "ai_used", "template_name", "read_error", "timezone", "notes_result", "check_result",
             "multi_cells", "read_args", "draft_id", "draft_saved", "remember_pref"]


def reset_file_state() -> None:
    """Forget the file on the New import page by removing every key in FILE_KEYS from session state.

    Also deletes the file's draft (the copy kept for a browser refresh) and takes its id out of the URL: the file
    was imported, cleared with "Start over" or replaced by another upload, so there is nothing left to restore.
    Doesn't touch the uploader itself: callers that want the file uploader emptied too also bump
    ss.uploader_version, which gives the uploader a new key.
    """
    store.delete_draft(ss.get("draft_id"))
    for k in FILE_KEYS:
        ss.pop(k, None)
    if "draft" in st.query_params:
        del st.query_params["draft"]


# ---------------------------------------------------------------------------
# Drafts: a browser refresh doesn't lose a mapping in progress (since 0.15.0)
# ---------------------------------------------------------------------------
# A refresh starts a new Streamlit session, which has empty session state. So the file being mapped is also kept
# on disk as a *draft* (Store.create_draft: DATA_DIR/drafts/<random id>/), its id is put in the page's URL
# (?draft=<id>), and the state below is saved there whenever it changes. After a refresh the URL still carries the
# id, and the New import page restores the file, how it was read and the mapping. Only that browser tab knows the
# id, and a draft is only restored for the user who uploaded the file. Not kept: Claude's answers waiting for
# Accept / Discard (run them again). Drafts go away on "Start import", "Start over", another upload, or after 24
# hours without changes.

def draft_state() -> dict:
    """The New import state worth restoring after a refresh, as JSON-ready data.

    read_args (re-reading the stored file with them gives the same table), the mapping (with column notes, the
    general note and skipped columns), map_meta (the banner), the template name as typed, the client time zone,
    whether Claude was involved, and the "Remember this mapping" box.
    """
    return {"read_args": ss.get("read_args") or {"sheet": None, "header_row": "auto"},
            "mapping": ss.mapping.model_dump(mode="json"), "map_meta": ss.map_meta,
            "template_name": ss.get(template_key(), ss.get("template_name")), "timezone": ss.get("timezone"),
            "ai_used": bool(ss.get("ai_used")), "remember": ss.get("remember_mapping", True),
            "business": ss.get("imp_business"), "service": ss.get("imp_service")}


def save_draft() -> None:
    """Keep the file and its state on disk, and its draft id in the URL; called at the end of New import.

    The file is written once (when the draft is created); the state only when it changed since the last save.
    If the draft was deleted meanwhile (e.g. the same URL was imported in another tab), a new one is made. Disk
    errors are logged and otherwise ignored: a draft is a convenience and must never break the page.
    """
    if ss.get("read") is None:
        return
    state = draft_state()
    text = json.dumps(state, sort_keys=True)
    try:
        if not ss.get("draft_id"):
            ss.draft_id, ss.draft_saved = store.create_draft(user["username"], ss.file_name, ss.file_bytes), None
        if text != ss.get("draft_saved"):
            if not store.save_draft_state(ss.draft_id, state):
                ss.draft_id = store.create_draft(user["username"], ss.file_name, ss.file_bytes)
                store.save_draft_state(ss.draft_id, state)
            ss.draft_saved = text
    except OSError:
        log.exception("Could not save the New import draft")
        return
    if st.query_params.get("draft") != ss.draft_id:
        st.query_params["draft"] = ss.draft_id


def restore_draft(draft_id: str) -> None:
    """After a refresh: bring back the file and mapping of the draft in the URL (?draft=<id>).

    The file is re-read with the saved read_args and the saved mapping is applied (normalised against the
    current target format). If the draft has no saved state yet, or it no longer fits (e.g. the target format
    changed), the file is mapped afresh as if just uploaded. A draft that is gone (imported, cleared, older than
    24 hours) or belongs to another user isn't restored; the page says so once and the id leaves the URL.
    """
    draft = store.load_draft(draft_id, user["username"])
    if draft is None:
        del st.query_params["draft"]
        ss.draft_missing = True
        return
    ss.file_sig = (draft.filename, hashlib.sha1(draft.data).hexdigest())
    ss.file_name, ss.file_bytes, ss.draft_id = draft.filename, draft.data, draft.id
    ss.draft_restored = draft.filename
    saved = draft.state
    try:
        args = saved["read_args"]
        rr = read_file(ss.file_name, ss.file_bytes, sheet=args.get("sheet"), header_row=args.get("header_row", "auto"))
        mapping = Mapping.model_validate(saved["mapping"]).normalized(schema, list(rr.df.columns))
        map_meta = dict(saved["map_meta"])
    except Exception:  # no state saved yet, or it doesn't fit any more: start the mapping over for this file
        load_and_map()
        return
    ss.read, ss.read_error, ss.read_args = rr, None, args
    ss.profiles = profile_columns(rr.df, n_samples=settings.ai_sample_values)
    ss.mapping, ss.map_meta = mapping, map_meta
    ss.template_name = saved.get("template_name") or Path(ss.file_name).stem
    ss.timezone = saved.get("timezone") if saved.get("timezone") in ZONE_LABEL else "America/New_York"
    ss.ai_used = bool(saved.get("ai_used"))
    ss.remember_pref = bool(saved.get("remember", True))
    # Business and service chosen before the refresh (Customer Records), unless chosen again since.
    if saved.get("business") and not ss.get("imp_business"):
        ss.imp_business = saved["business"]
    if saved.get("service") and not ss.get("imp_service"):
        ss.imp_service = saved["service"]
    ss.multi_cells, ss.pending, ss.notes_result, ss.check_result, ss.edit_errors = {}, None, None, None, []
    ss.draft_saved = json.dumps(saved, sort_keys=True)
    ss.editor_version += 1


def load_and_map(sheet: str | None = None, header_row: int | None | str = "auto", use_ai: bool = True) -> None:
    """Read the uploaded file and build the first mapping suggestion for it.

    Inputs: `sheet` (Excel sheet name; None = the reader's choice), `header_row` ("auto" = detect, None = the file
    has no header row, int = 0-based row of the raw grid), `use_ai` (allow Claude to refine the mapping; it is
    only actually used when an assistant exists and AI_AUTO_MAP is on).

    Reads ss.file_name / ss.file_bytes. Writes ss.read and ss.read_error; on success also ss.read_args, ss.profiles,
    ss.mapping, ss.map_meta, ss.template_name, ss.timezone, ss.ai_used, and clears ss.multi_cells, ss.pending,
    ss.notes_result, ss.check_result and ss.edit_errors. Bumps ss.editor_version so both data editors start fresh. Any edits the
    reviewer made to the old mapping are discarded, which is what "Re-read file" should do.

    A ReadError (unsupported type, empty or unreadable file) is stored for the page to show rather than raised.
    """
    try:
        rr = read_file(ss.file_name, ss.file_bytes, sheet=sheet, header_row=header_row)
    except ReadError as exc:
        ss.read = None
        ss.read_error = str(exc)
        return
    ss.read_error = None
    ss.read = rr
    ss.read_args = {"sheet": sheet, "header_row": header_row}  # as requested, so a restored draft reads the same
    # The number of sample values per column is the same setting that limits what Claude is shown, so the
    # samples in the UI match what Claude would see.
    ss.profiles = profile_columns(rr.df, n_samples=settings.ai_sample_values)
    # Order inside initial_mapping: a saved template with exactly these headers wins outright (no Claude call);
    # otherwise rules, seeded from a similar template if there is one, then Claude only when AI_AUTO_MAP is on.
    with st.spinner("Matching columns" + (" (asking Claude)…" if use_ai and assistant and settings.ai_auto_map
                                          else "…")):
        sug = initial_mapping(schema, rr.df, ss.profiles, store, assistant, use_ai=use_ai and settings.ai_auto_map)
    ss.mapping = sug.mapping
    ss.map_meta = {"source": sug.source, "notes": sug.notes,
                   "template": sug.template.name if sug.template else None, "similarity": sug.similarity}
    # Suggest the template's own name only for an exact layout match, so saving updates that template. For a
    # merely similar layout, suggest the file name instead: saving must not overwrite the similar template.
    ss.template_name = sug.template.name if (sug.template and sug.similarity == 1.0) else Path(ss.file_name).stem
    # Client time zone: the one remembered by the (exact or similar) template, else the server's zone, else
    # Eastern. A zone we don't offer in US_ZONES is ignored so the select box always has a valid choice.
    tpl_tz = sug.template.read_options.get("timezone") if sug.template else None
    ss.timezone = tpl_tz if tpl_tz in ZONE_LABEL else (settings.timezone if settings.timezone in ZONE_LABEL
                                                       else "America/New_York")
    ss.ai_used = sug.source == "rules+ai"
    ss.multi_cells = {}  # a re-read (other sheet or header row) can change the cells behind the same column names
    ss.pending = None
    ss.notes_result = None
    ss.check_result = None
    ss.edit_errors = []
    ss.editor_version += 1


def multi_number_cells(df: pd.DataFrame, field, columns: list[str]) -> int:
    """Cells of these phone columns that hold several numbers, counted once per file and column set.

    Cached in ss.multi_cells (a FILE_KEYS entry, so a new file starts empty) because the mapping page reruns on
    every edit and the count reads the whole column. See engine.count_multi_number_cells.
    """
    cache = ss.setdefault("multi_cells", {})
    key = tuple(columns)
    if key not in cache:
        cache[key] = count_multi_number_cells(df, field, columns)
    return cache[key]


def template_key() -> str:
    """Return the session-state key of the "Template name" text box for the current file.

    The key includes the first 10 hex characters of the file's SHA-1. A keyed text_input keeps what was typed and
    ignores its `value=` once it has state, so a per-file key is what lets each new upload start with its own
    suggested name. start_import() reads the typed name back through this same key.
    """
    return f"template_name_{ss.file_sig[1][:10]}" if ss.get("file_sig") else "template_name"


def origin_label(fm) -> str:
    """Return the "Mapped by" text for one field mapping, e.g. "Rules · high", "Claude · medium" or "You".

    `fm` is a FieldMapping. Its origin (template / rule / ai / user) says who chose the mapping, and confidence is
    appended when the mapper gave one. Unmapped fields get an empty string; an unknown origin is shown as-is.
    """
    names = {"template": "Template", "rule": "Rules", "ai": "Claude", "user": "You", "": ""}
    if fm.transform == "none":
        return ""
    label = names.get(fm.origin, fm.origin)
    return f"{label} · {fm.confidence}" if fm.confidence else label


def on_map_edit(key: str) -> None:
    """on_change callback of the field grid ("Review column mapping"): apply the edited rows to ss.mapping.

    `key` is the data editor's widget key; its session-state value holds the edits as {"edited_rows": {row index:
    {column label: new value}}}. Streamlit calls this before the script reruns, so the page redraws with the
    updated mapping.

    Row i of the grid is schema.fields[i] because the grid is built in schema order and rows can't be added,
    removed or reordered (num_rows="fixed"). Each row goes through converter.editor.apply_row_edit, which picks a
    sensible transform and parses the Option cell.

    Writes ss.mapping and ss.edit_errors (shown under the grid), then bumps ss.editor_version. The new key gives
    the grid a clean slate; otherwise these same edits would be applied again on top of the grid rebuilt from
    the updated mapping.
    """
    state = ss.get(key, {})
    mapping: Mapping = ss.mapping
    errors = []
    for idx, changes in state.get("edited_rows", {}).items():
        f = schema.fields[int(idx)]
        mapping, err = apply_row_edit(schema, mapping, f.name, changes, COL_HOW, COL_SRC, COL_OPT)
        if err:
            errors.append(f"{f.header}: {err}")
    ss.mapping = mapping  # incomplete rows stay visible and are flagged in the Check column
    ss.edit_errors = errors
    ss.editor_version += 1


def mapping_diff(sch: TargetSchema, before: Mapping, changes: Mapping) -> pd.DataFrame:
    """Return a table of the fields whose mapping really changes between `before` and `changes`.

    `changes` is usually a proposal from Claude and may also list fields it left as they were; only fields whose
    transform, source columns or options differ are listed. Columns: Field (the output header, not the internal
    name), Before, After, Why (Claude's or the rule's reason).

    The columns are always present, even with no rows, so callers can check `.empty` and use diff["Field"]
    without special cases. Callers use diff["Field"] to decide which proposed fields to accept.
    """
    rows = []
    for name, fm in changes.fields.items():
        old = before.get(name)
        if (old.transform, old.sources, old.params) == (fm.transform, fm.sources, fm.params):
            continue
        rows.append({"Field": sch.field(name).header, "Before": old.summary(), "After": fm.summary(),
                     "Why": fm.reason})
    return pd.DataFrame(rows, columns=["Field", "Before", "After", "Why"])


def start_import() -> int:
    """Queue the reviewed file for conversion and return the new import's id ("Start import" button).

    Reads ss.read, ss.mapping, ss.file_name, ss.file_bytes, ss.timezone, ss.ai_used, the "Remember this mapping"
    checkbox (ss.remember_mapping) and the typed template name (see template_key()).

    Side effects:
    - When "Remember this mapping" is ticked and a name was typed, the mapping is saved as a template in the
      database (never for a file without a header row). save_template replaces a template that already has this
      name, and its use count goes up (template matching prefers the most used template when several fit exactly).
    - A new import row is created in the database with status "queued", and a copy of the uploaded file is
      written to data/imports/<id>/. The import stores a snapshot of the target schema (schema_json), so its
      results still make sense after someone edits the YAML.
    - The id is handed to the background runner, which converts the file in a worker thread. This function
      returns straight away; the page then switches to the import's detail view to follow its progress.
    - Customer Records: the import is first registered in PostgreSQL with the chosen business and service and the
      file's SHA-1 (records.store.create_import, status "queued"); its id goes into the job's read options
      ("records_import_id"), where the runner's sink finds it to store the records.
    """
    rr = ss.read
    tpl_name = None
    typed = (ss.get(template_key()) or "").strip()
    # Never for a file without a header row (see the checkbox in page_new_import), whatever the widget state says.
    remember = ss.get("remember_mapping", True) and not has_generic_columns(list(rr.df.columns))
    if remember and typed:
        tpl_name = typed
    # How the file was read. Saved with the template and the import, so a template match or a re-run reads the
    # file the same way (header_row is 0-based; None means the file has no header row).
    read_options = {"sheet": rr.sheet, "header_row": rr.header_row, "timezone": ss.get("timezone")}
    if remember and typed:
        # Consent and last_contacted are left out: a template is applied automatically, and those fields must only
        # be filled by a person choosing the column for each file.
        store.save_template(tpl_name, list(rr.df.columns), ss.mapping.without_manual(schema), read_options,
                            schema.name, user["username"])
        store.mark_template_used(tpl_name)
    rec_id = records.create_import(ss.imp_business, int(ss.imp_service), ss.file_name, ss.file_sig[1], rr.sheet,
                                   user["username"])
    import_id = store.create_import(
        username=user["username"], filename=ss.file_name, data=ss.file_bytes, schema_label=schema.label,
        schema_json=schema.model_dump_json(), mapping=ss.mapping,
        read_options=dict(read_options, records_import_id=rec_id), template_name=tpl_name,
        ai_used=bool(ss.get("ai_used")))
    records.set_job(rec_id, import_id)
    runner.submit(import_id)
    return import_id


def rerun_import(rec: ImportRecord, overrides: dict | None = None, ai_used: bool | None = None,
                 mapping: Mapping | None = None) -> int:
    """Start a new import from a previous one's original file and mapping, with the current target format.

    Used by "Re-run this import", "Re-run the import with these changes" (Claude's mistake check) and "Apply
    fixes and re-run" (Claude value fixes). The original import is never changed; the new one is linked to it
    through parent_id, so its page shows "Re-run of import #N".

    Inputs:
    - rec: the import to repeat. Its stored original file must still exist; callers check this first, since
      files are deleted after FILE_RETENTION_DAYS.
    - overrides: approved value fixes, {field name: {original value: fixed value}}. They are merged into the old
      import's fixes, so fixes approved earlier are kept.
    - ai_used: whether Claude was involved; None keeps the old import's flag.
    - mapping: a replacement mapping (e.g. with Claude's suggested changes); None reuses the old one.

    The *current* schema is used rather than the old import's snapshot, so a re-run picks up changes to the target
    format. The read options (sheet, header row, client time zone) are reused as they were. Writes a new import
    row and file copy to the database / data folder, queues it on the runner, and returns the new id.

    Customer Records: the re-run is registered as a new import in PostgreSQL for the same business and service.
    Callers only offer a re-run when the import has no records stored (it didn't complete, or its records were
    deleted; see can_rerun), so a re-run never stores the same rows twice.
    """
    data = store.source_path(rec).read_bytes()
    # Shallow copy: the per-field dicts inside rec.overrides are updated in place. That's harmless here because
    # `rec` is a fresh object read from the database on every run, not something cached.
    merged = dict(rec.overrides)
    for k, v in (overrides or {}).items():
        merged.setdefault(k, {}).update(v)
    old = records.get_import(int(rec.read_options["records_import_id"]))
    rec_id = records.create_import(old["business_id"], old["service_id"], rec.filename, old["file_sha1"],
                                   rec.read_options.get("sheet"), user["username"])
    new_id = store.create_import(
        username=user["username"], filename=rec.filename, data=data, schema_label=schema.label,
        schema_json=schema.model_dump_json(), mapping=mapping or rec.mapping,
        read_options=dict(rec.read_options, records_import_id=rec_id),
        overrides=merged, template_name=rec.template_name,
        ai_used=rec.ai_used if ai_used is None else ai_used, parent_id=rec.id)
    records.set_job(rec_id, new_id)
    runner.submit(new_id)
    return new_id


def can_rerun(rec: ImportRecord) -> tuple[bool, str]:
    """Whether an import can be re-run, and if not, why (Customer Records).

    It needs its stored original file, a business and service (imports made in Customer Records have them), and no
    records stored: a completed import's records must be deleted first, or its rows would be stored twice.
    """
    if rec.files_purged or not store.source_path(rec).exists():
        return False, "The original file is no longer stored, so this import can't be re-run."
    rid = rec.read_options.get("records_import_id")
    if rid is None:
        return False, "This import has no business and service, so it can't be re-run here."
    imp = records.get_import(int(rid))
    if imp is None:
        return False, "This import's business or service no longer exists."
    if imp["status"] == "completed":
        return False, ("This import's records are stored. To run it again, delete its records first (below), "
                       "so they aren't stored twice.")
    return True, ""


def open_import(import_id: int) -> None:
    """Go to the detail page of one import (the Imports page with ?id=<import_id> in the URL).

    st.switch_page ends the current script run immediately, so nothing after a call to this function runs;
    callers do their clean-up (e.g. reset_file_state) first. PAGE_IMPORTS is defined in the Navigation section at
    the bottom of the file; that's fine because this is only called while a page is running, after it exists.
    """
    st.switch_page(PAGE_IMPORTS, query_params={"id": str(import_id)})


# ---------------------------------------------------------------------------
# Page: New import
# ---------------------------------------------------------------------------

def page_new_import() -> None:
    """Page "New import": upload a file, review how its columns map to the target format, and start the import.

    Three steps on one page: 1 · File (what was detected, client time zone, sheet/header options and a raw
    preview), 2 · Review column mapping (the field grid plus the "Columns in your file" panel), 3 · Start import.

    All state lives in session state (the FILE_KEYS, plus the per-file widget keys), because the page is rebuilt
    from scratch on every click. Reading and mapping happen only when a *new* file arrives (or on "Re-read
    file"), not on every rerun, which keeps the page fast and keeps the reviewer's edits. "Start import" queues the
    conversion in the background and moves the browser to that import's page.
    """
    st.header("New import")
    st.caption(f"Record fields: **{schema.label}** · supported files: "
               f"{', '.join(e.lstrip('.') for e in SUPPORTED_EXTENSIONS)}")
    # After a browser refresh session state is empty, but the URL still names the draft of the file being mapped.
    if not ss.get("file_name") and st.query_params.get("draft"):
        restore_draft(st.query_params["draft"])
    if name := ss.pop("draft_restored", None):
        st.info(f"Picked up where you left off with **{name}** (the page was reloaded).")
    if ss.pop("draft_missing", False):
        st.caption("The mapping in progress from before the reload isn't kept any more (it was imported, cleared, "
                   "or unchanged for 24 hours). Upload the file again.")
    # After the draft is restored: it may set the business and service, which must happen before their boxes exist.
    _business_and_service()

    top = st.columns([4, 1], vertical_alignment="bottom")
    with top[0]:
        # The key includes uploader_version: bumping it ("Start over", after "Start import") gives a brand-new,
        # empty uploader. There's no other reliable way to clear a file_uploader from code.
        up = st.file_uploader("Upload a file", type=[e.lstrip(".") for e in SUPPORTED_EXTENSIONS],
                              key=f"uploader_{ss.uploader_version}", label_visibility="collapsed")
    with top[1]:
        if ss.get("file_name") and st.button("Start over", width="stretch"):
            reset_file_state()
            ss.uploader_version += 1
            st.rerun()

    if up is not None:
        # The uploader returns the same file on every rerun until it's cleared, so compare a signature (name +
        # content hash) and only re-read when the file really is different. Re-uploading a file with the same name
        # but changed content counts as a new file.
        data = up.getvalue()
        sig = (up.name, hashlib.sha1(data).hexdigest())
        if ss.get("file_sig") != sig:
            reset_file_state()
            ss.file_sig, ss.file_name, ss.file_bytes = sig, up.name, data
            load_and_map()

    # Safety net: a file is in session state but was never read (e.g. an earlier run stopped before load_and_map
    # got that far). Read and map it now so the rest of the page has what it needs.
    if ss.get("file_name") and "read" not in ss:
        load_and_map()

    if not ss.get("file_name"):
        st.info("Choose the business and service, then upload a CSV or Excel file. Columns are matched "
                "automatically and you review the mapping; the import then runs in the background and appears "
                "under **Imports**, where you can follow its progress and compare the original and cleaned values "
                "side by side. Every row of the file becomes a record you can search under **Find**.")
        return
    if ss.get("read_error"):
        st.error(ss.read_error)
        return
    rr = ss.read
    df = rr.df

    # The same file already imported for this business: say so (importing it again stores its rows twice).
    if ss.get("imp_business"):
        for prev in records.same_file_imports(ss.imp_business, ss.file_sig[1]):
            st.warning(f"This exact file was already imported for this business: import #{prev['job_id']} "
                       f"({prev['service_name']}, {prev['records']:,} records). Importing it again stores its rows "
                       "a second time.")

    # ---------------- Step 1: file
    st.subheader("1 · File")
    info = [f"**{ss.file_name}**", fmt_size(len(ss.file_bytes)), f"{len(df):,} rows", f"{len(df.columns)} columns"]
    if rr.sheet:
        info.append(f"sheet '{rr.sheet}'")
    if rr.encoding:
        info.append(f"encoding {rr.encoding}")
    if rr.delimiter:
        # Name the invisible / easily confused delimiters; anything else is shown as the character itself.
        info.append("delimiter " + {"\t": "TAB", ",": "comma", ";": "semicolon", "|": "pipe"}.get(rr.delimiter,
                                                                                                    rr.delimiter))
    st.markdown(" · ".join(info))
    for w in rr.warnings:
        st.warning(w)
    # Client time zone. Per-file key (see template_key for why); the starting choice comes from ss.timezone, which
    # load_and_map took from the template or the server setting. The chosen zone is used by the sample output,
    # the mistake check and the import itself (stored in its read_options).
    tz_col, _ = st.columns([2, 3])
    tz_label = tz_col.selectbox(
        "Client time zone", list(US_ZONES), index=list(US_ZONES).index(ZONE_LABEL.get(ss.get("timezone"), "Eastern")),
        key=f"tz_{ss.file_sig[1][:10]}",
        help="Times in the file that include a zone (Z, UTC, EST, +00:00…) and Unix timestamps are converted to "
             "this zone. Times without a zone are kept exactly as written.")
    if US_ZONES[tz_label] != ss.get("timezone"):
        ss.timezone = US_ZONES[tz_label]

    # Opened automatically when the reader had warnings (e.g. an unsure header row), so the user sees the raw rows.
    with st.expander("File options and raw preview", expanded=bool(rr.warnings)):
        c1, c2, c3 = st.columns([2, 2, 1], vertical_alignment="bottom")
        sheet = rr.sheet
        if rr.sheet_names and len(rr.sheet_names) > 1:
            sheet = c1.selectbox("Sheet", rr.sheet_names, index=rr.sheet_names.index(rr.sheet),
                                 format_func=lambda s: f"{s} (hidden)" if s in getattr(rr, "hidden_sheets", []) else s)
        # The box is 1-based for people (0 = "no header row"); the reader uses a 0-based row index or None.
        hdr_default = 0 if rr.header_row is None else rr.header_row + 1
        hdr = c2.number_input("Header is on row (0 = file has no header row)", min_value=0, max_value=100,
                              value=hdr_default, step=1)
        # Sheet and header changes only take effect on this button: re-reading re-maps the file from scratch and
        # throws away edits, so it shouldn't happen just because a box changed.
        if c3.button("Re-read file", width="stretch"):
            load_and_map(sheet=sheet, header_row=None if hdr == 0 else int(hdr) - 1)
            st.rerun()
        st.caption("First rows exactly as they appear in the file (row numbers as in the box above):")
        st.dataframe(pd.DataFrame(rr.raw_preview, index=range(1, len(rr.raw_preview) + 1)), hide_index=False,
                     width="stretch")

    # ---------------- Step 2: mapping
    st.subheader("2 · Review column mapping")
    meta = ss.map_meta
    if meta["source"] == "template":
        st.success(f"Recognised this layout — using saved template **{meta['template']}**.")
    elif meta["source"] == "rules+ai":
        st.info("Columns matched by rules and Claude. Check the rows marked low/medium before importing.")
    else:
        st.info("Columns matched automatically. Check the rows marked low/medium, edit anything in the grid, or "
                "change where each column goes under Columns in your file below.")
    for n in meta["notes"]:
        # The success banner above already says which template matched; don't repeat it.
        if not n.startswith("Matched saved template"):
            st.caption("ℹ️ " + n)

    mapping: Mapping = ss.mapping
    # Converts only the first few rows, so the grid can show real sample output for every field on each rerun.
    preview = preview_values(df, schema, mapping, timezone=ss.get("timezone"))
    # Columns whose values don't look like the type of the field they feed, keyed by field for the Check column.
    field_warn = {fname: f"'{col}' may not be a {schema.field(fname).type} column — see Columns in your file"
                  for col, (fname, _) in column_warnings(schema, mapping, ss.profiles).items()}
    # One grid row per target field, in schema order. on_map_edit relies on that order (row i = schema.fields[i]).
    # Check column: a broken or incomplete mapping (row_problem) is shown first; a suspicious-looking source
    # column (field_warn) only when the mapping itself is fine.
    grid = pd.DataFrame([
        {
            COL_FIELD: f.header + (" *" if f.required else "") + (" 🔒" if f.fixed_blank else "")
            + (" ✋" if f.manual_only else ""),
            COL_TYPE: f.type,
            COL_HOW: LABELS[mapping.get(f.name).transform],
            COL_SRC: mapping.get(f.name).sources,
            COL_OPT: option_text(mapping.get(f.name), f),
            COL_PREVIEW: preview.get(f.name, ""),
            COL_CHECK: ("⚠ " + p) if (p := row_problem(schema, mapping, f.name) or field_warn.get(f.name, "")) else "",
            COL_BY: "Fixed" if f.fixed_blank else origin_label(mapping.get(f.name)),
            COL_WHY: "Always blank (fixed in the target format); can't be changed" if f.fixed_blank
            else ("Mapped by hand only: pick its column here or under Columns in your file"
                  if f.manual_only and mapping.get(f.name).transform == "none" else mapping.get(f.name).reason),
        }
        for f in schema.fields
    ])
    # The return value of st.data_editor isn't used: edits are handled in the on_map_edit callback (which gets the
    # key so it can read the edits) and the grid is always rebuilt from ss.mapping. Only How, Source column(s) and
    # Option can be edited. height="content" shows every row, so the page scrolls instead of the grid: each edit
    # gives the grid a new key (see "Widget-key versioning"), and a grid scrolled inside itself would jump back
    # to its first row on every click (1.0.2).
    key = f"map_editor_{ss.editor_version}"
    st.data_editor(
        grid, key=key, on_change=on_map_edit, args=(key,), hide_index=True, num_rows="fixed", width="stretch",
        height="content",
        disabled=[COL_FIELD, COL_TYPE, COL_PREVIEW, COL_CHECK, COL_BY, COL_WHY],
        column_config={
            COL_FIELD: st.column_config.TextColumn(help="Target field. * = required. 🔒 = always blank (locked). "
                                                        "✋ = only filled when you map a column to it yourself.",
                                                   pinned=True),
            COL_HOW: st.column_config.SelectboxColumn(options=list(FROM_LABEL), required=True,
                                                      help="How the value is built from the source column(s)."),
            COL_SRC: st.column_config.MultiselectColumn(options=list(df.columns),
                                                        help="Column(s) from the uploaded file. Order matters for "
                                                             "'First non-empty' and 'Combine columns'."),
            COL_OPT: st.column_config.TextColumn(help=OPTION_HELP),
            COL_PREVIEW: st.column_config.TextColumn(help="Converted values from the first rows of the file."),
            COL_WHY: st.column_config.TextColumn(width="large"),
        },
    )
    for e in ss.get("edit_errors") or []:
        st.error(e)
    # Several phone columns feeding the phone field, or cells holding several numbers, mean "one row per different
    # number"; say so, because the converted file will then have more rows than the original.
    each = mapping.row_field(schema)
    if each is not None:
        each_fm = mapping.get(each.name)
        phones = list(each_fm.sources)
        if each_fm.transform == "area_code":
            # 'Phone + area code column': area code columns hold no numbers of their own.
            areas, area_of = area_code_pairs(phones, df.head(5000).to_dict("records"))
            uses = [f"{', '.join(phones[i] for i in area_of if area_of[i] == a)} from **{phones[a]}**"
                    for a in areas if a in area_of.values()]
            if uses:
                st.caption("ℹ️ 7-digit numbers get the area code of the same row: " + "; ".join(uses)
                           + ". Numbers that already have an area code are kept as they are.")
            else:
                st.caption("⚠️ None of these columns holds area codes, so the numbers are read as they are.")
            phones = [c for i, c in enumerate(phones) if i not in areas]
        multi = multi_number_cells(df, each, phones)
        if len(phones) > 1 or multi:
            note = (f"ℹ️ A customer with different numbers in {', '.join(phones)} keeps all of them on one record "
                    f"(**{each.header}**); each is searchable and goes into a tag's export once.")
            if multi:
                note += (f" {multi:,} cell{'s hold' if multi != 1 else ' holds'} several numbers; each different "
                         "number counts (numbers labelled fax only when there is no other).")
            st.caption(note)
    # Source columns that feed no field. "(skipped)" marks the ones the reviewer excluded on purpose, as opposed
    # to columns that simply didn't match anything.
    unused = [c for c in df.columns if c not in mapping.used_sources()]
    if unused:
        skipped = set(mapping.excluded_columns)
        st.caption("Columns that feed no field (kept on each record as written, searchable as *Column: …*): "
                   + ", ".join(f"`{c}`" + (" (skipped)" if c in skipped else "") for c in unused))

    _notes_and_checks(df)

    # ---------------- Step 3: start
    st.subheader("3 · Start import")
    # Blocking problems (e.g. the required phone field has no source column) disable the Start button.
    problems = ss.mapping.problems(schema)
    if problems:
        st.warning("Fix before importing: " + "; ".join(f"**{schema.field(k).header}** — {v}"
                                                        for k, v in problems.items()))
    # Customer Records: every import is for one business and one service (chosen at the top of the page).
    if not (ss.get("imp_business") and ss.get("imp_service")):
        problems = dict(problems, _business="choose the business and service at the top of the page")
        st.warning("Choose the **business** and **service** at the top of the page before importing.")
    c1, c2, c3 = st.columns([1.2, 2.5, 1.3], vertical_alignment="bottom")
    # Files without a header row don't use templates at all: their stand-in column names (Column 1, Column 2…) say
    # nothing about the layout, so a template could only be matched to another client's file with as many columns.
    headerless = has_generic_columns(list(df.columns))
    if headerless:
        c1.checkbox("Remember this mapping", value=False, disabled=True, key="remember_mapping_headerless",
                    help="Files without a header row are not saved as templates.")
    else:
        # remember_pref: the box as it was before a browser refresh (restore_draft); otherwise ticked.
        c1.checkbox("Remember this mapping", value=ss.get("remember_pref", True), key="remember_mapping",
                    help="Saves the mapping as a template; the next file with the same columns maps instantly.")
    c2.text_input("Template name", value=ss.get("template_name") or "", key=template_key(),
                  disabled=headerless or not ss.get("remember_mapping", True))
    if c3.button("Start import", type="primary", disabled=bool(problems), width="stretch"):
        import_id = start_import()
        # Clear the page *before* switching: open_import ends this run, and coming back to New import should
        # show an empty uploader rather than the file that was just imported. This also deletes the draft.
        reset_file_state()
        ss.uploader_version += 1
        open_import(import_id)
    # Everything above may have changed the mapping or the file's settings: keep them for a browser refresh.
    save_draft()


def _business_and_service() -> None:
    """Customer Records: the business and service every import is for, chosen at the top of New import.

    The choices (ss.imp_business: business ID, ss.imp_service: service id) stay for the next file, since several
    files of one business are often imported in a row. Only active services are offered. When there are no
    businesses or services yet, the page says where to add them.
    """
    businesses = records.list_businesses()
    services = records.list_services(active_only=True)
    c1, c2 = st.columns(2)
    ids = [b["id"] for b in businesses]
    names = {b["id"]: f"{b['id']} · {b['name']}" for b in businesses}
    if ss.get("imp_business") not in ids:
        ss.pop("imp_business", None)
    c1.selectbox("Business", ids, index=None, key="imp_business", format_func=names.get,
                 placeholder="Choose the business" if ids else "No businesses yet — add them under Businesses",
                 help="The business these customers belong to (its ID is the main system's).")
    sids = [s["id"] for s in services]
    snames = {s["id"]: s["name"] for s in services}
    if ss.get("imp_service") not in sids:
        ss.pop("imp_service", None)
    c2.selectbox("Service", sids, index=None, key="imp_service", format_func=snames.get,
                 placeholder="Choose the service" if sids else "No services yet — add them under Services")


# Column headers of the "Columns in your file" table (one row per column of the uploaded file).
COL_GOES, COL_SKIP, COL_NOTE = "Goes to", "Skip", "Your note"


def on_columns_edit(key: str, columns: list[str]) -> None:
    """Edits in the "Columns in your file" table: where a column goes, skipping it, and notes.

    on_change callback of that data editor. `key` is the editor's widget key (its state holds {"edited_rows":
    {row index: {column label: new value}}}); `columns` is the file's column list in table order, so row i is
    columns[i] (rows can't be added, removed or reordered).

    Per edited row:
    - Your note: stored in mapping.column_notes (an emptied note is removed). Notes are saved with the template
      and the import and are what "Apply notes with Claude" works from.
    - Goes to / Skip: passed to converter.editor.apply_column_edit, which moves the column to the chosen field(s)
      (a field that gains the column takes it as its only source, except the phone field, which adds it) or
      excludes it so rules, templates and Claude won't map it again. "Goes to" holds output headers, so they are
      turned back into field names first; locked (always blank) fields aren't among the choices.

    Writes ss.mapping and ss.column_messages (what changed, shown once under the table), then bumps
    ss.notes_version and ss.editor_version so both data editors are rebuilt from the new mapping: a change here
    also changes the field grid above.
    """
    state = ss.get(key, {})
    mapping: Mapping = ss.mapping
    by_header = {f.header: f.name for f in schema.mappable_fields}
    notes = dict(mapping.column_notes)
    messages: list[str] = []
    for idx, changes in state.get("edited_rows", {}).items():
        col = columns[int(idx)]
        if COL_NOTE in changes:
            text = (changes[COL_NOTE] or "").strip()
            if text:
                notes[col] = text
            else:
                notes.pop(col, None)
        # None means "this cell wasn't touched", which apply_column_edit treats differently from an empty list
        # (an empty Goes to = leave the column out) or False (un-skip).
        targets = None
        if COL_GOES in changes:
            targets = [by_header[h] for h in (changes[COL_GOES] or []) if h in by_header]
        skip = changes.get(COL_SKIP)
        if targets is not None or skip is not None:
            mapping, msgs = apply_column_edit(schema, mapping, col, targets=targets, skip=skip)
            messages.extend(msgs)
    # Notes are applied last, on top of whatever apply_column_edit returned (it doesn't touch notes).
    ss.mapping = mapping.model_copy(update={"column_notes": notes})
    ss.column_messages = messages
    ss.notes_version = ss.get("notes_version", 0) + 1
    ss.editor_version += 1


def on_general_note(key: str) -> None:
    """on_change callback of the "General note" box: store the stripped text on ss.mapping.general_note.

    `key` is the text area's widget key (per file). The note is saved with the template and the import, and is
    sent to Claude together with the column notes by "Apply notes with Claude".
    """
    ss.mapping = ss.mapping.model_copy(update={"general_note": (ss.get(key) or "").strip()})


def _show_changes(changes: Mapping, base: Mapping, columns: list[str], key: str, clear=None) -> None:
    """Diff of proposed field changes with Accept / Discard (New import page). `clear` hides them afterwards.

    Inputs: `changes` are the field mappings Claude proposed (from "Apply notes" or "Check for mistakes"),
    `base` is the current mapping, `columns` the file's columns, and `key` the session-state key that holds the
    proposal; it also makes the button keys unique, since two proposals can be on screen at once. `clear` is how
    to hide the proposal once it's been accepted or discarded: by default ss[key] is removed; the mistake check
    passes its own, so its findings stay visible.

    Accept: only the fields listed in the diff (the ones that really change; matched on the output header, which
    is what the diff shows) are merged onto the base mapping. Notes and skipped columns are kept, and the result
    is normalized against the file's columns. Writes ss.mapping and ss.ai_used, bumps ss.editor_version, and
    reruns: the grids above were already drawn in this run with the old mapping.
    """
    clear = clear or (lambda: ss.pop(key, None))
    diff = mapping_diff(schema, base, changes)
    if diff.empty:
        st.caption("No mapping changes proposed.")
        return
    st.markdown("**Proposed mapping changes**")
    st.dataframe(diff, hide_index=True, width="stretch")
    a1, a2, _ = st.columns([1, 1, 3])
    if a1.button("Accept changes", type="primary", width="stretch", key=f"accept_{key}"):
        changed = Mapping(fields={k: v for k, v in changes.fields.items()
                                  if schema.field(k).header in set(diff["Field"])})
        ss.mapping = merge(base, changed).normalized(schema, columns)
        ss.ai_used = True
        clear()
        ss.editor_version += 1
        st.rerun()
    if a2.button("Discard", width="stretch", key=f"discard_{key}"):
        clear()
        st.rerun()


def _findings_table(findings, sch: TargetSchema) -> None:
    """Show the results of a mistake check: a count by severity and one table row per finding.

    `findings` is the list of converter.checks.Finding objects from check_for_mistakes (rule findings plus
    Claude's, if it ran); `sch` is the schema used to turn field names into output headers (Finding.as_row). An
    empty list shows a green "No problems found" instead. Used on New import and on a finished import.
    """
    if not findings:
        st.success("No problems found.", icon="✅")
        return
    n_err = sum(f.severity == "error" for f in findings)
    n_warn = sum(f.severity == "warning" for f in findings)
    st.markdown(f"**{len(findings)} finding{'s' if len(findings) != 1 else ''}** · {n_err} error(s) · "
                f"{n_warn} warning(s)")
    # as_row gives an untitled first column ("") holding a coloured severity dot, hence the "" column config.
    st.dataframe(pd.DataFrame([f.as_row(sch) for f in findings]), hide_index=True, width="stretch",
                 column_config={"": st.column_config.TextColumn(width="small"),
                                "Issue": st.column_config.TextColumn(width="large"),
                                "Suggestion": st.column_config.TextColumn(width="large")})


def _notes_and_checks(df: pd.DataFrame) -> None:
    """Draw the "Columns in your file" panel of New import, with its two actions.

    `df` is the uploaded file as read (all text). The panel has:
    - a table with one row per source column: sample values, Goes to, Skip, a Check warning and Your note
      (edits are handled by on_columns_edit);
    - a General note box (on_general_note);
    - "Apply notes with Claude": sends the column names, sample values and notes to Claude, which proposes mapping
      changes and reports a result per note. The proposal is shown with Accept / Discard (_show_changes).
    - "Check for mistakes": converts the first 1,000 rows in this request (not in the background runner) and runs
      the rule checks on them, plus Claude's review of a sample when Claude is configured (phone digits partly
      masked unless AI_MASK_PHONES=false). Claude may also propose mapping changes.

    Session state read/written: ss.mapping (via the callbacks and Accept), ss.profiles, ss.timezone,
    ss.notes_result, ss.check_result, ss.column_messages (popped after it's shown once), and the
    notes_version / editor_version counters used in the table's key. Results go into session state because a
    button is only "clicked" for the one run right after the click; Accept / Discard cause another rerun, and the
    results must still be there for it.
    """
    columns = list(df.columns)
    mapping: Mapping = ss.mapping
    with st.container(border=True):
        st.markdown("**Columns in your file**")
        st.caption("Change where a column goes with **Goes to** (clear it to leave the column out), or tick "
                   "**Skip** to keep a column out of the import completely — rules, saved templates and Claude "
                   "won't map it again. Picking a field here replaces whatever fed that field before — except "
                   "the phone field: each phone column you add is kept too (every different number stays on the "
                   "record). Every column is also kept on the record as written. "
                   "You can also write a note on what a column is, where it goes or how to read it — e.g. "
                   "*“this is the last visit date”*, *“dates are day-first”*. "
                   + ("Claude applies the notes when you click *Apply notes*. " if assistant else
                      "Claude isn't configured, so notes are saved with the template as documentation. ")
                   + "Everything here is saved with the template and the import.")
        # {column: (field name, message)} for columns whose values don't fit the field they feed.
        warnings = column_warnings(schema, mapping, ss.profiles)
        samples = {p.name: p.samples for p in ss.profiles}
        excluded = set(mapping.excluded_columns)
        # One row per source column, in file order: on_columns_edit maps row i back to columns[i].
        notes_df = pd.DataFrame([{
            "Column": c,
            "Sample values": " | ".join(samples.get(c, [])[:3]),
            COL_GOES: [schema.field(n).header for n in mapping.targets_of(c) if n in schema.field_names],
            COL_SKIP: c in excluded,
            COL_CHECK: ("⚠ " + warnings[c][1]) if c in warnings else ("Skipped" if c in excluded else ""),
            COL_NOTE: mapping.column_notes.get(c, ""),
        } for c in columns])
        # The key includes editor_version as well as notes_version: an edit in the field grid changes "Goes to"
        # here too, so this table must also start fresh when the grid's version changes.
        nkey = f"notes_editor_{ss.editor_version}_{ss.get('notes_version', 0)}"
        # Every row shown (height="content"), like the field grid: a table scrolled inside itself would jump back
        # to its first row after each edit, which gives it a new key (1.0.2).
        st.data_editor(notes_df, key=nkey, on_change=on_columns_edit, args=(nkey, columns), hide_index=True,
                       num_rows="fixed", width="stretch", height="content",
                       disabled=["Column", "Sample values", COL_CHECK],
                       column_config={
                           "Column": st.column_config.TextColumn(pinned=True),
                           "Sample values": st.column_config.TextColumn(width="medium"),
                           COL_GOES: st.column_config.MultiselectColumn(
                               options=[f.header for f in schema.mappable_fields], width="medium",
                               help="Field(s) this column goes to. Empty = not imported. Picking a field makes this "
                                    "column its only source."),
                           COL_SKIP: st.column_config.CheckboxColumn(
                               help="Leave this column out of the import and stop it from being mapped again."),
                           COL_CHECK: st.column_config.TextColumn(width="medium"),
                           COL_NOTE: st.column_config.TextColumn(width="large"),
                       })
        # Popped, not read: the "what changed" messages from the last edit are shown once and then disappear.
        for msg in ss.pop("column_messages", None) or []:
            st.caption("✔️ " + msg)
        if warnings:
            st.warning("Check " + ", ".join(f"**{c}**" for c in warnings) + " — " +
                       ("its" if len(warnings) == 1 else "their") + " values don't fit the field "
                       + ("it goes" if len(warnings) == 1 else "they go") + " to. Clear **Goes to** or tick "
                       "**Skip** if that's a mistake.")
        # Per-file key, so a new upload doesn't inherit the previous file's general note (a template's note still
        # comes through `value=`, because the new key has no state yet).
        gkey = f"general_note_{ss.file_sig[1][:10]}"
        st.text_area("General note (optional)", value=mapping.general_note, key=gkey, on_change=on_general_note,
                     args=(gkey,), height=68,
                     placeholder="e.g. all dates in this file are day-first · times are in Pacific time")

        b1, b2, _ = st.columns([1.3, 1.3, 2])
        # Count of non-empty notes (column notes + the general note); with none there's nothing for Claude to apply.
        n_notes = len([n for n in ss.mapping.column_notes.values() if n]) + bool(ss.mapping.general_note)
        apply_clicked = b1.button("Apply notes with Claude", type="primary", width="stretch",
                                  disabled=not (assistant and n_notes),
                                  help=None if assistant else "Set ANTHROPIC_API_KEY on the server to enable.")
        check_clicked = b2.button("Check for mistakes", width="stretch",
                                  help="Runs rule checks on the first 1,000 rows" +
                                       (" and asks Claude to review a sample." if assistant else "."))
        if apply_clicked:
            # AIError covers API/network failures and unusable answers; the error is shown and any earlier
            # result is left as it was.
            try:
                with st.spinner("Claude is applying your notes…"):
                    res = assistant.apply_notes(schema, ss.profiles, ss.mapping, ss.mapping.column_notes,
                                                ss.mapping.general_note)
                ss.notes_result = res
            except AIError as exc:
                st.error(str(exc))
        # Shown on every rerun until Accept / Discard removes ss.notes_result (via _show_changes' default clear).
        res = ss.get("notes_result")
        if res is not None:
            if res.summary:
                st.markdown(f"**Claude:** {res.summary}")
            if res.note_results:
                icon = {"applied": "✅ applied", "already_correct": "✔️ already correct", "not_applied": "⚠️ not applied"}
                st.dataframe(pd.DataFrame([{"Note on": r.get("column"), "Result": icon.get(r.get("status"), ""),
                                            "Details": r.get("message", "")} for r in res.note_results]),
                             hide_index=True, width="stretch",
                             column_config={"Details": st.column_config.TextColumn(width="large")})
            if res.dropped:
                st.caption("Ignored invalid suggestions: " + "; ".join(res.dropped[:5]))
            _show_changes(res.mapping, ss.mapping, columns, "notes_result")

        if check_clicked:
            # A dry run on the first 1,000 rows only, so it's quick enough to run while the user waits. Nothing is
            # written to the database; only the result is kept in session state.
            head = df.head(1000)
            with st.spinner("Checking for mistakes" + (" (rules + Claude)…" if assistant else "…")):
                prev = convert(head, schema, ss.mapping, timezone=ss.get("timezone"))
                findings, ai_res, err = check_for_mistakes(
                    schema, ss.mapping, head, prev.audit, ss.profiles, assistant, use_claude=bool(assistant),
                    mask_phones=settings.ai_mask_phones, timezone_label=ZONE_LABEL.get(ss.get("timezone"), ""),
                    scope=f"preview of the first {len(head):,} rows before import")
            # findings: rule + Claude findings; ai: Claude's full result or None; error: why Claude didn't run.
            ss.check_result = {"findings": findings, "ai": ai_res, "error": err, "rows": len(head)}
        chk = ss.get("check_result")
        if chk is not None:
            st.markdown(f"**Mistake check** · first {chk['rows']:,} rows"
                        + (" · rules + Claude" if chk["ai"] else " · rules"))
            if chk["error"]:
                st.warning(f"Claude's review didn't run ({chk['error']}). Rule checks are below.")
            if chk["ai"] and chk["ai"].summary:
                st.markdown(f"**Claude:** {chk['ai'].summary}")
            _findings_table(chk["findings"], schema)
            # After Accept / Discard only the proposed changes are hidden: the custom `clear` sets a flag inside
            # the check_result dict (the same object held in session state, so the flag persists) instead of
            # dropping the whole result, so the findings stay on screen.
            if chk["ai"] and chk["ai"].mapping.fields and not chk.get("changes_done"):
                _show_changes(chk["ai"].mapping, ss.mapping, columns, "check_changes",
                              clear=lambda: chk.update(changes_done=True))


# ---------------------------------------------------------------------------
# Page: Imports (list + detail)
# ---------------------------------------------------------------------------

def page_imports() -> None:
    """Page "Imports": every import by everyone, with filters, live progress and a link to each import.

    With ?id=<n> in the URL this page shows that one import instead (page_import_detail). Keeping the detail view
    under the same navigation entry, selected by a query parameter, gives each import a URL that can be bookmarked
    or shared, and lets open_import() link to it from anywhere.

    The filters are keyed widgets (imp_who, imp_status, imp_search), so they keep their values across reruns. The
    list itself is drawn by _import_list as a fragment that refreshes every 1.5 s, but only while some import is
    queued or running; otherwise the page doesn't poll at all.
    """
    qid = st.query_params.get("id")
    if qid and str(qid).isdigit():
        page_import_detail(int(qid))
        return

    st.header("Imports")
    f1, f2, f3, f4 = st.columns([1.2, 2.6, 2, 0.8], vertical_alignment="bottom")
    who = f1.segmented_control("Show", ["Everyone", "Mine"], default="Everyone", key="imp_who")
    statuses = f2.pills("Status", ["running", "queued", "completed", "failed", "cancelled", "interrupted"],
                        selection_mode="multi", key="imp_status",
                        format_func=lambda s: s.capitalize())
    search = f3.text_input("Search", placeholder="file name, template or #id", key="imp_search")
    if f4.button("↻ Refresh", width="stretch"):
        st.rerun()

    # run_every is fixed when the fragment is created, so whether to poll is decided here, once per full run.
    # `polling` is passed in so the fragment can tell when everything has finished and stop the polling.
    active = store.active_imports()
    live = st.fragment(run_every=1.5 if active else None)(_import_list)
    live(who == "Mine", list(statuses or []), search, bool(active))


def _import_list(mine: bool, statuses: list[str], search: str, polling: bool) -> None:
    """Draw the "In progress" cards and the table of imports (runs as a fragment of the Imports page).

    Inputs: `mine` (only the signed-in user's imports), `statuses` (empty = any status), `search` (file name,
    template name or #id) and `polling` (whether this fragment was created with run_every, i.e. something was
    running when the page was drawn).

    When polling, Streamlit re-runs only this function every 1.5 s, so it re-reads the imports from the database
    itself rather than relying on anything computed by the outer page. Running imports get a card with a
    progress bar, the current step, elapsed time and an estimate of the time left, plus Open and Cancel. Below,
    up to 500 matching imports (newest first) are listed; selecting a row opens that import.
    """
    active = store.active_imports()
    # A fragment can't change its own run_every. When the last import finishes, rerun the whole app so the page
    # is rebuilt with a non-polling fragment.
    if polling and not active:
        st.rerun(scope="app")  # everything finished: switch the page back to static mode

    if active:
        st.markdown(f"**In progress ({len(active)})**")
        for rec in active:
            with st.container(border=True):
                a, b = st.columns([5, 1.2], vertical_alignment="center")
                with a:
                    st.markdown(f"**#{rec.id} · {rec.filename}** · {rec.username} · {fmt_size(rec.file_size)}")
                    pct = int(rec.progress * 100)
                    # Queued imports wait for a free worker (IMPORT_WORKERS run at once); for them elapsed() is the
                    # waiting time. rec.eta() has no estimate until about 5% is done; fmt_eta shows that case.
                    if rec.status == "queued":
                        st.progress(0.0, text=f"Waiting for a free worker · queued {fmt_duration(rec.elapsed())}")
                    else:
                        st.progress(rec.progress, text=f"{pct}% · {rec.stage} · {fmt_duration(rec.elapsed())} "
                                                       f"elapsed · {fmt_eta(rec.eta())} left")
                with b:
                    if st.button("Open", key=f"open_{rec.id}", width="stretch"):
                        open_import(rec.id)
                    # Cancelling only sets a flag in the database; the worker sees it at its next progress check
                    # and stops, so the card can stay for a moment. The button is disabled once it's requested.
                    if st.button("Cancel", key=f"cancel_{rec.id}", width="stretch",
                                 disabled=rec.cancel_requested):
                        runner.cancel(rec.id)
                        st.toast(f"Cancelling import #{rec.id}…")

    # Newest first, at most 500. Running imports appear here too (with their progress), not only in the cards.
    recs = store.list_imports(limit=500, username=user["username"] if mine else None,
                              statuses=statuses or None, search=search or None)
    if not recs:
        st.info("No imports yet." if not (mine or statuses or search) else "No imports match these filters.")
        return
    # Customer Records: each job's business, service and stored records, from PostgreSQL.
    linked = records.imports_for_jobs([r.id for r in recs])
    table = pd.DataFrame([{
        "#": r.id,
        "File": r.filename,
        "Business": f"{linked[r.id]['business_id']} · {linked[r.id]['business_name']}" if r.id in linked else "",
        "Service": linked[r.id]["service_name"] if r.id in linked else "",
        "By": r.username,
        "Status": status_text(r) + (" · records deleted" if linked.get(r.id, {}).get("status") == "deleted" else ""),
        "Progress": r.progress,
        "Started": fmt_dt(r.started_at or r.created_at, TZ),
        "Duration": fmt_duration(r.elapsed()),
        "Rows": r.total_rows,
        "Records": linked[r.id]["records"] if linked.get(r.id, {}).get("status") == "completed" else None,
        "No usable number": linked[r.id]["no_phone"] if linked.get(r.id, {}).get("status") == "completed" else None,
        "Template": r.template_name or "",
    } for r in recs])
    st.caption("Select a row to open the import.")
    event = st.dataframe(
        table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
        key="imports_table",
        column_config={
            "#": st.column_config.NumberColumn(width="small", format="%d"),
            "Progress": st.column_config.ProgressColumn(min_value=0, max_value=1, format="percent", width="small"),
            "Rows": st.column_config.NumberColumn(format="localized"),
            "Records": st.column_config.NumberColumn(format="localized"),
            "No usable number": st.column_config.NumberColumn(format="localized"),
        },
    )
    # on_select="rerun": clicking a row starts a new run in which the selection is filled in, and we navigate to
    # that import. The selection holds positions in `table`, so the id is read from its "#" column.
    sel = event.selection.rows if event and event.selection else []
    if sel:
        open_import(int(table.iloc[sel[0]]["#"]))


def page_import_detail(import_id: int) -> None:
    """Show one import (the Imports page with ?id=<import_id>).

    Draws the header and metadata (who, when, size, target format, template, whether Claude was used) and, for a
    re-run, a link back to the original import. Then, depending on the status:
    - queued / running: a live progress panel (_live_progress) that refreshes every second;
    - failed / cancelled / interrupted: the status, the error if there is one, and a re-run button.
      "interrupted" means the server restarted while the import was queued or running.
    - completed: the full results (_completed_view).
    """
    # Clearing ?id= and rerunning sends the user back to the list on the same page.
    if st.button("← All imports"):
        st.query_params.clear()
        st.rerun()
    rec = store.get_import(import_id)
    if rec is None:
        st.error(f"Import #{import_id} not found.")
        return

    st.header(f"Import #{rec.id} · {rec.filename}")
    linked = records.imports_for_jobs([rec.id]).get(rec.id)
    if linked:
        st.markdown(f"**{linked['business_id']} · {linked['business_name']}** · {linked['service_name']}")
    meta = [f"by **{rec.username}**", f"uploaded {fmt_dt(rec.created_at, TZ)}", fmt_size(rec.file_size),
            f"fields: {rec.schema_label}"]
    if rec.template_name:
        meta.append(f"template: {rec.template_name}")
    if rec.ai_used:
        meta.append("Claude used")
    st.caption(" · ".join(meta))
    if rec.parent_id:
        if st.button(f"Re-run of import #{rec.parent_id} — open original", type="tertiary"):
            open_import(rec.parent_id)

    if rec.active:
        # Only the progress panel refreshes (every second); the rest of the page isn't redrawn while it waits.
        st.fragment(run_every=1)(_live_progress)(import_id)
        return

    if rec.status in ("failed", "cancelled", "interrupted"):
        st.badge(rec.status.capitalize(), color=status_color(rec.status))
        if rec.error:
            st.error(rec.error)
        else:
            st.warning(f"This import was {rec.status}.")
        _rerun_button(rec)
        return

    _completed_view(rec)


def _live_progress(import_id: int) -> None:
    """Progress panel for a queued or running import (runs as a fragment that refreshes every second).

    Re-reads the import from the database on every tick (the background worker writes progress there) and shows
    the status, a progress bar with the current step, elapsed time, estimated time left, start time and a Cancel
    button. Cancel sets cancel_requested in the database; the worker stops at its next progress check.
    """
    rec = store.get_import(import_id)
    # Finished (or gone): a fragment can only redraw itself, so rerun the whole app to swap this panel for the
    # results view. st.rerun raises to stop this run, so the lines below never see rec=None.
    if rec is None or not rec.active:
        st.rerun(scope="app")
    st.badge(rec.status.capitalize(), color=status_color(rec.status))
    if rec.status == "queued":
        st.progress(0.0, text="Waiting for a free worker…")
    else:
        st.progress(rec.progress, text=f"{int(rec.progress * 100)}% · {rec.stage}")
    m = st.columns(4)
    m[0].metric("Elapsed", fmt_duration(rec.elapsed()))
    m[1].metric("Time left", fmt_eta(rec.eta()) if rec.status == "running" else "—")
    m[2].metric("Started", fmt_dt(rec.started_at, TZ, with_date=False) if rec.started_at else "not yet")
    m[3].metric("Progress", f"{int(rec.progress * 100)}%")
    if st.button("Cancel import", disabled=rec.cancel_requested):
        runner.cancel(rec.id)
        st.toast("Cancelling…")
    if rec.cancel_requested:
        st.caption("Cancelling…")


def _rerun_button(rec: ImportRecord) -> None:
    """Show "Re-run this import", or a note saying why it can't be re-run.

    A re-run needs the stored original file, which is deleted after FILE_RETENTION_DAYS (files_purged) or may be
    missing on disk. Clicking creates a new import with the same file and mapping but the current target format
    (see rerun_import), and opens it.
    """
    ok, why = can_rerun(rec)
    if not ok:
        st.caption(why)
        return
    if st.button("↻ Re-run this import", help="Runs the same original file and mapping again with the current "
                                              "record fields, for the same business and service. Creates a new "
                                              "import."):
        open_import(rerun_import(rec))


def _completed_view(rec: ImportRecord) -> None:
    """Results of a completed import: the outcome banner, counts, downloads, five tabs and follow-up actions.

    - Banner: bad rows are flagged loudly in red, because they are NOT in the converted file and someone has to
      deal with them. Otherwise the runner's summary message is shown.
    - Counts: rows in file, converted (including extra rows made for customers with several phone numbers), bad
      rows, duplicates removed, empty rows, time taken.
    - Downloads: converted file, rejected rows (bad + duplicates, with reasons), summary, the original file, and a
      side-by-side Excel workbook that is only built on request (slow on big files) and then kept in
      ss["cmp_xlsx_<id>"] for the rest of the session.
    - Tabs: Side by side, What changed, Converted file, Bad & duplicate rows, Mapping used.
    - Then: Check this import for mistakes, Re-run, and Claude value fixes.

    Everything here uses the import's own schema snapshot (rec.schema_json), not the current YAML, so an old
    import is shown with the format it was actually converted to. rec.summary is the dict written by the runner
    (message, warnings, extra_rows, reasons, blanked, skipped, repaired, fill_rates, failed_values…). Once the
    stored files have been purged (FILE_RETENTION_DAYS) only the banner and counts are left, since the tabs and
    downloads need those files.
    """
    rec_schema = TargetSchema.model_validate_json(rec.schema_json)
    s = rec.summary
    rs = s.get("records") or {}
    linked = records.imports_for_jobs([rec.id]).get(rec.id)
    deleted = bool(linked and linked["status"] == "deleted")
    if deleted:
        st.warning("This import's records were deleted. The file and the side-by-side view are kept until the files "
                   "are purged; it can be run again (below).")
    elif rec.rejected:
        st.error(f"**{rec.rejected:,} row{'s were' if rec.rejected != 1 else ' was'} NOT stored.** "
                 "See *Side by side → Bad rows* for the reason on each row.", icon="⚠️")
    elif not rs.get("stored"):
        st.error("No records were stored. " + s.get("message", ""))
    else:
        st.success(f"{rs['stored']:,} record{'s' if rs['stored'] != 1 else ''} stored, with {rs.get('numbers', 0):,} "
                   f"different phone number{'s' if rs.get('numbers', 0) != 1 else ''}"
                   + (f"; {rs['no_phone']:,} without a usable number (searchable, never exported)"
                      if rs.get("no_phone") else "") + ".", icon="✅")
    for w in s.get("warnings", []):
        st.caption("ℹ️ File note: " + w)
    if rs.get("first_visit_as"):
        st.caption(f"ℹ️ The file has no last-activity date, so tag exports use its sign-up / creation date as "
                   f"**{rs['first_visit_as']}**, as the converter does.")

    m = st.columns(6)
    m[0].metric("Rows in file", f"{rec.total_rows:,}")
    m[1].metric("Records stored", f"{rs.get('stored', 0):,}")
    m[2].metric("Phone numbers", f"{rs.get('numbers', 0):,}", help="Different usable numbers in this file.")
    m[3].metric("No usable number", f"{rs.get('no_phone', 0):,}",
                help="Stored and searchable; never in a tag's export.")
    m[4].metric("Empty rows", f"{rec.blank_rows:,}")
    m[5].metric("Took", fmt_duration(rec.elapsed()))
    if linked and not deleted and st.button("Show this import's records in Find"):
        records_ui.go_find(ctx, {"t": "find", "requests": [{"omit": False, "criteria": [["import", str(rec.id)]]}]},
                           f"Import #{rec.id} · {rec.filename}")

    if rec.files_purged:
        st.info(f"The files for this import were deleted after {settings.file_retention_days} days. "
                "The counts above are kept.")
        return

    # Downloads
    # on_click="ignore" means clicking a download doesn't trigger a rerun, so the page stays exactly as it is.
    # store.file_path returns None for a file that wasn't produced or no longer exists; its button is then left out.
    folder = store.files_root / str(rec.id)
    d = st.columns(5)
    out_p = store.file_path(rec, "output")
    if out_p:
        d[0].download_button(f"⬇ Cleaned rows ({rec.imported:,})", out_p.read_bytes(), out_p.name,
                             width="stretch", on_click="ignore", disabled=not rec.imported,
                             help="The cleaned values, one row per phone number (for checking; tags export from "
                                  "the records).")
    rej_p = store.file_path(rec, "rejects")
    if rej_p:
        # The rejected-rows file holds duplicates as well as bad rows, so both are counted on the button.
        n_rej = (rec.rejected or 0) + (rec.duplicates or 0)
        d[1].download_button(f"⬇ Rejected rows ({n_rej:,})", rejects_download(str(rej_p), rej_p.stat().st_mtime),
                             rec.files.get("rejects_download", "rejected.csv"), mime="text/csv", width="stretch",
                             on_click="ignore", disabled=not n_rej)
    sum_p = store.file_path(rec, "summary")
    if sum_p:
        d[2].download_button("⬇ Summary", sum_p.read_bytes(), rec.files.get("summary_download", "summary.txt"),
                             mime="text/plain", width="stretch", on_click="ignore")
    src_p = store.source_path(rec)
    if src_p.exists():
        d[3].download_button("⬇ Original file", src_p.read_bytes(), rec.filename, width="stretch",
                             on_click="ignore")
    # Side-by-side workbook, in two steps: "Prepare" builds it (slow for big files, so only on request) and keeps
    # the bytes in session state; the rerun then turns the button into a download for the rest of the session.
    xkey = f"cmp_xlsx_{rec.id}"
    src_df, audit = load_import_tables(rec.id, str(folder))
    # Which source columns feed which output fields: used by the side-by-side view and the Excel workbook.
    plan = column_plan(rec_schema, rec.mapping, list(src_df.columns))
    if xkey in ss:
        d[4].download_button("⬇ Side-by-side (Excel)", ss[xkey], f"{Path(rec.filename).stem}_side_by_side.xlsx",
                             width="stretch", on_click="ignore")
    elif d[4].button("Prepare side-by-side Excel", width="stretch"):
        with st.spinner("Building the side-by-side workbook…"):
            ss[xkey] = comparison_xlsx(src_df, audit, rec_schema, plan,
                                       f"Import #{rec.id} — {rec.filename} → {rec.schema_label}")
        st.rerun()

    tabs = st.tabs(["Side by side", "What changed", "Cleaned rows", "Rows not stored", "Mapping used"])
    with tabs[0]:
        _side_by_side(rec, rec_schema, src_df, audit, plan)
    with tabs[1]:
        st.caption("How each target field was produced across all rows of the file.")
        st.dataframe(field_change_summary(audit, rec_schema, rec.mapping), hide_index=True, width="stretch",
                     column_config={"Examples": st.column_config.TextColumn(width="large")})
        # One table for the four kinds of problem the runner counts, each with what it meant for the row:
        # reasons → row rejected, blanked → optional value left blank, skipped → an extra phone number dropped,
        # repaired → a likely typo fixed and imported (worth checking by hand).
        reasons = [{"Problem": r, "Rows": c, "Effect": "row not imported"} for r, c in s.get("reasons", {}).items()]
        reasons += [{"Problem": r, "Rows": c, "Effect": "value left blank"} for r, c in s.get("blanked", {}).items()]
        reasons += [{"Problem": r, "Rows": c, "Effect": "extra number skipped (row imported with its other number)"}
                    for r, c in s.get("skipped", {}).items()]
        reasons += [{"Problem": r, "Rows": c, "Effect": "repaired from a likely typo and imported — please check"}
                    for r, c in s.get("repaired", {}).items()]
        if reasons:
            st.markdown("**Problems found**")
            st.dataframe(pd.DataFrame(reasons), hide_index=True, width="stretch")
        fills = s.get("fill_rates", {})
        if fills:
            st.markdown("**How full each column is in the converted file**")
            st.dataframe(pd.DataFrame([{"Field": k, "Filled": v} for k, v in fills.items()]), hide_index=True,
                         width="stretch", column_config={
                             "Filled": st.column_config.ProgressColumn(min_value=0, max_value=1, format="percent")})
    with tabs[2]:
        # Rebuilt from the audit's out.<field> columns rather than by re-reading the output file, which may be in
        # any output format. Capped at 1,000 rows to keep the page quick; the download has everything.
        imported = audit[audit["_status"] == "imported"]
        out = pd.DataFrame({f.header: imported[f"out.{f.name}"].values for f in rec_schema.fields})
        st.caption(f"{len(out):,} rows" + (" · showing the first 1,000" if len(out) > 1000 else ""))
        st.dataframe(out.head(1000), hide_index=True, width="stretch")
    with tabs[3]:
        bad = audit[audit["_status"].isin(["rejected", "duplicate"])]
        if bad.empty:
            st.caption("None.")
        else:
            # src_df is lined up row for row with audit (same index), so bad.index picks out the original rows.
            # File row, status and reason come first, followed by the original columns.
            view = pd.concat([
                pd.DataFrame({"File row": bad["_row"].values, "Status": bad["_status"].str.capitalize().values,
                              "Reason": bad["_reason"].values}),
                src_df.loc[bad.index].reset_index(drop=True)], axis=1)
            st.dataframe(view, hide_index=True, width="stretch",
                         column_config={"Reason": st.column_config.TextColumn(width="large")})
    with tabs[4]:
        st.dataframe(pd.DataFrame([{
            "Field": f.header, "Built from": rec.mapping.get(f.name).summary(),
            "Option": option_text(rec.mapping.get(f.name), f), "Mapped by": origin_label(rec.mapping.get(f.name)),
        } for f in rec_schema.fields]), hide_index=True, width="stretch")
        if rec.mapping.column_notes or rec.mapping.general_note:
            st.markdown("**Notes on columns**")
            notes_rows = [{"Column": c, "Note": n} for c, n in rec.mapping.column_notes.items()]
            if rec.mapping.general_note:
                notes_rows.append({"Column": "(general)", "Note": rec.mapping.general_note})
            st.dataframe(pd.DataFrame(notes_rows), hide_index=True, width="stretch")
        # rec.overrides is {field: {original value: approved fix}}; count every approved fix across the fields.
        n_fix = sum(len(v) for v in rec.overrides.values())
        if n_fix:
            st.caption(f"{n_fix} value fix{'es' if n_fix != 1 else ''} approved from Claude suggestions were applied.")
        # Header row is stored 0-based (None = no header row) and shown 1-based, as on the New import page. Older
        # imports without a stored time zone are shown as Eastern.
        tz = rec.read_options.get("timezone")
        st.caption(f"Read options: sheet {rec.read_options.get('sheet') or '—'}, header row "
                   f"{'none' if rec.read_options.get('header_row') is None else rec.read_options['header_row'] + 1}"
                   f", client time zone {ZONE_LABEL.get(tz, tz or 'Eastern')}")

    st.divider()
    _import_check_section(rec, rec_schema, src_df, audit)
    a, b = st.columns([1, 3])
    with a:
        _rerun_button(rec)
    with b:
        _repair_section(rec, rec_schema, audit)
    if linked and not deleted:
        _delete_records_section(rec, linked)


def _delete_records_section(rec: ImportRecord, linked: dict) -> None:
    """Customer Records: delete this import's records (e.g. to fix the mapping and run it again).

    Deleting removes the records from every tag they are in (each tag's history says so). The import stays listed,
    marked "records deleted", and can then be re-run. Asks for a tick first.
    """
    with st.expander("Delete this import's records"):
        hit = records.db.fetch("""SELECT t.name, count(*) AS n FROM tag_records tr JOIN records r ON r.id = tr.record_id
                                  JOIN tags t ON t.id = tr.tag_id WHERE r.import_id = %s GROUP BY t.name""",
                               (linked["id"],))
        st.caption(f"Deletes the {linked['records']:,} records of this import"
                   + (" and takes them out of these tags: " + ", ".join(f"{h['name']} ({h['n']:,})" for h in hit)
                      if hit else "") + ". The import can then be run again with a corrected mapping.")
        sure = st.checkbox("Yes, delete these records", key=f"del_imp_ok_{rec.id}")
        if st.button("Delete records", disabled=not sure, key=f"del_imp_{rec.id}"):
            res = records.delete_import(linked["id"], user["username"])
            st.success(f"Deleted {res['records']:,} records.")
            st.rerun()


@st.cache_data(max_entries=6, show_spinner=False)
def _import_profiles(import_id: int, folder: str):
    """Return the column profiles (sample values, type guesses) of a finished import's source file.

    Needed by the mistake check on a finished import, whose New import session state (ss.profiles) is long gone.
    Inputs as for load_import_tables; returns a list of ColumnProfile. Cached for the same reason: the stored
    files don't change, and profiling a large file on every click would be slow.
    """
    src, _ = load_import_tables(import_id, folder)
    return profile_columns(src, n_samples=settings.ai_sample_values)


def _import_check_section(rec: ImportRecord, rec_schema: TargetSchema, src_df: pd.DataFrame,
                          audit: pd.DataFrame) -> None:
    """Show the "Check this import for mistakes" action on a finished import, and handle Claude's suggestions.

    Inputs: the import, its schema snapshot, and its source/audit tables (as from load_import_tables). Unlike the
    check on New import, the rule checks run over every row. Claude, when configured, reviews a sample of rows
    with phone digits partly masked unless AI_MASK_PHONES=false.

    The result is kept in ss["import_check_<id>"] so it stays visible while the user clicks around the page. If
    Claude proposes mapping changes, they are shown as a diff with "Re-run the import with these changes",
    which starts a new linked import with the changed mapping (via rerun_import). This import is left as it was.
    The button is disabled when the original file is no longer stored.
    """
    ckey = f"import_check_{rec.id}"
    c1, c2 = st.columns([1.3, 3.7], vertical_alignment="center")
    if c1.button("Check this import for mistakes", key=f"btn_{ckey}", width="stretch"):
        profiles = _import_profiles(rec.id, str(store.files_root / str(rec.id)))
        with st.spinner("Checking for mistakes" + (" (rules + Claude)…" if assistant else "…")):
            findings, ai_res, err = check_for_mistakes(
                rec_schema, rec.mapping, src_df, audit, profiles, assistant, use_claude=bool(assistant),
                mask_phones=settings.ai_mask_phones,
                timezone_label=ZONE_LABEL.get(rec.read_options.get("timezone"), ""),
                scope=f"all {len(audit):,} rows of a finished import")
        ss[ckey] = {"findings": findings, "ai": ai_res, "error": err}
    c2.caption("Rule checks over every row" + (" plus a Claude review of a sample of rows (phone digits partly "
                                               "hidden)." if assistant else ". Set ANTHROPIC_API_KEY to add a "
                                               "Claude review."))
    chk = ss.get(ckey)
    if chk is None:
        return
    if chk["error"]:
        st.warning(f"Claude's review didn't run ({chk['error']}). Rule checks are below.")
    if chk["ai"] and chk["ai"].summary:
        st.markdown(f"**Claude:** {chk['ai'].summary}")
    _findings_table(chk["findings"], rec_schema)
    if chk["ai"] and chk["ai"].mapping.fields:
        diff = mapping_diff(rec_schema, rec.mapping, chk["ai"].mapping)
        if not diff.empty:
            st.markdown("**Claude's suggested mapping changes**")
            st.dataframe(diff, hide_index=True, width="stretch")
            ok, why = can_rerun(rec)
            if not ok:
                st.caption(why)
            if st.button("Re-run the import with these changes", type="primary", disabled=not ok,
                         key=f"rerun_{ckey}"):
                # Same selection as Accept on New import: only fields that really change (matched by output
                # header, as shown in the diff), merged onto the old mapping so its notes and skipped columns stay.
                changed = Mapping(fields={k: v for k, v in chk["ai"].mapping.fields.items()
                                          if rec_schema.field(k).header in set(diff["Field"])})
                new_map = merge(rec.mapping, changed)
                open_import(rerun_import(rec, ai_used=True, mapping=new_map))


def _side_by_side(rec: ImportRecord, rec_schema: TargetSchema, src_df: pd.DataFrame, audit: pd.DataFrame,
                  plan) -> None:
    """The "Side by side" tab: original rows on the left, converted rows on the right, one page at a time.

    Inputs: the import, its schema snapshot, the source and audit tables lined up row for row
    (load_import_tables), and `plan`, the compare.ColumnPlan saying which source column feeds which field. The
    view can be filtered (all, rows with changes, problems, bad rows, duplicates, imported, empty), searched for
    a value or row number, and paged. The coloured HTML table itself comes from compare.build_compare_html.

    Widget keys include the import id, so each import keeps its own filter, search and page while the session
    lasts.
    """
    counts = status_counts(audit, rec_schema)
    c1, c2 = st.columns([3.2, 1.6], vertical_alignment="bottom")
    # Only offer filters that match something (with their counts); "All rows" and "Rows with changes" are always
    # shown so the control never looks empty. The user can deselect the control (None), which counts as "all".
    mode = c1.segmented_control(
        "Show", [k for k in FILTERS if counts.get(k, 0) or k in ("all", "changed")],
        format_func=lambda k: f"{FILTERS[k]} ({counts.get(k, 0):,})", default="all", key=f"cmp_mode_{rec.id}")
    search = c2.text_input("Find", placeholder="value or row number", key=f"cmp_q_{rec.id}")
    # Audit row indices to show, in file order.
    rows = filter_rows(audit, src_df, rec_schema, mode or "all", search)

    o1, o2, o3 = st.columns([1.6, 1, 2.4], vertical_alignment="bottom")
    show_was = o1.toggle("Show original under changed values", value=True, key=f"cmp_was_{rec.id}")
    per_page = o2.selectbox("Rows per page", [50, 100, 250], index=1, key=f"cmp_pp_{rec.id}")
    pages = max(1, -(-len(rows) // per_page))  # ceiling division; at least one page even with no rows
    # The page box's key includes the filter, search and page size, so changing any of them gives a fresh box that
    # starts on page 1. Otherwise a page number left over from before could be beyond the new last page.
    page = o3.number_input(f"Page (of {pages:,})", min_value=1, max_value=pages, value=1, step=1,
                           key=f"cmp_page_{rec.id}_{mode}_{search}_{per_page}")
    start = (page - 1) * per_page
    page_rows = rows[start:start + per_page]
    st.caption(f"Showing rows {start + 1 if page_rows else 0:,}–{start + len(page_rows):,} of {len(rows):,}. "
               "Left: the original file exactly as uploaded. Right: what was produced for the import. "
               "Headers show where each column went.")
    st.html(build_compare_html(src_df, audit, page_rows, rec_schema, plan, file_label=f"Original · {rec.filename}",
                               target_label=f"Converted · {rec.schema_label}", show_was=show_was,
                               dark=current_theme() == "dark"))


def _repair_section(rec: ImportRecord, rec_schema: TargetSchema, audit: pd.DataFrame) -> None:
    """Show "Try to fix N unconvertible values with Claude" on a finished import (if Claude is set up and needed).

    Works from rec.summary["failed_values"] ({field name: unique original values that couldn't be converted}),
    limited to fields that exist in this import's schema. "Suggest fixes" asks Claude, field by field, how each
    value should be *reformatted*, giving up to 5 values that did convert as examples. Every suggestion is then
    re-validated by validate_repairs, and anything that adds or changes data (rather than just reformatting it) is
    refused. Values Claude didn't answer are listed as "can't be fixed".

    The proposals are kept in ss["repairs_<id>"]. "Apply … and re-run" starts a new linked import with the
    accepted fixes as overrides ({field: {original: fixed}}) and opens it; this import is never changed, so
    every fix stays traceable.
    """
    # Excel error codes (#REF!, #VALUE!…) are left out: they hold no data, so there is nothing to reformat.
    failed = {k: kept for k, v in rec.summary.get("failed_values", {}).items()
              if k in rec_schema.field_names and (kept := [x for x in v if not sheet_error(x)])}
    if not failed or not assistant:
        return
    n_vals = sum(len(v) for v in failed.values())
    pkey = f"repairs_{rec.id}"
    with st.expander(f"Try to fix {n_vals:,} unconvertible value{'s' if n_vals != 1 else ''} with Claude"):
        st.caption("Claude only reformats values (e.g. 'Jan 5th 24', '516 5OO 0100'). Every suggestion is re-checked "
                   "and suggestions that add or change data are refused. Approved fixes run as a new import.")
        if st.button("Suggest fixes", key=f"suggest_{rec.id}"):
            proposals = {}
            # One try around the whole loop: if a Claude call fails part-way, nothing is stored, and any earlier
            # proposals in session state stay as they were.
            try:
                with st.spinner("Claude is reviewing the values…"):
                    for fname, values in failed.items():
                        f = rec_schema.field(fname)
                        # Up to 5 converted values of this field, so Claude can see the expected output style.
                        good = [v for v in audit.loc[audit["_status"] == "imported", f"out.{fname}"].tolist() if v][:5]
                        pairs, _ = assistant.repair_values(f, values, good)
                        # accepted: {original: final converted value}; rejected: [(original, suggestion, why)].
                        accepted, rejected = validate_repairs(rec_schema, rec.mapping, fname, pairs,
                                                              timezone=rec.read_options.get("timezone"))
                        # Values Claude made a suggestion for (good or refused); the rest got no suggestion.
                        fixed = set(accepted) | {r[0] for r in rejected}
                        proposals[fname] = {"accepted": accepted, "rejected": rejected,
                                            "unfixable": [v for v in values if v not in fixed]}
                ss[pkey] = proposals
            except AIError as exc:
                st.error(str(exc))
        props = ss.get(pkey)
        if props:
            rows = []
            for fname, p in props.items():
                h = rec_schema.field(fname).header
                rows += [{"Field": h, "Original": a, "Fix": b, "Status": "✓ will apply"} for a, b in p["accepted"].items()]
                rows += [{"Field": h, "Original": a, "Fix": b, "Status": f"✗ refused: {why}"}
                         for a, b, why in p["rejected"]]
                rows += [{"Field": h, "Original": a, "Fix": "", "Status": "can't be fixed"} for a in p["unfixable"]]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            n_ok = sum(len(p["accepted"]) for p in props.values())
            ok, why = can_rerun(rec)
            if n_ok and not ok:
                st.caption(why)
            if n_ok and st.button(f"Apply {n_ok} fix{'es' if n_ok != 1 else ''} and re-run", type="primary",
                                  key=f"apply_{rec.id}", disabled=not ok):
                new_id = rerun_import(rec, overrides={k: p["accepted"] for k, p in props.items() if p["accepted"]},
                                      ai_used=True)
                # Drop the proposals before leaving (open_import ends the run), so coming back to this import
                # doesn't offer the same fixes again.
                ss.pop(pkey, None)
                open_import(new_id)


# ---------------------------------------------------------------------------
# Page: Number history
# ---------------------------------------------------------------------------

# How a recorded consent value reads in the Number history table.
CONSENT_TEXT = {"true": "true — subscribed", "false": "false — opted out", "": "blank — no change"}


def page_numbers() -> None:
    """Page "Number history": every import a phone number was converted in, and where its consent came from.

    Reads the record kept by converter.numberlog (table number_log), which stays after each import's files are
    deleted (FILE_RETENTION_DAYS), for NUMBER_RECORD_DAYS (default forever). The typed number is read like a phone
    cell ("(516) 555-0177", "+1 516.555.0177"). One table row per import: when and by whom, the file and its row,
    the number and consent as written in the file, the consent sent to the import and the column it came from.
    Selecting a row opens the import; the table can be downloaded as CSV (e.g. to answer a complaint).
    """
    st.header("Number history")
    st.caption("Look up a phone number: every import it was converted in, the file and row it came from, and the "
               "consent value sent with it. Kept after the import's files are deleted"
               + (f" (for {settings.number_record_days} days)." if settings.number_record_days > 0 else "."))
    text = st.text_input("Phone number", placeholder="(516) 555-0177", key="num_search")
    if not text.strip():
        return
    res = number_lookup(store, text, schema)
    if res.error:
        st.error(f"'{text.strip()}' can't be looked up: {res.error}.")
        return
    if not res.entries:
        st.info(f"**{res.number}** isn't in any recorded import. Imports are recorded since version 0.21.0 (earlier "
                "ones too, if their files hadn't been deleted yet).")
        return
    st.markdown(f"**{res.number}** — converted in {len(res.entries):,} import"
                f"{'s' if len(res.entries) != 1 else ''} (newest first).")
    table = pd.DataFrame([{
        "Imported": fmt_dt(e["imported_at"], TZ), "#": e["import_id"], "File": e["filename"],
        "Sheet": e["sheet"], "Row": e["file_row"], "Number in file": e["phone_value"],
        "Consent sent": CONSENT_TEXT.get(e["consent"], e["consent"]), "Consent in file": e["consent_value"],
        "Consent from": e["consent_from"] or "not mapped", "By": e["username"],
    } for e in res.entries])
    if not table["Sheet"].astype(bool).any():
        table = table.drop(columns="Sheet")
    st.caption("Select a row to open the import.")
    event = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                         key="numbers_table",
                         column_config={"#": st.column_config.NumberColumn(width="small", format="%d"),
                                        "Row": st.column_config.NumberColumn(width="small", format="%d")})
    # The CSV keeps exact UTC times; cells are neutralised so a spreadsheet never runs one as a formula.
    csv = pd.DataFrame(res.entries).rename(columns={"imported_at": "imported_at_utc"})
    csv.insert(0, "number", res.number)
    st.download_button("⬇ Download (CSV)", neutralise_csv(csv.to_csv(index=False).encode("utf-8-sig")),
                       f"number_history_{res.number}.csv", mime="text/csv")
    sel = event.selection.rows if event and event.selection else []
    if sel:
        open_import(int(table.iloc[sel[0]]["#"]))


# ---------------------------------------------------------------------------
# Pages: templates, target format, users
# ---------------------------------------------------------------------------

def page_templates() -> None:
    """Page "Templates": list the saved column mappings and rename, delete or download one.

    Only templates made for the current target schema are listed. Choosing one shows its columns and how each
    field is mapped. Rename and delete write to the database straight away. Deleting is limited to the person
    who created the template, or an admin. The JSON download is a read-only export, for reference or backup.
    """
    st.header("Saved templates")
    st.caption("A template remembers how one file layout maps to the record fields. When a file with the same "
               "columns is uploaded, its template is applied automatically (the business and service are chosen "
               "for each import).")
    templates = store.list_templates(schema.name)
    if not templates:
        st.info("No templates yet. They are saved when you start an import with 'Remember this mapping' on.")
        return
    st.dataframe(pd.DataFrame([{
        "Name": t.name, "Columns": len(t.headers), "Times used": t.use_count, "Created by": t.created_by,
        "Updated": fmt_dt(t.updated_at, TZ), "Updated by": t.updated_by,
    } for t in templates]), hide_index=True, width="stretch")

    name = st.selectbox("Template", [t.name for t in templates])
    t = next(x for x in templates if x.name == name)
    st.markdown("**Columns in this layout:** " + ", ".join(f"`{h}`" for h in t.headers))
    st.dataframe(pd.DataFrame([{
        "Field": f.header, "Mapping": t.mapping.get(f.name).summary(), "Option": option_text(t.mapping.get(f.name), f)
    } for f in schema.fields]), hide_index=True, width="stretch")

    c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
    new_name = c1.text_input("Rename to", value=t.name)
    # The button comes first in the condition so it is always drawn; the rename only happens for a non-empty name
    # that is actually different.
    if c2.button("Rename", width="stretch") and new_name.strip() and new_name.strip() != t.name:
        # A name that's already taken fails on the database's UNIQUE constraint and is shown as an error.
        # st.rerun() isn't swallowed by `except Exception`: Streamlit's rerun signal derives from BaseException.
        try:
            store.rename_template(t.name, new_name)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    can_delete = user["is_admin"] or user["username"] == t.created_by
    if c3.button("Delete", width="stretch", disabled=not can_delete,
                 help=None if can_delete else "Only the creator or an admin can delete"):
        store.delete_template(t.name)
        st.rerun()
    st.download_button("Download template (JSON)", json.dumps({
        "name": t.name, "headers": t.headers, "mapping": t.mapping.model_dump(), "read_options": t.read_options,
        "schema": t.schema_name}, indent=2), f"template_{t.name}.json", mime="application/json", on_click="ignore")


def page_schema() -> None:
    """Page "Fields": the record fields every import is mapped to, and the import format tags export into.

    Both are read-only descriptions of their YAML files (schemas/records_schema.yaml, schemas/target_schema.yaml):
    each field in order (type, required, format, what happens to invalid values, special rules). To change them,
    edit the YAML on the server; a new record field also needs a restart (its database column is added then).
    """
    st.header("Fields")
    st.markdown(f"**Record fields: {schema.label}** — {schema.description}")
    st.caption(f"Defined in `{rsettings.records_schema_path}`. Every column of an imported file is also kept as "
               "written (searchable as *Column: …* on Find). A new field needs a restart of the app.")
    _schema_table(schema, record_fields=True)
    st.markdown(f"**Tag export format: {export_schema.label}** — {export_schema.description}")
    st.caption(f"Defined in `{rsettings.export_schema_path}` (the file converter's import format). A tag exports "
               "one row per unique phone number; consent is left blank for now.")
    _schema_table(export_schema, record_fields=False)
    o = export_schema.output
    # repr() makes invisible delimiters readable (a tab shows as '\t').
    st.markdown(f"**Export file:** {o.format.upper()}"
                + (f", delimiter `{repr(o.delimiter)}`, encoding {o.encoding}" if o.format == "csv" else "")
                + (", header row included" if o.include_header else ", no header row"))
    # The whole f-string is the "if" branch of the conditional expression; FILE_RETENTION_DAYS=0 or less means
    # files are never purged.
    st.markdown(f"**Stored files:** each import's original file and side-by-side data are kept for "
                f"{settings.file_retention_days} days (the records stay)" if settings.file_retention_days > 0
                else "**Stored files:** kept indefinitely")


def _schema_table(sch: TargetSchema, record_fields: bool) -> None:
    """One schema's fields as a table (see page_schema)."""
    def _notes(f) -> str:
        """Return the plain-English "Notes" cell for one schema field (FieldSpec `f`).

        Spells out the rules that aren't obvious from the other columns: a locked always-blank field (consent),
        the year written when a date has no year (no_year_value, e.g. 1600 for birthday/anniversary), the 12:00 AM
        given to date-only values in datetime fields, and the phone countries accepted. Rules are joined with "; ".
        """
        if f.fixed_blank:
            return "🔒 always blank on every row — can't be mapped or edited"
        bits = ["✋ mapped by hand only (never by rules, templates or Claude); blank unless you map it"] \
            if f.manual_only else []
        if f.no_year_value is not None:
            bits.append(f"month/day with no year → year {f.no_year_value}")
        if f.type == "datetime" and f.format:
            bits.append("date-only values get 12:00 AM")
        if f.type == "phone":
            bits.append("allowed: " + ", ".join(f.countries))
            if record_fields:
                bits.append("every different number of a row is kept on its record")
        if f.creation_date:
            bits.append("sign-up / creation date; stands in for the last activity in exports of files without one")
        if not record_fields and f.name == "consent":
            bits.append("blank in tag exports for now")
        return "; ".join(bits)

    st.dataframe(pd.DataFrame([{
        "#": i + 1, ("Field" if record_fields else "Output header"): f.header,
        "Type": "always blank" if f.fixed_blank else f.type,
        "Required": "yes" if f.required else "", "Format": f.format or "",
        "If invalid": "" if f.fixed_blank else ("reject row" if f.on_invalid == "reject" else "leave blank"),
        "Notes": _notes(f),
    } for i, f in enumerate(sch.fields)]), hide_index=True, width="stretch")


def page_users() -> None:
    """Page "Users" (admins only): list accounts, add a user, change a password or admin flag, delete a user.

    The page is only added to the navigation for admins, so other users can't reach it, not even by URL. All
    changes are written to the users table straight away. The Store validates input (e.g. passwords need 8+
    characters, usernames must be unique) and raises; those errors are shown on the form. Each successful change
    reruns the page so the table above shows it.
    """
    st.header("Users")
    users = store.list_users()
    st.dataframe(pd.DataFrame([{
        "User": u["username"], "Admin": "yes" if u["is_admin"] else "", "Created": fmt_dt(u["created_at"], TZ),
        "Last sign-in": fmt_dt(u["last_login"], TZ)} for u in users]), hide_index=True, width="stretch")
    c1, c2 = st.columns(2)
    # Forms: widgets inside don't cause reruns while being filled in; everything is submitted at once.
    with c1, st.form("add_user", clear_on_submit=True):
        st.markdown("**Add user**")
        u = st.text_input("Username")
        p = st.text_input("Temporary password (8+ characters)", type="password")
        a = st.checkbox("Administrator")
        if st.form_submit_button("Add user", type="primary"):
            try:
                store.add_user(u, p, a)
                # Only flashes briefly: the rerun right after it redraws the page (with the new user in the table).
                st.success(f"Added {u}.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    with c2, st.form("manage_user"):
        st.markdown("**Manage user**")
        names = [x["username"] for x in users]
        target = st.selectbox("User", names)
        newpw = st.text_input("New password (leave empty to keep)", type="password")
        # Gotcha: inside a form, picking a different user doesn't rerun the page, so until the form is submitted
        # this checkbox still shows the admin flag of the user who was selected when the page was drawn.
        make_admin = st.checkbox("Administrator", value=bool(next((x["is_admin"] for x in users
                                                                  if x["username"] == target), False)))
        delete = st.checkbox("Delete this user")
        if st.form_submit_button("Save"):
            try:
                if delete:
                    # Deleting your own account isn't allowed, so an admin can't lock themselves out by accident.
                    if target == user["username"]:
                        st.error("You can't delete your own account.")
                    else:
                        store.delete_user(target)
                else:
                    if newpw:
                        store.set_password(target, newpw)
                    store.set_admin(target, make_admin)
                # Note: this also runs after the "can't delete your own account" error, so that message is only
                # visible for a moment.
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


# ---------------------------------------------------------------------------
# Pages: Find, Tags, Businesses, Services (Customer Records; drawn by records/ui.py)
# ---------------------------------------------------------------------------

def page_find() -> None:
    """Page "Find": find requests, the found set, tagging it (records.ui.page_find)."""
    records_ui.page_find(ctx)


def page_tags() -> None:
    """Page "Tags": the tags, one tag (?tag=<id>) and its export (records.ui.page_tags)."""
    records_ui.page_tags(ctx)


def page_businesses() -> None:
    """Page "Businesses": add and correct businesses (records.ui.page_businesses)."""
    records_ui.page_businesses(ctx)


def page_services() -> None:
    """Page "Services": the services imports pick from (records.ui.page_services)."""
    records_ui.page_services(ctx)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

# Runs on every rerun, after login. The pages are declared here, at the end of the module, because st.Page needs
# the page functions defined above; helpers such as open_import() refer to PAGE_IMPORTS only when they're called,
# by which time it exists.
_landing = os.getenv("DEFAULT_PAGE", "new")  # "new", "imports" or "find": which page opens first
# What records/ui.py's pages need from here (the pages themselves are filled in below, once they exist).
ctx = records_ui.Ctx(db=rdb, records=records, schema=schema, export=export_schema, user=user, tz=TZ,
                     open_import=open_import)
# url_path is the address of each page (e.g. /imports?id=12), so links and bookmarks keep working.
PAGE_NEW = st.Page(page_new_import, title="New import", icon="⬆️", url_path="new",
                   default=_landing not in ("imports", "find"))
PAGE_IMPORTS = st.Page(page_imports, title="Imports", icon="📋", url_path="imports", default=_landing == "imports")
PAGE_FIND = st.Page(page_find, title="Find", icon="🔎", url_path="find", default=_landing == "find")
PAGE_TAGS = st.Page(page_tags, title="Tags", icon="🏷️", url_path="tags")
PAGE_BUSINESSES = st.Page(page_businesses, title="Businesses", icon="🏪", url_path="businesses")
PAGE_SERVICES = st.Page(page_services, title="Services", icon="🧾", url_path="services")
PAGE_TEMPLATES = st.Page(page_templates, title="Templates", icon="🧩", url_path="templates")
PAGE_SCHEMA = st.Page(page_schema, title="Fields", icon="🎯", url_path="fields")
PAGE_USERS = st.Page(page_users, title="Users", icon="👥", url_path="users")
ctx.pages.update(find=PAGE_FIND, tags=PAGE_TAGS, imports=PAGE_IMPORTS)

# The Users page is only registered for admins, and only while logins are on (AUTH_MODE=password): without logins
# accounts do nothing. That is the access control for it: a page that isn't passed to st.navigation can't be
# opened, even by typing its URL. The converter's Number history page isn't registered: records replace it.
logins_on = settings.auth_mode != "none"
nav = st.navigation({"Records": [PAGE_FIND, PAGE_TAGS, PAGE_NEW, PAGE_IMPORTS],
                     "Setup": [PAGE_BUSINESSES, PAGE_SERVICES, PAGE_TEMPLATES, PAGE_SCHEMA]
                     + ([PAGE_USERS] if user["is_admin"] and logins_on else [])})

# Sidebar, drawn on every page under the navigation menu.
with st.sidebar:
    # A static count, refreshed whenever the page reruns (it doesn't poll on its own).
    running = store.active_imports()
    if running:
        st.caption(f"🔵 {len(running)} import{'s' if len(running) != 1 else ''} in progress")
    st.caption(f"{records.totals()['records']:,} records")
    for w in db_warnings:
        st.caption("⚠️ " + w)
    st.divider()
    # With AUTH_MODE=none there are no accounts (everyone is the local admin): no name, password or sign-out.
    if logins_on:
        st.caption(f"Signed in as **{user['username']}**" + (" (admin)" if user["is_admin"] else ""))
        with st.expander("My account"):
            with st.form("pw_self", clear_on_submit=True):
                p1 = st.text_input("New password", type="password")
                if st.form_submit_button("Change password"):
                    try:
                        store.set_password(user["username"], p1)
                        st.success("Changed.")
                    except Exception as exc:
                        st.error(str(exc))
            # logout() clears the whole session state (the user and any New import work in progress); the rerun
            # then lands on the login form.
            if st.button("Sign out"):
                logout()
                st.rerun()
    st.caption("Claude: " + (f"connected ({settings.mapping_model})" if assistant else "not configured"))
    st.caption(f"Customer Records v{__version__} · conversion engine: file-converter {ENGINE_VERSION}")

# Light/dark toggle, fixed at the top right of every page (drawn before the page so it's there while it loads).
theme_toggle()

# Run the page function for the current URL (or the default page). This is where the page content is drawn.
nav.run()
