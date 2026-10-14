"""A tag's export: one row per unique phone number, in the converter's import format.

Overview
--------
A tag holds records of one business. Its export (:func:`export_table`, :func:`export_file`) is the file imported
into the main system, in exactly the converter's import format (``schemas/target_schema.yaml``: headers, order,
formats, CSV settings), written by the converter's own writer. Each usable phone number of the tag's records
appears once; records without a usable number are left out (and counted).

Each export column is filled from the record field with the same name (``phone_number``, ``calls_last``,
``birthday``…); an export column with no such field is blank. When one number is in several records:

=======================  ===========================================================================================
Export column            Value
=======================  ===========================================================================================
phone number             the number, formatted as the import format says (10 digits)
consent                  **always blank** for now: blank changes nobody's consent in the main system, which handles
                         opt-outs per business (decided 2026-10-14; the records keep the file's consent, searchable)
date-time columns        the latest of the records' values (calls_last, transactions_last, in_sms_last,
                         redemptions_last, spin_last, last_contacted)
birthday, anniversary    the date with a real year that most of the records give (the latest record's on a tie);
                         if none has a year, the month/day most of them give, with the year 1600
other columns            the latest record's value (none in the current import format)
=======================  ===========================================================================================

Creation date as the last activity: for an import whose file had no last-activity column at all, the converter
writes the file's creation date ("Date Created", "Sign Up Date") as the last transaction (or last call, in a call
log). Customer Records keeps it in ``first_visit`` and stores on the import which column it stands in for
(``imports.first_visit_as``, see records.importer), so those records give the same value here. That keeps a tag of
one file identical to the converter's output for that file (checked by scripts/regression/parity.py), apart from
consent and the merging of repeated numbers.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime

import pandas as pd
from psycopg import sql

from converter import normalizers as N
from converter.output import write_output
from converter.schema import FieldSpec, TargetSchema

from .db import Database, column_type, date_only

# Export columns that are always blank in a tag export (see the module docstring).
ALWAYS_BLANK = {"consent"}


def _fmt(f: FieldSpec, v) -> str:
    """A database value in export field ``f``'s format."""
    if v is None:
        return ""
    if isinstance(v, (date, datetime)):
        dt = v if isinstance(v, datetime) else datetime(v.year, v.month, v.day)
        fmt = f.format or ("%Y-%m-%d %H:%M:%S" if f.type == "datetime" else "%Y-%m-%d")
        # The converter's portable formatting: the year is written as text, so 1600 looks the same on every system.
        return N._strftime_year(dt, fmt, f"{dt.year:04d}")
    return str(v)


def _pick_date(values: list[tuple[int, date]], no_year: int | None) -> date | None:
    """Birthday / anniversary of a number: the most common date with a real year (latest record on a tie), else the
    most common month/day without one (year ``no_year``). ``values`` are (record id, date)."""
    real = [(rid, d) for rid, d in values if d is not None and (no_year is None or d.year != no_year)]
    pool = real or [(rid, d) for rid, d in values if d is not None]
    if not pool:
        return None
    count = Counter(d for _, d in pool)
    latest = {d: rid for rid, d in sorted(pool)}
    return max(count, key=lambda d: (count[d], latest[d]))


def export_table(db: Database, tag_id: int, schema: TargetSchema, export: TargetSchema) -> tuple[pd.DataFrame, dict]:
    """The export rows of a tag (columns = the import format's headers, values = final text) and counts.

    Counts: records, no_phone (records without a usable number, left out), numbers (rows), shared (numbers in
    several records), merged (numbers whose records disagreed on a date and were merged as above).
    """
    rec_fields = {f.name: f for f in schema.fields if column_type(f) is not None}
    creation = next((f.name for f in schema.fields if f.creation_date), None)
    cols = sorted(rec_fields)
    rows = db.fetch(sql.SQL("""
        SELECT rp.phone, r.id AS record_id, i.first_visit_as, {cols}
        FROM tag_records tr JOIN records r ON r.id = tr.record_id
        JOIN record_phones rp ON rp.record_id = r.id JOIN imports i ON i.id = r.import_id
        WHERE tr.tag_id = %s ORDER BY r.id, rp.position""").format(
        cols=sql.SQL(", ").join(sql.SQL("r.{}").format(sql.Identifier(c)) for c in cols)), (tag_id,))
    totals = db.fetch_one("""SELECT count(*) AS records, count(*) FILTER (WHERE NOT EXISTS (
                                 SELECT 1 FROM record_phones rp WHERE rp.record_id = tr.record_id)) AS no_phone
                             FROM tag_records tr WHERE tr.tag_id = %s""", (tag_id,))
    # The creation date stands in for the last-activity column its import lacked (see the module docstring).
    for r in rows:
        target = r["first_visit_as"]
        if creation and target in rec_fields and r.get(target) is None and r.get(creation) is not None:
            v = r[creation]
            # A date-only field (the last visit) takes the day of the creation date-time.
            r[target] = v.date() if date_only(rec_fields[target]) and isinstance(v, datetime) else v
    by_number: dict[str, list[dict]] = {}
    for r in rows:
        by_number.setdefault(r["phone"], []).append(r)

    phone_field = next((f for f in export.fields if f.type == "phone"), None)
    phone_ctx = N.prepare(phone_field, list(by_number), {}) if phone_field else {}
    out_rows, merged = [], 0
    for number, recs in by_number.items():
        recs = sorted(recs, key=lambda x: x["record_id"])
        row, disagreed = {}, False
        for f in export.fields:
            if f.type == "phone":
                row[f.header] = N.normalize(number, f, {}, phone_ctx)[0]
                continue
            if f.name in ALWAYS_BLANK or f.name not in rec_fields:
                row[f.header] = ""
                continue
            vals = [x[f.name] for x in recs]
            present = [v for v in vals if v is not None]
            if f.type == "date" and rec_fields[f.name].type == "date":
                v = _pick_date([(x["record_id"], x[f.name]) for x in recs], f.no_year_value)
            elif f.type in ("date", "datetime"):
                v = max(present) if present else None
            else:
                v = present[-1] if present else None
            disagreed |= len(set(present)) > 1 and f.type in ("date", "datetime")
            row[f.header] = _fmt(f, v)
        merged += disagreed
        out_rows.append(row)
    table = pd.DataFrame(out_rows, columns=[f.header for f in export.fields])
    counts = {"records": totals["records"], "no_phone": totals["no_phone"], "numbers": len(out_rows),
              "shared": sum(len({x["record_id"] for x in v}) > 1 for v in by_number.values()), "merged": merged}
    return table, counts


def export_file(db: Database, tag_id: int, schema: TargetSchema, export: TargetSchema) -> tuple[bytes, dict]:
    """The tag's export file (bytes, in the import format's file type) and the counts of :func:`export_table`."""
    table, counts = export_table(db, tag_id, schema, export)
    return write_output(table, export), counts


def export_filename(business_id: str, tag_name: str, export: TargetSchema, stamp: str) -> str:
    """``<business id>_<tag name>_<YYYYMMDD-HHMM>.csv`` with only safe characters."""
    def safe(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "tag"
    ext = "xlsx" if export.output.format == "xlsx" else "csv"
    return f"{safe(business_id)}_{safe(tag_name)}_{stamp}.{ext}"
