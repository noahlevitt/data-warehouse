"""Sending to the main app's bucket (1.7.0): completed imports, deleted imports and tags, as CSV files.

Overview
--------
Customer Records runs beside the main app (sendsational) and hands its data over through the main app's bucket
(Cloudflare R2, through its S3 API), where the main app's own import files land; the main app loads them into its
ClickHouse. Three kinds of file, under ``BUCKET_PREFIX`` (``data/``):

======================================================================  ===========================================
``customer-records/<ULID>-<merchant>-import-<uid>.csv``                 a completed import: one row per record
``customer-records/<ULID>-<merchant>-import-<uid>-deleted.csv``         the same records marked deleted
                                                                        (``is_deleted`` = 1), after the import's
                                                                        records were deleted here
``customer-records-tags/<ULID>-<merchant>-tag-<uid>.csv``               all of a tag's numbers (a snapshot)
======================================================================  ===========================================

The names follow the main app's (``<ULID>-<merchant id>-<name>.csv``): the ULID is the file's time (a newer file
wins), the merchant ID is read from the name. ``<uid>`` is the import's or the tag's UUID (``imports.uid``,
``tags.uid``), unique also after a reset. Columns, formats and how the main app can load them:
docs/BUCKET_FILES.md.

What is sent, and when
----------------------
The queue is the table ``bucket_sends``; :class:`BucketSender` works through it in a background thread, one file at
a time, in the order queued. A failed send is tried again after 1, 2, 4… minutes, at most an hour apart, until it
goes; the pages show why it is waiting.

* **Imports:** every completed import of a main app merchant (whose ID is a number), also those completed before
  the bucket was set up, once its run has finished (the worker's result in data/converter.db).
* **Deleted imports:** deleting the records of an import whose file was sent, or may have been (a try that failed or
  was cut off can still have landed), queues its deletion file; it waits until the import's own send is settled, so
  it is never written before an import file that arrives. An import whose records were deleted before its file went
  is never sent; one never tried needs no deletion.
* **Tags:** each export of a tag, and *Send to the main app* on the tag's page, queue a snapshot (one at a time per
  tag: a snapshot waiting already covers later changes, being built when it is sent). Deleting a tag that was sent
  queues a last snapshot that says so (``tag_deleted`` = 1).

Nothing is sent without BUCKET_ENDPOINT, BUCKET_NAME and the key (records.config); what is queued meanwhile waits.
The key is never written to the pages or the log. One app server sends per database: a new sender takes over the
sends another process left half done (the server stopped during them) at once.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import IO, Callable

from converter.schema import TargetSchema

from .db import Database, column_type
from .tags import export_table

log = logging.getLogger(__name__)

IMPORTS_FOLDER = "customer-records"
TAGS_FOLDER = "customer-records-tags"
# The columns of an import's file, before and after one column per record field (except the phone field: its
# numbers are phone_number, the first, and phone_numbers, all of them).
FIRST_COLUMNS = ["record_key", "import_uid", "is_deleted", "merchant_id", "location_id", "service_id", "service",
                 "import_id", "record_id", "file_row", "file_name", "imported_at", "imported_by", "phone_number",
                 "phone_numbers", "phone_issue"]
LAST_COLUMNS = ["first_visit_as", "source"]
# The columns a tag's file adds to the import format's.
TAG_COLUMNS = ["tag_uid", "tag_id", "tag_name", "merchant_id", "row_kind", "tag_deleted"]
# Longest wait between two tries of a failed send, in seconds.
MAX_WAIT = 3600
# A send left 'sending' this long (the server stopped during it) is tried again.
STALE_MINUTES = 30
# Merchant IDs the main app has: whole numbers (its file names carry them as digits).
MERCHANT_ID_RE = re.compile(r"^[0-9]+$")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ulid_lock = threading.Lock()
_last_ms = 0
# Senders whose thread runs (stop_all ends them: tests, before the app's cached resources are dropped).
_RUNNING: set["BucketSender"] = set()


class BucketError(RuntimeError):
    """The file couldn't be put in the bucket; the message says why (never with the key)."""


def new_ulid(now_ms: int | None = None) -> str:
    """A ULID: 26 characters, 48 bits of milliseconds then 80 random bits, in Crockford's base 32.

    Each one is later than any made before in this process (a millisecond is added when needed), because the main
    app reads a file's version from it and a newer file must win.
    """
    global _last_ms
    with _ulid_lock:
        ms = int(time.time() * 1000) if now_ms is None else now_ms
        if ms <= _last_ms:
            ms = _last_ms + 1
        _last_ms = ms
    value = (ms << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def object_key(prefix: str, folder: str, merchant_id: str, name: str, ulid: str | None = None) -> str:
    """``<prefix><folder>/<ULID>-<merchant id>-<name>.csv`` (the main app's naming of import files)."""
    return f"{prefix}{folder}/{ulid or new_ulid()}-{merchant_id}-{name}.csv"


def _cell(v) -> str:
    """A database value as file text: dates YYYY-MM-DD, date-times YYYY-MM-DD HH:MM:SS, true/false, plain numbers.

    A moment with a time zone (when an import completed) is written in UTC with a Z; a record's date-times have
    none: they are the client's local time, as in its file.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


def record_fields(schema: TargetSchema) -> list[str]:
    """The record fields with a column, in the record fields file's order (an import file's middle columns)."""
    names = [f.name for f in schema.fields if column_type(f) is not None]
    clash = sorted(set(names) & set(FIRST_COLUMNS + LAST_COLUMNS))
    if clash:
        raise ValueError(f"Record field(s) {', '.join(clash)} have the name of a column of the files sent to the "
                         "main app; rename them in the record fields file.")
    return names


def record_columns(schema: TargetSchema) -> list[str]:
    """The header of an import's file (and of its deletion file)."""
    return FIRST_COLUMNS + record_fields(schema) + LAST_COLUMNS


def get_import(db: Database, import_id: int) -> dict | None:
    """An import with what its files say about it: merchant, location, service, uid, who and when."""
    return db.fetch_one("""SELECT i.id, i.uid, i.status, i.business_id, i.location_id, i.service_id, i.file_name,
                                  i.username, i.completed_at, i.first_visit_as, s.name AS service_name
                           FROM imports i JOIN services s ON s.id = i.service_id WHERE i.id = %s""", (import_id,))


def _base(imp: dict, deleted: bool) -> dict:
    return {"import_uid": str(imp["uid"]), "is_deleted": "1" if deleted else "0", "merchant_id": imp["business_id"],
            "location_id": imp["location_id"] or "", "service_id": str(imp["service_id"]),
            "service": imp["service_name"], "import_id": str(imp["id"])}


def write_records_file(db: Database, schema: TargetSchema, imp: dict, fh: IO[str]) -> int:
    """Write an import's records as CSV to ``fh`` (one row per record, in file order); returns the rows written."""
    fields = record_fields(schema)
    columns = record_columns(schema)
    w = csv.writer(fh, lineterminator="\n")
    w.writerow(columns)
    base = _base(imp, deleted=False)
    base.update(file_name=imp["file_name"], imported_at=_cell(imp["completed_at"]), imported_by=imp["username"],
                first_visit_as=imp["first_visit_as"] or "")
    select = ", ".join(f'r."{f}"' for f in fields)
    n = 0
    with db.conn() as c:
        with c.cursor(name="bucket_records") as cur:  # a server-side cursor: a big import isn't read all at once
            cur.itersize = 2000
            cur.execute(f"""SELECT r.id, r.file_row, r.phone_issue, r.source, {select},
                                   coalesce((SELECT string_agg(p.phone, '|' ORDER BY p.position)
                                             FROM record_phones p WHERE p.record_id = r.id), '') AS phones
                            FROM records r WHERE r.import_id = %s ORDER BY r.file_row""", (imp["id"],))
            for r in cur:
                row = dict(base, record_key=f"{imp['uid']}:{r['file_row']}", record_id=str(r["id"]),
                           file_row=str(r["file_row"]), phone_number=r["phones"].split("|")[0],
                           phone_numbers=r["phones"], phone_issue=r["phone_issue"],
                           source=json.dumps(r["source"], ensure_ascii=False, separators=(",", ":")))
                row.update({f: _cell(r[f]) for f in fields})
                w.writerow([row.get(col, "") for col in columns])
                n += 1
    return n


def write_delete_file(schema: TargetSchema, imp: dict, file_rows: list[int], fh: IO[str]) -> int:
    """Write the deletion of an import's records (``is_deleted`` = 1, keys only) to ``fh``; returns the rows."""
    columns = record_columns(schema)
    w = csv.writer(fh, lineterminator="\n")
    w.writerow(columns)
    base = _base(imp, deleted=True)
    for fr in sorted(file_rows):
        row = dict(base, record_key=f"{imp['uid']}:{fr}", file_row=str(fr))
        w.writerow([row.get(col, "") for col in columns])
    return len(file_rows)


def write_tag_file(db: Database, schema: TargetSchema, export: TargetSchema, send: dict,
                   fh: IO[str]) -> tuple[int, str]:
    """Write a snapshot of the tag of ``send`` (a bucket_sends row) to ``fh``; returns the numbers written.

    The columns are the import format's (as a tag export) plus :data:`TAG_COLUMNS`. The first row
    (``row_kind`` = tag) has no number and says which tag this is, so even an empty or deleted tag replaces the
    tag's earlier snapshot; then one row per number (``row_kind`` = number). A tag deleted since it was queued
    gives just that first row, with ``tag_deleted`` = 1. Returns (the numbers written, the tag's name written).
    """
    tag = db.fetch_one("SELECT id, name, business_id FROM tags WHERE id = %s AND uid = %s",
                       (send["tag_id"], send["tag_uid"]))
    headers = [f.header for f in export.fields]
    w = csv.writer(fh, lineterminator="\n")
    w.writerow(headers + TAG_COLUMNS)
    ident = [str(send["tag_uid"]), str(send["tag_id"]), tag["name"] if tag else send["tag_name"],
             tag["business_id"] if tag else send["business_id"]]
    w.writerow([""] * len(headers) + ident + ["tag", "0" if tag else "1"])
    if tag is None:
        return 0, ident[2]
    table, _ = export_table(db, tag["id"], schema, export)
    for values in table.itertuples(index=False):
        w.writerow([("" if v is None else str(v)) for v in values] + ident + ["number", "0"])
    return len(table), ident[2]


class BucketClient:
    """Puts files in the bucket through the S3 API (Cloudflare R2, or any S3-compatible store)."""

    def __init__(self, endpoint: str, bucket: str, access_key_id: str, secret_access_key: str,
                 region: str = "auto"):
        self.endpoint = endpoint.strip().rstrip("/")
        self.bucket = bucket.strip()
        self.region = region or "auto"
        self._key_id = access_key_id
        self._secret = secret_access_key
        self._client = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # never shows the key
        return f"BucketClient({self.endpoint!r}, {self.bucket!r})"

    @property
    def configured(self) -> bool:
        """True when the address, the bucket and both halves of the key are set."""
        return bool(self.endpoint and self.bucket and self._key_id and self._secret)

    def _s3(self):
        with self._lock:
            if self._client is None:
                import boto3  # loaded on the first send: a copy without the bucket set up never needs it
                from botocore.config import Config

                self._client = boto3.client(
                    "s3", endpoint_url=self.endpoint, region_name=self.region, aws_access_key_id=self._key_id,
                    aws_secret_access_key=self._secret,
                    # Path-style addresses and checksums only when required: what R2 and other S3-compatible
                    # stores accept (boto3's newer default checksums aren't supported by all of them).
                    config=Config(s3={"addressing_style": "path"}, request_checksum_calculation="when_required",
                                  response_checksum_validation="when_required", connect_timeout=15,
                                  read_timeout=120, retries={"max_attempts": 3, "mode": "standard"}))
            return self._client

    def put(self, key: str, path: str) -> None:
        """Upload the file at ``path`` as ``key``. Raises BucketError."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            with open(path, "rb") as body:
                self._s3().put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="text/csv; charset=utf-8")
        except ClientError as exc:
            err = exc.response.get("Error", {})
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            detail = ": ".join(x for x in (err.get("Code"), err.get("Message")) if x)
            raise BucketError(f"The bucket refused the file (HTTP {status}{', ' + detail if detail else ''}).") \
                from None
        except BotoCoreError as exc:
            raise BucketError(f"Can't reach the bucket at {self.endpoint}: {exc}") from None


class BucketSender:
    """Works through ``bucket_sends`` (see the module docstring); one per server process.

    :meth:`start` runs :meth:`run_once` every ``interval`` seconds in a background thread (and at once after
    :meth:`wake`); without bucket settings it does nothing. ``jobs`` is the app's import store (data/converter.db):
    an import is only queued once its run there has completed.
    """

    def __init__(self, db: Database, jobs, schema: Callable[[], TargetSchema], export: Callable[[], TargetSchema],
                 client: BucketClient, prefix: str = "data/", interval: float = 10.0):
        self.db = db
        self.jobs = jobs
        self.schema = schema
        self.export = export
        self.client = client
        self.prefix = prefix
        self.interval = interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Who claims a send: this sender (host, process, object). A send another one left 'sending' is taken over at
        # this sender's first run (the server stopped during it), not only after STALE_MINUTES.
        self.identity = f"{socket.gethostname()}:{os.getpid()}:{id(self):x}"
        self._took_over = False

    @property
    def configured(self) -> bool:
        return self.client.configured

    def start(self) -> None:
        """Start the background thread (once; not without bucket settings)."""
        if not self.configured or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bucket-sender", daemon=True)
        self._thread.start()
        _RUNNING.add(self)

    def wake(self) -> None:
        """Look at the queue now instead of at the next interval."""
        self._wake.set()

    def stop(self, wait: float = 10.0) -> None:
        """End the background thread (after the file it may be sending)."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(wait)
        _RUNNING.discard(self)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - e.g. the database briefly unreachable: try again next time
                log.exception("Sending to the bucket failed")
            self._wake.wait(self.interval)
            self._wake.clear()

    def run_once(self, limit: int = 100) -> int:
        """Queue the completed imports not yet queued, then send what is due (at most ``limit`` files).

        Returns how many files were sent. Does nothing without bucket settings.
        """
        if not self.configured:
            return 0
        if not self._took_over:
            self.db.execute("""UPDATE bucket_sends SET status = 'pending', next_try_at = now()
                               WHERE status = 'sending' AND claimed_by <> %s""", (self.identity,))
            self._took_over = True
        self.db.execute(f"""UPDATE bucket_sends SET status = 'pending', next_try_at = now()
                            WHERE status = 'sending' AND claimed_at < now() - interval '{STALE_MINUTES} minutes'""")
        self.queue_completed_imports()
        sent = 0
        for _ in range(limit):
            send = self._claim()
            if send is None:
                break
            sent += self._send(send)
        return sent

    def queue_completed_imports(self) -> int:
        """Queue every completed import of a main app merchant that isn't queued yet; returns how many."""
        rows = self.db.fetch("""SELECT i.id, i.job_id, i.business_id FROM imports i
                                WHERE i.status = 'completed' AND i.business_id ~ '^[0-9]+$' AND NOT EXISTS (
                                    SELECT 1 FROM bucket_sends s WHERE s.kind = 'import' AND s.import_id = i.id)
                                ORDER BY i.id""")
        n = 0
        for r in rows:
            job = self.jobs.get_import(r["job_id"]) if r["job_id"] else None
            if job is None or job.status != "completed":
                continue  # still finishing (or ended otherwise): the worker's outcome decides
            n += self.db.execute("""INSERT INTO bucket_sends (kind, import_id, business_id) VALUES ('import', %s, %s)
                                    ON CONFLICT DO NOTHING""", (r["id"], r["business_id"]))
        return n

    def _claim(self) -> dict | None:
        """The next send due, marked 'sending'. A deletion waits while its import's own send isn't settled."""
        return self.db.fetch_one("""
            UPDATE bucket_sends SET status = 'sending', attempts = attempts + 1, claimed_at = now(), claimed_by = %s
            WHERE id = (SELECT s.id FROM bucket_sends s WHERE s.status = 'pending' AND s.next_try_at <= now()
                        AND NOT (s.kind = 'delete' AND EXISTS (
                            SELECT 1 FROM bucket_sends i WHERE i.kind = 'import' AND i.import_id = s.import_id
                            AND i.status IN ('pending', 'sending')))
                        ORDER BY s.id FOR UPDATE OF s SKIP LOCKED LIMIT 1)
            RETURNING *""", (self.identity,))

    def _build(self, send: dict, fh: IO[str]) -> tuple[str | None, int]:
        """Write the file of ``send``; returns (its key, rows), or (None, 0) when there is nothing to send."""
        if send["kind"] == "tag":
            key = object_key(self.prefix, TAGS_FOLDER, send["business_id"], f"tag-{send['tag_uid']}")
            rows, name = write_tag_file(self.db, self.schema(), self.export(), send, fh)
            send["tag_name"] = name  # the name written (a tag renamed since it was queued)
            return key, rows
        imp = get_import(self.db, send["import_id"])
        if imp is None:
            return None, 0
        if send["kind"] == "import":
            if imp["status"] != "completed":
                return None, 0  # its records were deleted (or discarded) before the file went
            key = object_key(self.prefix, IMPORTS_FOLDER, imp["business_id"], f"import-{imp['uid']}")
            return key, write_records_file(self.db, self.schema(), imp, fh)
        key = object_key(self.prefix, IMPORTS_FOLDER, imp["business_id"], f"import-{imp['uid']}-deleted")
        return key, write_delete_file(self.schema(), imp, send["file_rows"] or [], fh)

    def _send(self, send: dict) -> int:
        """Build and upload one queued file; returns 1 when it went. A failure waits (1, 2, 4… minutes)."""
        fd, path = tempfile.mkstemp(prefix="bucket-", suffix=".csv")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                key, rows = self._build(send, fh)
            if key is None:
                self.db.execute("UPDATE bucket_sends SET status = 'cancelled', error = '' WHERE id = %s",
                                (send["id"],))
                return 0
            size = os.path.getsize(path)
            self.client.put(key, path)
        except Exception as exc:  # noqa: BLE001 - kept on the send and shown on the pages
            msg = str(exc) if isinstance(exc, BucketError) else f"{type(exc).__name__}: {exc}"
            if send["kind"] == "import" and (get_import(self.db, send["import_id"]) or {}).get("status") != "completed":
                # Its records were deleted during this try: don't try again (its deletion, held until now, goes).
                self.db.execute("UPDATE bucket_sends SET status = 'cancelled', error = %s WHERE id = %s",
                                (msg[:1000], send["id"]))
                return 0
            wait = min(60 * 2 ** max(send["attempts"] - 1, 0), MAX_WAIT)
            self.db.execute("""UPDATE bucket_sends SET status = 'pending', error = %s,
                               next_try_at = now() + make_interval(secs => %s) WHERE id = %s""",
                            (msg[:1000], wait, send["id"]))
            log.warning("Sending %s #%s to the bucket failed (try %s, next in %ss): %s", send["kind"], send["id"],
                        send["attempts"], wait, msg)
            return 0
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.db.execute("""UPDATE bucket_sends SET status = 'sent', sent_at = now(), object_key = %s, rows = %s,
                           bytes = %s, error = '', tag_name = %s WHERE id = %s""",
                        (key, rows, size, send["tag_name"], send["id"]))
        log.info("Sent %s #%s to the bucket: %s (%s rows)", send["kind"], send["id"], key, rows)
        return 1


def stop_all() -> None:
    """Stop every running sender (tests: before the app's cached resources, with their database pool, go)."""
    for sender in list(_RUNNING):
        sender.stop()


# ------------------------------------------------------------------------------------------------ what the pages show

def import_sends(db: Database, import_ids: list[int]) -> dict[int, dict]:
    """{import id: its latest send (the deletion when there is one)} for the Imports pages."""
    if not import_ids:
        return {}
    rows = db.fetch("""SELECT DISTINCT ON (import_id) * FROM bucket_sends
                       WHERE import_id = ANY(%s) AND status <> 'cancelled' ORDER BY import_id, id DESC""",
                    (import_ids,))
    return {r["import_id"]: r for r in rows}


def tag_sends(db: Database, tag_id: int) -> dict:
    """A tag's latest send ("latest", or None) and its latest one that went ("sent", or None)."""
    rows = db.fetch("""SELECT * FROM bucket_sends WHERE kind = 'tag' AND tag_id = %s AND status <> 'cancelled'
                       ORDER BY id DESC LIMIT 20""", (tag_id,))
    return {"latest": rows[0] if rows else None, "sent": next((r for r in rows if r["status"] == "sent"), None)}


def summary(db: Database, recent: int = 25) -> dict:
    """Counts (waiting, of which retrying after a failure; sent) and the latest sends, for the Merchants page."""
    counts = db.fetch_one("""SELECT count(*) FILTER (WHERE status IN ('pending', 'sending')) AS waiting,
                                    count(*) FILTER (WHERE status = 'pending' AND error <> '') AS retrying,
                                    count(*) FILTER (WHERE status = 'sent') AS sent
                             FROM bucket_sends""")
    error = db.fetch_one("""SELECT error FROM bucket_sends WHERE status = 'pending' AND error <> ''
                            ORDER BY id DESC LIMIT 1""")
    rows = db.fetch("SELECT * FROM bucket_sends ORDER BY id DESC LIMIT %s", (recent,))
    return dict(counts, error=error["error"] if error else "", recent=rows)


def retry_now(db: Database, send_ids: list[int] | None = None) -> int:
    """Make waiting sends (all, or these) due now; returns how many."""
    if send_ids is None:
        return db.execute("UPDATE bucket_sends SET next_try_at = now() WHERE status = 'pending'")
    return db.execute("UPDATE bucket_sends SET next_try_at = now() WHERE status = 'pending' AND id = ANY(%s)",
                      (send_ids,))
