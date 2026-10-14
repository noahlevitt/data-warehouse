"""The import sink: turns a converted file into records (one per file row) in PostgreSQL.

Overview
--------
An import runs exactly like a converter import (``converter.jobs.run_import``: read the file, ``engine.convert``
with the record fields of ``schemas/records_schema.yaml``, write the side-by-side data). At the step where the
converter would record its numbers, the runner calls :meth:`RecordSink.save` instead, which:

1. groups the engine's working rows back into file rows (``_src``): the engine makes one working row per different
   phone number, Customer Records keeps one record per file row with all its numbers;
2. turns each field's converted text into a typed value (dates, date-times, numbers, true/false);
3. stores the records, their numbers (``record_phones``, with ``repaired`` and the number as written) and the row
   exactly as in the file (``source``, every column, also those that feed no field, except columns ticked *Skip*
   on the mapping page), in one transaction;
4. marks the import completed with its counts and the file's column names (for "Other column" searches).

Rows are stored as follows:

* a row with no usable phone number is still a record (business decision for Customer Records); ``phone_issue``
  says why (the converter's reason, e.g. "too few digits (3)", or "no phone number");
* an empty row is skipped, as in the converter;
* a row the engine rejected (only possible if a field is made ``required`` / ``on_invalid: reject`` in the record
  fields) is not stored, and is counted.

``first_visit_as``: the converter writes a file's creation date ("Date Created", "Sign Up Date") as the last
transaction (or last call, in a call log) when the file has no last-activity column at all. Customer Records keeps
that date in ``first_visit`` instead, and remembers on the import which export field it stands in for, so a tag
export gives the same value the converter would have (``records.tags``). Computed with the same rules as the
converter's step 7 (``converter.heuristics``): the column must be 80%+ dates and not a birthday, expiry or opt-in
date.

When the import then ends cancelled or failed (a cancel noticed after saving), :meth:`RecordSink.discard` deletes
what was saved.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pandas as pd
from psycopg import sql
from psycopg.types.json import Jsonb

from converter import heuristics as H
from converter.engine import CELL_REPAIRED, ConversionResult
from converter.mapping import Mapping, profile_columns
from converter.schema import FieldSpec, TargetSchema
from converter.storage import ImportRecord, Store

from .db import column_type
from .store import RecordStore

log = logging.getLogger(__name__)

# Shown on a record without a usable number when the engine gave no reason (the cell was empty or not mapped).
NO_PHONE = "no phone number"


def typed_value(f: FieldSpec, text: str):
    """The database value of field ``f`` for the engine's converted text (None when empty or unreadable).

    The engine writes dates as ``f.format`` (e.g. 09/12/1600, 10/09/2026 03:45 PM); they are read back with the
    same pattern. Numbers come as plain decimal text ("1234.5"), true/false as the field's true/false values.
    """
    if text is None or text == "":
        return None
    try:
        if f.type in ("date", "datetime"):
            dt = datetime.strptime(text, f.format) if f.format else datetime.fromisoformat(text)
            return dt.date() if f.type == "date" else dt
        if f.type == "integer":
            return int(text)
        if f.type == "decimal":
            return Decimal(text)
        if f.type == "boolean":
            return True if text == f.true_value else False if text == f.false_value else None
    except (ValueError, InvalidOperation):
        log.warning("Field %s: converted value %r couldn't be stored", f.name, text)
        return None
    return text


def creation_fallback(schema: TargetSchema, mapping: Mapping, df: pd.DataFrame) -> str | None:
    """Which last-activity field the creation date stands in for in a tag export, or None (see module docstring).

    Mirrors ``converter.heuristics._map_generic_dates``: only when no last-activity field (a date-time field that
    isn't the creation date) is mapped, the creation-date field is, and its column is 80%+ dates and isn't a
    consent, birthday, expiry or opt-in column. The field is the one marked ``generic_date: call_log`` in a file
    that looks like a call log, otherwise ``generic_date: default``.
    """
    auto = schema.auto_fields
    creation = [f for f in auto if f.creation_date and mapping.get(f.name).transform != "none"]
    if not creation:
        return None
    if any(f.type == "datetime" and not f.creation_date and mapping.get(f.name).transform != "none" for f in auto):
        return None
    targets = {f.generic_date: f.name for f in auto if f.generic_date}
    if not targets:
        return None
    col = mapping.get(creation[0].name).sources[0]
    profiles = profile_columns(df)
    p = next((x for x in profiles if x.name == col), None)
    if p is None or H.is_consent_column(p.name) or set(H._words(p.name)) & H.NEVER_ACTIVITY_DATE_WORDS:
        return None
    if not H.enough_evidence(p, p.shares.get("date", 0.0), minimum=0.8):
        return None
    call_log = H._looks_like_call_log(profiles)
    return targets.get("call_log" if call_log else "default") or next(iter(targets.values()))


def build_records(schema: TargetSchema, df: pd.DataFrame, result: ConversionResult,
                  skip: set[str] | frozenset = frozenset()) -> tuple[list[dict], dict]:
    """One dict per stored record from the engine's audit, plus counts. No database access (unit-testable).

    Each dict: file_row, values {field: typed value} (no phone field), phones [(number, original, repaired)],
    phone_issue, issues {field: reason}, source {column: value}, search_text. Columns in ``skip`` (ticked *Skip*
    on the mapping page: "keep this column out of the import completely") are left out of ``source`` too.
    Counts: rows (file rows that weren't empty), stored, rejected (not stored), with_phone, no_phone.
    """
    audit = result.audit
    phone = next((f for f in schema.fields if f.type == "phone"), None)
    fields = [f for f in schema.fields if column_type(f) is not None]
    columns = [None if c in skip else c for c in df.columns]
    file_values = df.values.tolist()
    # Plain lists instead of per-row pandas access: a 150,000-row file is grouped in about a second.
    a_src, a_row = audit["_src"].tolist(), audit["_row"].tolist()
    a_status, a_issues = audit["_status"].tolist(), audit["_issues"].tolist()
    a_out = {f.name: audit[f"out.{f.name}"].tolist() for f in fields}
    if phone is not None:
        p_out, p_raw, p_st = (audit[f"{k}.{phone.name}"].tolist() for k in ("out", "raw", "st"))
    out: list[dict] = []
    counts = {"rows": 0, "stored": 0, "rejected": 0, "with_phone": 0, "no_phone": 0}
    n, i = len(audit), 0
    # The audit lists working rows in file order; the extra rows of one file row (one per phone number) follow it
    # directly, with the same _src.
    while i < n:
        j = i + 1
        while j < n and a_src[j] == a_src[i]:
            j += 1
        group = range(i, j)
        base, i = group[0], j
        if a_status[base] == "empty":
            continue
        counts["rows"] += 1
        if a_status[base] in ("rejected", "duplicate"):
            counts["rejected"] += 1
            continue
        issues: dict[str, str] = {}
        for k in group:
            if a_issues[k]:
                issues.update(json.loads(a_issues[k]))
        phones: list[tuple[str, str, bool]] = []
        if phone is not None:
            seen = set()
            for k in group:
                if p_out[k] and p_out[k] not in seen:
                    seen.add(p_out[k])
                    phones.append((p_out[k], p_raw[k] or "", p_st[k] == CELL_REPAIRED))
        texts = {f.name: a_out[f.name][base] for f in fields}
        values = {f.name: typed_value(f, texts[f.name]) for f in fields}
        source = {c: str(v) for c, v in zip(columns, file_values[int(a_src[base])])
                  if c is not None and v is not None and str(v) != ""}
        issue = "" if phones else ((issues.get(phone.name, "") if phone is not None else "") or NO_PHONE)
        words = [p[0] for p in phones] + [t for t in texts.values() if t] + list(source.values())
        out.append({"file_row": int(a_row[base]), "values": values, "phones": phones, "phone_issue": issue,
                    "issues": issues, "source": source, "search_text": " ".join(words).lower()})
        counts["stored"] += 1
        counts["with_phone" if phones else "no_phone"] += 1
    return out, counts


class RecordSink:
    """Stores an import's rows as records (the ``sink`` of ``converter.jobs.ImportRunner``)."""

    def __init__(self, records: RecordStore, jobs: Store):
        """``records``: the PostgreSQL store; ``jobs``: the app's import store (data/converter.db)."""
        self.records = records
        self.jobs = jobs

    @staticmethod
    def import_id(rec: ImportRecord) -> int:
        """The Customer Records import (``imports.id``) a job belongs to, from its read options."""
        rid = rec.read_options.get("records_import_id")
        if rid is None:
            raise RuntimeError("This import has no business and service (it was started outside Customer Records)")
        return int(rid)

    def save(self, job_id: int, rec: ImportRecord, rr, result: ConversionResult, schema: TargetSchema) -> dict:
        """Store the converted rows as records (one transaction) and complete the import. Returns summary keys."""
        imp_id = self.import_id(rec)
        imp = self.records.get_import(imp_id)
        if imp is None:
            raise RuntimeError(f"Customer Records import {imp_id} not found")
        skip = set(rec.mapping.excluded_columns)
        rows, counts = build_records(schema, rr.df, result, skip)
        fallback = creation_fallback(schema, rec.mapping, rr.df)
        fields = [f for f in schema.fields if column_type(f) is not None]
        cols = ["import_id", "business_id", "service_id", "file_row", "phone_issue", "issues", "source",
                "search_text"] + [f.name for f in fields]
        numbers = len({p[0] for r in rows for p in r["phones"]})
        with self.records.db.conn() as c:
            c.execute("DELETE FROM records WHERE import_id = %s", (imp_id,))  # a retried save starts clean
            copy_sql = sql.SQL("COPY records ({}) FROM STDIN").format(
                sql.SQL(", ").join(sql.Identifier(x) for x in cols))
            with c.cursor().copy(copy_sql) as cp:
                for r in rows:
                    cp.write_row([imp_id, imp["business_id"], imp["service_id"], r["file_row"], r["phone_issue"],
                                  Jsonb(r["issues"]), Jsonb(r["source"]), r["search_text"]]
                                 + [r["values"][f.name] for f in fields])
            ids = {x["file_row"]: x["id"] for x in c.execute(
                "SELECT id, file_row FROM records WHERE import_id = %s", (imp_id,)).fetchall()}
            with c.cursor().copy("COPY record_phones (record_id, phone, position, repaired, original) "
                                 "FROM STDIN") as cp:
                for r in rows:
                    for pos, (num, orig, repaired) in enumerate(r["phones"]):
                        cp.write_row([ids[r["file_row"]], num, pos, repaired, orig or ""])
            c.execute("""UPDATE imports SET status = 'completed', completed_at = now(), records = %s, phones = %s,
                         no_phone = %s, first_visit_as = %s, columns = %s, sheet = %s, job_id = %s
                         WHERE id = %s""",
                      (counts["stored"], numbers, counts["no_phone"], fallback,
                       Jsonb([c for c in rr.df.columns if c not in skip]), rr.sheet, job_id, imp_id))
        counts["numbers"] = numbers
        counts["first_visit_as"] = fallback
        log.info("Import %s (job %s): %s records stored", imp_id, job_id, counts["stored"])
        return {"records": counts, "records_import_id": imp_id}

    def discard(self, job_id: int) -> None:
        """Remove what :meth:`save` stored for a job that then ended cancelled or failed."""
        rec = self.jobs.get_import(job_id)
        if rec is None or rec.read_options.get("records_import_id") is None:
            return
        imp_id = int(rec.read_options["records_import_id"])
        with self.records.db.conn() as c:
            c.execute("DELETE FROM records WHERE import_id = %s", (imp_id,))
            c.execute("UPDATE imports SET status = 'not completed', records = 0, phones = 0, no_phone = 0 "
                      "WHERE id = %s", (imp_id,))
