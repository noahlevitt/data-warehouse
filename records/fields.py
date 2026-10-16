"""What a find request can search: the record fields, plus business, service, import, tag and every file column.

Overview
--------
:func:`find_fields` builds the list shown in the Field column of a find request. Each :class:`FindField` has a
stable ``key`` (stored in the page's state), the ``label`` people see, and a ``kind`` that decides how a criterion
is read (``records.criteria``):

==============  ===========================================================================================
kind            searched on
==============  ===========================================================================================
``text``        a text column of the record (names, email, address, city, state, ZIP…)
``phone``       the record's phone numbers (any of them; digits only, any formatting typed)
``number``      a number column (total visits, total spent), or the Record ID / file row
``job``         the import's number as the Imports page shows it
``date``        a date or date-time column; birthday / anniversary also take month/day without a year; also the
                **Date** (key ``date``): the later of the last visit and the last call, as a day
                (``records.db.date_expr``), the date searched most; every date takes days of the week (``fri``)
``bool``        a true/false column (consent)
``name``        first and last name together ("Full name": "John Smith")
``first``       First name: the stored one, or the first part of a whole name kept in First name with no Last name
                (split at a comma, else at the first space; ``criteria.name_condition``)
``last``        Last name: the stored one, or the rest of such a name
``any``         every value of the record, cleaned and as written in the file ("Any field")
``column``      one column of the original file, as written ("Column: Points"), for columns that feed no field
``business``    the merchant: its ID (exact) or its name
``service``     the service's name
``tag``         the tags the record is in (by name)
``file``        the imported file's name
``created``     when the record was stored ("Record created", a date-time on the app's clock)
``issue``       why the record has no usable phone number ("Phone problem")
==============  ===========================================================================================
"""

from __future__ import annotations

from dataclasses import dataclass

from converter.schema import TargetSchema

from .db import date_expr, date_only

KIND_OF_TYPE = {"phone": "phone", "integer": "number", "decimal": "number", "date": "date", "datetime": "date",
                "boolean": "bool"}


@dataclass(frozen=True)
class FindField:
    """One searchable thing (see the module docstring)."""

    key: str             # stable id: "f:<field name>", "col:<file column>", or a special name ("business", …)
    label: str           # shown in the Field dropdown
    kind: str            # how criteria are read (records.criteria)
    column: str = ""     # records column (record fields), file column name (kind "column"), or special column
    expr: str = ""       # an SQL expression on ``records r`` searched instead of a column (the Date)
    datetime: bool = False   # a date-time column (whole days match from 12:00 AM; a typed time means that minute)
    no_year_value: int | None = None   # birthday / anniversary: the year meaning "no year" (1600)
    help: str = ""


# Merchant and service have pickers of their own on the Find page (so does location, which has no FindField), so
# they are never offered as boxes there (the page's layout, which boxes each request shows and which are in its Add
# a field menu, is records.ui.find_layout).
PICKER_KEYS = {"business", "service"}


def find_fields(schema: TargetSchema, columns_seen: list[str]) -> list[FindField]:
    """Every searchable field: common ones, record fields, the record's ID and import, then the file columns."""
    out = [FindField("any", "Any field", "any", help="every value of the record, cleaned and as in the file"),
           FindField("business", "Merchant", "business", help="merchant ID (exact) or name"),
           FindField("service", "Service", "service")]
    date = date_expr(schema)
    if date:
        out.append(FindField("date", "Date", "date", expr=date,
                             help="the last visit or, in a call log, the last call (the later of the two), a day"))
    names = {f.name for f in schema.fields}
    if {"first_name", "last_name"} <= names:
        out.append(FindField("name", "Full name", "name", help="first and last name together"))
    both = {"first_name", "last_name"} <= names
    for f in schema.fields:
        kind = KIND_OF_TYPE.get(f.type, "text")
        if both and f.name in ("first_name", "last_name"):
            kind = "first" if f.name == "first_name" else "last"
        out.append(FindField(f"f:{f.name}", f.header, kind, column=f.name,
                             datetime=f.type == "datetime" and not date_only(f),
                             no_year_value=f.no_year_value, help=f.description))
    out += [FindField("issue", "Phone problem", "issue", column="phone_issue",
                      help="why the record has no usable phone number"),
            FindField("tag", "Tag", "tag", help="tag name; * = in any tag, = = in no tag"),
            FindField("import", "Import #", "job", help="the import's number on the Imports page"),
            FindField("file", "File name", "file"),
            FindField("created", "Record created", "created", datetime=True,
                      help="when the record was stored (its import), on the app's clock"),
            FindField("record", "Record ID", "number", column="id", help="the record's own number"),
            FindField("row", "File row", "number", column="file_row")]
    out += [FindField(f"col:{c}", f"Column: {c}", "column", column=c, help="as written in the file")
            for c in columns_seen]
    return out
