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
``date``        a date or date-time column; birthday / anniversary also take month/day without a year
``bool``        a true/false column (consent)
``name``        first and last name together ("Full name": "John Smith")
``any``         every value of the record, cleaned and as written in the file ("Any field")
``column``      one column of the original file, as written ("Column: Points"), for columns that feed no field
``business``    the business: its ID (exact) or its name
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

KIND_OF_TYPE = {"phone": "phone", "integer": "number", "decimal": "number", "date": "date", "datetime": "date",
                "boolean": "bool"}


@dataclass(frozen=True)
class FindField:
    """One searchable thing (see the module docstring)."""

    key: str             # stable id: "f:<field name>", "col:<file column>", or a special name ("business", …)
    label: str           # shown in the Field dropdown
    kind: str            # how criteria are read (records.criteria)
    column: str = ""     # records column (record fields), file column name (kind "column"), or special column
    datetime: bool = False   # a date-time column (whole days match from 12:00 AM; a typed time means that minute)
    no_year_value: int | None = None   # birthday / anniversary: the year meaning "no year" (1600)
    help: str = ""


# The Find page shows these as boxes in every request, followed by one box per record field; everything else
# (Any field, Full name, Tag, Import #, File name, File row, Phone problem, Column: …) is under "More criteria".
# Business and service have pickers of their own on the page, so they aren't offered there either.
FORM_KEYS = ["record", "created"]
PICKER_KEYS = {"business", "service"}


def find_fields(schema: TargetSchema, columns_seen: list[str]) -> list[FindField]:
    """Every searchable field: common ones, record fields, the record's ID and import, then the file columns."""
    out = [FindField("any", "Any field", "any", help="every value of the record, cleaned and as in the file"),
           FindField("business", "Business", "business", help="business ID (exact) or name"),
           FindField("service", "Service", "service")]
    names = {f.name for f in schema.fields}
    if {"first_name", "last_name"} <= names:
        out.append(FindField("name", "Full name", "name", help="first and last name together"))
    for f in schema.fields:
        kind = KIND_OF_TYPE.get(f.type, "text")
        out.append(FindField(f"f:{f.name}", f.header, kind, column=f.name, datetime=f.type == "datetime",
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
