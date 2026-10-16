"""Customer Records: the Date (the later of the last visit and the last call, as a day) and days of the week in
date criteria (1.3.0). The records are imported through the real import path into a fresh database."""

from datetime import date, datetime

import pytest
from psycopg import sql

from converter.storage import Store
from records import find as F
from records.db import date_expr
from records.fields import find_fields
from records.tags import export_file, export_table
from records.ui import find_layout

from .records_util import import_file

# Fri 10/9, Sat 10/10, Sun 10/11, Mon 10/12, Fri 10/16, Fri 9/4 (2026).
VISITS = """First Name,Phone,Last Visit,Birthday
Fay,5165550101,10/09/2026 7:45 PM,5/6/1990
Sam,5165550102,10/10/2026,9/12
Sue,5165550103,10/11/2026,
Mo,5165550104,10/12/2026,
Fred,5165550105,10/16/2026,
Sep,5165550106,09/04/2026,
Nell,5165550107,,
"""
# A call log: the last call keeps its time in its own field; the Date is its day (11:30 PM Friday stays Friday).
CALLS = """First Name,Phone,Last Call
Cal,9175550201,10/09/2026 11:30 PM
Carl,9175550202,10/11/2026 9:00 AM
"""
# Both: the Date is the later of the two.
BOTH = """First Name,Phone,Last Visit,Last Call
Bo,6465550301,10/01/2026,10/05/2026 2:00 PM
Bea,6465550302,10/07/2026,10/02/2026 2:00 PM
"""


@pytest.fixture
def data(rstore, records_schema, tmp_path):
    jobs = Store(tmp_path / "c.db")
    import_file(rstore, jobs, records_schema, "visits.csv", VISITS)
    import_file(rstore, jobs, records_schema, "calls.csv", CALLS)
    import_file(rstore, jobs, records_schema, "both.csv", BOTH)
    return rstore, records_schema


def found(data, requests, sort="Import order"):
    rs, schema = data
    fields = find_fields(schema, rs.columns_seen())
    cond, params = F.where(F.find_node(requests), fields, collation=rs.db.collation, today=date(2026, 10, 14))
    return [r["first_name"] for r in F.page(rs.db, schema, cond, params, sort=sort, limit=100)]


def names(data, key, text):
    return sorted(found(data, [{"criteria": [[key, text]]}]))


def test_the_date_is_the_last_visit_or_the_last_call_whichever_is_later(data):
    assert names(data, "date", "10/9/2026") == ["Cal", "Fay"]           # a visit and a call on that day
    assert names(data, "date", "10/5/2026") == ["Bo"]                   # visit 10/1, call 10/5: the call
    assert names(data, "date", "10/7/2026") == ["Bea"]                  # visit 10/7, call 10/2: the visit
    assert names(data, "date", "10/1/2026") == []                       # Bo's visit is not his Date
    assert names(data, "date", "...10/2/2026") == ["Sep"]
    assert names(data, "date", "=") == ["Nell"]
    assert len(names(data, "date", "*")) == 10
    # The two fields are still stored (and exported) as they were.
    assert names(data, "f:transactions_last", "10/1/2026") == ["Bo"]
    assert names(data, "f:calls_last", "10/9/2026 11:30 PM") == ["Cal"]


def test_a_day_of_the_week_alone(data):
    fridays = ["Cal", "Fay", "Fred", "Sep"]          # not Bo: his visit was Thu 10/1, his Date (a call) Mon 10/5
    for text in ["fri", "Fri", "FRI", "friday", "Friday", "fridays", "fri.", " fri "]:
        assert names(data, "date", text) == fridays, text
    assert names(data, "date", "sat sun") == ["Carl", "Sam", "Sue"]
    assert names(data, "date", "sat, sun") == names(data, "date", "sat and sun") == ["Carl", "Sam", "Sue"]
    assert names(data, "date", "mon...fri") == ["Bea", "Bo", "Cal", "Fay", "Fred", "Mo", "Sep"]
    assert names(data, "date", "fri...sun") == ["Cal", "Carl", "Fay", "Fred", "Sam", "Sep", "Sue"]   # over the weekend
    assert names(data, "date", "fri..sun") == names(data, "date", "fri…sun") == names(data, "date", "fri...sun")
    assert names(data, "date", "mon") == ["Bo", "Mo"]
    assert names(data, "date", "wed") == ["Bea"]
    for text in ["mon-fri", "Mon.-Fri.", "mon - fri", "mon to fri", "mon through fri"]:
        assert names(data, "date", text) == names(data, "date", "mon...fri"), text
    assert names(data, "date", "sat/sun") == names(data, "date", "sat / sun") == ["Carl", "Sam", "Sue"]


def test_a_day_of_the_week_with_a_date(data):
    assert names(data, "date", "fri 10/2026") == ["Cal", "Fay", "Fred"]
    assert names(data, "date", "fri 10/1/2026...10/12/2026") == ["Cal", "Fay"]
    assert names(data, "date", "fri ...10/1/2026") == ["Sep"]
    assert names(data, "date", "fri >10/9/2026") == ["Fred"]
    assert names(data, "date", "sat sun 10/2026") == ["Carl", "Sam", "Sue"]
    assert names(data, "date", "fri 10/9/2026") == ["Cal", "Fay"]
    assert names(data, "date", "Friday, October 9, 2026") == ["Cal", "Fay"]
    assert names(data, "date", "sat 10/9/2026") == []                   # that day isn't a Saturday


def test_omit_the_weekends_and_combine_with_other_fields(data):
    assert sorted(found(data, [{"criteria": [["date", "10/2026"]]},
                               {"omit": True, "criteria": [["date", "sat sun"]]}])) == \
        ["Bea", "Bo", "Cal", "Fay", "Fred", "Mo"]
    assert names(data, "f:first_name", "f*") == ["Fay", "Fred"]
    assert sorted(found(data, [{"criteria": [["date", "fri"], ["f:first_name", "f*"]]}])) == ["Fay", "Fred"]


def test_days_of_the_week_in_other_dates(data):
    rs, schema = data
    # Record created is on the app's clock: 11:30 PM Friday in New York is Saturday in UTC.
    rs.db.execute("UPDATE records SET created_at = '2026-10-09 23:30:00-04' WHERE first_name = 'Fay'")
    rs.db.execute("UPDATE records SET created_at = '2026-10-12 10:00:00-04' WHERE first_name <> 'Fay'")
    assert names(data, "created", "fri") == ["Fay"]
    assert names(data, "created", "sat") == []
    # Birthdays: 5/6/1990 was a Sunday; a birthday without a year (stored 1600) never matches a day of the week.
    assert names(data, "f:birthday", "sun") == ["Fay"]
    assert names(data, "f:birthday", "tue") == []                       # 9/12/1600 was a Tuesday
    assert names(data, "f:birthday", "9/12") == ["Sam"]


def test_what_cant_be_read_says_so(data):
    rs, schema = data
    fields = find_fields(schema, rs.columns_seen())
    for text, msg in [("<fri", "can't be compared"), ("1/1/2026...fri", "can't be compared"),
                      ("oct...mon", "can't be compared"), ("x...fri", "can't be compared"),
                      (">fri 10/1/2026", "can't be compared"), ("> fri 10/1/2026", "can't be compared"),
                      ("fri 13/45/2026", "isn't a"), ("someday", "or Fri")]:
        with pytest.raises(F.FindError, match=msg):
            F.where(F.find_node([{"criteria": [["date", text]]}]), fields)


def test_the_found_set_shows_sorts_and_writes_the_date(data):
    rs, schema = data
    fields = find_fields(schema, rs.columns_seen())
    assert "Date (newest first)" in F.available_sorts(schema)
    assert "Last visit (newest first)" not in F.available_sorts(schema)
    assert found(data, [{}], sort="Date (newest first)")[:4] == ["Fred", "Mo", "Sue", "Carl"]   # a tie: import order
    cond, params = F.where(F.find_node([{"criteria": [["f:first_name", "bo"]]}]), fields)
    rows = F.page(rs.db, schema, cond, params)
    show = [F.DATE, "transactions_last", "calls_last", "birthday", "first_name"]
    table = F.page_frame(rows, schema, show)
    assert table.iloc[0][F.DATE_COL] == date(2026, 10, 5)              # values stay dates: shown by the page as
    assert table.iloc[0]["Last visit / order"] == date(2026, 10, 1)    # "Mon, 10/5/2026" (date_columns)
    assert table.iloc[0]["Last call"] == datetime(2026, 10, 5, 14, 0)
    assert F.date_columns(schema, show) == {
        "Created": ("datetime", "ddd, M/D/YYYY h:mm A"), F.DATE_COL: ("date", "ddd, M/D/YYYY"),
        "Last visit / order": ("date", "ddd, M/D/YYYY"), "Last call": ("datetime", "ddd, M/D/YYYY h:mm A"),
        "Birthday": ("date", "M/D/YYYY")}                              # no day of the week for birthdays
    csv = F.found_set_csv(rs.db, schema, cond, params).decode("utf-8-sig").splitlines()
    head = csv[0].split(",")
    assert head[11:13] == ["Date", "Day"]
    assert csv[1].split(",")[11:13] == ["10/05/2026", "Mon"]
    assert F.record_date(schema, rs.record(rows[0]["id"])) == date(2026, 10, 5)


def test_the_find_page_shows_the_date_instead_of_last_visit_and_last_call(data):
    rs, schema = data
    fields = find_fields(schema, rs.columns_seen())
    layout = find_layout(schema, fields)
    assert [f.label for f in layout.main] == ["Date"]
    menu = [f.key for fs in layout.groups.values() for f in fs]
    assert "f:transactions_last" not in menu and "f:calls_last" not in menu
    assert {"f:in_sms_last", "f:redemptions_last", "created", "f:birthday"} <= set(menu)


def test_the_date_has_an_index_and_finds_and_the_sort_use_indexes(data):
    """Criteria are NULL-safe without COALESCE (1.3.0), so the database can use the fields' indexes."""
    rs, schema = data
    rows = rs.db.fetch("SELECT indexname, indexdef FROM pg_indexes WHERE indexname LIKE 'records_date_%'")
    assert len(rows) == 1
    assert "GREATEST(transactions_last, (calls_last)::date) DESC NULLS LAST" in rows[0]["indexdef"]
    index = rows[0]["indexname"]
    fields = find_fields(schema, rs.columns_seen())

    def plan(query, params=None):
        with rs.db.conn() as c:
            c.execute("SET LOCAL enable_seqscan = off")
            return "\n".join(r["QUERY PLAN"] for r in c.execute(query, params))

    def searched_by_index(text: str) -> bool:
        """An index condition does the comparison itself (not only "value IS NOT NULL", the NULL guard)."""
        return any("Index Cond" in line and any(op in line for op in (" > ", " >= ", " < ", " <= ", " = ", " ~* "))
                   for line in text.splitlines())

    for key, text, used in [("date", "1/1/2026...1/20/2026", index), ("date", "fri 10/2026", index),
                            ("date", ">=10/1/2026", index),
                            ("f:transactions_last", "10/2026", "records_f_transactions_last"),
                            ("f:city", "bay", "records_f_city"), ("f:total_visits", ">5", "records_f_total_visits"),
                            ("f:total_visits", "3...8", "records_f_total_visits"), ("record", "5", "records_pkey"),
                            ("f:total_spent", ">=10.5", "records_f_total_spent"),
                            ("f:first_name", "fay", "records_f_first_name"),
                            ("f:first_name", "fay", "records_first_name_split"), ("any", "fay", "records_search")]:
        cond, params = F.where(F.find_node([{"criteria": [[key, text]]}]), fields, collation=rs.db.collation)
        shown = plan(sql.SQL("EXPLAIN SELECT r.id FROM records r WHERE {}").format(cond), params)
        assert used in shown and searched_by_index(shown), (key, text, shown)
    assert index in plan(f"EXPLAIN SELECT r.id FROM records r ORDER BY {date_expr(schema)} DESC NULLS LAST, r.id "
                         "LIMIT 100")


def test_a_changed_date_index_replaces_the_old_one(data):
    rs, schema = data
    rs.db.execute("CREATE INDEX records_date_0000abcd ON records (transactions_last)")
    rs.db.setup(schema)
    assert [r["indexname"] for r in rs.db.fetch("SELECT indexname FROM pg_indexes WHERE indexname LIKE "
                                                "'records_date_%'")] != ["records_date_0000abcd"]
    assert len(rs.db.fetch("SELECT 1 FROM pg_indexes WHERE indexname LIKE 'records_date_%'")) == 1


def test_dates_on_screen_show_their_day_but_tag_exports_keep_the_import_format(data, real_schema):
    """The screen shows "Fri, 10/9/2026" (decided 2026-10-15); a tag export writes what the main system imports."""
    assert F.shown(date(2026, 10, 9)) == "Fri, 10/9/2026"
    assert F.shown(datetime(2026, 10, 9, 15, 45)) == "Fri, 10/9/2026 3:45 PM"
    assert F.shown(datetime(2026, 10, 9, 0, 5)) == "Fri, 10/9/2026 12:05 AM"
    assert F.shown(datetime(2026, 10, 9, 12, 0)) == "Fri, 10/9/2026 12:00 PM"
    assert F.shown(date(1600, 9, 12), weekday=False) == "9/12/1600"
    assert F.shown(None) == "" and F.shown("Bayside") == "Bayside"
    rs, schema = data
    tag = rs.create_tag("1001", "Fridays")
    cond, params = F.where(F.find_node([{"criteria": [["date", "fri"]]}]), find_fields(schema, rs.columns_seen()))
    rs.add_to_tag(tag, cond, params)
    table, _ = export_table(rs.db, tag, schema, real_schema)
    by = {r["phone_number"]: r for r in table.to_dict("records")}
    assert by["5165550101"]["transactions_last"] == "10/09/2026 12:00 AM"       # Fay: the last visit, a day
    assert by["9175550201"]["calls_last"] == "10/09/2026 11:30 PM"              # Cal: the call keeps its time
    assert not any("Fri" in str(v) for r in by.values() for v in r.values())
    data_file, _ = export_file(rs.db, tag, schema, real_schema)
    assert "10/09/2026 12:00 AM" in data_file.decode() and "Fri," not in data_file.decode()
