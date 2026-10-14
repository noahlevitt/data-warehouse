"""Customer Records: an import stores one record per file row, with all its numbers and every column.

Runs real imports (converter.jobs.run_import + records.importer.RecordSink) against a fresh PostgreSQL database.
"""

import pytest

from converter.heuristics import auto_map
from converter.mapping import FieldMapping, profile_columns
from converter.reader import read_file
from converter.schema import FieldSpec
from converter.storage import Store
from records.importer import NO_PHONE, RecordSink, creation_fallback

from .records_util import import_file, phones_of


@pytest.fixture
def jobs(tmp_path):
    """The app's import store (SQLite) in a temporary folder."""
    return Store(tmp_path / "converter.db")


GUESTS = """Name,Phone,Phone 2,Email,Visits,Total Spend,Created At,Last Visit,Birthday,Notes,Customer ID
"Smith, John",(516) 555-0100,917-555-1234,JOHN@X.COM,5,"$1,234.50",2024-01-05,10/09/2026 3:45 PM,9/12,vip,C1
Mary Jones,516-555-0101 / 516-555-0199,,mary@x.com,2,12,2024-02-01,,1985-03-04,,C2
No Phone,,,np@x.com,1,0,2024-03-01,,,,C3
Bad Phone,123,,,1,,,,,,C4
,,,,,,,,,,
Ann Lee,516-555-0102,,,,,,,,,C5
"""


def records_of(rs, imp):
    return rs.db.fetch("SELECT * FROM records WHERE import_id = %s ORDER BY file_row", (imp,))


def test_one_record_per_file_row_with_all_its_numbers(rstore, jobs, records_schema):
    """Several phone columns and several numbers in one cell stay on one record."""
    rec, imp = import_file(rstore, jobs, records_schema, "guests.csv", GUESTS)
    assert rec.status == "completed", rec.error
    rows = records_of(rstore, imp)
    assert [r["file_row"] for r in rows] == [2, 3, 4, 5, 7]  # the empty row 6 is skipped
    assert phones_of(rstore, rows[0]["id"]) == ["5165550100", "9175551234"]
    assert phones_of(rstore, rows[1]["id"]) == ["5165550101", "5165550199"]
    assert (rows[0]["first_name"], rows[0]["last_name"]) == ("John", "Smith")
    imp_row = rstore.get_import(imp)
    assert (imp_row["status"], imp_row["records"], imp_row["phones"], imp_row["no_phone"]) == ("completed", 5, 5, 2)
    assert rec.summary["records"]["stored"] == 5 and rec.summary["records"]["numbers"] == 5


def test_rows_without_a_usable_number_are_kept_with_the_reason(rstore, jobs, records_schema):
    """Business decision: such rows are records too (searchable), with why they have no number."""
    _, imp = import_file(rstore, jobs, records_schema, "guests.csv", GUESTS)
    rows = {r["first_name"]: r for r in records_of(rstore, imp)}
    assert rows["No"]["phone_issue"] == NO_PHONE and not phones_of(rstore, rows["No"]["id"])
    assert rows["Bad"]["phone_issue"] == "too few digits (3)"
    assert rows["Ann"]["phone_issue"] == "" and rows["Ann"]["email"] is None


def test_every_column_is_kept_as_written(rstore, jobs, records_schema):
    """Columns that feed no field (Notes, Customer ID) are in the record's source row, with the others."""
    _, imp = import_file(rstore, jobs, records_schema, "guests.csv", GUESTS)
    r = records_of(rstore, imp)[0]
    assert r["source"]["Notes"] == "vip" and r["source"]["Customer ID"] == "C1"
    assert r["source"]["Phone"] == "(516) 555-0100" and r["source"]["Total Spend"] == "$1,234.50"
    assert "Notes" in rstore.get_import(imp)["columns"] and "Notes" in rstore.columns_seen()
    assert "vip" in r["search_text"] and "5165550100" in r["search_text"] and "john" in r["search_text"]


def test_skipped_columns_are_left_out_completely(rstore, jobs, records_schema):
    """A column ticked Skip on the mapping page ("keep it out of the import") isn't kept as written either."""
    def skip_notes(m):
        return m.model_copy(update={"excluded_columns": ["Notes"]})
    _, imp = import_file(rstore, jobs, records_schema, "guests.csv", GUESTS, edit=skip_notes)
    r = records_of(rstore, imp)[0]
    assert "Notes" not in r["source"] and "vip" not in r["search_text"] and r["source"]["Customer ID"] == "C1"
    assert "Notes" not in rstore.get_import(imp)["columns"]


def test_values_are_typed(rstore, jobs, records_schema):
    """Dates, date-times, money and counts are stored as such; birthdays without a year get 1600."""
    _, imp = import_file(rstore, jobs, records_schema, "guests.csv", GUESTS)
    a, b = records_of(rstore, imp)[:2]
    assert str(a["total_spent"]) == "1234.5" and a["total_visits"] == 5
    assert a["email"] == "john@x.com"
    assert a["birthday"].isoformat() == "1600-09-12" and b["birthday"].isoformat() == "1985-03-04"
    assert a["transactions_last"].isoformat() == "2026-10-09T15:45:00"
    assert a["first_visit"].isoformat() == "2024-01-05T00:00:00"


def test_consent_only_when_a_person_maps_it(rstore, jobs, records_schema):
    """As in the converter: the rules never fill consent; a person's mapping stores true/false."""
    csv = "Phone,SMS Opt In\n5165550100,Y\n5165550101,N\n5165550102,maybe\n"
    _, imp = import_file(rstore, jobs, records_schema, "optin.csv", csv)
    assert [r["consent"] for r in records_of(rstore, imp)] == [None, None, None]

    def by_hand(m):
        m.fields["consent"] = FieldMapping(sources=["SMS Opt In"], transform="copy", origin="user")
        return m
    _, imp2 = import_file(rstore, jobs, records_schema, "optin2.csv", csv, edit=by_hand)
    assert [r["consent"] for r in records_of(rstore, imp2)] == [True, False, None]


def test_repaired_numbers_are_marked_and_keep_the_original(rstore, jobs, records_schema):
    """A 7-digit number given the file's main area code (approved repair) is stored marked, with the cell."""
    csv = "Phone\n" + "".join(f"516-555-01{i:02d}\n" for i in range(6)) + "555-0177\n"
    _, imp = import_file(rstore, jobs, records_schema, "short.csv", csv)
    last = records_of(rstore, imp)[-1]
    [p] = rstore.record(last["id"])["phones"]
    assert (p["phone"], p["repaired"], p["original"]) == ("5165550177", True, "555-0177")


def test_creation_date_stands_in_only_without_a_last_activity_column(rstore, jobs, records_schema):
    """Like the converter: a file whose only date is a creation date exports it as the last transaction."""
    only_created = "Phone,Created At\n5165550100,2024-01-05\n5165550101,2024-02-05\n"
    _, imp = import_file(rstore, jobs, records_schema, "created.csv", only_created)
    assert rstore.get_import(imp)["first_visit_as"] == "transactions_last"
    with_visit = "Phone,Created At,Last Visit\n5165550100,2024-01-05,10/01/2026\n"
    _, imp2 = import_file(rstore, jobs, records_schema, "visit.csv", with_visit)
    assert rstore.get_import(imp2)["first_visit_as"] is None
    call_log = "From,To,Duration,Created At\n5165550100,5165559999,30,2024-01-05\n5165550101,5165559999,12,2024-01-06\n"
    _, imp3 = import_file(rstore, jobs, records_schema, "calls.csv", call_log)
    assert rstore.get_import(imp3)["first_visit_as"] == "calls_last"


def test_creation_fallback_needs_a_real_date_column(records_schema):
    """A creation column of mostly non-dates doesn't stand in for the last activity (the converter's 80% rule)."""
    rr = read_file("x.csv", b"Phone,Created At\n5165550100,yes\n5165550101,no\n5165550102,2024-01-01\n")
    m = auto_map(records_schema, profile_columns(rr.df), rr.df)
    m.fields["first_visit"] = FieldMapping(sources=["Created At"], transform="copy", origin="user")
    assert creation_fallback(records_schema, m, rr.df) is None


def test_a_generic_date_still_goes_to_the_last_visit_next_to_a_creation_date(records_schema):
    """creation_date fields don't count as a last date: "Date" still routes to transactions_last (step 7)."""
    rr = read_file("x.csv", b"Phone,Date,Created At\n5165550100,10/01/2026,2024-01-05\n"
                            b"5165550101,10/02/2026,2024-01-06\n")
    m = auto_map(records_schema, profile_columns(rr.df), rr.df)
    assert m.get("first_visit").sources == ["Created At"]
    assert m.get("transactions_last").sources == ["Date"]


def test_creation_date_attribute_is_checked():
    """creation_date only on date/date-time fields without generic_date."""
    with pytest.raises(ValueError):
        FieldSpec(name="x", type="string", creation_date=True)
    with pytest.raises(ValueError):
        FieldSpec(name="x", type="datetime", creation_date=True, generic_date="default")
    assert FieldSpec(name="x", type="datetime", creation_date=True).creation_date


def test_discard_removes_what_was_saved(rstore, jobs, records_schema):
    """An import that ends cancelled or failed after saving keeps no records."""
    rec, imp = import_file(rstore, jobs, records_schema, "guests.csv", GUESTS)
    RecordSink(rstore, jobs).discard(rec.id)
    assert not records_of(rstore, imp) and rstore.get_import(imp)["status"] == "not completed"


def test_an_import_without_business_fails_cleanly(rstore, jobs, records_schema):
    """A job with no Customer Records import (e.g. made by the converter's CLI) fails with a message."""
    from converter.jobs import run_import
    data = b"Phone\n5165550100\n"
    mapping = auto_map(records_schema, profile_columns(read_file("a.csv", data).df))
    job = jobs.create_import(username="t", filename="a.csv", data=data, schema_label=records_schema.label,
                             schema_json=records_schema.model_dump_json(), mapping=mapping, read_options={},
                             template_name=None, ai_used=False)
    rec = run_import(jobs, job, RecordSink(rstore, jobs))
    assert rec.status == "failed" and "business and service" in rec.error


def test_cancel_after_saving_removes_the_records(rstore, jobs, records_schema, monkeypatch):
    """A cancel noticed after the records were saved (the 0.99 step) leaves none behind."""
    import converter.jobs as J
    real = J.ProgressTracker.__call__

    def cancel_at_finish(self, fraction, stage, force=False):
        if stage == "Finishing":
            raise J.ImportCancelled()
        return real(self, fraction, stage, force)
    monkeypatch.setattr(J.ProgressTracker, "__call__", cancel_at_finish)
    rec, imp = import_file(rstore, jobs, records_schema, "guests.csv", GUESTS)
    assert rec.status == "cancelled" and not records_of(rstore, imp)


def test_same_file_is_noticed_per_business(rstore, jobs, records_schema):
    """The same file imported again for the business is listed (the page warns); another business isn't."""
    import hashlib
    _, imp = import_file(rstore, jobs, records_schema, "guests.csv", GUESTS)
    sha = hashlib.sha1(GUESTS.encode()).hexdigest()
    assert [x["id"] for x in rstore.same_file_imports("1001", sha)] == [imp]
    assert rstore.same_file_imports("2002", sha) == []


def test_deleting_an_import_removes_its_records_from_tags(rstore, jobs, records_schema):
    """The records go, the tags lose them and say so in their history; the import is marked deleted."""
    from records import find as F
    _, imp = import_file(rstore, jobs, records_schema, "guests.csv", GUESTS)
    tag = rstore.create_tag("1001", "Promo")
    cond, params = F.where(F.all_records(), [])
    assert rstore.add_to_tag(tag, cond, params)["added"] == 5
    res = rstore.delete_import(imp, "noah")
    assert res == {"records": 5, "tags": {"Promo": 5}}
    assert rstore.tag(tag)["records"] == 0 and rstore.get_import(imp)["status"] == "deleted"
    assert rstore.tag_events(tag)[0]["action"] == "removed" and "deleted" in rstore.tag_events(tag)[0]["detail"]


def test_big_file_is_stored_quickly(rstore, jobs, records_schema):
    """20,000 rows with two numbers each are stored as 20,000 records (one transaction)."""
    import time
    lines = ["First Name,Last Name,Phone,Mobile,Last Visit"]
    for i in range(20000):
        line = i % 10000
        lines.append(f"A{i},B{i},516{i % 900 + 100:03d}{line:04d},917{i % 900 + 100:03d}{line:04d},10/01/2026")
    t = time.time()
    rec, imp = import_file(rstore, jobs, records_schema, "big.csv", "\n".join(lines) + "\n")
    assert rec.status == "completed", rec.error
    assert rstore.get_import(imp)["records"] == 20000
    assert time.time() - t < 60
