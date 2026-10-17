"""Merchants, locations, services, imports, records and tags: every read and write of Customer Records outside
finds and tag exports (``records.find``, ``records.tags``), which build their own SQL.

Overview
--------
:class:`RecordStore` wraps :class:`records.db.Database`. Methods take and return plain Python values (dicts,
lists, ints) so the web pages and tests stay simple. Errors a person can fix (a tag name used twice, a service
still in use, an import for a merchant without a location) raise :class:`StoreError` with a sentence to show.

Rules enforced here
-------------------
* Merchants and locations are the main app's (1.6.0, :meth:`load_merchants`); the table of merchants keeps its
  1.0 name, ``businesses``, and "business" in the code means a merchant. They are never deleted here: one that
  leaves the main app's list is marked removed and no longer offered.
* Every import is for one location of its merchant (:meth:`create_import`); its records carry both.
* A service can only be deleted while nothing refers to it; it can be deactivated instead (it is then no longer
  offered on New import, and its records keep it).
* A tag belongs to one merchant: :meth:`add_to_tag` only adds that merchant's records and says how many records
  of the found set were left out for belonging to another merchant.
* Every change to a tag is logged in ``tag_events`` (who, when, what, how many records).
* Deleting an import deletes its records; each tag that loses records gets an event saying so.
"""

from __future__ import annotations

import json
import re
from typing import Any

import psycopg
from psycopg import sql

from .db import Database


class StoreError(ValueError):
    """A problem a person can fix (shown on the page as it is)."""


def _clean(text: str | None) -> str:
    """Trimmed text, '' for None."""
    return (text or "").strip()


def queue_import_deletion(c: psycopg.Connection, import_id: int, username: str = "") -> None:
    """Before an import's records are deleted (on connection ``c``, in the same transaction): queue the deletion
    file for the main app's bucket when the import's file was sent or may have been (a try was made: it can have
    landed even if it failed or was cut off); a send never tried is dropped instead. The sender holds the deletion
    until the import's own send is settled (records.bucket)."""
    imp = c.execute("""SELECT status, attempts FROM bucket_sends WHERE kind = 'import' AND import_id = %s""",
                    (import_id,)).fetchone()
    if imp is None:
        return
    if imp["status"] == "pending" and imp["attempts"] == 0:
        c.execute("""UPDATE bucket_sends SET status = 'cancelled' WHERE kind = 'import' AND import_id = %s""",
                  (import_id,))
        return
    if imp["status"] == "pending":
        # Waiting after a failed try: make it due now, so it is cancelled (records gone) and the deletion can go.
        c.execute("""UPDATE bucket_sends SET next_try_at = now() WHERE kind = 'import' AND import_id = %s""",
                  (import_id,))
    rows = [r["file_row"] for r in c.execute("SELECT file_row FROM records WHERE import_id = %s",
                                             (import_id,)).fetchall()]
    c.execute("""INSERT INTO bucket_sends (kind, import_id, file_rows, business_id, created_by)
                 SELECT 'delete', id, %s, business_id, %s FROM imports WHERE id = %s
                 ON CONFLICT DO NOTHING""", (rows, username, import_id))


class RecordStore:
    """Merchants, locations, services, imports, records and tags in PostgreSQL (see the module docstring)."""

    def __init__(self, db: Database):
        """Use ``db`` (already set up)."""
        self.db = db

    # ------------------------------------------------------------------ merchants and locations

    def list_businesses(self) -> list[dict]:
        """Every merchant (``businesses``) with its status, number of locations, imports, records and tags, by name.

        Merchants no longer in the main app's list (``removed_at`` set) are included, last.
        """
        return self.db.fetch("""
            SELECT b.id, b.name, b.notes, b.status, b.removed_at, b.synced_at, b.created_at, b.created_by,
                   (SELECT count(*) FROM locations l WHERE l.business_id = b.id AND l.removed_at IS NULL) AS locations,
                   (SELECT count(*) FROM imports i WHERE i.business_id = b.id AND i.status = 'completed') AS imports,
                   (SELECT count(*) FROM records r WHERE r.business_id = b.id) AS records,
                   (SELECT count(*) FROM tags t WHERE t.business_id = b.id) AS tags
            FROM businesses b ORDER BY b.removed_at IS NOT NULL, lower(b.name), b.id""")

    def business(self, business_id: str) -> dict | None:
        """One merchant, or None."""
        return self.db.fetch_one("SELECT * FROM businesses WHERE id = %s", (str(business_id),))

    def add_business(self, business_id: str, name: str, notes: str = "", username: str = "") -> None:
        """Add one merchant by hand (tests and scripts; the app loads them with :meth:`load_merchants`)."""
        bid, nm = _clean(str(business_id)), _clean(name)
        if not bid or not nm:
            raise StoreError("A merchant needs an ID (the same as in the main app) and a name.")
        try:
            self.db.execute("INSERT INTO businesses (id, name, notes, created_by) VALUES (%s, %s, %s, %s)",
                            (bid, nm, _clean(notes), username))
        except psycopg.errors.UniqueViolation:
            raise StoreError(f"A merchant with ID '{bid}' already exists.") from None

    def add_location(self, location_id: str, business_id: str, name: str, address: str = "",
                     kind: str = "store") -> None:
        """Add one location by hand (tests and scripts; the app loads them with :meth:`load_merchants`)."""
        self.db.execute("INSERT INTO locations (id, business_id, name, address, kind) VALUES (%s, %s, %s, %s, %s)",
                        (_clean(location_id), str(business_id), _clean(name), _clean(address), _clean(kind)))

    def list_locations(self, business_ids: list[str] | None = None, current_only: bool = False) -> list[dict]:
        """Locations (of these merchants, or all) with their merchant's name, by merchant and location name.

        ``current_only``: only locations still in the main app's list, of active merchants still in it.
        """
        where = [sql.SQL("true")]
        if business_ids is not None:
            where.append(sql.SQL("l.business_id = ANY(%(b)s)"))
        if current_only:
            where.append(sql.SQL("l.removed_at IS NULL AND b.removed_at IS NULL AND b.status = 'active'"))
        return self.db.fetch(sql.SQL("""
            SELECT l.*, b.name AS business_name FROM locations l JOIN businesses b ON b.id = l.business_id
            WHERE {} ORDER BY lower(b.name), b.id, lower(l.name), l.id""").format(sql.SQL(" AND ").join(where)),
            {"b": [str(x) for x in business_ids or []]})

    def location(self, location_id: str) -> dict | None:
        """One location with its merchant's name, or None."""
        return self.db.fetch_one("""SELECT l.*, b.name AS business_name FROM locations l
                                    JOIN businesses b ON b.id = l.business_id WHERE l.id = %s""", (location_id,))

    def load_merchants(self, merchants: list[dict], locations: list[dict], source: str, username: str = "") -> dict:
        """Make the merchants and locations those of the main app (records.merchants reads them), in one transaction.

        ``merchants``: {"id", "name", "status"}; ``locations``: {"id", "merchant_id", "name", "address", "kind"}.
        New ones are added, known ones updated (name, status, address, a location's merchant), and ones missing
        from the lists are marked removed (``removed_at``): kept, because imports, records and tags refer to them,
        but no longer offered. One that comes back is offered again ("back"). An empty merchant list changes nothing
        (a failed export, not a main app without merchants). Returns the counts of each and what changed; the time,
        source and counts are kept in ``app_meta`` ("merchants_loaded"). A merchant added by hand before 1.6.0 whose
        ID is a main app merchant ID becomes that merchant (its name replaced), with its imports and records.
        """
        if not merchants:
            raise StoreError("The list has no merchants, so nothing was changed.")
        ids = [m["id"] for m in merchants]
        loc_ids = [x["id"] for x in locations]
        unknown = sorted({x["merchant_id"] for x in locations} - set(ids))
        if unknown:
            raise StoreError(f"Locations refer to merchants that aren't in the list ({', '.join(unknown[:5])}); "
                             "export the merchants and their locations together.")
        with self.db.conn() as c:
            known_m = {r["id"]: r["removed_at"] is None
                       for r in c.execute("SELECT id, removed_at FROM businesses").fetchall()}
            known_l = {r["id"]: r["removed_at"] is None
                       for r in c.execute("SELECT id, removed_at FROM locations").fetchall()}
            with c.cursor() as cur:
                cur.executemany("""
                    INSERT INTO businesses (id, name, status, removed_at, synced_at, created_by)
                    VALUES (%(id)s, %(name)s, %(status)s, NULL, now(), %(by)s)
                    ON CONFLICT (id) DO UPDATE SET name = excluded.name, status = excluded.status, removed_at = NULL,
                                                   synced_at = now()""",
                                [dict(m, by=username) for m in merchants])
                if locations:
                    cur.executemany("""
                        INSERT INTO locations (id, business_id, name, address, kind, removed_at, synced_at)
                        VALUES (%(id)s, %(merchant_id)s, %(name)s, %(address)s, %(kind)s, NULL, now())
                        ON CONFLICT (id) DO UPDATE SET business_id = excluded.business_id, name = excluded.name,
                            address = excluded.address, kind = excluded.kind, removed_at = NULL,
                            synced_at = now()""", locations)
            gone_m = c.execute("UPDATE businesses SET removed_at = now() WHERE removed_at IS NULL AND "
                               "id <> ALL(%s)", (ids,)).rowcount
            gone_l = c.execute("UPDATE locations SET removed_at = now() WHERE removed_at IS NULL AND "
                               "id <> ALL(%s)", (loc_ids,)).rowcount
            summary = {"merchants": len(ids), "locations": len(loc_ids),
                       "new_merchants": len(set(ids) - set(known_m)), "new_locations": len(set(loc_ids) - set(known_l)),
                       "back_merchants": sum(known_m.get(i) is False for i in ids),
                       "back_locations": sum(known_l.get(i) is False for i in loc_ids),
                       "removed_merchants": gone_m, "removed_locations": gone_l}
            c.execute("""INSERT INTO app_meta (key, value) VALUES ('merchants_loaded', %s)
                         ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
                      (json.dumps(dict(summary, source=source, by=username,
                                       at=c.execute("SELECT now() AS t").fetchone()["t"].isoformat())),))
        return summary

    def merchants_loaded(self) -> dict | None:
        """When, from where and by whom the merchants were last loaded, with the counts (or None: never)."""
        row = self.db.fetch_one("SELECT value FROM app_meta WHERE key = 'merchants_loaded'")
        return json.loads(row["value"]) if row else None

    # ------------------------------------------------------------------ services

    def list_services(self, active_only: bool = False) -> list[dict]:
        """Services by name, with how many imports and records use each."""
        return self.db.fetch(sql.SQL("""
            SELECT s.id, s.name, s.active, s.created_at, s.created_by,
                   (SELECT count(*) FROM imports i WHERE i.service_id = s.id AND i.status = 'completed') AS imports,
                   (SELECT count(*) FROM records r WHERE r.service_id = s.id) AS records
            FROM services s {} ORDER BY lower(s.name)""").format(
            sql.SQL("WHERE s.active") if active_only else sql.SQL("")))

    def add_service(self, name: str, username: str = "") -> int:
        """Add a service to the list; returns its id."""
        nm = _clean(name)
        if not nm:
            raise StoreError("A service needs a name.")
        try:
            return self.db.fetch_one("INSERT INTO services (name, created_by) VALUES (%s, %s) RETURNING id",
                                     (nm, username))["id"]
        except psycopg.errors.UniqueViolation:
            raise StoreError(f"The service '{nm}' already exists.") from None

    def update_service(self, service_id: int, name: str, active: bool) -> None:
        """Rename a service or (de)activate it. Its records keep it either way."""
        nm = _clean(name)
        if not nm:
            raise StoreError("A service needs a name.")
        try:
            self.db.execute("UPDATE services SET name = %s, active = %s WHERE id = %s", (nm, active, service_id))
        except psycopg.errors.UniqueViolation:
            raise StoreError(f"The service '{nm}' already exists.") from None

    def delete_service(self, service_id: int) -> None:
        """Delete a service no import uses (otherwise deactivate it)."""
        if self.db.fetch_one("SELECT 1 FROM imports WHERE service_id = %s LIMIT 1", (service_id,)):
            raise StoreError("This service is used by imports; deactivate it instead.")
        self.db.execute("DELETE FROM services WHERE id = %s", (service_id,))

    # ------------------------------------------------------------------ imports

    def create_import(self, business_id: str, service_id: int, file_name: str, file_sha1: str,
                      sheet: str | None, username: str, location_id: str | None = None) -> int:
        """Register an import before it runs (status 'queued'); returns its id (``imports.id``).

        ``location_id``: the location of the merchant ``business_id`` the file is for. Left out, the merchant's only
        location is used; a merchant with several needs one chosen, and one without any can't be imported for.
        """
        biz = self.business(business_id) if business_id else None
        if not biz:
            raise StoreError("Choose a merchant for this import.")
        if biz["removed_at"] is not None or biz["status"] != "active":
            raise StoreError(f"Merchant {biz['id']} · {biz['name']} is {biz['status']} or no longer in the main "
                             "app.")
        current = self.list_locations([biz["id"]], current_only=True)
        if location_id is None:
            if not current:
                raise StoreError(f"Merchant {biz['id']} · {biz['name']} has no location in the main app; add one "
                                 "there first, then reload the merchants.")
            if len(current) > 1:
                raise StoreError("Choose the location this file is for.")
            location_id = current[0]["id"]
        elif (location_id := location_id.strip().lower()) not in {x["id"] for x in current}:
            raise StoreError("Choose one of this merchant's locations.")
        svc = self.db.fetch_one("SELECT active FROM services WHERE id = %s", (service_id,))
        if not svc:
            raise StoreError("Choose a service for this import.")
        return self.db.fetch_one("""INSERT INTO imports (business_id, location_id, service_id, file_name, file_sha1,
                                    sheet, username) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                                 (biz["id"], location_id, service_id, file_name, file_sha1, sheet, username))["id"]

    def set_job(self, import_id: int, job_id: int) -> None:
        """Remember the app's import number (data/converter.db) that runs this import."""
        self.db.execute("UPDATE imports SET job_id = %s WHERE id = %s", (job_id, import_id))

    def get_import(self, import_id: int) -> dict | None:
        """One import with its merchant, location and service names (location: None before 1.6.0)."""
        return self.db.fetch_one("""
            SELECT i.*, b.name AS business_name, l.name AS location_name, l.address AS location_address,
                   s.name AS service_name
            FROM imports i JOIN businesses b ON b.id = i.business_id JOIN services s ON s.id = i.service_id
            LEFT JOIN locations l ON l.id = i.location_id
            WHERE i.id = %s""", (import_id,))

    def imports_for_jobs(self, job_ids: list[int]) -> dict[int, dict]:
        """{job id: its import (newest when several)} for the Imports list and pages."""
        if not job_ids:
            return {}
        rows = self.db.fetch("""
            SELECT DISTINCT ON (i.job_id) i.*, b.name AS business_name, l.name AS location_name,
                   s.name AS service_name
            FROM imports i JOIN businesses b ON b.id = i.business_id JOIN services s ON s.id = i.service_id
            LEFT JOIN locations l ON l.id = i.location_id
            WHERE i.job_id = ANY(%s) ORDER BY i.job_id, i.id DESC""", (job_ids,))
        return {r["job_id"]: r for r in rows}

    def same_file_imports(self, business_id: str, file_sha1: str) -> list[dict]:
        """Completed imports of exactly this file for this merchant (to warn before importing it twice)."""
        return self.db.fetch("""SELECT i.id, i.job_id, i.file_name, i.completed_at, i.records, s.name AS service_name
                                FROM imports i JOIN services s ON s.id = i.service_id
                                WHERE i.business_id = %s AND i.file_sha1 = %s AND i.status = 'completed'
                                ORDER BY i.id""", (business_id, file_sha1))

    def set_import_status(self, import_id: int, status: str) -> None:
        """Mark an import failed / cancelled (its records, if any, are deleted by the caller)."""
        self.db.execute("UPDATE imports SET status = %s WHERE id = %s", (status, import_id))

    def delete_import(self, import_id: int, username: str = "") -> dict:
        """Delete an import's records (and with them their place in tags). The import stays, marked 'deleted'.

        Returns {"records": n, "tags": {tag name: records removed}}. Each tag that lost records gets an event. An
        import already sent to the main app's bucket gets its deletion queued (records.bucket); one waiting to be
        sent isn't sent.
        """
        with self.db.conn() as c:
            job = (c.execute("SELECT job_id FROM imports WHERE id = %s", (import_id,)).fetchone() or {}).get("job_id")
            hit = c.execute("""SELECT t.id, t.name, count(*) AS n FROM tag_records tr
                               JOIN records r ON r.id = tr.record_id JOIN tags t ON t.id = tr.tag_id
                               WHERE r.import_id = %s GROUP BY t.id, t.name""", (import_id,)).fetchall()
            queue_import_deletion(c, import_id, username)
            n = c.execute("DELETE FROM records WHERE import_id = %s", (import_id,)).rowcount
            c.execute("UPDATE imports SET status = 'deleted' WHERE id = %s", (import_id,))
            for t in hit:
                c.execute("""INSERT INTO tag_events (tag_id, username, action, count, detail)
                             VALUES (%s, %s, 'removed', %s, %s)""",
                          (t["id"], username, t["n"], f"import #{job or import_id} was deleted"))
                c.execute("UPDATE tags SET updated_at = now() WHERE id = %s", (t["id"],))
        return {"records": n, "tags": {t["name"]: t["n"] for t in hit}}

    def columns_seen(self) -> list[str]:
        """Every column name found in completed imports' files ("Other column" searches), sorted."""
        rows = self.db.fetch("""SELECT DISTINCT jsonb_array_elements_text(columns) AS c FROM imports
                                WHERE status = 'completed' ORDER BY 1""")
        return [r["c"] for r in rows]

    # ------------------------------------------------------------------ records

    def record(self, record_id: int) -> dict | None:
        """One record with its phone numbers, merchant, location, service, import and tags."""
        rec = self.db.fetch_one("""
            SELECT r.*, b.name AS business_name, l.name AS location_name, s.name AS service_name, i.file_name,
                   i.job_id, i.sheet
            FROM records r JOIN businesses b ON b.id = r.business_id JOIN services s ON s.id = r.service_id
            JOIN imports i ON i.id = r.import_id LEFT JOIN locations l ON l.id = r.location_id
            WHERE r.id = %s""", (record_id,))
        if rec is None:
            return None
        rec["phones"] = self.db.fetch("""SELECT phone, position, repaired, original FROM record_phones
                                         WHERE record_id = %s ORDER BY position""", (record_id,))
        rec["tags"] = self.db.fetch("""SELECT t.id, t.name, tr.added_at, tr.added_by FROM tag_records tr
                                       JOIN tags t ON t.id = tr.tag_id WHERE tr.record_id = %s ORDER BY t.name""",
                                    (record_id,))
        return rec

    def totals(self) -> dict:
        """Counts for the sidebar and empty states: records, merchants (current), services, tags."""
        return self.db.fetch_one("""SELECT (SELECT count(*) FROM records) AS records,
                                           (SELECT count(*) FROM businesses WHERE removed_at IS NULL) AS businesses,
                                           (SELECT count(*) FROM services WHERE active) AS services,
                                           (SELECT count(*) FROM tags) AS tags""")

    # ------------------------------------------------------------------ tags

    def list_tags(self, business_id: str | None = None) -> list[dict]:
        """Tags (optionally of one merchant) with record and unique-number counts, newest change first."""
        where = sql.SQL("WHERE t.business_id = %(b)s") if business_id else sql.SQL("")
        return self.db.fetch(sql.SQL("""
            SELECT t.id, t.name, t.notes, t.business_id, b.name AS business_name, t.created_at, t.created_by,
                   t.updated_at,
                   (SELECT count(*) FROM tag_records tr WHERE tr.tag_id = t.id) AS records,
                   (SELECT count(DISTINCT p.phone) FROM tag_records tr JOIN record_phones p
                      ON p.record_id = tr.record_id WHERE tr.tag_id = t.id) AS numbers
            FROM tags t JOIN businesses b ON b.id = t.business_id {} ORDER BY t.updated_at DESC, t.id DESC""").format(
            where), {"b": business_id})

    def tag(self, tag_id: int) -> dict | None:
        """One tag with its merchant's name and counts (records, unique numbers, records without a number)."""
        return self.db.fetch_one("""
            SELECT t.*, b.name AS business_name,
                   (SELECT count(*) FROM tag_records tr WHERE tr.tag_id = t.id) AS records,
                   (SELECT count(DISTINCT p.phone) FROM tag_records tr JOIN record_phones p
                      ON p.record_id = tr.record_id WHERE tr.tag_id = t.id) AS numbers,
                   (SELECT count(*) FROM tag_records tr WHERE tr.tag_id = t.id AND NOT EXISTS
                      (SELECT 1 FROM record_phones p WHERE p.record_id = tr.record_id)) AS no_phone
            FROM tags t JOIN businesses b ON b.id = t.business_id WHERE t.id = %s""", (tag_id,))

    def create_tag(self, business_id: str, name: str, notes: str = "", username: str = "") -> int:
        """Create an empty tag for one merchant; returns its id. Names are unique per merchant."""
        nm = _clean(name)
        if not nm:
            raise StoreError("A tag needs a name.")
        if not business_id or not self.business(business_id):
            raise StoreError("Choose the merchant this tag is for.")
        try:
            with self.db.conn() as c:
                tid = c.execute("""INSERT INTO tags (business_id, name, notes, created_by) VALUES (%s, %s, %s, %s)
                                   RETURNING id""", (business_id, nm, _clean(notes), username)).fetchone()["id"]
                c.execute("INSERT INTO tag_events (tag_id, username, action) VALUES (%s, %s, 'created')",
                          (tid, username))
            return tid
        except psycopg.errors.UniqueViolation:
            raise StoreError(f"Merchant {business_id} already has a tag named '{nm}'.") from None

    def update_tag(self, tag_id: int, name: str, notes: str, username: str = "") -> None:
        """Rename a tag or change its notes."""
        nm = _clean(name)
        if not nm:
            raise StoreError("A tag needs a name.")
        try:
            with self.db.conn() as c:
                old = c.execute("SELECT name FROM tags WHERE id = %s", (tag_id,)).fetchone()
                c.execute("UPDATE tags SET name = %s, notes = %s, updated_at = now() WHERE id = %s",
                          (nm, _clean(notes), tag_id))
                if old and old["name"] != nm:
                    c.execute("""INSERT INTO tag_events (tag_id, username, action, detail)
                                 VALUES (%s, %s, 'renamed', %s)""", (tag_id, username, f"was '{old['name']}'"))
        except psycopg.errors.UniqueViolation:
            raise StoreError(f"This merchant already has a tag named '{nm}'.") from None

    def delete_tag(self, tag_id: int, username: str = "") -> None:
        """Delete a tag (its records stay; only the list goes).

        A tag already sent to the main app's bucket gets a last snapshot queued that says it was deleted
        (records.bucket); one never sent has its waiting snapshot dropped.
        """
        with self.db.conn() as c:
            t = c.execute("SELECT id, uid, name, business_id FROM tags WHERE id = %s", (tag_id,)).fetchone()
            if t is None:
                return
            sent = c.execute("""SELECT 1 FROM bucket_sends WHERE kind = 'tag' AND tag_id = %s
                                 AND status IN ('sent', 'sending') LIMIT 1""", (tag_id,)).fetchone()
            if not sent:
                c.execute("""UPDATE bucket_sends SET status = 'cancelled' WHERE kind = 'tag' AND tag_id = %s
                             AND status = 'pending'""", (tag_id,))
            elif c.execute("""UPDATE bucket_sends SET tag_name = %s WHERE kind = 'tag' AND tag_id = %s
                              AND status = 'pending'""", (t["name"], tag_id)).rowcount == 0:  # its name now
                c.execute("""INSERT INTO bucket_sends (kind, tag_id, tag_uid, tag_name, business_id, created_by)
                             VALUES ('tag', %s, %s, %s, %s, %s)""",
                          (tag_id, t["uid"], t["name"], t["business_id"], username))
            c.execute("DELETE FROM tags WHERE id = %s", (tag_id,))

    def queue_tag_send(self, tag_id: int, username: str = "") -> bool:
        """Queue a snapshot of the tag for the main app's bucket (records.bucket). Returns False when one is
        already waiting (it is built when sent, so it will hold this tag as it is then)."""
        with self.db.conn() as c:
            t = c.execute("SELECT id, uid, name, business_id FROM tags WHERE id = %s", (tag_id,)).fetchone()
            if t is None:
                raise StoreError("This tag no longer exists.")
            if not re.fullmatch(r"[0-9]+", t["business_id"]):  # ASCII digits, as the main app's file names
                raise StoreError(f"Merchant {t['business_id']} isn't a merchant of the main app, so its tags can't "
                                 "be sent there.")
            if c.execute("""SELECT 1 FROM bucket_sends WHERE kind = 'tag' AND tag_id = %s AND status = 'pending'
                         """, (tag_id,)).fetchone():
                return False
            c.execute("""INSERT INTO bucket_sends (kind, tag_id, tag_uid, tag_name, business_id, created_by)
                         VALUES ('tag', %s, %s, %s, %s, %s)""",
                      (tag_id, t["uid"], t["name"], t["business_id"], username))
        return True

    def tag_events(self, tag_id: int) -> list[dict]:
        """What was done to a tag, newest first."""
        return self.db.fetch("SELECT at, username, action, count, detail FROM tag_events WHERE tag_id = %s "
                             "ORDER BY at DESC, id DESC", (tag_id,))

    def add_to_tag(self, tag_id: int, where: sql.Composable, params: dict[str, Any], username: str = "",
                   detail: str = "") -> dict:
        """Add the records matching ``where`` (a condition on ``records r``, from records.find) to a tag.

        Only records of the tag's merchant are added. Returns {"added": new records, "already": records that were
        in the tag already, "other_business": matching records of other merchants, left out}.
        """
        with self.db.conn() as c:
            t = c.execute("SELECT business_id FROM tags WHERE id = %s", (tag_id,)).fetchone()
            if t is None:
                raise StoreError("This tag no longer exists.")
            p = dict(params, _tag=tag_id, _biz=t["business_id"], _user=username)
            counts = c.execute(sql.SQL("""
                SELECT count(*) FILTER (WHERE r.business_id <> %(_biz)s) AS other,
                       count(*) FILTER (WHERE r.business_id = %(_biz)s AND EXISTS (
                           SELECT 1 FROM tag_records x WHERE x.tag_id = %(_tag)s AND x.record_id = r.id)) AS already
                FROM records r WHERE {}""").format(where), p).fetchone()
            added = c.execute(sql.SQL("""
                INSERT INTO tag_records (tag_id, record_id, added_by)
                SELECT %(_tag)s, r.id, %(_user)s FROM records r WHERE r.business_id = %(_biz)s AND ({})
                ON CONFLICT DO NOTHING""").format(where), p).rowcount
            if added:
                c.execute("""INSERT INTO tag_events (tag_id, username, action, count, detail)
                             VALUES (%s, %s, 'added', %s, %s)""", (tag_id, username, added, detail))
                c.execute("UPDATE tags SET updated_at = now() WHERE id = %s", (tag_id,))
        return {"added": added, "already": counts["already"], "other_business": counts["other"]}

    def remove_from_tag(self, tag_id: int, where: sql.Composable, params: dict[str, Any], username: str = "",
                        detail: str = "") -> int:
        """Remove the records matching ``where`` from a tag; returns how many were removed."""
        with self.db.conn() as c:
            p = dict(params, _tag=tag_id)
            n = c.execute(sql.SQL("""DELETE FROM tag_records tr USING records r
                                     WHERE tr.tag_id = %(_tag)s AND r.id = tr.record_id AND ({})""").format(where),
                          p).rowcount
            if n:
                c.execute("""INSERT INTO tag_events (tag_id, username, action, count, detail)
                             VALUES (%s, %s, 'removed', %s, %s)""", (tag_id, username, n, detail))
                c.execute("UPDATE tags SET updated_at = now() WHERE id = %s", (tag_id,))
        return n

    def log_export(self, tag_id: int, username: str, numbers: int, file_name: str) -> None:
        """Note that a tag was exported (how many numbers, which file name)."""
        self.db.execute("""INSERT INTO tag_events (tag_id, username, action, count, detail)
                           VALUES (%s, %s, 'exported', %s, %s)""", (tag_id, username, numbers, file_name))
