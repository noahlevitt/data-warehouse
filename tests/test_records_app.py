"""Customer Records' pages, driven through the real app.py with Streamlit's AppTest.

Pages other than the first can't be opened with AppTest.switch_page, so ``run_page`` gives AppTest a small script
that makes st.navigation run the wanted page (docs/HANDOFF.md, "Checks before a release"). The app caches its
database pool and runner for the process, so the module shares one data folder and one database.
"""

import json
import os
import textwrap
import time
from datetime import date

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from converter.schema import load_schema
from converter.storage import Store
from records import find as F
from records.db import Database, close_all
from records.store import RecordStore

from .conftest import ROOT, drop_database, fresh_database
from .records_util import import_file

CSV = b"First Name,Last Name,Phone,Last Visit\nJohn,Smith,5165550100,10/01/2026\nAnn,Lee,9175551234,10/02/2026\n"


@pytest.fixture(scope="module")
def env(tmp_path_factory, pg_server):
    """(app's job store, records store) for a data folder and database used by every test here."""
    data = tmp_path_factory.mktemp("appdata")
    url = fresh_database(pg_server)
    old = {k: os.environ.get(k) for k in ("DATA_DIR", "AUTH_MODE", "ANTHROPIC_API_KEY", "DATABASE_URL")}
    os.environ.update(DATA_DIR=str(data), ANTHROPIC_API_KEY="", DATABASE_URL=url)
    os.environ.pop("AUTH_MODE", None)
    close_all()
    st.cache_resource.clear()
    db = Database(url, max_size=2)
    db.setup(load_schema(ROOT / "schemas" / "records_schema.yaml"))
    yield Store(data / "converter.db"), RecordStore(db)
    close_all()
    st.cache_resource.clear()
    drop_database(pg_server, url)
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def run_page(title: str, query: dict | None = None, state: dict | None = None) -> AppTest:
    """Run app.py showing the page with this title, with optional URL parameters and session state."""
    script = textwrap.dedent(f"""
        import os, runpy, streamlit as st
        os.chdir({str(ROOT)!r})
        real = getattr(st, "_real_navigation", st.navigation)
        st._real_navigation = real
        def nav(pages, **kw):
            real(pages, **kw)
            flat = [p for group in pages.values() for p in group] if isinstance(pages, dict) else pages
            page = next(p for p in flat if p.title == {title!r})
            return type("Nav", (), {{"run": lambda self: page._page()}})()
        st.navigation = nav
        try:
            runpy.run_path("app.py", run_name="__main__")
        finally:
            st.navigation = real
    """)
    at = AppTest.from_string(script, default_timeout=60)
    for k, v in (query or {}).items():
        at.query_params[k] = v
    for k, v in (state or {}).items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def button(at, label):
    return next(b for b in at.button if b.label.startswith(label))


def by_label(elements, label):
    return next(e for e in elements if e.label == label)


def test_database_problems_are_explained(env, monkeypatch):
    """PostgreSQL not running: the page says what to do instead of a stack trace."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost:1/nothing")
    st.cache_resource.clear()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert at.error and "Can't connect to PostgreSQL" in at.error[0].value and "brew install" in at.error[0].value
    monkeypatch.undo()
    st.cache_resource.clear()


MERCHANTS = [{"id": "1001", "name": "Krave It Bayside", "status": "active"},
             {"id": "63", "name": "Flakowitz Corporate", "status": "active"},
             {"id": "203", "name": "La Familia Deli & Grill CORPORATE", "status": "active"}]
LOCATIONS = [{"id": "kb-1", "merchant_id": "1001", "name": "Bayside", "address": "39-12 Bell Blvd", "kind": "store"},
             {"id": "fl-1", "merchant_id": "63", "name": "Boca Raton", "address": "1 A St", "kind": "store"},
             {"id": "fl-2", "merchant_id": "63", "name": "Delray", "address": "2 B St", "kind": "store"},
             {"id": "fl-hq", "merchant_id": "63", "name": "Head office", "address": "", "kind": "office"}]


def test_merchants_come_from_the_main_app_and_services_are_added(env):
    """The Merchants page lists what was loaded (here straight into the store: AppTest can't upload a file) and
    shows how to load it; there is nothing to type in. Services are added on their page."""
    _, rs = env
    rs.load_merchants(MERCHANTS, LOCATIONS, "the file merchants.csv", "noah")
    at = run_page("Merchants")
    assert not at.exception, [e.value for e in at.exception]
    assert at.header[0].value == "Merchants" and not at.text_input
    assert any("from the file merchants.csv by noah" in c.value and "3 merchants and 4 locations" in c.value
               for c in at.caption)
    assert any("pg.merchants" in c.value for c in at.code)                # the query to export
    merchants, locations = [d.value for d in at.dataframe]
    assert dict(zip(merchants["Merchant"], merchants["Locations"])) == {
        "Flakowitz Corporate": 3, "Krave It Bayside": 1, "La Familia Deli & Grill CORPORATE": 0}
    assert len(locations) == 4
    assert any("203 · La Familia" in c.value and "no location" in c.value for c in at.caption)
    at = run_page("Services")
    by_label(at.text_input, "Name").input("Toast export")
    at.button(key="FormSubmitter:add_service-Add service").click().run()
    assert [s["name"] for s in rs.list_services()] == ["Toast export"]


def test_new_import_needs_a_merchant_location_and_service_and_stores_records(env):
    """Start import is blocked until all three are chosen (a merchant's only location is filled in); the import
    then stores one record per row, with its location."""
    jobs, rs = env
    if rs.business("1001") is None:
        rs.load_merchants(MERCHANTS, LOCATIONS, "test")
    sid = next((s["id"] for s in rs.list_services() if s["name"] == "Toast export"), None) or rs.add_service(
        "Toast export")
    did = jobs.create_draft("local", "guests.csv", CSV)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.query_params["draft"] = did
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert button(at, "Start import").disabled
    at.selectbox(key="imp_service").select(sid).run()
    at.selectbox(key="imp_business").select("63").run()               # several locations: choose one
    assert at.selectbox(key="imp_location").value is None and button(at, "Start import").disabled
    assert at.selectbox(key="imp_location").options == ["Boca Raton — 1 A St", "Delray — 2 B St",
                                                         "Head office (office)"]
    at.selectbox(key="imp_business").select("203").run()              # no location: can't be imported for
    assert at.selectbox(key="imp_location").disabled and button(at, "Start import").disabled
    assert any("has no location in the main app" in w.value for w in at.warning)
    at.selectbox(key="imp_business").select("1001").run()             # one location: filled in
    assert at.selectbox(key="imp_location").value == "kb-1"
    assert not button(at, "Start import").disabled
    button(at, "Start import").click().run()
    job = jobs.list_imports(limit=1)[0]
    for _ in range(600):  # each import runs in a worker process (records.runner), which takes a moment to start
        if not jobs.get_import(job.id).active:
            break
        time.sleep(0.1)
    assert jobs.get_import(job.id).status == "completed", jobs.get_import(job.id).error
    imp = rs.imports_for_jobs([job.id])[job.id]
    assert (imp["business_id"], imp["location_id"], imp["service_id"], imp["records"]) == ("1001", "kb-1", sid, 2)
    assert {r["location_id"] for r in rs.db.fetch("SELECT location_id FROM records")} == {"kb-1"}
    # Importing the same file for the same merchant again is flagged.
    did = jobs.create_draft("local", "guests.csv", CSV)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.query_params["draft"] = did
    at.session_state["imp_business"] = "1001"
    at.run()
    assert any("already imported for this merchant" in w.value for w in at.warning)


def test_import_page_shows_the_records_and_opens_them_in_find(env):
    jobs, rs = env
    job = jobs.list_imports(limit=1, statuses=["completed"])[0]
    at = run_page("Imports", query={"id": str(job.id)})
    assert any("2 records stored" in s.value for s in at.success)
    assert any("Krave It Bayside** · Bayside" in m.value for m in at.markdown)


def test_find_with_requests_and_tag_the_found_set(env):
    """A criterion typed into a request's box finds records; the found set goes into a new tag."""
    _, rs = env
    at = run_page("Find")
    by_label(at.text_input, "Last name").input("smith")
    button(at, "Find").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert [m.value for m in at.metric][:2] == ["1", "1"]
    by_label(at.selectbox, "New tag for merchant").select("1001")
    by_label(at.text_input, "Tag name").input("Smiths")
    at.button(key="FormSubmitter:new_tag_from_found-Create tag with found set").click().run()
    [tag] = [t for t in rs.list_tags() if t["name"] == "Smiths"]
    assert tag["records"] == 1 and tag["numbers"] == 1


def test_find_shows_why_a_criterion_cant_be_read(env):
    at = run_page("Find")
    at.button(key="fq_1_1_add_f:total_visits").click().run()
    by_label(at.text_input, "Total visits").input("lots")
    button(at, "Find").click().run()
    assert any("isn't a number" in e.value for e in at.error)


def test_nothing_is_required_a_blank_find_shows_every_record(env):
    at = run_page("Find")
    assert not at.error
    button(at, "Find").click().run()
    assert not at.error and [m.value for m in at.metric][0] == "2"
    assert any("Blank find" in c.value for c in at.caption)
    [found] = [d for d in at.dataframe if F.ID_COL in d.value.columns]
    assert len(found.value) == 2 and str(found.value["Created"].dtype).startswith("datetime64")
    config = json.loads(found.proto.columns)       # shown as "Thu, 10/15/2026 10:48 AM", sorted as a date-time
    assert found.proto.placeholder == ""            # an empty value shows nothing (not "None")
    assert config["Created"]["type_config"]["format"] == "ddd, M/D/YYYY h:mm A"


def test_a_business_without_one_of_its_services(env):
    """Pick the business in request 1, then an Omit request with the service: nothing of that service is left."""
    _, rs = env
    sid = next(s["id"] for s in rs.list_services() if s["name"] == "Toast export")
    at = run_page("Find")
    at.multiselect(key="fq_1_1_biz").set_value(["1001"])
    button(at, "Find").click().run()
    assert [m.value for m in at.metric][0] == "2"
    button(at, "+ Omit request").click().run()
    assert len([m for m in at.markdown if m.value.startswith("**Request")]) == 2
    assert at.multiselect(key="fq_1_1_biz").value == ["1001"]           # request 1 kept what was picked
    at.multiselect(key="fq_1_2_svc").set_value([sid])
    button(at, "Find").click().run()
    assert [m.value for m in at.metric][0] == "0"
    assert any("omit Toast export" in c.value for c in at.caption)


def test_find_or_omit_is_always_one_of_the_two(env):
    """Each request is Find or Omit, never neither (a toggle clicked twice used to turn an Omit into a Find)."""
    at = run_page("Find")
    kind = at.radio(key="fq_1_1_kind")
    assert kind.options == ["Find", "Omit"] and kind.value == "Find"


def test_a_typo_in_constrain_keeps_the_found_set(env):
    """The found set is only replaced once the new step can be read; a blank Constrain changes nothing."""
    smith = F.find_node([{"omit": False, "criteria": [["f:last_name", "smith"]]}])
    at = run_page("Find", state={"found": smith, "found_note": "Find"})
    button(at, "Constrain").click().run()
    assert any("Fill in something to constrain" in w.value for w in at.warning)
    assert at.session_state["found"] == smith
    by_label(at.text_input, "Date").input("someday")
    button(at, "Constrain").click().run()
    assert any("isn't a date" in e.value and "unchanged" in e.value for e in at.error)
    assert at.session_state["found"] == smith and [m.value for m in at.metric][0] == "1"
    by_label(at.text_input, "Date").input("")
    by_label(at.text_input, "First name").input("john")
    button(at, "Constrain").click().run()
    assert not at.error and [m.value for m in at.metric][0] == "1"


def test_the_most_used_fields_are_shown_and_the_others_added(env):
    """Business, Services and Date, then Phone, First and Last name; others from Add a field (and ✕ off)."""
    at = run_page("Find")
    assert [t.label for t in at.text_input][:4] == ["Date", "Phone", "First name", "Last name"]
    assert not [t for t in at.text_input if t.label in ("City", "Record created", "Full name")]
    by_label(at.text_input, "Last name").input("smith")
    at.button(key="fq_1_1_add_name").click().run()                    # Full name, from the menu
    assert by_label(at.text_input, "Last name").value == "smith"        # what was typed is kept
    by_label(at.text_input, "Full name").input("john smith")
    at.button(key="fq_1_1_add_created").click().run()                 # Record created, from the menu
    assert [t.label for t in at.text_input][4:6] == ["Full name", "Record created"]
    button(at, "Find").click().run()
    assert [m.value for m in at.metric][0] == "1"
    at.button(key="fq_1_1_drop_name").click().run()                   # ✕ takes it off again
    assert not [t for t in at.text_input if t.label == "Full name"]
    at.selectbox(key="fq_1_1_colpick").select("col:Last Visit")
    at.button(key="fq_1_1_addcol").click().run()                      # a column of the files
    assert [t for t in at.text_input if t.label == "Column: Last Visit"]


def test_a_sole_star_or_equals_in_a_box(env):
    """A sole * finds the records where the field has a value, a sole = those where it is blank (both records here
    have a phone and a last visit, and no city)."""
    for label, text, count in [("Phone", "*", "2"), ("Phone", "=", "0"), ("Date", "=", "0"),
                               ("Date", " * ", "2"), ("City", "=", "2"), ("City", "*", "0")]:
        at = run_page("Find")
        if label == "City":
            at.button(key="fq_1_1_add_f:city").click().run()
        by_label(at.text_input, label).input(text)
        button(at, "Find").click().run()
        assert not at.error, (label, text, [e.value for e in at.error])
        assert [m.value for m in at.metric][0] == count, (label, text)


def test_a_day_of_the_week_in_the_date_box(env):
    """Date "fri" finds the Fridays (John: Thu 10/1, Ann: Fri 10/2); the found set shows the Date with its day."""
    at = run_page("Find")
    assert "Fri" in by_label(at.text_input, "Date").placeholder
    assert "the later of the two" in by_label(at.text_input, "Date").help     # the field's own help is kept
    by_label(at.text_input, "Date").input("fri")
    button(at, "Find").click().run()
    assert [m.value for m in at.metric][0] == "1"
    table = at.dataframe[0].value
    assert list(table["Date"]) == [date(2026, 10, 2)]                                   # a date, sorted as one
    assert json.loads(at.dataframe[0].proto.columns)["Date"]["type_config"]["format"] == "ddd, M/D/YYYY"
    assert at.selectbox(key="found_sort").options[3] == "Date (newest first)"


def test_gender_from_the_add_a_field_menu(env):
    """John and Ann (no Gender column) were guessed Mb and Fb on import; Gender f finds Ann."""
    at = run_page("Find")
    at.button(key="fq_1_1_add_f:gender").click().run()
    box = by_label(at.text_input, "Gender")
    assert box.placeholder == "F, M, Fb or Mb" and "likely" in box.help
    box.input("f")
    button(at, "Find").click().run()
    assert [m.value for m in at.metric][0] == "1"
    assert list(at.dataframe[0].value["Gender"]) == ["Fb"]


def test_omit_selected_and_show_omitted(env):
    """The found-set steps work from the page (state set as the page would)."""
    at = run_page("Find", state={"found": F.all_records(), "found_note": "All records"})
    assert [m.value for m in at.metric][0] == "2"
    button(at, "Show omitted only").click().run()
    assert [m.value for m in at.metric][0] == "0"


def test_a_prepared_csv_is_for_one_found_set_only(env):
    """Changing the found set or the sort after preparing the CSV asks for a new one: the old file would hold
    records, or an order, the page no longer shows."""
    at = run_page("Find", state={"found": F.all_records(), "found_note": "All records"})
    button(at, "Prepare CSV").click().run()
    assert at.session_state["found_csv"]["data"].decode("utf-8-sig").count("\n") == 3  # header + 2 records
    assert not [b for b in at.button if b.label.startswith("Prepare CSV")]  # the download is offered instead
    at.selectbox(key="found_sort").select("Last name").run()
    assert [b for b in at.button if b.label.startswith("Prepare CSV")]


def test_tag_page_exports_the_numbers(env):
    _, rs = env
    tag = next(t for t in rs.list_tags() if t["name"] == "Smiths")
    at = run_page("Tags", query={"tag": str(tag["id"])})
    assert at.header[0].value == "Tag: Smiths"
    button(at, "Prepare export").click().run()
    assert not at.exception, [e.value for e in at.exception]
    prepared = at.session_state[f"tag_export_{tag['id']}"]
    lines = prepared["data"].decode().splitlines()
    assert lines[0].startswith("phone_number,consent,") and lines[1].startswith("5165550100,,")
    assert prepared["name"].startswith("1001_Smiths_") and prepared["name"].endswith(".csv")
    # The tag's records table: Record ID (which Remove selected reads) and when each record was created.
    [records] = [d.value for d in at.dataframe if F.ID_COL in d.value.columns]
    assert "Created" in records.columns and len(records) == 1


def test_fields_page_lists_both_formats(env):
    at = run_page("Fields")
    assert at.header[0].value == "Fields"
    assert len(at.dataframe) == 2


def test_logins_on_the_server(env, monkeypatch):
    """AUTH_MODE=password (the Docker setup): nothing shows before signing in, each person signs in with their own
    account, what they add is recorded under their name, and only admins get the Users page."""
    jobs, rs = env
    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    names = {u["username"] for u in jobs.list_users()}
    for name, admin in (("noah", True), ("staff1", False)):
        if name not in names:
            jobs.add_user(name, "long-enough-pw", is_admin=admin)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert [t.label for t in at.text_input] == ["Username", "Password"]
    assert not at.sidebar.caption and not at.header  # no page, no record count before signing in
    at.text_input[0].input("staff1")
    at.text_input[1].input("wrong-password")
    at.button(key="FormSubmitter:login-Sign in").click().run()
    assert at.error[0].value == "Wrong username or password." and not at.sidebar.caption
    at.text_input[1].input("long-enough-pw")
    at.button(key="FormSubmitter:login-Sign in").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(c.value == "Signed in as **staff1**" for c in at.sidebar.caption)
    staff = {"username": "staff1", "is_admin": False}
    at = run_page("Services", state={"user": staff})
    by_label(at.text_input, "Name").input("Login Test Service")
    at.button(key="FormSubmitter:add_service-Add service").click().run()
    assert next(x for x in rs.list_services() if x["name"] == "Login Test Service")["created_by"] == "staff1"
    [table] = [d.value for d in at.dataframe if "Added by" in d.value.columns]
    assert table.set_index("Service").loc["Login Test Service", "Added by"] == "staff1"
    with pytest.raises(AssertionError):  # not in the navigation (run_page finds no such page): staff can't open it
        run_page("Users", state={"user": staff})
    at = run_page("Users", state={"user": {"username": "noah", "is_admin": True}})
    assert at.header[0].value == "Users"


def test_a_deleted_account_is_signed_out_and_admin_rights_apply_at_once(env, monkeypatch):
    """With logins on, a tab already signed in follows what an admin changed on the Users page."""
    jobs, _ = env
    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    jobs.add_user("temp1", "long-enough-pw", is_admin=True)
    if "noah" not in {u["username"] for u in jobs.list_users()}:
        jobs.add_user("noah", "long-enough-pw", is_admin=True)
    jobs.set_admin("temp1", False)
    with pytest.raises(AssertionError):  # signed in as an admin earlier, but no longer one: no Users page
        run_page("Users", state={"user": {"username": "temp1", "is_admin": True}})
    jobs.delete_user("temp1")
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.session_state["user"] = {"username": "temp1", "is_admin": False}
    at.run()
    assert [t.label for t in at.text_input] == ["Username", "Password"] and not at.sidebar.caption


def test_find_by_location_of_a_corporate_account(env):
    """A merchant with several locations gets a Locations picker in each request; picking one finds its records
    only, and the found set shows each record's location."""
    jobs, rs = env
    schema = load_schema(ROOT / "schemas" / "records_schema.yaml")
    import_file(rs, jobs, schema, "delray.csv", "First Name,Phone\nDel,5165550111\nRay,5165550112\n",
                business="63", location="fl-2")
    import_file(rs, jobs, schema, "boca.csv", "First Name,Phone\nBo,5165550113\n", business="63", location="fl-1")
    at = run_page("Find")
    loc = at.multiselect(key="fq_1_1_loc")
    assert loc.options == ["Flakowitz Corporate · Boca Raton — 1 A St", "Flakowitz Corporate · Delray — 2 B St",
                           "Flakowitz Corporate · Head office (office)"]   # not Krave It's: it has one location
    loc.set_value(["fl-2"])
    button(at, "Find").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert [m.value for m in at.metric][0] == "2"
    assert any("Flakowitz Corporate · Delray" in c.value for c in at.caption)
    [found] = [d.value for d in at.dataframe if F.ID_COL in d.value.columns]
    assert set(found["Location"]) == {"Delray"} and set(found["Merchant"]) == {"63 · Flakowitz Corporate"}
    at.multiselect(key="fq_1_1_biz").set_value(["63"])
    at.multiselect(key="fq_1_1_loc").set_value([])
    button(at, "Find").click().run()
    assert [m.value for m in at.metric][0] == "3"                       # the merchant: all its locations


def test_a_location_that_leaves_the_main_app_is_never_swapped_for_another(env):
    """Found by the independent test of 1.6.0: Delray chosen, then a load without Delray (and its office) left one
    location, which was filled in, so the file went to Boca Raton without a word. Now the page says the location is
    gone and waits for one to be chosen."""
    jobs, rs = env
    sid = next(s["id"] for s in rs.list_services() if s["name"] == "Toast export")
    did = jobs.create_draft("local", "guests.csv", CSV)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.query_params["draft"] = did
    at.run()
    at.selectbox(key="imp_service").select(sid).run()
    at.selectbox(key="imp_business").select("63").run()
    at.selectbox(key="imp_location").select("fl-2").run()
    assert not button(at, "Start import").disabled
    try:
        rs.load_merchants(MERCHANTS, [x for x in LOCATIONS if x["id"] not in ("fl-2", "fl-hq")], "test")
        at.run()
        assert at.selectbox(key="imp_location").value is None and button(at, "Start import").disabled
        assert any("Delray — 2 B St" in w.value and "no longer in the main app" in w.value for w in at.warning)
        at.run()                                                        # still not filled in on the next click
        assert at.selectbox(key="imp_location").value is None
        at.selectbox(key="imp_location").select("fl-1").run()           # chosen again: fine
        assert not button(at, "Start import").disabled
        assert not any("no longer in the main app" in w.value for w in at.warning)
    finally:
        rs.load_merchants(MERCHANTS, LOCATIONS, "test")
