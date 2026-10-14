"""Businesses, services, imports, records and tags: every read and write of Customer Records outside finds and
tag exports (``records.find``, ``records.tags``), which build their own SQL.

Overview
--------
:class:`RecordStore` wraps :class:`records.db.Database`. Methods take and return plain Python values (dicts,
lists, ints) so the web pages and tests stay simple. Errors a person can fix (a business ID that already exists, a
tag name used twice, a service still in use) raise :class:`StoreError` with a sentence to show.

Rules enforced here
-------------------
* A business ID is the main system's ID, typed in by hand; it can be corrected later (it follows into imports,
  records and tags). A business or service can only be deleted while nothing refers to it; a service can be
  deactivated instead (it is then no longer offered on New import, and its records keep it).
* A tag belongs to one business: :meth:`add_to_tag` only adds that business's records and says how many records
  of the found set were left out for belonging to another business.
* Every change to a tag is logged in ``tag_events`` (who, when, what, how many records).
* Deleting an import deletes its records; each tag that loses records gets an event saying so.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql

from .db import Database


class StoreError(ValueError):
    """A problem a person can fix (shown on the page as it is)."""


def _clean(text: str | None) -> str:
    """Trimmed text, '' for None."""
    return (text or "").strip()


class RecordStore:
    """Businesses, services, imports, records and tags in PostgreSQL (see the module docstring)."""

    def __init__(self, db: Database):
        """Use ``db`` (already set up)."""
        self.db = db

    # ------------------------------------------------------------------ businesses

    def list_businesses(self) -> list[dict]:
        """Every business with its numbers of imports, records and tags, by ID."""
        return self.db.fetch("""
            SELECT b.id, b.name, b.notes, b.created_at, b.created_by,
                   (SELECT count(*) FROM imports i WHERE i.business_id = b.id AND i.status = 'completed') AS imports,
                   (SELECT count(*) FROM records r WHERE r.business_id = b.id) AS records,
                   (SELECT count(*) FROM tags t WHERE t.business_id = b.id) AS tags
            FROM businesses b ORDER BY b.id""")

    def business(self, business_id: str) -> dict | None:
        """One business, or None."""
        return self.db.fetch_one("SELECT * FROM businesses WHERE id = %s", (business_id,))

    def add_business(self, business_id: str, name: str, notes: str = "", username: str = "") -> None:
        """Add a business. ``business_id`` is the main system's ID for it (any text; spaces trimmed)."""
        bid, nm = _clean(business_id), _clean(name)
        if not bid or not nm:
            raise StoreError("A business needs an ID (the same as in the main system) and a name.")
        try:
            self.db.execute("INSERT INTO businesses (id, name, notes, created_by) VALUES (%s, %s, %s, %s)",
                            (bid, nm, _clean(notes), username))
        except psycopg.errors.UniqueViolation:
            raise StoreError(f"A business with ID '{bid}' already exists.") from None

    def update_business(self, business_id: str, new_id: str, name: str, notes: str = "") -> None:
        """Change a business's ID, name or notes. A new ID follows into its imports, records and tags."""
        nid, nm = _clean(new_id), _clean(name)
        if not nid or not nm:
            raise StoreError("A business needs an ID and a name.")
        try:
            n = self.db.execute("UPDATE businesses SET id = %s, name = %s, notes = %s WHERE id = %s",
                                (nid, nm, _clean(notes), business_id))
        except psycopg.errors.UniqueViolation:
            raise StoreError(f"A business with ID '{nid}' already exists.") from None
        if not n:
            raise StoreError(f"Business '{business_id}' not found.")

    def delete_business(self, business_id: str) -> None:
        """Delete a business that has no imports and no tags."""
        used = self.db.fetch_one("""SELECT (SELECT count(*) FROM imports WHERE business_id = %(b)s) AS i,
                                           (SELECT count(*) FROM tags WHERE business_id = %(b)s) AS t""",
                                 {"b": business_id})
        if used["i"] or used["t"]:
            raise StoreError(f"Business '{business_id}' has {used['i']} import(s) and {used['t']} tag(s); delete "
                             "those first.")
        self.db.execute("DELETE FROM businesses WHERE id = %s", (business_id,))

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
                      sheet: str | None, username: str) -> int:
        """Register an import before it runs (status 'queued'); returns its id (``imports.id``)."""
        if not self.business(business_id):
            raise StoreError("Choose a business for this import.")
        svc = self.db.fetch_one("SELECT active FROM services WHERE id = %s", (service_id,))
        if not svc:
            raise StoreError("Choose a service for this import.")
        return self.db.fetch_one("""INSERT INTO imports (business_id, service_id, file_name, file_sha1, sheet,
                                    username) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                                 (business_id, service_id, file_name, file_sha1, sheet, username))["id"]

    def set_job(self, import_id: int, job_id: int) -> None:
        """Remember the app's import number (data/converter.db) that runs this import."""
        self.db.execute("UPDATE imports SET job_id = %s WHERE id = %s", (job_id, import_id))

    def get_import(self, import_id: int) -> dict | None:
        """One import with its business and service names."""
        return self.db.fetch_one("""
            SELECT i.*, b.name AS business_name, s.name AS service_name
            FROM imports i JOIN businesses b ON b.id = i.business_id JOIN services s ON s.id = i.service_id
            WHERE i.id = %s""", (import_id,))

    def imports_for_jobs(self, job_ids: list[int]) -> dict[int, dict]:
        """{job id: its import (newest when several)} for the Imports list and pages."""
        if not job_ids:
            return {}
        rows = self.db.fetch("""
            SELECT DISTINCT ON (i.job_id) i.*, b.name AS business_name, s.name AS service_name
            FROM imports i JOIN businesses b ON b.id = i.business_id JOIN services s ON s.id = i.service_id
            WHERE i.job_id = ANY(%s) ORDER BY i.job_id, i.id DESC""", (job_ids,))
        return {r["job_id"]: r for r in rows}

    def same_file_imports(self, business_id: str, file_sha1: str) -> list[dict]:
        """Completed imports of exactly this file for this business (to warn before importing it twice)."""
        return self.db.fetch("""SELECT i.id, i.job_id, i.file_name, i.completed_at, i.records, s.name AS service_name
                                FROM imports i JOIN services s ON s.id = i.service_id
                                WHERE i.business_id = %s AND i.file_sha1 = %s AND i.status = 'completed'
                                ORDER BY i.id""", (business_id, file_sha1))

    def set_import_status(self, import_id: int, status: str) -> None:
        """Mark an import failed / cancelled (its records, if any, are deleted by the caller)."""
        self.db.execute("UPDATE imports SET status = %s WHERE id = %s", (status, import_id))

    def delete_import(self, import_id: int, username: str = "") -> dict:
        """Delete an import's records (and with them their place in tags). The import stays, marked 'deleted'.

        Returns {"records": n, "tags": {tag name: records removed}}. Each tag that lost records gets an event.
        """
        with self.db.conn() as c:
            job = (c.execute("SELECT job_id FROM imports WHERE id = %s", (import_id,)).fetchone() or {}).get("job_id")
            hit = c.execute("""SELECT t.id, t.name, count(*) AS n FROM tag_records tr
                               JOIN records r ON r.id = tr.record_id JOIN tags t ON t.id = tr.tag_id
                               WHERE r.import_id = %s GROUP BY t.id, t.name""", (import_id,)).fetchall()
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
        """One record with its phone numbers, business, service, import and tags."""
        rec = self.db.fetch_one("""
            SELECT r.*, b.name AS business_name, s.name AS service_name, i.file_name, i.job_id, i.sheet
            FROM records r JOIN businesses b ON b.id = r.business_id JOIN services s ON s.id = r.service_id
            JOIN imports i ON i.id = r.import_id WHERE r.id = %s""", (record_id,))
        if rec is None:
            return None
        rec["phones"] = self.db.fetch("""SELECT phone, position, repaired, original FROM record_phones
                                         WHERE record_id = %s ORDER BY position""", (record_id,))
        rec["tags"] = self.db.fetch("""SELECT t.id, t.name, tr.added_at, tr.added_by FROM tag_records tr
                                       JOIN tags t ON t.id = tr.tag_id WHERE tr.record_id = %s ORDER BY t.name""",
                                    (record_id,))
        return rec

    def totals(self) -> dict:
        """Counts for the sidebar and empty states: records, businesses, services, tags."""
        return self.db.fetch_one("""SELECT (SELECT count(*) FROM records) AS records,
                                           (SELECT count(*) FROM businesses) AS businesses,
                                           (SELECT count(*) FROM services WHERE active) AS services,
                                           (SELECT count(*) FROM tags) AS tags""")

    # ------------------------------------------------------------------ tags

    def list_tags(self, business_id: str | None = None) -> list[dict]:
        """Tags (optionally of one business) with record and unique-number counts, newest change first."""
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
        """One tag with its business name and counts (records, unique numbers, records without a number)."""
        return self.db.fetch_one("""
            SELECT t.*, b.name AS business_name,
                   (SELECT count(*) FROM tag_records tr WHERE tr.tag_id = t.id) AS records,
                   (SELECT count(DISTINCT p.phone) FROM tag_records tr JOIN record_phones p
                      ON p.record_id = tr.record_id WHERE tr.tag_id = t.id) AS numbers,
                   (SELECT count(*) FROM tag_records tr WHERE tr.tag_id = t.id AND NOT EXISTS
                      (SELECT 1 FROM record_phones p WHERE p.record_id = tr.record_id)) AS no_phone
            FROM tags t JOIN businesses b ON b.id = t.business_id WHERE t.id = %s""", (tag_id,))

    def create_tag(self, business_id: str, name: str, notes: str = "", username: str = "") -> int:
        """Create an empty tag for one business; returns its id. Names are unique per business."""
        nm = _clean(name)
        if not nm:
            raise StoreError("A tag needs a name.")
        if not self.business(business_id):
            raise StoreError("Choose the business this tag is for.")
        try:
            with self.db.conn() as c:
                tid = c.execute("""INSERT INTO tags (business_id, name, notes, created_by) VALUES (%s, %s, %s, %s)
                                   RETURNING id""", (business_id, nm, _clean(notes), username)).fetchone()["id"]
                c.execute("INSERT INTO tag_events (tag_id, username, action) VALUES (%s, %s, 'created')",
                          (tid, username))
            return tid
        except psycopg.errors.UniqueViolation:
            raise StoreError(f"Business '{business_id}' already has a tag named '{nm}'.") from None

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
            raise StoreError(f"This business already has a tag named '{nm}'.") from None

    def delete_tag(self, tag_id: int) -> None:
        """Delete a tag (its records stay; only the list goes)."""
        self.db.execute("DELETE FROM tags WHERE id = %s", (tag_id,))

    def tag_events(self, tag_id: int) -> list[dict]:
        """What was done to a tag, newest first."""
        return self.db.fetch("SELECT at, username, action, count, detail FROM tag_events WHERE tag_id = %s "
                             "ORDER BY at DESC, id DESC", (tag_id,))

    def add_to_tag(self, tag_id: int, where: sql.Composable, params: dict[str, Any], username: str = "",
                   detail: str = "") -> dict:
        """Add the records matching ``where`` (a condition on ``records r``, from records.find) to a tag.

        Only records of the tag's business are added. Returns {"added": new records, "already": records that were
        in the tag already, "other_business": matching records of other businesses, left out}.
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
