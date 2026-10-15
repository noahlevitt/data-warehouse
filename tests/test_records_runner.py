"""Imports run in worker processes (records.runner, 1.5.0): several at once, same records, cancel and crashes.

The runner starts real worker processes (``python -m records.runner``) against the test database, as the app does.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from converter.mapping import profile_columns
from converter.reader import read_file
from converter.services import initial_mapping
from converter.storage import Store
from records.config import get_records_settings
from records.importer import RecordSink
import records.runner as R
from records.runner import PARENT_GONE, STOPPED, ProcessImportRunner, exit_reason

from .conftest import ROOT
from .records_util import ensure_business, ensure_service, import_file

GUESTS = """First Name,Last Name,Phone,Phone 2,Email,Visits,Total Spend,Last Visit,Birthday,Gender
John,Smith,(516) 555-0100,917-555-1234,JOHN@X.COM,5,"$1,234.50",10/09/2026 3:45 PM,9/12,
Mary,Jones,516-555-0101 / 516-555-0199,,mary@x.com,2,12,10/02/2026,1985-03-04,Woman
Pat,Doe,123,,,1,,,,
Ann,Lee,516-555-0102,,,,,,,F
"""
GENDERS = ["Mb", "F", None, "F"]  # John guessed from the name, Woman, Pat not guessed, F


def big_file(rows: int, seed: int = 0) -> str:
    """A guest list with ``rows`` rows (enough work that an import takes a few seconds)."""
    names = ["Mary", "John", "Lisa", "Jose", "Ann", "Wei", "Jordan", "Maria"]
    lines = ["First Name,Last Name,Phone,Email,Last Visit,Visits"]
    for i in range(rows):
        lines.append(f"{names[(i + seed) % len(names)]},Smith{i},516{2000000 + i * 7 + seed:07d},u{i}@x.com,"
                     f"{i % 12 + 1}/{i % 28 + 1}/2026 {i % 12 + 1}:15 PM,{i % 40}")
    return "\n".join(lines) + "\n"


def queue(rs, jobs: Store, schema, name: str, data: str) -> tuple[int, int]:
    """Create an import as the app does (business 1001, service "Toast export") without running it: (job, import)."""
    raw = data.encode()
    ensure_business(rs, "1001")
    sid = ensure_service(rs, "Toast export")
    rr = read_file(name, raw)
    m = initial_mapping(schema, rr.df, profile_columns(rr.df), None, None, use_ai=False).mapping
    imp = rs.create_import("1001", sid, name, hashlib.sha1(raw).hexdigest(), rr.sheet, "tester")
    job = jobs.create_import(username="tester", filename=name, data=raw, schema_label=schema.label,
                             schema_json=schema.model_dump_json(), mapping=m,
                             read_options={"sheet": rr.sheet, "header_row": rr.header_row,
                                           "timezone": "America/New_York", "records_import_id": imp},
                             template_name=None, ai_used=False)
    rs.set_job(imp, job)
    return job, imp


def stored(rs, imp: int) -> list[tuple]:
    """An import's records and numbers, without what differs between two imports of the same file."""
    rows = rs.db.fetch("""SELECT r.*, (SELECT string_agg(phone || ':' || position || ':' || repaired || ':' || original,
                                                          ',' ORDER BY position)
                                       FROM record_phones WHERE record_id = r.id) AS numbers
                          FROM records r WHERE import_id = %s ORDER BY file_row""", (imp,))
    return [tuple((k, v) for k, v in r.items() if k not in ("id", "import_id", "created_at")) for r in rows]


def wait(jobs: Store, ids, timeout: float = 120) -> int:
    """Wait until the imports have ended; returns the most of them seen running at the same time."""
    end, most = time.monotonic() + timeout, 0
    while time.monotonic() < end:
        recs = [jobs.get_import(i) for i in ids]
        most = max(most, sum(r.status == "running" for r in recs))
        if not any(r.active for r in recs):
            return most
        time.sleep(0.05)
    raise AssertionError(f"imports still running after {timeout} s: {[jobs.get_import(i).status for i in ids]}")


@pytest.fixture
def jobs(tmp_path):
    return Store(tmp_path / "converter.db")


def runner_for(rstore, jobs, workers, cls=ProcessImportRunner):
    """A runner as the app makes it. Made before the imports are queued: a new runner marks queued imports
    interrupted (they belonged to the previous server)."""
    return cls(jobs, rstore.db.url, workers=workers, retention_days=0, record_days=0, sink=RecordSink(rstore, jobs))


def test_imports_run_at_the_same_time_up_to_the_number_of_workers(rstore, jobs, records_schema):
    """Three slots, four imports: three run at once (each in its own process), the fourth waits, all complete."""
    runner = runner_for(rstore, jobs, 3)
    ids = [queue(rstore, jobs, records_schema, f"g{k}.csv", big_file(4000, k))[0] for k in range(4)]
    for j in ids:
        runner.submit(j)
    most = wait(jobs, ids)
    runner.pool.shutdown(wait=True)
    assert [jobs.get_import(j).status for j in ids] == ["completed"] * 4, [jobs.get_import(j).error for j in ids]
    assert most == 3
    assert rstore.db.fetch_one("SELECT count(*) AS n FROM records")["n"] == 16000


def test_a_worker_process_stores_the_same_records_as_before(rstore, jobs, records_schema):
    """The same file gives the same records (values, numbers, gender, issues) as an import run in the server."""
    runner = runner_for(rstore, jobs, 5)
    _, before = import_file(rstore, jobs, records_schema, "guests.csv", GUESTS)
    job, imp = queue(rstore, jobs, records_schema, "guests.csv", GUESTS)
    runner.submit(job)
    wait(jobs, [job])
    runner.pool.shutdown(wait=True)
    rec = jobs.get_import(job)
    assert rec.status == "completed", rec.error
    assert rec.summary["records"]["stored"] == 4
    assert stored(rstore, imp) == stored(rstore, before)
    assert [dict(r)["gender"] for r in stored(rstore, imp)] == GENDERS
    assert rstore.get_import(imp)["status"] == "completed"


def test_cancel_stops_a_running_worker_process(rstore, jobs, records_schema):
    """Cancel works through the import's cancel flag, as with threads, and leaves no records behind."""
    runner = runner_for(rstore, jobs, 5)
    job, imp = queue(rstore, jobs, records_schema, "big.csv", big_file(20000))
    runner.submit(job)
    end = time.monotonic() + 60
    while jobs.get_import(job).status != "running" and time.monotonic() < end:
        time.sleep(0.05)
    runner.cancel(job)
    wait(jobs, [job])
    runner.pool.shutdown(wait=True)
    assert jobs.get_import(job).status == "cancelled"
    assert rstore.db.fetch_one("SELECT count(*) AS n FROM records WHERE import_id = %s", (imp,))["n"] == 0


class Dies(ProcessImportRunner):
    """A runner whose worker process marks the import running and then ends with ``how`` ("real": a real one)."""

    how = "kill"

    def command(self, import_id):
        if self.how == "real":
            return super().command(import_id)
        end = {"kill": "os.kill(os.getpid(), signal.SIGKILL)", "exit": "sys.exit(5)", "quiet": "pass"}[self.how]
        return [sys.executable, "-c", "import os, signal, sys\nfrom converter.storage import Store\n"
                f"Store({str(self.store.db_path)!r}).start_import({import_id})\n{end}"]


@pytest.mark.skipif(os.name != "posix", reason="signals")
def test_a_killed_worker_process_fails_only_its_import(rstore, jobs, records_schema):
    """Out of memory (signal 9): the import says so, what it stored is removed, the next import still runs."""
    runner = runner_for(rstore, jobs, 2, Dies)
    job, imp = queue(rstore, jobs, records_schema, "guests.csv", GUESTS)
    # As if the worker process had saved the records just before it was killed.
    rstore.db.execute("INSERT INTO records (import_id, business_id, service_id, file_row, phone_issue, issues, source,"
                      " search_text) SELECT %s, business_id, service_id, 2, '', %s, %s, '' FROM imports WHERE id = %s",
                      (imp, Jsonb({}), Jsonb({}), imp))
    rstore.db.execute("UPDATE imports SET status = 'completed' WHERE id = %s", (imp,))
    runner.submit(job)
    wait(jobs, [job])
    rec = jobs.get_import(job)
    assert (rec.status, rec.stage) == ("failed", "Failed")
    assert "stopped by the system (signal 9)" in rec.error and "IMPORT_WORKERS" in rec.error
    assert rstore.db.fetch_one("SELECT count(*) AS n FROM records WHERE import_id = %s", (imp,))["n"] == 0
    assert rstore.get_import(imp)["status"] == "not completed"
    # The runner carries on: the next import runs normally in a real worker process.
    runner.how = "real"
    job2, _ = queue(rstore, jobs, records_schema, "guests2.csv", GUESTS)
    runner.submit(job2)
    wait(jobs, [job2])
    runner.pool.shutdown(wait=True)
    assert jobs.get_import(job2).status == "completed", jobs.get_import(job2).error


@pytest.mark.parametrize("how, says", [("exit", "stopped unexpectedly (exit code 5)"),
                                       ("quiet", "ended without recording a result")])
def test_a_worker_process_that_ends_without_a_result_fails_its_import(rstore, jobs, records_schema, how, says):
    runner = runner_for(rstore, jobs, 1, Dies)
    job, _ = queue(rstore, jobs, records_schema, "guests.csv", GUESTS)
    runner.how = how
    runner.submit(job)
    wait(jobs, [job])
    runner.pool.shutdown(wait=True)
    rec = jobs.get_import(job)
    assert rec.status == "failed" and says in rec.error


def test_a_worker_process_that_cannot_start_fails_its_import(rstore, jobs, records_schema, monkeypatch):
    runner = runner_for(rstore, jobs, 1)
    job, _ = queue(rstore, jobs, records_schema, "guests.csv", GUESTS)
    monkeypatch.setattr(runner, "command", lambda i: [str(ROOT / "no-such-program")])
    runner.submit(job)
    wait(jobs, [job])
    runner.pool.shutdown(wait=True)
    rec = jobs.get_import(job)
    assert rec.status == "failed" and "couldn't be started" in rec.error


def test_an_import_that_is_no_longer_queued_starts_no_process(rstore, jobs, records_schema, monkeypatch):
    """Imports marked interrupted (or already run) are left alone."""
    runner = runner_for(rstore, jobs, 1)
    job, _ = queue(rstore, jobs, records_schema, "guests.csv", GUESTS)
    jobs.finish_import(job, "interrupted", stage="Interrupted")
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(a))
    runner.submit(job)
    runner.pool.shutdown(wait=True)
    assert started == [] and jobs.get_import(job).status == "interrupted"


def _worker_pids(job: int) -> list[int]:
    """Process ids of running worker processes of an import (Linux: from /proc)."""
    out = []
    for d in Path("/proc").iterdir():
        try:
            if d.name.isdigit() and (d / "stat").read_text().split(") ")[-1][0] != "Z":
                args = (d / "cmdline").read_bytes().split(b"\0")
                if b"records.runner" in args and str(job).encode() in args:
                    out.append(int(d.name))
        except OSError:
            pass
    return out


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc (Linux)")
def test_ctrl_c_is_left_to_the_server(rstore, jobs, records_schema):
    """Ctrl+C (SIGINT) is for the server: a worker process ignores it and finishes its import."""
    runner = runner_for(rstore, jobs, 1)
    job, _ = queue(rstore, jobs, records_schema, "big.csv", big_file(6000))
    runner.submit(job)
    end = time.monotonic() + 60
    while not _worker_pids(job) and time.monotonic() < end:
        time.sleep(0.05)
    while jobs.get_import(job).status != "running" and time.monotonic() < end:
        time.sleep(0.05)
    for pid in _worker_pids(job):
        os.kill(pid, signal.SIGINT)
    wait(jobs, [job])
    runner.pool.shutdown(wait=True)
    assert jobs.get_import(job).status == "completed", jobs.get_import(job).error


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc (Linux)")
def test_a_worker_process_stops_when_the_server_is_gone(rstore, jobs, records_schema):
    """No import carries on after its server's process ended (the next start marks it interrupted)."""
    job, _ = queue(rstore, jobs, records_schema, "big.csv", big_file(40000))
    # A stand-in server: starts the worker process for the import, then disappears without waiting for it.
    server = ("import os, subprocess, sys\n"
              f"subprocess.Popen([sys.executable, '-m', 'records.runner', {str(jobs.db_path)!r}, '{job}', "
              "str(os.getpid())], cwd=sys.argv[1], env=dict(os.environ, DATABASE_URL=sys.argv[2]))\n"
              "import time; time.sleep(4)\nos._exit(0)")
    subprocess.run([sys.executable, "-c", server, str(ROOT), rstore.db.url], check=True, cwd=ROOT)
    assert jobs.get_import(job).status == "running"  # it had started
    end = time.monotonic() + 10
    while _worker_pids(job) and time.monotonic() < end:
        time.sleep(0.1)
    assert not _worker_pids(job)
    time.sleep(1)
    assert jobs.get_import(job).status == "running"  # never completed; a new server marks it interrupted
    assert PARENT_GONE == 3


def test_exit_reasons():
    assert "signal 9" in exit_reason(-9) and "memory" in exit_reason(-9)
    assert "signal 15" in exit_reason(-15)
    assert "exit code 1" in exit_reason(1)
    assert "without recording a result" in exit_reason(0)
    assert "without recording a result" in exit_reason(None)


def test_five_imports_at_once_by_default(monkeypatch):
    """IMPORT_WORKERS is 5 for Customer Records unless set; DATABASE_POOL_SIZE is 16. Both have a minimum."""
    monkeypatch.delenv("IMPORT_WORKERS", raising=False)
    monkeypatch.delenv("DATABASE_POOL_SIZE", raising=False)
    s = get_records_settings()
    assert (s.import_workers, s.database_pool_size) == (5, 16)
    monkeypatch.setenv("IMPORT_WORKERS", "3")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "30")
    s = get_records_settings()
    assert (s.import_workers, s.database_pool_size) == (3, 30)
    monkeypatch.setenv("IMPORT_WORKERS", "0")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "1")
    s = get_records_settings()
    assert (s.import_workers, s.database_pool_size) == (1, 2)


def records_count(rs, imp: int) -> int:
    return rs.db.fetch_one("SELECT count(*) AS n FROM records WHERE import_id = %s", (imp,))["n"]


def test_stopping_the_server_stops_its_imports(rstore, jobs, records_schema, monkeypatch):
    """The running import is stopped at once and the queued one isn't started: both interrupted, nothing stored."""
    runner = runner_for(rstore, jobs, 1)
    running, imp = queue(rstore, jobs, records_schema, "big.csv", big_file(20000))
    waiting, _ = queue(rstore, jobs, records_schema, "guests.csv", GUESTS)
    runner.submit(running)
    runner.submit(waiting)
    end = time.monotonic() + 60
    while jobs.get_import(running).status != "running" and time.monotonic() < end:
        time.sleep(0.05)
    monkeypatch.setattr(R, "server_stopping", lambda: True)  # as when the server's main thread has ended
    started = time.monotonic()
    wait(jobs, [running, waiting], timeout=10)
    assert time.monotonic() - started < 5
    runner.pool.shutdown(wait=True)
    for j in (running, waiting):
        rec = jobs.get_import(j)
        assert (rec.status, rec.stage, rec.error) == ("interrupted", "Interrupted", STOPPED)
    assert records_count(rstore, imp) == 0 and not _worker_pids(running)


SERVER = """
import os, signal, sys, time
sys.path.insert(0, sys.argv[1])
from converter.schema import load_schema
from converter.storage import Store
from records.db import Database
from records.importer import RecordSink
from records.runner import ProcessImportRunner
from records.store import RecordStore
import tests.test_records_runner as T
schema = load_schema(os.path.join(sys.argv[1], "schemas", "records_schema.yaml"))
db = Database(sys.argv[2], max_size=2)
store = Store(sys.argv[3])
runner = ProcessImportRunner(store, sys.argv[2], workers=1, sink=RecordSink(RecordStore(db), store))
rs = RecordStore(db)
ids = [T.queue(rs, store, schema, "big.csv", T.big_file(20000))[0]] + [
    T.queue(rs, store, schema, f"g{k}.csv", T.GUESTS)[0] for k in range(2)]
for j in ids:
    runner.submit(j)
while store.get_import(ids[0]).status != "running":
    time.sleep(0.05)
print(*ids, flush=True)
os.kill(os.getpid(), signal.SIGINT)
time.sleep(120)
"""


@pytest.mark.skipif(os.name != "posix", reason="signals")
def test_ctrl_c_stops_the_server_and_its_imports(rstore, jobs, records_schema):
    """A real server process (one slot, three imports): Ctrl+C ends it within seconds, not after its imports."""
    started = time.monotonic()
    out = subprocess.run([sys.executable, "-c", SERVER, str(ROOT), rstore.db.url, str(jobs.db_path)], cwd=ROOT,
                         capture_output=True, text=True, timeout=120)
    ids = [int(x) for x in out.stdout.split()]
    assert len(ids) == 3, out.stderr
    assert time.monotonic() - started < 30
    assert [(jobs.get_import(j).status, jobs.get_import(j).error) for j in ids] == [("interrupted", STOPPED)] * 3
    assert not any(_worker_pids(j) for j in ids)
    assert rstore.db.fetch_one("SELECT count(*) AS n FROM records")["n"] == 0


def test_a_new_server_removes_what_interrupted_imports_stored(rstore, jobs, records_schema):
    """Server and worker stopped together just after the save: the next start leaves no records behind."""
    runner_for(rstore, jobs, 1).pool.shutdown()
    job, imp = queue(rstore, jobs, records_schema, "guests.csv", GUESTS)
    jobs.start_import(job)
    rstore.db.execute("INSERT INTO records (import_id, business_id, service_id, file_row, phone_issue, issues, source,"
                      " search_text) SELECT %s, business_id, service_id, 2, '', %s, %s, '' FROM imports WHERE id = %s",
                      (imp, Jsonb({}), Jsonb({}), imp))
    rstore.db.execute("UPDATE imports SET status = 'completed' WHERE id = %s", (imp,))
    runner_for(rstore, jobs, 1).pool.shutdown()
    assert jobs.get_import(job).status == "interrupted"
    assert records_count(rstore, imp) == 0 and rstore.get_import(imp)["status"] == "not completed"


def test_no_database_fails_at_once_with_the_reason(rstore, jobs, records_schema):
    """A worker process that can't reach PostgreSQL says why, without converting the file first."""
    from psycopg.conninfo import make_conninfo
    runner = ProcessImportRunner(jobs, make_conninfo(rstore.db.url, dbname="crtest_no_such_database"), workers=1)
    job, _ = queue(rstore, jobs, records_schema, "guests.csv", GUESTS)
    runner.submit(job)
    wait(jobs, [job], timeout=60)
    runner.pool.shutdown(wait=True)
    rec = jobs.get_import(job)
    assert rec.status == "failed" and rec.error.startswith("The import couldn't connect to the database:")
    assert "crtest_no_such_database" in rec.error and "does not exist" in rec.error
    assert not (jobs.import_dir(job) / "source.parquet").exists()  # the file was never read


def test_a_relative_data_folder_works_from_any_folder(rstore, records_schema, tmp_path, monkeypatch):
    """DATA_DIR relative to where the server was started: the worker process (run in the project folder) gets the
    job store's full path."""
    monkeypatch.chdir(tmp_path)
    jobs = Store(Path("rel") / "converter.db")
    runner = runner_for(rstore, jobs, 1)
    assert runner.command(7)[3] == str(tmp_path / "rel" / "converter.db")
    job, _ = queue(rstore, jobs, records_schema, "guests.csv", GUESTS)
    runner.submit(job)
    wait(jobs, [job])
    runner.pool.shutdown(wait=True)
    assert jobs.get_import(job).status == "completed", jobs.get_import(job).error
    assert not (ROOT / "rel").exists()
