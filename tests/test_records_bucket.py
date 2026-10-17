"""Sending to the main app's bucket (records.bucket, 1.7.0): the files, the queue, retries, the S3 client.

Most tests use a stand-in client that keeps the files it is given; one runs boto3 against a small local HTTP server
that answers like an S3 bucket (Cloudflare R2's API is S3's).
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest
from psycopg import sql

from converter.storage import Store
from records import bucket as B
from records.store import StoreError

from .records_util import import_file

FILE = ("First Name,Last Name,Phone,Phone 2,Email,Last Visit,Birthday,Total Spent,Favorite\n"
        "Ann,Lee,516-555-0100,(631) 555-0101,ann@example.com,10/01/2026 7:30 PM,03/04/1980,12.50,Bagels\n"
        "Bob,Ray,,,,,,,Lox\n"
        "Cy,Tan,516-555-0102,,,09/15/2026,,,\n")


class FakeClient:
    """Keeps every file put, as {key: text}; ``fail`` makes the next puts raise BucketError."""

    configured = True

    def __init__(self):
        self.files: dict[str, str] = {}
        self.fail: str | None = None

    def put(self, key: str, path: str) -> None:
        if self.fail:
            raise B.BucketError(self.fail)
        with open(path, encoding="utf-8") as fh:
            self.files[key] = fh.read()


def rows_of(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


@pytest.fixture
def jobs(tmp_path):
    return Store(tmp_path / "converter.db")


@pytest.fixture
def setup(rstore, jobs, records_schema, real_schema):
    """(store, jobs, sender with a FakeClient, client)."""
    client = FakeClient()
    sender = B.BucketSender(rstore.db, jobs, lambda: records_schema, lambda: real_schema, client, prefix="data/")
    return rstore, jobs, sender, client


def sends(rstore) -> list[dict]:
    return rstore.db.fetch("SELECT * FROM bucket_sends ORDER BY id")


def test_ulids_are_the_main_apps_and_always_later(monkeypatch):
    """26 characters of Crockford's base 32, the time first (the main app reads a file's version from it)."""
    monkeypatch.setattr(B, "_last_ms", 0)       # as in a new process (other tests made ULIDs of today)
    a = B.new_ulid(1_760_000_000_000)
    b = B.new_ulid(1_760_000_000_000)          # same millisecond: still later
    c = B.new_ulid(1_000)                      # a clock going back: still later
    assert all(re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", x) for x in (a, b, c))
    assert a < b < c
    # 1,760,000,000,000 ms in base 32, ten characters (ClickHouse's ULIDStringToDateTime reads it back exactly).
    assert a[:10] == "01K742SG00"
    key = B.object_key("data/", B.IMPORTS_FOLDER, "63", "import-abc", ulid=a)
    assert key == f"data/customer-records/{a}-63-import-abc.csv"


def test_a_completed_import_is_sent_with_every_record(setup, records_schema):
    """One row per record, also the one without a number; IDs, location, service, numbers, typed values, the row as
    in the file. Sent once."""
    rstore, jobs, sender, client = setup
    rec, imp = import_file(rstore, jobs, records_schema, "guests.csv", FILE, business="63")
    assert sender.run_once() == 1 and sender.run_once() == 0
    [(key, text)] = client.files.items()
    uid = str(rstore.db.fetch_one("SELECT uid FROM imports WHERE id = %s", (imp,))["uid"])
    assert re.fullmatch(rf"data/customer-records/[0-9A-Z]{{26}}-63-import-{uid}\.csv", key)
    rows = rows_of(text)
    assert list(rows[0]) == B.record_columns(records_schema)
    assert [r["file_row"] for r in rows] == ["2", "3", "4"]          # the spreadsheet's rows (1 is the header)
    ann, bob, cy = rows
    assert ann["record_key"] == f"{uid}:2" and ann["import_uid"] == uid and ann["is_deleted"] == "0"
    assert (ann["merchant_id"], ann["location_id"], ann["service"]) == ("63", "63-main", "Toast export")
    assert (ann["phone_number"], ann["phone_numbers"]) == ("5165550100", "5165550100|6315550101")
    assert (ann["first_name"], ann["email"], ann["total_spent"]) == ("Ann", "ann@example.com", "12.5")
    assert ann["transactions_last"] == "2026-10-01" and ann["birthday"] == "1980-03-04"
    assert json.loads(ann["source"])["Favorite"] == "Bagels"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", ann["imported_at"]) and ann["imported_by"] == "tester"
    assert (bob["phone_number"], bob["phone_issue"]) == ("", "no phone number")
    assert cy["phone_numbers"] == "5165550102"
    [s] = sends(rstore)
    assert (s["kind"], s["status"], s["rows"], s["object_key"]) == ("import", "sent", 3, key)
    assert s["bytes"] == len(text.encode())


def test_an_import_waits_for_its_run_to_finish(setup, records_schema):
    """Records saved but the run not completed yet (or ended otherwise): not queued."""
    rstore, jobs, sender, client = setup
    rec, imp = import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="63")
    real = jobs.get_import

    class Running:
        status = "running"
    jobs.get_import = lambda job_id: Running()
    assert sender.run_once() == 0 and sends(rstore) == []
    jobs.get_import = real
    assert sender.run_once() == 1


def test_deleting_records_sends_their_deletion(setup, records_schema):
    """Sent, then deleted: a file with the same keys marked deleted. Deleted before it went: nothing is sent."""
    rstore, jobs, sender, client = setup
    _, imp = import_file(rstore, jobs, records_schema, "guests.csv", FILE, business="63")
    sender.run_once()
    rstore.delete_import(imp, "noah")
    assert sender.run_once() == 1
    key = next(k for k in client.files if k.endswith("-deleted.csv"))
    uid = str(rstore.db.fetch_one("SELECT uid FROM imports WHERE id = %s", (imp,))["uid"])
    assert key.endswith(f"-63-import-{uid}-deleted.csv")
    rows = rows_of(client.files[key])
    assert [(r["record_key"], r["is_deleted"], r["first_name"]) for r in rows] == \
        [(f"{uid}:2", "1", ""), (f"{uid}:3", "1", ""), (f"{uid}:4", "1", "")]
    assert sends(rstore)[-1]["created_by"] == "noah"

    _, imp2 = import_file(rstore, jobs, records_schema, "b.csv", "Phone\n5165550109\n", business="63")
    sender.queue_completed_imports()
    rstore.delete_import(imp2)                      # deleted while waiting: never sent
    before = len(client.files)
    assert sender.run_once() == 0 and len(client.files) == before
    assert [s["status"] for s in sends(rstore) if s["import_id"] == imp2] == ["cancelled"]


def test_an_import_deleted_while_its_send_fails_is_never_sent(setup, records_schema):
    """Its send was being tried (and failed); the records are deleted meanwhile: the deletion goes, the import never
    does (it would come after the deletion and bring the records back)."""
    rstore, jobs, sender, client = setup
    _, imp = import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="63")
    sender.queue_completed_imports()
    rstore.db.execute("UPDATE bucket_sends SET status = 'sending' WHERE import_id = %s", (imp,))
    rstore.delete_import(imp)
    rstore.db.execute("UPDATE bucket_sends SET status = 'pending' WHERE kind = 'import'")   # the try failed
    assert sender.run_once() == 1
    assert [k.endswith("-deleted.csv") for k in client.files] == [True]
    assert [s["status"] for s in sends(rstore)] == ["cancelled", "sent"]


def uid_of(rstore, import_id: int) -> str:
    return str(rstore.db.fetch_one("SELECT uid FROM imports WHERE id = %s", (import_id,))["uid"])


def test_a_deletion_waits_until_its_imports_send_is_settled(setup, records_schema):
    """Found by the independent test of 1.7.0: a deletion went out while its import's send was still being tried.
    Now it is held until that send is settled, and goes after an import file that arrives."""
    rstore, jobs, sender, client = setup
    _, imp = import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="63")
    sender.queue_completed_imports()
    rstore.db.execute("UPDATE bucket_sends SET status = 'sending', claimed_by = %s, attempts = 1",
                      (sender.identity,))                             # being uploaded by this sender right now
    rstore.delete_import(imp)
    assert sender.run_once() == 0 and client.files == {}             # held while the upload runs
    assert [s["status"] for s in sends(rstore)] == ["sending", "pending"]

    real_put = client.put
    _, imp2 = import_file(rstore, jobs, records_schema, "b.csv", "Phone\n5165550101\n", business="63")
    done = []

    def deleted_during_upload(key, path):                            # the upload itself succeeds
        if f"import-{uid_of(rstore, imp2)}.csv" in key and not done:
            done.append(key)
            rstore.delete_import(imp2)
        real_put(key, path)
    client.put = deleted_during_upload
    sender.run_once()
    keys = sorted(k for k in client.files if uid_of(rstore, imp2) in k)
    assert [k.endswith("-deleted.csv") for k in keys] == [False, True]   # the import's file first (ULID order)


def test_a_try_that_may_have_landed_still_gets_its_deletion(setup, records_schema):
    """The import's send failed (or was cut off) and waits; its records are deleted: the file may have reached the
    bucket all the same, so the deletion goes, once the import's send is given up. One never tried needs none."""
    rstore, jobs, sender, client = setup
    _, imp = import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="63")
    client.fail = "Can't reach the bucket at https://x: read timeout"
    sender.run_once()
    client.fail = None
    rstore.delete_import(imp)
    assert sender.run_once() == 1
    assert [(s["kind"], s["status"]) for s in sends(rstore)] == [("import", "cancelled"), ("delete", "sent")]
    assert [k.endswith(f"-63-import-{uid_of(rstore, imp)}-deleted.csv") for k in client.files] == [True]

    _, imp2 = import_file(rstore, jobs, records_schema, "b.csv", "Phone\n5165550101\n", business="63")
    sender.queue_completed_imports()
    rstore.delete_import(imp2)                                       # never tried: nothing to delete there
    assert sender.run_once() == 0
    assert [s["status"] for s in sends(rstore) if s["import_id"] == imp2] == ["cancelled"]


def test_sends_another_process_left_half_done_are_taken_over_at_once(setup, records_schema):
    """Found by the independent test of 1.7.0: after a restart a send left 'sending' waited 30 minutes. A new
    sender takes such sends over at its first run (one app server per database)."""
    rstore, jobs, sender, client = setup
    _, imp = import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="63")
    _, imp2 = import_file(rstore, jobs, records_schema, "b.csv", "Phone\n5165550101\n", business="63")
    sender.queue_completed_imports()
    rstore.db.execute("""UPDATE bucket_sends SET status = 'sending', claimed_by = 'old-server:1:0', attempts = 1,
                         claimed_at = now()""")
    rstore.delete_import(imp2)                                       # deleted while its upload was cut off
    assert sender.run_once() == 2
    assert sorted((s["import_id"], s["kind"], s["status"]) for s in sends(rstore)) == \
        [(imp, "import", "sent"), (imp2, "delete", "sent"), (imp2, "import", "cancelled")]


def test_a_deleted_tags_last_file_has_its_latest_name(setup, records_schema):
    """Found by the independent test of 1.7.0: a snapshot waiting when the tag was renamed and then deleted kept
    the old name."""
    rstore, jobs, sender, client = setup
    import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="63")
    tid = rstore.create_tag("63", "T3old")
    rstore.queue_tag_send(tid)
    sender.run_once()
    client.fail = "down"
    rstore.queue_tag_send(tid)
    sender.run_once()                                                # waits after a failure
    rstore.update_tag(tid, "T3new", "")
    rstore.delete_tag(tid)
    client.fail = None
    B.retry_now(rstore.db)
    sender.run_once()
    last = rows_of(client.files[max(k for k in client.files if "-tag-" in k)])
    assert [(r["tag_name"], r["tag_deleted"]) for r in last] == [("T3new", "1")]
    assert [s["tag_name"] for s in sends(rstore) if s["kind"] == "tag"] == ["T3old", "T3new"]


def test_a_tag_is_sent_as_a_snapshot(setup, records_schema, real_schema):
    """The import format's columns plus the tag's; a first row naming the tag; one row per number. One snapshot
    waits at a time; a tag that was sent and is deleted gets a last one saying so; one never sent, none."""
    rstore, jobs, sender, client = setup
    import_file(rstore, jobs, records_schema, "guests.csv", FILE, business="63")
    tid = rstore.create_tag("63", "October promo")
    rstore.add_to_tag(tid, sql.SQL("true"), {})
    assert rstore.queue_tag_send(tid, "noah") is True
    assert rstore.queue_tag_send(tid, "noah") is False             # one is waiting already
    sender.run_once()
    uid = str(rstore.db.fetch_one("SELECT uid FROM tags WHERE id = %s", (tid,))["uid"])
    key = next(k for k in client.files if "customer-records-tags/" in k)
    assert re.fullmatch(rf"data/customer-records-tags/[0-9A-Z]{{26}}-63-tag-{uid}\.csv", key)
    rows = rows_of(client.files[key])
    assert list(rows[0]) == [f.header for f in real_schema.fields] + B.TAG_COLUMNS
    head, *numbers = rows
    assert (head["row_kind"], head["phone_number"], head["tag_name"], head["tag_uid"], head["tag_deleted"]) == \
        ("tag", "", "October promo", uid, "0")
    assert sorted(r["phone_number"] for r in numbers) == ["5165550100", "5165550102", "6315550101"]
    assert {r["row_kind"] for r in numbers} == {"number"} and {r["merchant_id"] for r in numbers} == {"63"}
    assert next(r for r in numbers if r["phone_number"] == "5165550100")["transactions_last"] == \
        "10/01/2026 12:00 AM"
    rstore.delete_tag(tid, "noah")
    assert sender.run_once() == 1
    last = rows_of(client.files[max(k for k in client.files if "-tag-" in k)])
    assert [(r["row_kind"], r["tag_deleted"], r["tag_uid"]) for r in last] == [("tag", "1", uid)]

    never = rstore.create_tag("63", "Draft")
    rstore.queue_tag_send(never)
    rstore.delete_tag(never)
    assert sender.run_once() == 0
    assert [s["status"] for s in sends(rstore) if s["tag_id"] == never] == ["cancelled"]


def test_a_failed_send_waits_longer_each_time(setup, records_schema):
    """The reason is kept; tries 1, 2, 4… minutes apart; Try again now; nothing lost meanwhile."""
    rstore, jobs, sender, client = setup
    import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="63")
    client.fail = "The bucket refused the file (HTTP 403, AccessDenied: Access Denied)."
    assert sender.run_once() == 0
    [s] = sends(rstore)
    assert (s["status"], s["attempts"], s["error"]) == ("pending", 1, client.fail)
    wait = rstore.db.fetch_one("SELECT extract(epoch FROM next_try_at - now()) AS s FROM bucket_sends")["s"]
    assert 50 < wait <= 60
    assert sender.run_once() == 0 and sends(rstore)[0]["attempts"] == 1     # not due yet
    assert B.retry_now(rstore.db) == 1
    assert sender.run_once() == 0
    wait = rstore.db.fetch_one("SELECT extract(epoch FROM next_try_at - now()) AS s FROM bucket_sends")["s"]
    assert 110 < wait <= 120                                                  # the second try: 2 minutes
    assert B.summary(rstore.db)["retrying"] == 1 and B.summary(rstore.db)["error"] == client.fail
    client.fail = None
    B.retry_now(rstore.db, [s["id"]])
    assert sender.run_once() == 1
    assert (sends(rstore)[0]["status"], sends(rstore)[0]["error"]) == ("sent", "")


def test_a_send_cut_off_by_a_restart_is_tried_again(setup, records_schema):
    rstore, jobs, sender, client = setup
    import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="63")
    sender.queue_completed_imports()
    rstore.db.execute("UPDATE bucket_sends SET status = 'sending', claimed_at = now() - interval '2 hours'")
    assert sender.run_once() == 1


def test_without_bucket_settings_nothing_is_sent(rstore, jobs, records_schema, real_schema):
    """What is queued waits; the thread doesn't start."""
    sender = B.BucketSender(rstore.db, jobs, lambda: records_schema, lambda: real_schema,
                            B.BucketClient("", "", "", ""))
    import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="63")
    sender.start()
    assert not sender.configured and sender._thread is None
    assert sender.run_once() == 0 and sends(rstore) == []


def test_only_main_app_merchants_are_sent(setup, records_schema):
    """A business typed in by hand before 1.6.0 (its ID not a number) isn't sent; its tags can't be."""
    rstore, jobs, sender, client = setup
    import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="KRV-1")
    assert sender.run_once() == 0 and sends(rstore) == []
    tid = rstore.create_tag("KRV-1", "Promo")
    with pytest.raises(StoreError, match="isn't a merchant of the main app"):
        rstore.queue_tag_send(tid)
    rstore.add_business("٣", "Arabic-Indic three")                 # digits, but not the ASCII ones the main app reads
    with pytest.raises(StoreError, match="isn't a merchant of the main app"):
        rstore.queue_tag_send(rstore.create_tag("٣", "Promo"))


def test_the_background_thread_sends(setup, records_schema):
    rstore, jobs, sender, client = setup
    sender.interval = 30
    import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="63")
    sender.start()
    try:
        end = time.monotonic() + 10
        while not client.files and time.monotonic() < end:
            time.sleep(0.05)
        assert len(client.files) == 1
        tid = rstore.create_tag("63", "Promo")
        rstore.queue_tag_send(tid)
        sender.wake()                                   # at once, not in 30 s
        while len(client.files) < 2 and time.monotonic() < end:
            time.sleep(0.05)
        assert len(client.files) == 2
    finally:
        sender.stop()


def test_a_field_named_like_a_file_column_is_refused(records_schema):
    bad = records_schema.model_copy(deep=True)
    bad.fields[1].name = "service"
    with pytest.raises(ValueError, match="service"):
        B.record_columns(bad)


class FakeS3(BaseHTTPRequestHandler):
    """Answers PUT like an S3 bucket (path-style: /<bucket>/<key>); ``status`` 403 refuses like R2 does."""

    protocol_version = "HTTP/1.1"
    objects: dict[str, bytes] = {}
    seen: list[dict] = []
    status = 200

    def do_PUT(self):  # noqa: N802 - http.server's name
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        FakeS3.seen.append({"path": unquote(self.path), "auth": self.headers.get("Authorization", ""),
                            "type": self.headers.get("Content-Type", "")})
        if FakeS3.status != 200:
            payload = (b'<?xml version="1.0" encoding="UTF-8"?><Error><Code>AccessDenied</Code>'
                       b"<Message>Access Denied</Message></Error>")
            self.send_response(FakeS3.status)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        FakeS3.objects[unquote(self.path)] = body
        self.send_response(200)
        self.send_header("ETag", '"0"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def s3():
    FakeS3.objects, FakeS3.seen, FakeS3.status = {}, [], 200
    # Threaded: boto3 keeps its connection open (HTTP/1.1), which would hold a single-threaded server's shutdown.
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeS3)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_the_s3_client(s3, tmp_path):
    """boto3, path-style, signed with the key ID (never the secret); a refusal and an unreachable bucket are said
    plainly, without the key."""
    client = B.BucketClient(s3, "master-bucket", "AKID123", "s3cr3t-value")
    assert client.configured and "s3cr3t" not in repr(client)
    f = tmp_path / "x.csv"
    f.write_text("phone_number\n5165550100\n")
    client.put("data/customer-records/01K-63-import-x.csv", str(f))
    assert FakeS3.objects == {"/master-bucket/data/customer-records/01K-63-import-x.csv": b"phone_number\n5165550100\n"}
    [req] = FakeS3.seen
    assert req["auth"].startswith("AWS4-HMAC-SHA256 Credential=AKID123/") and "s3cr3t" not in req["auth"]
    assert req["type"].startswith("text/csv")
    FakeS3.status = 403
    with pytest.raises(B.BucketError, match=r"refused the file \(HTTP 403, AccessDenied: Access Denied\)") as exc:
        client.put("data/x.csv", str(f))
    assert "s3cr3t" not in str(exc.value)
    away = B.BucketClient("http://127.0.0.1:9", "master-bucket", "AKID123", "s3cr3t-value")
    with pytest.raises(B.BucketError, match="Can't reach the bucket at http://127.0.0.1:9") as exc:
        away.put("data/x.csv", str(f))
    assert "s3cr3t" not in str(exc.value)
