"""Customer Records' pages, driven through the real app.py with Streamlit's AppTest.

Pages other than the first can't be opened with AppTest.switch_page, so ``run_page`` gives AppTest a small script
that makes st.navigation run the wanted page (docs/HANDOFF.md, "Checks before a release"). The app caches its
database pool and runner for the process, so the module shares one data folder and one database.
"""

import os
import textwrap
import time

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from converter.storage import Store
from records import find as F
from records.db import Database, close_all
from records.store import RecordStore

from .conftest import ROOT, drop_database, fresh_database

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


def test_businesses_and_services_are_added_on_their_pages(env):
    _, rs = env
    at = run_page("Businesses")
    by_label(at.text_input, "ID (as in the main system)").input("1001")
    by_label(at.text_input, "Name").input("Krave It Bayside")
    at.button(key="FormSubmitter:add_business-Add business").click().run()
    assert rs.business("1001")["name"] == "Krave It Bayside"
    at = run_page("Services")
    by_label(at.text_input, "Name").input("Toast export")
    at.button(key="FormSubmitter:add_service-Add service").click().run()
    assert [s["name"] for s in rs.list_services()] == ["Toast export"]


def test_new_import_needs_a_business_and_service_and_stores_records(env):
    """Start import is blocked until both are chosen; the import then stores one record per row."""
    jobs, rs = env
    if rs.business("1001") is None:
        rs.add_business("1001", "Krave It Bayside")
    sid = next((s["id"] for s in rs.list_services() if s["name"] == "Toast export"), None) or rs.add_service(
        "Toast export")
    did = jobs.create_draft("local", "guests.csv", CSV)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.query_params["draft"] = did
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert button(at, "Start import").disabled
    at.selectbox(key="imp_business").select("1001").run()
    at.selectbox(key="imp_service").select(sid).run()
    assert not button(at, "Start import").disabled
    button(at, "Start import").click().run()
    job = jobs.list_imports(limit=1)[0]
    for _ in range(100):
        if not jobs.get_import(job.id).active:
            break
        time.sleep(0.1)
    assert jobs.get_import(job.id).status == "completed", jobs.get_import(job.id).error
    imp = rs.imports_for_jobs([job.id])[job.id]
    assert (imp["business_id"], imp["service_id"], imp["records"]) == ("1001", sid, 2)
    # Importing the same file for the same business again is flagged.
    did = jobs.create_draft("local", "guests.csv", CSV)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.query_params["draft"] = did
    at.session_state["imp_business"] = "1001"
    at.run()
    assert any("already imported for this business" in w.value for w in at.warning)


def test_import_page_shows_the_records_and_opens_them_in_find(env):
    jobs, rs = env
    job = jobs.list_imports(limit=1, statuses=["completed"])[0]
    at = run_page("Imports", query={"id": str(job.id)})
    assert any("2 records stored" in s.value for s in at.success)
    assert any("Krave It Bayside" in m.value for m in at.markdown)


def test_find_with_requests_and_tag_the_found_set(env):
    """A request typed into the editor finds records; the found set goes into a new tag."""
    _, rs = env
    req = {"id": 1, "omit": False, "base": pd.DataFrame({"Field": ["Last name"], "Criterion": ["smith"]})}
    at = run_page("Find", state={"find_reqs": [req], "find_ver": 0, "find_seq": 1})
    button(at, "Find").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert [m.value for m in at.metric][:2] == ["1", "1"]
    by_label(at.selectbox, "New tag for business").select("1001")
    by_label(at.text_input, "Tag name").input("Smiths")
    at.button(key="FormSubmitter:new_tag_from_found-Create tag with found set").click().run()
    [tag] = [t for t in rs.list_tags() if t["name"] == "Smiths"]
    assert tag["records"] == 1 and tag["numbers"] == 1


def test_find_shows_why_a_criterion_cant_be_read(env):
    req = {"id": 1, "omit": False, "base": pd.DataFrame({"Field": ["Total visits"], "Criterion": ["lots"]})}
    at = run_page("Find", state={"find_reqs": [req], "find_ver": 0, "find_seq": 1})
    button(at, "Find").click().run()
    assert any("isn't a number" in e.value for e in at.error)


def test_omit_selected_and_show_omitted(env):
    """The found-set steps work from the page (state set as the page would)."""
    at = run_page("Find", state={"found": F.all_records(), "found_note": "All records"})
    assert [m.value for m in at.metric][0] == "2"
    button(at, "Show omitted only").click().run()
    assert [m.value for m in at.metric][0] == "0"


def test_a_prepared_csv_is_for_one_found_set_only(env):
    """Changing Limit to (or the found set, or the sort) after preparing the CSV asks for a new one: the old file
    would hold records the page no longer shows."""
    at = run_page("Find", state={"found": F.all_records(), "found_note": "All records"})
    button(at, "Prepare CSV").click().run()
    assert at.session_state["found_csv"]["data"].decode("utf-8-sig").count("\n") == 3  # header + 2 records
    assert not [b for b in at.button if b.label.startswith("Prepare CSV")]  # the download is offered instead
    at.multiselect(key="find_biz").set_value(["1001"]).run()
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


def test_fields_page_lists_both_formats(env):
    at = run_page("Fields")
    assert at.header[0].value == "Fields"
    assert len(at.dataframe) == 2
