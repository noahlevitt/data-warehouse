"""Customer Records: businesses, services, tags and the database setup (records.store, records.db)."""

import pytest
import yaml

from converter.schema import TargetSchema
from converter.storage import Store
from records import find as F
from records.db import Database, check_fields
from records.fields import find_fields
from records.store import RecordStore, StoreError

from .conftest import ROOT, drop_database, fresh_database
from .records_util import import_file


@pytest.fixture
def jobs(tmp_path):
    return Store(tmp_path / "converter.db")


def test_business_ids_are_the_main_systems_and_unique(rstore):
    """Any text ID (trimmed); the same ID twice is refused; ID and name are required."""
    rstore.add_business(" 1001 ", "Krave It Bayside", "Bayside store", "noah")
    assert rstore.business("1001")["name"] == "Krave It Bayside"
    with pytest.raises(StoreError, match="already exists"):
        rstore.add_business("1001", "Other")
    for bid, name in (("", "x"), ("2002", " ")):
        with pytest.raises(StoreError):
            rstore.add_business(bid, name)


def test_correcting_a_business_id_follows_into_imports_records_and_tags(rstore, jobs, records_schema):
    """ON UPDATE CASCADE: a mistyped ID can be fixed without losing anything."""
    _, imp = import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="101")
    tag = rstore.create_tag("101", "T")
    rstore.update_business("101", "1001", "Krave It")
    assert rstore.get_import(imp)["business_id"] == "1001" and rstore.tag(tag)["business_id"] == "1001"
    assert rstore.db.fetch_one("SELECT business_id FROM records")["business_id"] == "1001"
    rstore.add_business("2002", "B")
    with pytest.raises(StoreError, match="already exists"):
        rstore.update_business("2002", "1001", "B")


def test_a_business_in_use_cant_be_deleted(rstore, jobs, records_schema):
    import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", business="1001")
    with pytest.raises(StoreError, match="import"):
        rstore.delete_business("1001")
    rstore.add_business("3003", "Unused")
    rstore.delete_business("3003")
    assert rstore.business("3003") is None


def test_services_unique_deactivated_and_deleted_only_when_unused(rstore, jobs, records_schema):
    sid = rstore.add_service("Toast export")
    with pytest.raises(StoreError, match="already exists"):
        rstore.add_service("toast EXPORT")
    rstore.update_service(sid, "Toast export", active=False)
    assert rstore.list_services(active_only=True) == []
    other = rstore.add_service("Square")
    import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n", service="Square")
    with pytest.raises(StoreError, match="deactivate"):
        rstore.delete_service(other)
    rstore.delete_service(sid)
    assert [s["name"] for s in rstore.list_services()] == ["Square"]


def test_an_import_needs_an_existing_business_and_service(rstore):
    rstore.add_business("1001", "A")
    sid = rstore.add_service("S")
    with pytest.raises(StoreError, match="business"):
        rstore.create_import("9999", sid, "a.csv", "x", None, "t")
    with pytest.raises(StoreError, match="service"):
        rstore.create_import("1001", 999, "a.csv", "x", None, "t")


def test_a_tag_holds_one_business(rstore, jobs, records_schema):
    """Adding a found set adds only the tag's business's records and counts the others left out."""
    import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n5165550101\n", business="1001")
    import_file(rstore, jobs, records_schema, "b.csv", "Phone\n9175550100\n", business="2002")
    tag = rstore.create_tag("1001", "Promo", username="noah")
    cond, params = F.where(F.all_records(), [])
    assert rstore.add_to_tag(tag, cond, params, "noah") == {"added": 2, "already": 0, "other_business": 1}
    assert rstore.add_to_tag(tag, cond, params, "noah") == {"added": 0, "already": 2, "other_business": 1}
    t = rstore.tag(tag)
    assert (t["records"], t["numbers"], t["no_phone"]) == (2, 2, 0)
    assert [e["action"] for e in rstore.tag_events(tag)] == ["added", "created"]


def test_tag_names_are_unique_per_business(rstore):
    rstore.add_business("1001", "A")
    rstore.add_business("2002", "B")
    rstore.create_tag("1001", "Promo")
    rstore.create_tag("2002", "Promo")
    with pytest.raises(StoreError, match="already has a tag"):
        rstore.create_tag("1001", "promo")
    with pytest.raises(StoreError, match="business"):
        rstore.create_tag("9999", "X")


def test_removing_renaming_and_deleting_tags(rstore, jobs, records_schema):
    import_file(rstore, jobs, records_schema, "a.csv", "Phone\n5165550100\n5165550101\n")
    tag = rstore.create_tag("1001", "Promo")
    everything, params = F.where(F.all_records(), [])
    rstore.add_to_tag(tag, everything, params)
    one, p1 = F.where(F.find_node([{"omit": False, "criteria": [["f:phone_number", "5165550100"]]}]),
                      find_fields(records_schema, []))
    assert rstore.remove_from_tag(tag, one, p1, "noah") == 1
    rstore.update_tag(tag, "Promo 2", "notes", "noah")
    assert rstore.tag(tag)["name"] == "Promo 2"
    assert [e["action"] for e in rstore.tag_events(tag)][:2] == ["renamed", "removed"]
    rstore.delete_tag(tag)
    assert rstore.tag(tag) is None and rstore.totals()["records"] == 2  # the records stay


def test_setup_is_repeatable_and_adds_new_fields(pg_server, records_schema):
    """setup() can run on every start; a new field in the YAML gets its column, a retyped one a warning."""
    url = fresh_database(pg_server)
    db = Database(url, max_size=2)
    try:
        assert db.setup(records_schema) == [] and db.setup(records_schema) == []
        assert db.trigram
        data = yaml.safe_load((ROOT / "schemas" / "records_schema.yaml").read_text())
        data["fields"].append({"name": "loyalty_points", "header": "Points", "type": "integer"})
        assert db.setup(TargetSchema.model_validate(data)) == []
        cols = {r["column_name"]: r["data_type"] for r in db.fetch(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'records'")}
        assert cols["loyalty_points"] == "bigint"
        data["fields"][-1]["type"] = "string"
        warnings = db.setup(TargetSchema.model_validate(data))
        assert len(warnings) == 1 and "loyalty_points" in warnings[0]
    finally:
        db.close()
        drop_database(pg_server, url)


def test_a_database_from_before_1_2_0_gets_a_date_only_last_visit(pg_server, records_schema):
    """1.0.x/1.1.0 stored the last visit with a time; setup converts the column to a date, keeping each day."""
    url = fresh_database(pg_server)
    db = Database(url, max_size=2)
    try:
        data = yaml.safe_load((ROOT / "schemas" / "records_schema.yaml").read_text())
        old = next(f for f in data["fields"] if f["name"] == "transactions_last")
        old.update(type="datetime", format="%m/%d/%Y %I:%M %p")
        db.setup(TargetSchema.model_validate(data))
        rs = RecordStore(db)
        rs.add_business("1001", "A")
        sid = rs.add_service("S")
        imp = rs.create_import("1001", sid, "f.csv", "x", None, "t")
        db.execute("INSERT INTO records (import_id, business_id, service_id, file_row, transactions_last) "
                   "VALUES (%s, '1001', %s, 2, '2026-10-09 23:30')", (imp, sid))
        assert db.setup(records_schema) == []
        col = db.fetch_one("SELECT data_type FROM information_schema.columns "
                           "WHERE table_name = 'records' AND column_name = 'transactions_last'")
        assert col["data_type"] == "date"
        assert db.fetch_one("SELECT transactions_last FROM records")["transactions_last"].isoformat() == "2026-10-09"
        assert db.setup(records_schema) == []   # and again on the next start: nothing to do
    finally:
        db.close()
        drop_database(pg_server, url)


def test_field_names_must_fit_a_database_column(records_schema):
    data = records_schema.model_dump()
    for bad in ("Last Visit", "source", "1st"):
        d = dict(data, fields=data["fields"] + [{"name": bad, "type": "string"}])
        with pytest.raises(ValueError):
            check_fields(TargetSchema.model_validate(d))
    two = dict(data, fields=data["fields"] + [{"name": "phone2", "type": "phone"}])
    with pytest.raises(ValueError, match="one phone field"):
        check_fields(TargetSchema.model_validate(two))
