"""Customer Records: merchants and locations, services, tags and the database setup (records.store, records.db)."""

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


def test_merchant_ids_are_unique(rstore):
    """Adding one by hand (tests, scripts): the ID trimmed; the same ID twice is refused; ID and name required."""
    rstore.add_business(" 1001 ", "Krave It Bayside", "Bayside store", "noah")
    assert rstore.business("1001")["name"] == "Krave It Bayside"
    with pytest.raises(StoreError, match="already exists"):
        rstore.add_business("1001", "Other")
    for bid, name in (("", "x"), ("2002", " ")):
        with pytest.raises(StoreError):
            rstore.add_business(bid, name)


MERCHANTS = [{"id": "63", "name": "Flakowitz Corporate", "status": "active"},
             {"id": "183", "name": "KPOT", "status": "active"},
             {"id": "203", "name": "La Familia Deli & Grill CORPORATE", "status": "active"},
             {"id": "281", "name": "delete", "status": "archived"}]
LOCATIONS = [{"id": "aaaa-1", "merchant_id": "63", "name": "Boca Raton", "address": "1 A St", "kind": "store"},
             {"id": "aaaa-2", "merchant_id": "63", "name": "Delray", "address": "2 B St", "kind": "store"},
             {"id": "bbbb-1", "merchant_id": "183", "name": "KPOT Queens", "address": "3 C St", "kind": "store"}]


def test_merchants_and_locations_load_from_the_main_app(rstore):
    """New ones are added, known ones updated, missing ones marked removed (kept), returning ones offered again."""
    s = rstore.load_merchants(MERCHANTS, LOCATIONS, "ClickHouse", "noah")
    assert s == {"merchants": 4, "locations": 3, "new_merchants": 4, "new_locations": 3, "back_merchants": 0,
                 "back_locations": 0, "removed_merchants": 0, "removed_locations": 0}
    assert [b["name"] for b in rstore.list_businesses()][:2] == ["delete", "Flakowitz Corporate"]
    assert {b["id"]: b["locations"] for b in rstore.list_businesses()} == {"63": 2, "183": 1, "203": 0, "281": 0}
    loaded = rstore.merchants_loaded()
    assert (loaded["source"], loaded["by"], loaded["merchants"]) == ("ClickHouse", "noah", 4) and loaded["at"]
    # KPOT renamed, Delray closed (gone from the list), a new location for La Familia.
    merchants = [dict(m, name="KPOT Flushing") if m["id"] == "183" else m for m in MERCHANTS]
    locations = [x for x in LOCATIONS if x["id"] != "aaaa-2"] + [
        {"id": "cccc-1", "merchant_id": "203", "name": "Hempstead", "address": "", "kind": "store"}]
    s = rstore.load_merchants(merchants, locations, "the file x.csv")
    assert (s["new_locations"], s["removed_locations"], s["new_merchants"], s["removed_merchants"]) == (1, 1, 0, 0)
    assert rstore.business("183")["name"] == "KPOT Flushing"
    assert rstore.location("aaaa-2")["removed_at"] is not None
    assert [x["id"] for x in rstore.list_locations(["63"], current_only=True)] == ["aaaa-1"]
    assert [x["id"] for x in rstore.list_locations(["63"])] == ["aaaa-1", "aaaa-2"]   # kept, for its records
    # Delray reopens: back, not new.
    s = rstore.load_merchants(merchants, locations + [LOCATIONS[1]], "ClickHouse")
    assert (s["new_locations"], s["back_locations"]) == (0, 1)
    assert rstore.location("aaaa-2")["removed_at"] is None


def test_a_bad_merchant_list_changes_nothing(rstore):
    rstore.load_merchants(MERCHANTS, LOCATIONS, "ClickHouse")
    with pytest.raises(StoreError, match="no merchants"):
        rstore.load_merchants([], [], "the file empty.csv")
    with pytest.raises(StoreError, match="aren't in the list"):
        rstore.load_merchants(MERCHANTS[:1], LOCATIONS, "the file part.csv")
    assert all(b["removed_at"] is None for b in rstore.list_businesses())


def test_every_import_is_for_one_current_location_of_its_merchant(rstore):
    """One location: taken by itself. Several: one must be chosen. None, archived, removed or another merchant's:
    refused with what to do."""
    rstore.load_merchants(MERCHANTS, LOCATIONS, "ClickHouse")
    sid = rstore.add_service("S")
    assert rstore.get_import(rstore.create_import("183", sid, "a.csv", "x", None, "t"))["location_id"] == "bbbb-1"
    with pytest.raises(StoreError, match="Choose the location"):
        rstore.create_import("63", sid, "a.csv", "x", None, "t")
    imp = rstore.get_import(rstore.create_import("63", sid, "a.csv", "x", None, "t", location_id=" AAAA-2 "))
    assert (imp["location_id"], imp["location_name"], imp["location_address"]) == ("aaaa-2", "Delray", "2 B St")
    with pytest.raises(StoreError, match="has no location in the main app"):
        rstore.create_import("203", sid, "a.csv", "x", None, "t")
    with pytest.raises(StoreError, match="archived"):
        rstore.create_import("281", sid, "a.csv", "x", None, "t")
    rstore.load_merchants([dict(MERCHANTS[1], status="deleted")] + MERCHANTS[:1] + MERCHANTS[2:], LOCATIONS,
                          "ClickHouse")
    with pytest.raises(StoreError, match="is deleted"):     # only active merchants take imports
        rstore.create_import("183", sid, "a.csv", "x", None, "t")
    rstore.load_merchants(MERCHANTS, LOCATIONS, "ClickHouse")
    with pytest.raises(StoreError, match="one of this merchant's locations"):
        rstore.create_import("63", sid, "a.csv", "x", None, "t", location_id="bbbb-1")
    rstore.load_merchants(MERCHANTS, LOCATIONS[:1] + LOCATIONS[2:], "ClickHouse")   # Delray closed
    with pytest.raises(StoreError, match="one of this merchant's locations"):
        rstore.create_import("63", sid, "a.csv", "x", None, "t", location_id="aaaa-2")


def test_a_database_from_before_1_6_0_gets_locations(pg_server, records_schema, tmp_path):
    """The upgrade adds the merchant status, the locations and every import's and record's location (none for the
    ones before); the hand-typed businesses are marked removed by the first load, keeping their records."""
    url = fresh_database(pg_server)
    db = Database(url, max_size=2)
    try:
        db.setup(records_schema)
        db.execute("""ALTER TABLE records DROP COLUMN location_id; ALTER TABLE imports DROP COLUMN location_id;
                      DROP TABLE locations; ALTER TABLE businesses DROP COLUMN status, DROP COLUMN removed_at,
                      DROP COLUMN synced_at""")
        db.execute("INSERT INTO businesses (id, name) VALUES ('1001', 'Krave It Bayside')")
        sid = db.fetch_one("INSERT INTO services (name) VALUES ('S') RETURNING id")["id"]
        imp = db.fetch_one("INSERT INTO imports (business_id, service_id, file_name, file_sha1, status) "
                           "VALUES ('1001', %s, 'old.csv', 'x', 'completed') RETURNING id", (sid,))["id"]
        db.execute("INSERT INTO records (import_id, business_id, service_id, file_row) VALUES (%s, '1001', %s, 2)",
                   (imp, sid))
        assert db.setup(records_schema) == []
        rs = RecordStore(db)
        assert rs.get_import(imp)["location_id"] is None and rs.business("1001")["status"] == "active"
        rs.load_merchants(MERCHANTS, LOCATIONS, "ClickHouse")
        assert rs.business("1001")["removed_at"] is not None and rs.totals()["records"] == 1
        cond, params = F.where(F.find_node([{"businesses": ["1001"]}]), [])
        assert F.counts(db, cond, params)["records"] == 1   # its records are still found
    finally:
        db.close()
        drop_database(pg_server, url)


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


def test_an_import_needs_an_existing_merchant_and_service(rstore):
    rstore.add_business("1001", "A")
    rstore.add_location("1001-main", "1001", "Main")
    sid = rstore.add_service("S")
    with pytest.raises(StoreError, match="merchant"):
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
    with pytest.raises(StoreError, match="merchant"):
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
        rs.add_location("1001-main", "1001", "Main")
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


def test_reset_records_starts_afresh(pg_server, records_schema, tmp_path, monkeypatch, capsys):
    """scripts/reset_records.py (for the update to 1.6.0): lists, and with --yes deletes, every merchant, location,
    import, record and tag and the import jobs with their files; services stay."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("reset_records", ROOT / "scripts" / "reset_records.py")
    reset = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reset)
    url = fresh_database(pg_server)
    db = Database(url, max_size=2)
    try:
        db.setup(records_schema)
        rs = RecordStore(db)
        jobs = Store(tmp_path / "converter.db")
        rec, _ = import_file(rs, jobs, records_schema, "a.csv", "Phone\n5165550100\n")
        rs.create_tag("1001", "Promo")
        assert (tmp_path / "imports" / str(rec.id)).exists()
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "elsewhere"))   # not the app's data folder: refused
        assert reset.main(["--yes"]) == 1 and "No app data" in capsys.readouterr().out
        assert rs.totals()["records"] == 1
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert reset.main([]) == 0 and "Nothing deleted" in capsys.readouterr().out
        assert rs.totals()["records"] == 1
        assert reset.main(["--yes"]) == 0
        assert rs.totals() == {"records": 0, "businesses": 0, "services": 1, "tags": 0}
        assert rs.list_locations() == [] and jobs.list_imports() == []
        assert not (tmp_path / "imports").exists()
    finally:
        db.close()
        drop_database(pg_server, url)
