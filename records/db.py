"""PostgreSQL: the connection pool and the tables of Customer Records.

Overview
--------
:class:`Database` owns a small connection pool (psycopg 3) and creates what the app needs when it starts
(:meth:`Database.setup`): the ``pg_trgm`` extension when the server allows it (fast "contains" and word searches),
the fixed tables, and one column per record field of ``schemas/records_schema.yaml``. Adding a field to that file
adds its column (and index) on the next start; nothing is ever dropped or retyped automatically.

Tables
------
==================  ===============================================================================================
``businesses``      id (the same ID as in the main system, typed in by hand), name, notes
``services``        the list chosen from on every import; deactivated ones stay on their records
``imports``         one per file imported: business, service, file name and SHA-1, who, when, status, counts, the
                    file's column names, and ``first_visit_as`` (see records.importer); ``job_id`` is the import's
                    number in the app (data/converter.db), whose page shows the side-by-side view
``records``         one per file row: business, service, import, file row, a column per record field (phone
                    numbers apart), ``phone_issue`` (why there is no usable number), ``issues`` (values that couldn't
                    be read), ``source`` (the row exactly as in the file, every column) and ``search_text``
``record_phones``   every usable number of a record (10 digits, phone columns' order), ``repaired``, ``original``
``tags``            a named list for one business; ``tag_records`` its records; ``tag_events`` what was done to it
``app_meta``        small key/value facts (e.g. whether pg_trgm is available)
==================  ===============================================================================================

Deleting an import deletes its records, which removes them from tags (``ON DELETE CASCADE``); the store logs that
on the tags concerned. A business ID corrected later follows into every table (``ON UPDATE CASCADE``).

Column types per record field type: date → date, datetime → timestamp (as written in the client's time zone, no
zone), integer → bigint, decimal → numeric, boolean → boolean, everything else → text. Phone fields have no column:
their numbers are rows of ``record_phones``.
"""

from __future__ import annotations

import atexit
import logging
import re
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from converter.schema import FieldSpec, TargetSchema

log = logging.getLogger(__name__)

# Column type of each record field type (phone fields have no column; see record_phones).
COLUMN_TYPES = {"date": "date", "datetime": "timestamp", "integer": "bigint", "decimal": "numeric",
                "boolean": "boolean"}
# Columns of `records` that aren't record fields; a field may not use these names.
RESERVED = {"id", "import_id", "business_id", "service_id", "file_row", "phone_issue", "issues", "source",
            "search_text", "created_at"}
# Field names become column names: lowercase letters, digits and underscores, starting with a letter.
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,50}$")
# Text types that get a trigram index (word and "contains" searches); others are matched on exact values.
TRIGRAM_TYPES = {"string", "email"}

_TABLES = """
CREATE TABLE IF NOT EXISTS app_meta (key text PRIMARY KEY, value text NOT NULL);
CREATE TABLE IF NOT EXISTS businesses (
    id text PRIMARY KEY CHECK (btrim(id) <> ''),
    name text NOT NULL CHECK (btrim(name) <> ''),
    notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS services (
    id serial PRIMARY KEY,
    name text NOT NULL CHECK (btrim(name) <> ''),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS services_name ON services (lower(name));
CREATE TABLE IF NOT EXISTS imports (
    id bigserial PRIMARY KEY,
    job_id bigint,
    business_id text NOT NULL REFERENCES businesses (id) ON UPDATE CASCADE,
    service_id int NOT NULL REFERENCES services (id),
    file_name text NOT NULL,
    file_sha1 text NOT NULL,
    sheet text,
    username text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'queued',
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    records int NOT NULL DEFAULT 0,
    phones int NOT NULL DEFAULT 0,
    no_phone int NOT NULL DEFAULT 0,
    first_visit_as text,
    columns jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS imports_job ON imports (job_id);
CREATE INDEX IF NOT EXISTS imports_business ON imports (business_id, file_sha1);
CREATE TABLE IF NOT EXISTS records (
    id bigserial PRIMARY KEY,
    import_id bigint NOT NULL REFERENCES imports (id) ON DELETE CASCADE,
    business_id text NOT NULL REFERENCES businesses (id) ON UPDATE CASCADE,
    service_id int NOT NULL REFERENCES services (id),
    file_row int NOT NULL,
    phone_issue text NOT NULL DEFAULT '',
    issues jsonb NOT NULL DEFAULT '{}'::jsonb,
    source jsonb NOT NULL DEFAULT '{}'::jsonb,
    search_text text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS records_import ON records (import_id);
CREATE INDEX IF NOT EXISTS records_business ON records (business_id, service_id);
CREATE INDEX IF NOT EXISTS records_service ON records (service_id);
CREATE TABLE IF NOT EXISTS record_phones (
    record_id bigint NOT NULL REFERENCES records (id) ON DELETE CASCADE,
    phone text NOT NULL,
    position smallint NOT NULL,
    repaired boolean NOT NULL DEFAULT false,
    original text NOT NULL DEFAULT '',
    PRIMARY KEY (record_id, phone)
);
CREATE INDEX IF NOT EXISTS record_phones_phone ON record_phones (phone);
CREATE TABLE IF NOT EXISTS tags (
    id serial PRIMARY KEY,
    business_id text NOT NULL REFERENCES businesses (id) ON UPDATE CASCADE,
    name text NOT NULL CHECK (btrim(name) <> ''),
    notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS tags_name ON tags (business_id, lower(name));
CREATE TABLE IF NOT EXISTS tag_records (
    tag_id int NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    record_id bigint NOT NULL REFERENCES records (id) ON DELETE CASCADE,
    added_at timestamptz NOT NULL DEFAULT now(),
    added_by text NOT NULL DEFAULT '',
    PRIMARY KEY (tag_id, record_id)
);
CREATE INDEX IF NOT EXISTS tag_records_record ON tag_records (record_id);
CREATE TABLE IF NOT EXISTS tag_events (
    id bigserial PRIMARY KEY,
    tag_id int NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    at timestamptz NOT NULL DEFAULT now(),
    username text NOT NULL DEFAULT '',
    action text NOT NULL,
    count int NOT NULL DEFAULT 0,
    detail text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tag_events_tag ON tag_events (tag_id, at);
"""


class DatabaseError(RuntimeError):
    """The database can't be reached or set up; the message says what to do (shown on the page)."""


def column_type(f: FieldSpec) -> str | None:
    """The PostgreSQL type of record field ``f``'s column, or None for a phone field (no column)."""
    if f.type == "phone":
        return None
    return COLUMN_TYPES.get(f.type, "text")


def check_fields(schema: TargetSchema) -> None:
    """Refuse record fields that can't become columns: bad names, reserved names, more than one phone field.

    Raises ValueError naming the field. Called by :meth:`Database.setup` before anything is created.
    """
    phones = [f.name for f in schema.fields if f.type == "phone"]
    if len(phones) > 1:
        raise ValueError(f"only one phone field is supported (found {', '.join(phones)})")
    for f in schema.fields:
        if not FIELD_NAME_RE.match(f.name):
            raise ValueError(f"field name '{f.name}': use lowercase letters, digits and underscores")
        if f.name in RESERVED:
            raise ValueError(f"field name '{f.name}' is used by the records table itself; choose another name")


def ensure_database(url: str) -> None:
    """Create the database named in ``url`` when it doesn't exist yet (needs the right to create databases).

    Connects to the server's ``postgres`` database to do so. Raises :class:`DatabaseError` with advice when the
    server can't be reached at all.
    """
    params = conninfo_to_dict(url)
    name = params.get("dbname")
    try:
        psycopg.connect(url, connect_timeout=5).close()
        return
    except psycopg.OperationalError as exc:
        if not name or f'database "{name}" does not exist' not in str(exc):
            raise DatabaseError(_advice(url, exc)) from exc
    admin = make_conninfo(url, dbname="postgres")
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
            try:
                # ICU rules for upper/lower case, so "åberg" finds Åberg whatever the server's own locale (Homebrew's
                # is C, which only folds A-Z). PostgreSQL 15+ built with ICU (Homebrew, Docker, Debian are).
                conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LOCALE_PROVIDER icu "
                                     "ICU_LOCALE 'und' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(name)))
            except psycopg.Error:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        log.info("Created database %s", name)
    except psycopg.Error as exc:
        raise DatabaseError(f"The database '{name}' doesn't exist and couldn't be created ({exc}). Create it "
                            f"with: createdb {name}") from exc


def _advice(url: str, exc: Exception) -> str:
    """A readable message for a failed connection, with the usual fixes (Mac first)."""
    safe = make_conninfo(url, password="***") if conninfo_to_dict(url).get("password") else url
    return (f"Can't connect to PostgreSQL ({safe}): {str(exc).strip().splitlines()[0]}. Is PostgreSQL running? "
            "On a Mac: brew install postgresql@17 && brew services start postgresql@17. Elsewhere, set "
            "DATABASE_URL in .env (see README → Database).")


_OPEN: set["Database"] = set()


def close_all() -> None:
    """Close every pool still open (at exit, and in tests before the app's cached resources are dropped).

    An open pool's threads otherwise hold up the end of the process for several seconds.
    """
    for db in list(_OPEN):
        db.close()


atexit.register(close_all)


class Database:
    """The connection pool and table setup. One per server process (the web app caches it)."""

    def __init__(self, url: str, min_size: int = 1, max_size: int = 8):
        """Open the pool (connections are made lazily, at most ``max_size`` at a time)."""
        self.url = url
        self.pool = ConnectionPool(url, min_size=min_size, max_size=max_size, open=True,
                                   kwargs={"row_factory": dict_row}, timeout=15)
        self.trigram = False
        # None: the database's own rules fold accented letters (Å/å). Otherwise the collation finds must use for
        # that (records.criteria.Params), chosen by setup().
        self.collation: str | None = None
        _OPEN.add(self)

    def close(self) -> None:
        """Close every connection of the pool."""
        _OPEN.discard(self)
        self.pool.close()

    @contextmanager
    def conn(self) -> Iterator[psycopg.Connection]:
        """A pooled connection inside a transaction: committed when the block ends, rolled back on an error."""
        with self.pool.connection() as c:
            yield c

    def fetch(self, query, params=None) -> list[dict]:
        """Run a query and return every row as a dict."""
        with self.conn() as c:
            return c.execute(query, params).fetchall()

    def fetch_one(self, query, params=None) -> dict | None:
        """Run a query and return its first row (or None)."""
        with self.conn() as c:
            return c.execute(query, params).fetchone()

    def execute(self, query, params=None) -> int:
        """Run a statement and return the number of rows it touched."""
        with self.conn() as c:
            return c.execute(query, params).rowcount

    def setup(self, schema: TargetSchema) -> list[str]:
        """Create the extension, tables, record-field columns and indexes that are missing. Returns warnings.

        Safe to run on every start (everything is IF NOT EXISTS). A field whose column already exists with another
        type is reported, not changed: retyping a column holding data is a decision for a person (KNOWN_ISSUES).
        """
        check_fields(schema)
        warnings: list[str] = []
        with self.conn() as c:
            # One setup at a time, also across several app processes sharing the database.
            c.execute("SELECT pg_advisory_xact_lock(4210551)")
            try:
                with c.transaction():
                    c.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                self.trigram = True
            except psycopg.Error as exc:
                self.trigram = bool(c.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'").fetchone())
                if not self.trigram:
                    warnings.append("The pg_trgm extension isn't available, so text searches don't use an index "
                                    f"(slower on big databases): {str(exc).splitlines()[0]}")
            c.execute(_TABLES)
            existing = {r["column_name"]: r["data_type"] for r in c.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'records'").fetchall()}
            mismatched = set()
            for f in schema.fields:
                ctype = column_type(f)
                if ctype is None:
                    continue
                if f.name in existing:
                    want = {"timestamp": "timestamp without time zone"}.get(ctype, ctype)
                    if existing[f.name] != want:
                        mismatched.add(f.name)
                        warnings.append(f"Column '{f.name}' is {existing[f.name]} in the database but the field is "
                                        f"{f.type}; values may not be stored. See KNOWN_ISSUES.")
                    continue
                c.execute(sql.SQL("ALTER TABLE records ADD COLUMN IF NOT EXISTS {} {}").format(
                    sql.Identifier(f.name), sql.SQL(ctype)))
                log.info("Added column records.%s (%s)", f.name, ctype)
            for f in schema.fields:
                # A column whose type doesn't match the field keeps the index it has (see the warning above).
                if column_type(f) is None or f.name in mismatched:
                    continue
                index = sql.Identifier(f"records_f_{f.name}")
                if f.type in TRIGRAM_TYPES and self.trigram:
                    c.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON records USING gin ({} gin_trgm_ops)").format(
                        index, sql.Identifier(f.name)))
                else:
                    c.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON records ({})").format(
                        index, sql.Identifier(f.name)))
            if self.trigram:
                c.execute("CREATE INDEX IF NOT EXISTS records_search ON records USING gin (search_text gin_trgm_ops)")
                c.execute("CREATE INDEX IF NOT EXISTS record_phones_trgm ON record_phones USING gin "
                          "(phone gin_trgm_ops)")
            c.execute("INSERT INTO app_meta (key, value) VALUES ('trigram', %s) ON CONFLICT (key) DO UPDATE "
                      "SET value = excluded.value", ("yes" if self.trigram else "no",))
            self.collation, note = _case_collation(c)
            if note:
                warnings.append(note)
        return warnings


# Collations that fold upper/lower case of accented letters, tried in this order when the database's own don't.
CASE_COLLATIONS = ["und-x-icu", "pg_c_utf8", "C.utf8", "C.UTF-8", "en_US.utf8", "en_US.UTF-8", "en-x-icu"]


def _case_collation(c: psycopg.Connection) -> tuple[str | None, str]:
    """(collation for case-insensitive finds, warning). None = the database's own rules already fold Å/å.

    A database created with the C locale (Homebrew's default, and databases made by hand) only folds A-Z, so
    "åberg" wouldn't find Åberg. Then finds use an explicit collation; that works but text searches can't use their
    index (slower on very large databases), which the warning says. Databases created by the app use ICU.
    """
    if c.execute("SELECT 'Åberg' ~* 'åb' AND lower('MÓR') = 'mór' AS ok").fetchone()["ok"]:
        return None, ""
    have = {r["collname"] for r in c.execute("SELECT collname FROM pg_collation").fetchall()}
    for name in CASE_COLLATIONS:
        if name not in have:
            continue
        try:
            with c.transaction():
                ok = c.execute(sql.SQL("SELECT ('Åberg' COLLATE {n}) ~* 'åb' AND lower('MÓR' COLLATE {n}) = 'mór' "
                                       "AS ok").format(n=sql.Identifier(name))).fetchone()["ok"]
        except psycopg.Error:
            continue
        if ok:
            return name, (f"The database's locale doesn't fold accented letters, so finds use the {name} collation "
                          "(correct, but text searches can't use their index). See KNOWN_ISSUES.")
    return None, ("The database can't match upper and lower case of accented letters (Å/å): such finds are case "
                  "sensitive. See KNOWN_ISSUES.")
