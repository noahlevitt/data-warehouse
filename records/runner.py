"""Runs Customer Records' imports in worker processes, so several imports really convert at the same time.

Why
---
The converter's :class:`converter.jobs.ImportRunner` runs each import in a thread of the web server's process.
Python runs the code of only one thread at a time, so imports in threads take turns: five 20,000-row imports started
together finished at about the time of five in a row (about 50 s on a 2-core test machine), and adding processor
cores changed nothing.

How
---
:class:`ProcessImportRunner` keeps everything of that runner (``IMPORT_WORKERS`` slots with a queue behind them,
cancelling through the import's cancel flag, imports left queued or running by a previous server marked
"interrupted" at start-up, the hourly purge of old files), but each slot runs its import in a new Python process::

    python -m records.runner <job database> <job id> <server process id>

with ``DATABASE_URL`` in its environment. The process opens its own connections (the job store, ``converter.db``,
and up to two PostgreSQL connections), runs :func:`converter.jobs.run_import` with a
:class:`records.importer.RecordSink`, exactly as the thread did, and exits. Progress and the outcome are written to
the job store as before, so the pages show imports the same way. Each process's memory goes back to the system when
its import ends. Starting a process takes about 1.5 s (loading pandas and the converter), little next to an import.

The processes are started with :mod:`subprocess`, not :mod:`multiprocessing`: under Streamlit the main module is
app.py, and multiprocessing's "spawn" would run it again in every worker.

When something goes wrong
-------------------------
* A worker process that ends without recording an outcome (stopped by the system because the server ran out of
  memory, or crashed): its import is marked failed with the reason, what it stored is removed
  (``RecordSink.discard``), and the other imports carry on.
* A worker process that can't connect to PostgreSQL fails its import at once with PostgreSQL's reason, before
  reading the file.
* The server stops (Ctrl+C, ``docker compose down``): its running imports are stopped at once and marked
  "interrupted", with what they stored removed, and queued ones too; each can be re-run (1.5.0; the threads used to
  finish the running and then the queued imports while the pages were already gone). Worker processes run in their
  own process group, so Ctrl+C in the terminal reaches only the server, which stops them itself.
* The server's process is killed: its worker processes stop within a second (on macOS and Linux), so no import
  carries on after the next start marked it interrupted. That start also removes what interrupted imports stored.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from converter.jobs import ImportRunner, _forget_numbers, run_import
from converter.storage import Store

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PARENT_GONE = 3  # exit code of a worker process whose server's process is gone
STOPPED = "The server stopped while this import was waiting or running. Use Re-run to start it again."


_STOPPING = threading.Event()  # set when the server's Python starts shutting down (see _watch_shutdown)


def server_stopping() -> bool:
    """True once the server is shutting down (its main thread has ended; Python then waits for the slots' threads)."""
    return _STOPPING.is_set() or not threading.main_thread().is_alive()


def _watch_shutdown() -> None:
    """Set ``_STOPPING`` when the server's Python shuts down, before it waits for the slots' threads.

    Python joins the threads of every ThreadPoolExecutor in a hook it runs at shutdown, before the main thread counts
    as ended; a hook registered after that one runs before it (they run in reverse order). ``threading._register_atexit``
    is the function concurrent.futures itself uses (Python 3.9 and later); without it, imports finish as before.
    """
    register = getattr(threading, "_register_atexit", None)
    if register is not None and not getattr(_watch_shutdown, "done", False):
        register(_STOPPING.set)
        _watch_shutdown.done = True


def exit_reason(code: int | None) -> str:
    """What to tell the user about a worker process that ended with ``code`` without recording an outcome."""
    if code is not None and code < 0:
        sig = -code
        if sig == getattr(signal, "SIGKILL", 9):
            return ("The import's worker process was stopped by the system (signal 9), usually because the server "
                    "ran out of memory. Run it again; if it happens again, lower IMPORT_WORKERS or give the server "
                    "more memory.")
        return f"The import's worker process was stopped (signal {sig}). Run it again."
    if code:
        return f"The import's worker process stopped unexpectedly (exit code {code}); the server log has the details."
    return "The import's worker process ended without recording a result; the server log has the details."


class ProcessImportRunner(ImportRunner):
    """The converter's runner, with each import run in its own worker process (see the module docstring).

    One per server process (the web app caches it), like :class:`converter.jobs.ImportRunner`. ``workers`` imports
    run at the same time; more wait as "queued". ``sink`` (the server's own ``RecordSink``) is only used to remove
    what a worker process stored when it ended without an outcome.
    """

    def __init__(self, store: Store, database_url: str, workers: int = 5, retention_days: int = 30,
                 record_days: int = 0, sink=None):
        """Start the slots, mark imports of a previous server "interrupted" and purge old files (as the base class).

        What those imports stored is removed: a server and its worker stopped together right after the worker saved
        the records, before it recorded the outcome, would otherwise leave an interrupted import with records,
        which can be neither re-run nor deleted on its page.
        """
        left = [r.id for r in store.active_imports()]
        super().__init__(store, workers=workers, retention_days=retention_days, record_days=record_days,
                         backfill=False, sink=sink)
        _watch_shutdown()  # after the slots' ThreadPoolExecutor exists, so this hook runs before Python joins them
        self.database_url = database_url
        self.workers = workers
        if sink is not None:
            for import_id in left:
                _forget_numbers(store, import_id, sink)

    def submit(self, import_id: int) -> None:
        """Queue an already-created import for a free slot; purge old files if the last purge was over an hour ago."""
        self.pool.submit(self._run, import_id)
        if time.monotonic() - self._last_purge > 3600:
            self.purge()

    def command(self, import_id: int) -> list[str]:
        """The command line of the worker process for an import (the job store's full path: the worker runs in the
        project folder, and DATA_DIR may be relative to the folder the server was started in)."""
        return [sys.executable, "-m", "records.runner", str(Path(self.store.db_path).resolve()), str(import_id),
                str(os.getpid())]

    def _run(self, import_id: int) -> None:
        """Run one import in a new worker process and wait for it (in a slot's thread). Never raises."""
        try:
            rec = self.store.get_import(import_id)
            if rec is None or rec.status != "queued":
                return  # already ran, or marked interrupted
            if server_stopping():
                self._end_if_unfinished(import_id, "interrupted", STOPPED)
                return
            env = dict(os.environ, DATABASE_URL=self.database_url,
                       PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])))
            # Its own process group: Ctrl+C in the terminal and a closed terminal are for the server only.
            group = ({"start_new_session": True} if os.name == "posix"
                     else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)})
            proc = subprocess.Popen(self.command(import_id), cwd=ROOT, env=env, **group)
            while True:
                try:
                    code = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if server_stopping():
                        proc.terminate()
                        proc.wait()
                        self._end_if_unfinished(import_id, "interrupted", STOPPED)
                        return
            reason = exit_reason(code)
        except Exception as exc:  # noqa: BLE001 - recorded on the import below
            log.exception("Import %s: the worker process couldn't be run", import_id)
            reason = f"The import's worker process couldn't be started ({type(exc).__name__}: {exc})."
        try:
            self._end_if_unfinished(import_id, "failed", reason)
        except Exception:  # noqa: BLE001 - a pool thread must not die; the import shows as running until a restart
            log.exception("Import %s: recording the end of its worker process failed", import_id)

    def _end_if_unfinished(self, import_id: int, status: str, reason: str) -> None:
        """End the import as ``status`` ("failed" or "interrupted") with ``reason`` when it is still queued or running
        (its worker process didn't record an outcome), and remove what it stored."""
        rec = self.store.get_import(import_id)
        if rec is None or not rec.active:
            return  # the worker process recorded the outcome (completed, failed or cancelled)
        log.error("Import %s: %s", import_id, reason)
        _forget_numbers(self.store, import_id, self.sink)
        self.store.finish_import(import_id, status, error=reason, stage=status.capitalize())


def _stop_with_server(server_pid: int) -> None:
    """Exit this worker process when the server's process is gone (macOS and Linux; checked every second)."""
    if os.name != "posix":
        return

    def watch() -> None:
        while os.getppid() == server_pid:
            time.sleep(1)
        os._exit(PARENT_GONE)

    threading.Thread(target=watch, name="server-watch", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    """Worker process: run one import (``<job database> <job id> <server process id>``) and exit."""
    db_path, job_id, server_pid = (argv if argv is not None else sys.argv[1:])[:3]
    job_id = int(job_id)
    # Ctrl+C is for the server, which then stops this process itself (in its own process group it doesn't get it).
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logging.basicConfig(level=logging.WARNING,
                        format=f"%(asctime)s %(levelname)s [import {job_id}] %(name)s: %(message)s")
    _stop_with_server(int(server_pid))
    import psycopg

    from records.db import Database
    from records.importer import RecordSink
    from records.store import RecordStore

    store = Store(db_path)
    url = os.environ["DATABASE_URL"]
    try:  # PostgreSQL's own reason, before the file is read (the pool would only say it timed out, much later)
        psycopg.connect(url, connect_timeout=15).close()
    except psycopg.Error as exc:
        rec = store.get_import(job_id)
        if rec is not None and rec.status == "queued":
            store.finish_import(job_id, "failed", stage="Failed",
                                error="The import couldn't connect to the database: " + " ".join(str(exc).split()))
        return 0
    db = Database(url, min_size=1, max_size=2)
    try:
        run_import(store, job_id, RecordSink(RecordStore(db), store))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
