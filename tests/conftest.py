"""Shared pytest setup: import paths, the schemas, the generated messy sample files, and PostgreSQL.

Most converter test modules run against one of two schemas: the example contact schema (generic features such as
names, addresses and E.164 phones) or the real target import format that the converted file is uploaded with. The
sample files are regenerated for each test session, so the tests check exactly what scripts/make_sample_files.py
produces and not a copy someone edited by hand.

Customer Records' tests (test_records_*.py, test_drafts.py) need PostgreSQL. ``pg_server`` uses
RECORDS_TEST_DATABASE_URL when it is set (a server where the user may create databases), otherwise it starts a
private server for the session in a temporary folder, with PostgreSQL's own programs (``initdb``/``pg_ctl``) found
on the PATH or in the usual places (Homebrew, Postgres.app, Debian/Ubuntu). Without either, those tests are skipped
with a message. Each test using ``rdb`` gets a fresh database, set up from schemas/records_schema.yaml.
"""

import glob
import itertools
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Make `converter` importable without installing the package, and expose scripts/ so the sample-file generator
# can be imported as a plain module. This has to happen before the two imports below (hence the noqa).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from converter.schema import load_schema  # noqa: E402
import make_sample_files  # noqa: E402


@pytest.fixture(scope="session")
def schema():
    """Example contact schema with names/addresses — exercises the generic features.

    Loaded once per session from tests/fixtures. It has a required E.164 phone and dedupes on phone + signup
    date, so generic behaviour is tested without depending on the real import format.
    """
    return load_schema(ROOT / "tests" / "fixtures" / "example_contact_schema.yaml")


@pytest.fixture(scope="session")
def real_schema():
    """The real target import format.

    schemas/target_schema.yaml: 10-digit US phone_number, consent (only mapped by a person), six activity
    timestamps and birthday/anniversary with 1600 for "no year". Tests using it lock in what the import receives.
    """
    return load_schema(ROOT / "schemas" / "target_schema.yaml")


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    """Folder with freshly generated messy sample files (title rows, cp1252, semicolons, Excel quirks, ...).

    Built once per session in a temp folder, so tests never depend on or modify the files in sample_data/.
    """
    return make_sample_files.make_all(tmp_path_factory.mktemp("samples"))


def read_sample(samples, name, **kw):
    """Read one generated sample through the real reader, as if the user had uploaded it.

    Keyword arguments are reader options such as sheet= or header_row=, so encoding, delimiter and header
    detection are exercised exactly as in the app.
    """
    from converter.reader import read_file

    p = samples / name
    return read_file(p.name, p.read_bytes(), **kw)


# ---------------------------------------------------------------------------------------------- PostgreSQL

def _pg_bindir() -> str | None:
    """Folder holding initdb and pg_ctl: on the PATH, Homebrew, Postgres.app or Debian/Ubuntu; else None."""
    found = shutil.which("initdb")
    if found and shutil.which("pg_ctl"):
        return str(Path(found).parent)
    patterns = ["/opt/homebrew/opt/postgresql@*/bin", "/usr/local/opt/postgresql@*/bin",
                "/opt/homebrew/opt/postgresql/bin", "/usr/local/opt/postgresql/bin",
                "/Applications/Postgres.app/Contents/Versions/*/bin", "/usr/lib/postgresql/*/bin",
                "C:/Program Files/PostgreSQL/*/bin"]
    for d in sorted(itertools.chain.from_iterable(glob.glob(p) for p in patterns), reverse=True):
        if Path(d, "initdb").exists() or Path(d, "initdb.exe").exists():
            return d
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg_server():
    """Admin URL of a PostgreSQL server the tests may create databases on (see the module docstring)."""
    url = os.environ.get("RECORDS_TEST_DATABASE_URL")
    if url:
        yield url
        return
    bindir = _pg_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL not found: install it (Mac: brew install postgresql@17) or set "
                    "RECORDS_TEST_DATABASE_URL")
    # PostgreSQL refuses to run as root: in a root container the server runs as the "postgres" user.
    as_root = hasattr(os, "geteuid") and os.geteuid() == 0
    prefix = ["runuser", "-u", "postgres", "--"] if as_root else []
    folder = tempfile.mkdtemp(prefix="crtest_pg_", dir="/tmp" if os.name != "nt" else None)
    data = Path(folder) / "data"
    if as_root:
        os.chmod(folder, 0o755)
        shutil.chown(folder, "postgres")
    port = _free_port()
    run = lambda *a: subprocess.run(prefix + list(a), check=True, capture_output=True)  # noqa: E731
    run(str(Path(bindir, "initdb")), "-D", str(data), "-A", "trust", "-U", "postgres", "-E", "UTF8",
        "--locale=C")
    opts = f"-p {port} -c listen_addresses=localhost -c unix_socket_directories='' -c fsync=off"
    run(str(Path(bindir, "pg_ctl")), "-D", str(data), "-o", opts, "-l", str(Path(folder, "log.txt")), "-w", "start")
    try:
        yield f"postgresql://postgres@localhost:{port}/postgres"
    finally:
        subprocess.run(prefix + [str(Path(bindir, "pg_ctl")), "-D", str(data), "-m", "immediate", "stop"],
                       capture_output=True)
        shutil.rmtree(folder, ignore_errors=True)


_db_counter = itertools.count()


def fresh_database(pg_server: str, icu: bool = True) -> str:
    """Create an empty database on the test server and return its URL.

    ``icu``: as the app creates its database (ICU case rules); False = the server's default locale (C on the
    private test server, like Homebrew's), to test the explicit-collation fallback.
    """
    import psycopg
    from psycopg.conninfo import make_conninfo

    name = f"crtest_{os.getpid()}_{next(_db_counter)}_{int(time.time() * 1000) % 100000}"
    with psycopg.connect(pg_server, autocommit=True) as c:
        try:
            if not icu:
                raise psycopg.errors.FeatureNotSupported("asked for the server's default")
            c.execute(f'CREATE DATABASE "{name}" TEMPLATE template0 ENCODING \'UTF8\' LOCALE_PROVIDER icu '
                      "ICU_LOCALE 'und' LC_COLLATE 'C' LC_CTYPE 'C'")
        except psycopg.Error:  # no ICU on this server: its default (finds then use an explicit collation)
            c.execute(f'CREATE DATABASE "{name}"')
    return make_conninfo(pg_server, dbname=name)


def drop_database(pg_server: str, url: str) -> None:
    """Drop a database made by fresh_database (closing anything still connected)."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    name = conninfo_to_dict(url)["dbname"]
    with psycopg.connect(pg_server, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def records_schema():
    """Customer Records' record fields (schemas/records_schema.yaml)."""
    return load_schema(ROOT / "schemas" / "records_schema.yaml")


@pytest.fixture
def rdb(pg_server, records_schema):
    """A fresh, set-up Customer Records database (records.db.Database) for one test."""
    from records.db import Database

    url = fresh_database(pg_server)
    db = Database(url, max_size=4)
    db.setup(records_schema)
    yield db
    db.close()
    drop_database(pg_server, url)


@pytest.fixture
def rstore(rdb):
    """records.store.RecordStore on the fresh database."""
    from records.store import RecordStore

    return RecordStore(rdb)
