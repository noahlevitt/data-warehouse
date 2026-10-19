"""Customer Records: a tag's export — one row per unique number, in the converter's import format."""

import sys

import pytest

from converter.mapping import FieldMapping
from converter.output import write_output
from converter.storage import Store
from records import find as F
from records.tags import export_file, export_filename, export_table

from .conftest import ROOT
from .records_util import import_file


@pytest.fixture
def jobs(tmp_path):
    return Store(tmp_path / "converter.db")


def tag_all(rs, business="1001", name="All"):
    """A tag holding every record of the business."""
    tag = rs.create_tag(business, name)
    cond, params = F.where(F.all_records(), [])
    rs.add_to_tag(tag, cond, params)
    return tag


def rows(table, real_schema):
    return {r["phone_number"]: r for r in table.to_dict("records")}


def test_export_is_exactly_the_import_format(rstore, jobs, records_schema, real_schema):
    """Same headers in the same order; the file is what the converter's writer makes of the rows."""
    import_file(rstore, jobs, records_schema, "a.csv", "Phone,Last Visit,Birthday\n5165550100,10/01/2026,9/12\n")
    tag = tag_all(rstore)
    table, counts = export_table(rstore.db, tag, records_schema, real_schema)
    assert list(table.columns) == [f.header for f in real_schema.fields]
    data, _ = export_file(rstore.db, tag, records_schema, real_schema)
    assert data == write_output(table, real_schema)
    assert data.decode().splitlines()[1] == "5165550100,,,10/01/2026 12:00 AM,,,,,09/12/1600,"


def test_each_number_once_with_the_latest_dates(rstore, jobs, records_schema, real_schema):
    """A number in several records (several files, or several numbers per row) is one row; dates are the latest."""
    import_file(rstore, jobs, records_schema, "a.csv",
                "Phone,Mobile,Last Visit,Last Call\n5165550100,9175550100,10/01/2026,09/01/2026 1:00 PM\n")
    import_file(rstore, jobs, records_schema, "b.csv",
                "Phone,Last Visit,Last Call\n(516) 555-0100,09/15/2026,09/20/2026 2:30 PM\n")
    tag = tag_all(rstore)
    table, counts = export_table(rstore.db, tag, records_schema, real_schema)
    by = rows(table, real_schema)
    assert set(by) == {"5165550100", "9175550100"}
    assert by["5165550100"]["transactions_last"] == "10/01/2026 12:00 AM"
    assert by["5165550100"]["calls_last"] == "09/20/2026 02:30 PM"
    assert by["9175550100"]["calls_last"] == "09/01/2026 01:00 PM"
    assert (counts["records"], counts["numbers"], counts["shared"], counts["merged"]) == (2, 2, 1, 1)


def test_birthday_with_a_year_wins(rstore, jobs, records_schema, real_schema):
    """A real year beats 'no year' (1600); otherwise the most common date, the latest record on a tie."""
    import_file(rstore, jobs, records_schema, "a.csv", "Phone,Birthday\n5165550100,9/12\n5165550101,3/1/1980\n")
    import_file(rstore, jobs, records_schema, "b.csv", "Phone,Birthday\n5165550100,9/12/1985\n5165550101,3/2/1981\n")
    tag = tag_all(rstore)
    by = rows(export_table(rstore.db, tag, records_schema, real_schema)[0], real_schema)
    assert by["5165550100"]["birthday"] == "09/12/1985"
    assert by["5165550101"]["birthday"] == "03/02/1981"


def test_consent_is_always_blank(rstore, jobs, records_schema, real_schema):
    """Decision 2026-10-14: the main system handles opt-outs per business; the records keep the file's consent."""
    def by_hand(m):
        m.fields["consent"] = FieldMapping(sources=["Opt In"], transform="copy", origin="user",
                                           params={"modes": {"Opt In": "both"}})
        return m
    import_file(rstore, jobs, records_schema, "a.csv", "Phone,Opt In\n5165550100,Y\n5165550101,N\n", edit=by_hand)
    assert [r["consent"] for r in rstore.db.fetch("SELECT consent FROM records ORDER BY id")] == [True, False]
    table, _ = export_table(rstore.db, tag_all(rstore), records_schema, real_schema)
    assert set(table["consent"]) == {""}


def test_records_without_a_number_are_left_out_and_counted(rstore, jobs, records_schema, real_schema):
    import_file(rstore, jobs, records_schema, "a.csv", "Name,Phone\nA,5165550100\nB,\nC,123\n")
    table, counts = export_table(rstore.db, tag_all(rstore), records_schema, real_schema)
    assert list(table["phone_number"]) == ["5165550100"]
    assert (counts["records"], counts["no_phone"], counts["numbers"]) == (3, 2, 1)


def test_creation_date_is_the_last_visit_only_for_files_without_one(rstore, jobs, records_schema, real_schema):
    """As the converter: per import, not per number (a blank Last Visit cell stays blank). The last visit is a date
    only (1.2.0), so the creation date stands in by its day."""
    import_file(rstore, jobs, records_schema, "created.csv", "Phone,Created At\n5165550100,2024-01-05 14:30\n")
    import_file(rstore, jobs, records_schema, "visits.csv",
                "Phone,Created At,Last Visit\n5165550101,2024-01-05,\n5165550102,2024-01-05,10/01/2026\n")
    by = rows(export_table(rstore.db, tag_all(rstore), records_schema, real_schema)[0], real_schema)
    assert by["5165550100"]["transactions_last"] == "01/05/2024 12:00 AM"
    assert by["5165550101"]["transactions_last"] == ""
    assert by["5165550102"]["transactions_last"] == "10/01/2026 12:00 AM"


def test_an_empty_tag_exports_only_the_header(rstore, real_schema, records_schema):
    rstore.add_business("1001", "A")
    tag = rstore.create_tag("1001", "Empty")
    data, counts = export_file(rstore.db, tag, records_schema, real_schema)
    assert data.decode().splitlines() == [",".join(f.header for f in real_schema.fields)]
    assert counts["numbers"] == 0


def test_export_file_name_is_safe(real_schema):
    assert export_filename("1001", "Oct promo / lapsed: 90 days", real_schema, "20261014-1100") == \
        "1001_Oct_promo_lapsed_90_days_20261014-1100.csv"


@pytest.mark.parametrize("name", ["restaurant_crm_export.csv", "pos_guest_export.csv", "loyalty_members.xlsx",
                                  "loyalty_activity.xlsx", "eu_style_semicolon.csv", "no_header.tsv"])
def test_a_tag_of_one_file_exports_what_the_converter_makes(rstore, samples, records_schema, real_schema, name):
    """Parity with the file converter (scripts/regression/parity.py runs this on all 406 regression files)."""
    sys.path.insert(0, str(ROOT / "scripts" / "regression"))
    import parity
    sid = rstore.add_service("parity") if not rstore.list_services() else rstore.list_services()[0]["id"]
    res = parity.compare_file(name, (samples / name).read_bytes(), real_schema, records_schema, rstore, sid)
    assert res["diffs"] == [], res["diffs"][:5]
    # loyalty_members.xlsx opens on its Summary sheet (converter KNOWN_ISSUES #33): no numbers either way.
    assert res["numbers"] > 0 or name == "loyalty_members.xlsx"
