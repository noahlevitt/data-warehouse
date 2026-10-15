"""Find requests, the found set, and what is read from it (counts, pages of records, CSV).

How a find works (FileMaker's rules)
------------------------------------
A find is a list of **requests**. Each request has the businesses and services picked for it and criteria, one per
field (``records.criteria`` explains what can be typed); a record matches a request when it matches **every** part
of it (AND): one of its businesses, one of its services, every criterion. Requests are applied top to bottom: a
normal request **adds** the records it matches to the found set (OR), an **Omit** request **removes** the records it
matches from what the requests above it found. When the first request is an Omit, it removes from all records.
Example: [Last name = smith] [Omit, City = brooklyn] [First name = ann] finds every Smith not in Brooklyn, plus
every Ann (also an Ann in Brooklyn); [Business 1001] [Omit, Service Catering] finds business 1001 without its
catering records.

Nothing is required: a request left blank is ignored, and a find with nothing filled in anywhere finds every
record (the Find page's "blank find").

On the found set: **Constrain** keeps only the records that also match new requests (AND), **Extend** adds the
records matching new requests (OR), **Omit selected** takes chosen records out, **Show omitted only** swaps the
found set for every other record, **Show all** finds everything. The found set is kept as a small tree of these
steps (:data:`Node`, plain dicts, stored in the page state), never as a list of millions of ids, and is turned into
one SQL condition each time it is shown (:func:`where`).

"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from psycopg import sql

from converter.output import neutralise_formula
from converter.schema import TargetSchema

from .criteria import FALSE, TRUE, CriterionError, Params, condition
from .db import Database, column_type, date_expr, date_fields, date_only
from .fields import FindField

# A found-set node: {"t": "find", "requests": [...]}, {"t": "and"|"or", "parts": [n, n]}, {"t": "not", "part": n},
# {"t": "all"}, {"t": "ids", "ids": [...]}, {"t": "tag", "id": n}. A request: {"omit": bool, "businesses": [business
# ID, ...], "services": [service id, ...], "criteria": [[field key, text], ...]}; every part is optional.
Node = dict[str, Any]


class FindError(ValueError):
    """The find can't run as typed (a criterion that can't be read, a field that's gone); shown to the person."""


def blank(requests: list[dict]) -> bool:
    """True when nothing is filled in in any request (no business, service or criterion)."""
    return not any(r.get("businesses") or r.get("services") or any((t or "").strip() for _, t in r.get("criteria", []))
                   for r in requests)


def find_node(requests: list[dict]) -> Node:
    """A new found set from find requests. Blank requests are ignored; all blank = every record."""
    return {"t": "find", "requests": requests}


def constrain(prev: Node, requests: list[dict]) -> Node:
    """The found set narrowed by new requests (records must match both)."""
    return {"t": "and", "parts": [prev, find_node(requests)]}


def extend(prev: Node, requests: list[dict]) -> Node:
    """The found set plus the records new requests find."""
    return {"t": "or", "parts": [prev, find_node(requests)]}


def omit_records(prev: Node, ids: list[int]) -> Node:
    """The found set without these records."""
    return {"t": "and", "parts": [prev, {"t": "not", "part": {"t": "ids", "ids": [int(i) for i in ids]}}]}


def show_omitted(prev: Node) -> Node:
    """Every record that isn't in the found set."""
    return {"t": "not", "part": prev}


def all_records() -> Node:
    """Every record."""
    return {"t": "all"}


def tag_records(tag_id: int) -> Node:
    """The records of a tag."""
    return {"t": "tag", "id": int(tag_id)}


@dataclass
class Compiler:
    """Turns nodes into SQL conditions on ``records r`` with one shared set of parameters.

    ``today`` defaults to today's date in ``timezone`` (the app's), not the server's clock: ``//`` means the same
    day for the person searching wherever the server runs. ``collation`` is records.db.Database.collation.
    """

    fields: dict[str, FindField]
    timezone: str = "America/New_York"
    today: date | None = None
    collation: str | None = None

    def __post_init__(self) -> None:
        today = self.today or datetime.now(ZoneInfo(self.timezone)).date()
        self.p = Params(self.collation, self.timezone, today)

    def request(self, req: dict) -> sql.Composable | None:
        """A request's businesses, services and criteria ANDed, or None when it has none of them (blank)."""
        parts = []
        if req.get("businesses"):
            parts.append(sql.SQL("r.business_id = ANY({})").format(self.p([str(b) for b in req["businesses"]])))
        if req.get("services"):
            parts.append(sql.SQL("r.service_id = ANY({})").format(self.p([int(s) for s in req["services"]])))
        for key, text in req.get("criteria", []):
            if not (text or "").strip():
                continue
            f = self.fields.get(key)
            if f is None:
                raise FindError(f"The field '{key.split(':', 1)[-1]}' isn't searchable any more; remove it.")
            try:
                parts.append(condition(f, text, self.p))
            except CriterionError as exc:
                raise FindError(str(exc)) from None
        if not parts:
            return None
        return sql.SQL("(") + sql.SQL(" AND ").join(parts) + sql.SQL(")")

    def node(self, n: Node) -> sql.Composable:
        """One node (see :data:`Node`)."""
        t = n["t"]
        if t == "all":
            return TRUE
        if t == "find":
            expr: sql.Composable | None = None
            for req in n["requests"]:
                c = self.request(req)
                if c is None:
                    continue
                omit = bool(req.get("omit"))
                if expr is None:
                    expr = TRUE if omit else FALSE
                expr = (sql.SQL("({} AND NOT {})") if omit else sql.SQL("({} OR {})")).format(expr, c)
            return TRUE if expr is None else expr   # nothing filled in anywhere: every record
        if t in ("and", "or"):
            return sql.SQL("(") + sql.SQL(f" {t.upper()} ").join(self.node(x) for x in n["parts"]) + sql.SQL(")")
        if t == "not":
            return sql.SQL("NOT {}").format(self.node(n["part"]))
        if t == "ids":
            return sql.SQL("r.id = ANY({})").format(self.p(list(n["ids"]))) if n["ids"] else FALSE
        if t == "tag":
            return sql.SQL("EXISTS (SELECT 1 FROM tag_records x WHERE x.record_id = r.id AND x.tag_id = {})").format(
                self.p(int(n["id"])))
        raise FindError(f"Unknown found-set step '{t}'.")


def where(node: Node, fields: list[FindField], timezone: str = "America/New_York", today: date | None = None,
          collation: str | None = None) -> tuple[sql.Composable, dict]:
    """The found set as (SQL condition on ``records r``, parameters).

    ``collation``: pass the database's (records.db.Database.collation) so upper/lower case of accented letters match.
    Raises :class:`FindError` (with a message for the page) when a criterion can't be read.
    """
    comp = Compiler({f.key: f for f in fields}, timezone, today, collation)
    return comp.node(node), comp.p.values


def counts(db: Database, cond: sql.Composable, params: dict) -> dict:
    """{"records", "numbers" (unique usable phone numbers), "no_phone", "businesses"} of the found set."""
    row = db.fetch_one(sql.SQL("""
        SELECT count(*) AS records,
               count(*) FILTER (WHERE r.phone_issue <> '') AS no_phone,
               count(DISTINCT r.business_id) AS businesses
        FROM records r WHERE {}""").format(cond), params)
    row["numbers"] = db.fetch_one(sql.SQL("""
        SELECT count(DISTINCT rp.phone) AS n FROM record_phones rp JOIN records r ON r.id = rp.record_id
        WHERE {}""").format(cond), params)["n"]
    return row


# Stands for the Date (records.db.date_expr) in SORTS: "{date}" in an order, DATE in what it needs.
DATE = "@date"
# Sort choices of the found-set table: label → (ORDER BY, record fields it needs). Every order ends on r.id so
# pages never overlap.
SORTS = {
    "Import order": ("r.id", set()),
    "Last name": ("lower(r.last_name) NULLS LAST, lower(r.first_name) NULLS LAST, r.id", {"last_name", "first_name"}),
    "First name": ("lower(r.first_name) NULLS LAST, lower(r.last_name) NULLS LAST, r.id", {"last_name", "first_name"}),
    "Date (newest first)": ("{date} DESC NULLS LAST, r.id", {DATE}),
    "Total spent (highest first)": ("r.total_spent DESC NULLS LAST, r.id", {"total_spent"}),
    "Total visits (most first)": ("r.total_visits DESC NULLS LAST, r.id", {"total_visits"}),
    "Business": ("r.business_id, r.id", set()),
    "Record created (newest first)": ("r.created_at DESC, r.id DESC", set()),
}


def available_sorts(schema: TargetSchema) -> list[str]:
    """The sort choices whose fields exist in the record fields."""
    names = set(schema.field_names) | ({DATE} if date_expr(schema) else set())
    return [label for label, (_, need) in SORTS.items() if need <= names]


def record_date(schema: TargetSchema, rec: dict):
    """A record's Date from its stored values (as records.db.date_expr computes it): the later day of its last
    visit and last call, or None."""
    days = [v.date() if isinstance(v, datetime) else v for v in (rec.get(f.name) for f in date_fields(schema))]
    days = [d for d in days if d is not None]
    return max(days) if days else None


def day_name(d) -> str:
    """The day of the week of a date, abbreviated ("Fri"); "" for None."""
    return d.strftime("%a") if d is not None else ""


def page(db: Database, schema: TargetSchema, cond: sql.Composable, params: dict, sort: str = "Import order",
         limit: int = 100, offset: int = 0) -> list[dict]:
    """One page of the found set: record ID, when it was created, business, service, import, file row, phones,
    problem, the Date (``activity_date``), every field."""
    fields = [f for f in schema.fields if column_type(f) is not None]
    date = date_expr(schema) or "NULL::date"
    order_sql = SORTS.get(sort, SORTS["Import order"])[0].replace("{date}", date)
    return db.fetch(sql.SQL("""
        SELECT r.id, r.created_at, r.business_id, b.name AS business_name, s.name AS service_name, i.job_id AS import_no,
               r.file_row,
               (SELECT string_agg(rp.phone, ', ' ORDER BY rp.position) FROM record_phones rp
                 WHERE rp.record_id = r.id) AS phones,
               r.phone_issue, {date} AS activity_date, {fields}
        FROM records r JOIN businesses b ON b.id = r.business_id JOIN services s ON s.id = r.service_id
        JOIN imports i ON i.id = r.import_id
        WHERE {cond} ORDER BY {order} LIMIT {limit} OFFSET {offset}""").format(
        date=sql.SQL(date), fields=sql.SQL(", ").join(sql.SQL("r.{}").format(sql.Identifier(f.name)) for f in fields),
        cond=cond, order=sql.SQL(order_sql), limit=sql.Literal(int(limit)), offset=sql.Literal(int(offset))), params)


def _cell(v, timezone: str | None = None) -> str:
    """A database value as CSV text (dates as MM/DD/YYYY, date-times with the time). A moment with a time zone
    (Record created) is shown on the app's clock, ``timezone``."""
    if v is None:
        return ""
    if hasattr(v, "hour"):
        if timezone and v.tzinfo is not None:
            v = v.astimezone(ZoneInfo(timezone))
        return v.strftime("%m/%d/%Y %I:%M %p")
    if hasattr(v, "strftime"):
        return v.strftime("%m/%d/%Y")
    return str(v)


def found_set_csv(db: Database, schema: TargetSchema, cond: sql.Composable, params: dict,
                  sort: str = "Import order", timezone: str = "America/New_York") -> bytes:
    """The whole found set as CSV (UTF-8 with BOM, for Excel): record and import details, numbers, the Date and its
    day of the week, every field.

    Cells that a spreadsheet would run as a formula get a leading apostrophe (converter.output.neutralise_formula).
    """
    fields = [f for f in schema.fields if column_type(f) is not None]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Record ID", "Record created", "Business ID", "Business", "Service", "Import #", "File row",
                "Phones", "Phone problem", "Date", "Day"] + [f.header for f in fields])
    offset, chunk = 0, 5000
    while True:
        rows = page(db, schema, cond, params, sort, limit=chunk, offset=offset)
        for r in rows:
            w.writerow([neutralise_formula(_cell(x, timezone)) for x in
                        [r["id"], r["created_at"], r["business_id"], r["business_name"], r["service_name"],
                         r["import_no"], r["file_row"], r["phones"], r["phone_issue"], r["activity_date"],
                         day_name(r["activity_date"])] + [r[f.name] for f in fields]])
        if len(rows) < chunk:
            break
        offset += chunk
    return buf.getvalue().encode("utf-8-sig")


# The found-set table's column with the record's ID; the pages read selected rows' IDs from it.
ID_COL = "Record ID"
# The found-set table's Date column, chosen like a field (by this key) and shown with its day of the week.
DATE_COL = "Date"
# How the found-set table shows dates (momentJS formats for st.column_config; the values stay dates, so a column
# sorts by date): the day of the week first, "Fri, 10/9/2026" and "Fri, 10/9/2026 3:45 PM" (decided 2026-10-15).
# Birthdays and anniversaries have no day of the week (often no year either: 1600). Tag exports and the found-set
# CSV keep the import format's dates (MM/DD/YYYY…): this is only how the screen shows them.
DAY_FORMAT = "ddd, M/D/YYYY"
DAY_TIME_FORMAT = "ddd, M/D/YYYY h:mm A"
NO_DAY_FORMAT = "M/D/YYYY"


def shown(v, weekday: bool = True) -> str:
    """A record value as the screen shows it: dates as "Fri, 10/9/2026", date-times as "Fri, 10/9/2026 3:45 PM"
    (without the day of the week when ``weekday`` is False: birthdays), anything else as text."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        text = f"{v.month}/{v.day}/{v.year} {v.hour % 12 or 12}:{v.minute:02d} {'AM' if v.hour < 12 else 'PM'}"
    elif isinstance(v, date):
        text = f"{v.month}/{v.day}/{v.year}"
    else:
        return str(v)
    return f"{v:%a}, {text}" if weekday else text


def date_columns(schema: TargetSchema, show: list[str]) -> dict[str, tuple[str, str]]:
    """The date columns of the found-set table (:func:`page_frame` with these chosen fields): {column: ("date" or
    "datetime", momentJS format)}, for the page's column_config."""
    out = {"Created": ("datetime", DAY_TIME_FORMAT)}
    by_name = {f.name: f for f in schema.fields}
    for k in show:
        if k == DATE:
            out[DATE_COL] = ("date", DAY_FORMAT)
        elif (f := by_name.get(k)) is not None and f.type in ("date", "datetime"):
            day = f.type == "date" or date_only(f)
            out[f.header] = ("date" if day else "datetime",
                             NO_DAY_FORMAT if f.no_year_value is not None else DAY_FORMAT if day else DAY_TIME_FORMAT)
    return out


def page_frame(rows: list[dict], schema: TargetSchema, show: list[str],
               timezone: str = "America/New_York") -> pd.DataFrame:
    """The found-set page as a table for the screen: fixed columns first, then the chosen fields (by name; DATE is
    the Date). Dates and date-times stay values (Record created on the app's clock), shown as :func:`date_columns`
    says; everything else is text."""
    heads = {f.name: f.header for f in schema.fields}
    dated = {f.name for f in schema.fields if f.type in ("date", "datetime")}

    def value(r: dict, k: str):
        if k == DATE:
            return r.get("activity_date")
        return r.get(k) if k in dated else _cell(r.get(k))

    heads[DATE] = DATE_COL
    zone = ZoneInfo(timezone)
    table = pd.DataFrame([{
        ID_COL: r["id"], "Created": r["created_at"].astimezone(zone).replace(tzinfo=None),
        "Business": f"{r['business_id']} · {r['business_name']}", "Service": r["service_name"],
        "Phones": r["phones"] or "",
        **{heads[k]: value(r, k) for k in show if k in heads and (k in r or k == DATE)},
        "Phone problem": r["phone_issue"], "Import #": r["import_no"], "Row": r["file_row"],
    } for r in rows])
    return table
