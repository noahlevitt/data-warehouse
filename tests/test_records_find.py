"""Customer Records: FileMaker-style finds (records.criteria, records.find) on a small set of real records.

The records come from two imports (two businesses, two services) run through the real import path; every test
then searches them as the Find page does.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from converter.storage import Store
from records import find as F
from records.criteria import CriterionError, date_token
from records.db import Database
from records.fields import find_fields
from records.store import RecordStore

from .conftest import drop_database, fresh_database
from .records_util import ensure_business, import_file

TOAST = ("First Name,Last Name,Phone,Email,City,State,Zip,Visits,Total Spend,Last Visit,Birthday,Created At,Points,"
         "Notes\n") + """John,Smith,516-555-0100,john@x.com,Brooklyn,NY,11201,5,100.50,10/09/2026 3:45 PM,9/12,2024-01-05,10,vip
Johnny,Smithson,516-555-0101,johnny@x.com,Queens,NY,11101,1,5,10/01/2026,12/25/1985,2024-02-01,,
Ann,Goldsmith,917-555-0102,ann@y.org,Brooklyn,NY,11215,12,"$1,200",09/15/2026,01/10,2023-12-31,50,"=HYPERLINK(""x"")"
Mary,O'Brien,,mary@z.com,Hoboken,NJ,07030,0,0,,,2024-03-01,,
Ann,Lee,631-555-0103 / 516-555-0100,,Mineola,NY,11501,,,10/10/2026,9/30/1990,,,
"""
SQUARE = """Name,Phone,Last Visit
Peter Pan,(212) 555-0104,08/01/2026
John Smith,516-555-0100,07/01/2026
"""
TODAY = date(2026, 10, 14)


@pytest.fixture(scope="module")
def data(pg_server, records_schema, tmp_path_factory):
    """(RecordStore, find fields) with the two imports above and a tag 'Promo' holding John Smith (1001)."""
    url = fresh_database(pg_server)
    db = Database(url, max_size=4)
    db.setup(records_schema)
    rs = RecordStore(db)
    jobs = Store(tmp_path_factory.mktemp("jobs") / "c.db")
    ensure_business(rs, "1001", "Krave It Bayside")
    ensure_business(rs, "2002", "Tara Mor")
    import_file(rs, jobs, records_schema, "toast_guests.csv", TOAST, business="1001", service="Toast export")
    import_file(rs, jobs, records_schema, "square.csv", SQUARE, business="2002", service="Square")
    tag = rs.create_tag("1001", "Promo")
    fields = find_fields(records_schema, rs.columns_seen())
    cond, params = F.where(F.find_node([{"criteria": [["f:first_name", "==john"], ["f:last_name", "==smith"],
                                                      ["business", "1001"]]}]), fields)
    rs.add_to_tag(tag, cond, params)
    yield rs, fields, records_schema
    db.close()
    drop_database(pg_server, url)


def req(*crit, omit=False, businesses=(), services=()):
    return {"omit": omit, "businesses": list(businesses), "services": list(services),
            "criteria": [list(c) for c in crit]}


def service_id(data, name):
    return next(s["id"] for s in data[0].list_services() if s["name"] == name)


def names(data, *requests, node=None, **kw):
    """Sorted 'First Last' of the records found."""
    rs, fields, schema = data
    cond, params = F.where(node or F.find_node(list(requests)), fields, today=TODAY, **kw)
    rows = F.page(rs.db, schema, cond, params, limit=100)
    return sorted(f"{r['first_name']} {r['last_name']}" for r in rows)


def one(data, key, text):
    return names(data, req((key, text)))


# ------------------------------------------------------------------------------------------------ text

def test_plain_text_finds_words_starting_with_it(data):
    """FileMaker: 'smith' finds Smith and Smithson, not Goldsmith; case doesn't matter."""
    assert one(data, "f:last_name", "smith") == ["John Smith", "John Smith", "Johnny Smithson"]
    assert one(data, "f:last_name", "SMITH") == one(data, "f:last_name", "smith")


def test_several_words_must_all_start_a_word(data):
    assert one(data, "name", "john smith") == ["John Smith", "John Smith", "Johnny Smithson"]
    assert one(data, "name", "smith john") == ["John Smith", "John Smith", "Johnny Smithson"]
    assert one(data, "name", "ann lee") == ["Ann Lee"]


def test_equals_is_a_whole_word_and_double_equals_the_whole_field(data):
    assert one(data, "f:first_name", "=john") == ["John Smith", "John Smith"]
    assert one(data, "f:city", "==brooklyn") == ["Ann Goldsmith", "John Smith"]
    assert one(data, "f:email", "==john@x.com") == ["John Smith"]
    assert one(data, "f:email", "==john") == []


def test_quotes_find_text_anywhere(data):
    assert one(data, "f:last_name", '"smith"') == ["Ann Goldsmith", "John Smith", "John Smith", "Johnny Smithson"]
    assert one(data, "f:email", '"@x.com"') == ["John Smith", "Johnny Smithson"]


def test_star_wildcard_and_empty_and_any_value(data):
    assert one(data, "f:last_name", "sm*n") == ["Johnny Smithson"]
    assert one(data, "f:email", "=") == ["Ann Lee", "John Smith", "Peter Pan"]
    assert len(one(data, "f:email", "*")) == 4


def test_emails_and_apostrophes_can_be_typed_as_they_are(data):
    assert one(data, "f:email", "ann@y.org") == ["Ann Goldsmith"]
    assert one(data, "f:last_name", "o'brien") == ["Mary O'Brien"]


def test_text_ranges_and_comparisons(data):
    assert one(data, "f:city", "h...m") == ["Ann Lee", "Mary O'Brien"]   # H through M: Hoboken and Mineola
    assert one(data, "f:state", ">=ny") == ["Ann Goldsmith", "Ann Lee", "John Smith", "Johnny Smithson"]


# ------------------------------------------------------------------------------------------------ phone

@pytest.mark.parametrize("typed", ["5165550100", "(516) 555-0100", "+1 516.555.0100", "1-516-555-0100",
                                   "=5165550100"])
def test_a_number_in_any_formatting(data, typed):
    """Any formatting finds the same number, in any of a record's numbers (Ann Lee has two)."""
    assert one(data, "f:phone_number", typed) == ["Ann Lee", "John Smith", "John Smith"]


def test_part_of_a_number_and_area_codes(data):
    assert one(data, "f:phone_number", "555-0103") == ["Ann Lee"]
    assert one(data, "f:phone_number", "917*") == ["Ann Goldsmith"]
    assert one(data, "f:phone_number", "=") == ["Mary O'Brien"]
    assert len(one(data, "f:phone_number", "*")) == 6


def test_phone_problems_are_searchable(data):
    assert one(data, "issue", "*") == ["Mary O'Brien"]
    assert one(data, "issue", "no phone") == ["Mary O'Brien"]


# ------------------------------------------------------------------------------------------------ numbers

def test_numbers(data):
    assert one(data, "f:total_visits", ">5") == ["Ann Goldsmith"]
    assert one(data, "f:total_visits", "1...5") == ["John Smith", "Johnny Smithson"]
    assert one(data, "f:total_spent", ">=$1,000") == ["Ann Goldsmith"]
    assert one(data, "f:total_spent", "0") == ["Mary O'Brien"]
    assert one(data, "f:total_visits", "=") == ["Ann Lee", "John Smith", "Peter Pan"]


# ------------------------------------------------------------------------------------------------ dates

def test_a_day_a_month_and_a_year(data):
    """A date-time field matches the whole day; a month or a year matches all of it."""
    assert one(data, "f:transactions_last", "10/9/2026") == ["John Smith"]
    assert one(data, "f:transactions_last", "10/2026") == ["Ann Lee", "John Smith", "Johnny Smithson"]
    assert len(one(data, "f:transactions_last", "2026")) == 6
    assert one(data, "f:transactions_last", "2026-10-09") == ["John Smith"]
    assert one(data, "f:transactions_last", "Oct 9 2026") == ["John Smith"]


def test_date_ranges_and_comparisons(data):
    assert one(data, "f:transactions_last", "9/1/2026...10/1/2026") == ["Ann Goldsmith", "Johnny Smithson"]
    assert one(data, "f:transactions_last", "<10/1/2026") == ["Ann Goldsmith", "John Smith", "Peter Pan"]
    assert one(data, "f:transactions_last", "<=10/1/2026") == ["Ann Goldsmith", "John Smith", "Johnny Smithson",
                                                                "Peter Pan"]
    assert one(data, "f:transactions_last", ">10/9/2026") == ["Ann Lee"]
    assert one(data, "f:transactions_last", ">=10/9/2026") == ["Ann Lee", "John Smith"]


def test_birthdays_by_month_and_day_in_any_year(data):
    """9/12 finds the birthday stored without a year (1600); a year finds only real years."""
    assert one(data, "f:birthday", "9/12") == ["John Smith"]
    assert one(data, "f:birthday", "9/*") == ["Ann Lee", "John Smith"]
    assert one(data, "f:birthday", "September") == ["Ann Lee", "John Smith"]
    assert one(data, "f:birthday", "Sep 30") == ["Ann Lee"]
    assert one(data, "f:birthday", "12/15...1/15") == ["Ann Goldsmith", "Johnny Smithson"]
    assert one(data, "f:birthday", "1985") == ["Johnny Smithson"]
    assert one(data, "f:birthday", ">1/1/1950") == ["Ann Lee", "Johnny Smithson"]


def test_today_and_record_created(data):
    """Every record has its ID and its creation time (when its import stored it), searchable like any date."""
    assert one(data, "f:transactions_last", "//") == []
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%m/%d/%Y")
    assert len(one(data, "created", today)) == 7
    assert len(one(data, "created", "...12/31/2099")) == 7 and one(data, "created", "...1/1/2020") == []
    assert len(one(data, "created", "1/1/2020...")) == 7


def test_half_ranges_up_to_and_from(data):
    """...X = up to and including X; X... = from X on (dates, month/day, numbers, text, business IDs)."""
    assert one(data, "f:transactions_last", "...10/1/2026") == ["Ann Goldsmith", "John Smith", "Johnny Smithson",
                                                                  "Peter Pan"]
    assert one(data, "f:transactions_last", "10/1/2026...") == ["Ann Lee", "John Smith", "Johnny Smithson"]
    assert one(data, "f:transactions_last", "...9/2026") == ["Ann Goldsmith", "John Smith", "Peter Pan"]
    assert one(data, "f:birthday", "...12/31/1989") == ["Johnny Smithson"]   # never a year-unknown 1600
    assert one(data, "f:birthday", "1990...") == ["Ann Lee"]
    assert one(data, "f:birthday", "...3/31") == ["Ann Goldsmith"]            # month/day: January 1 to March 31
    assert one(data, "f:birthday", "10/1...") == ["Johnny Smithson"]          # October 1 to December 31
    assert one(data, "f:total_visits", "...5") == ["John Smith", "Johnny Smithson", "Mary O'Brien"]
    assert one(data, "f:total_visits", "5...") == ["Ann Goldsmith", "John Smith"]
    assert one(data, "f:last_name", "...h") == ["Ann Goldsmith"]
    assert one(data, "business", "2000...") == ["John Smith", "Peter Pan"]


def test_date_token_reading():
    assert date_token("10/9/26", TODAY).start.isoformat() == "2026-10-09T00:00:00"
    assert date_token("10/9/85", TODAY).start.year == 1985
    assert date_token("2026-10", TODAY).end.isoformat() == "2026-11-01T00:00:00"
    assert date_token("12/2026", TODAY).end.isoformat() == "2027-01-01T00:00:00"
    assert (date_token("10/*").month, date_token("10/*").day) == (10, None)
    assert (date_token("Oct 9th").month, date_token("Oct 9th").day) == (10, 9)
    for bad in ("13/45", "2/30/2026", "someday"):
        with pytest.raises(CriterionError):
            date_token(bad, TODAY)


# ------------------------------------------------------------------------------------------------ other kinds

def test_business_by_id_or_name_and_service(data):
    assert one(data, "business", "2002") == ["John Smith", "Peter Pan"]
    assert one(data, "business", "tara") == ["John Smith", "Peter Pan"]
    assert len(one(data, "business", "krave")) == 5
    assert one(data, "service", "square") == ["John Smith", "Peter Pan"]


def test_phone_problem_comparisons_only_find_records_with_a_problem(data):
    assert one(data, "issue", "<z") == ["Mary O'Brien"]
    assert one(data, "issue", "...z") == ["Mary O'Brien"]
    assert len(one(data, "issue", "=")) == 6


def test_tags_files_imports_columns_any_field(data):
    assert one(data, "tag", "promo") == ["John Smith"]
    assert len(one(data, "tag", "=")) == 6
    assert one(data, "file", "square") == ["John Smith", "Peter Pan"]
    assert one(data, "col:Points", ">=20") == ["Ann Goldsmith"]  # text comparison on the cell as written
    assert one(data, "col:Notes", "vip") == ["John Smith"]
    assert one(data, "any", "hoboken") == ["Mary O'Brien"]
    assert one(data, "any", "vip") == ["John Smith"]


def test_import_number_is_the_one_on_the_imports_page(data):
    rs = data[0]
    square_job = rs.db.fetch_one("SELECT job_id FROM imports WHERE file_name = 'square.csv'")["job_id"]
    assert one(data, "import", str(square_job)) == ["John Smith", "Peter Pan"]
    assert one(data, "import", f"#{square_job}") == ["John Smith", "Peter Pan"]


def test_consent_true_false(data, tmp_path):
    rs, fields, schema = data
    assert one(data, "f:consent", "=") and one(data, "f:consent", "yes") == []
    with pytest.raises(F.FindError, match="true or false"):
        one(data, "f:consent", "maybe")


# ------------------------------------------------------------------------------------------------ requests

def test_requests_add_up_and_omit_removes_from_those_above(data):
    """FileMaker order: [Smith] [Omit Brooklyn] [first name Ann] = Smiths not in Brooklyn, plus every Ann."""
    found = names(data, req(("f:last_name", "smith")), req(("f:city", "brooklyn"), omit=True),
                  req(("f:first_name", "ann")))
    assert found == ["Ann Goldsmith", "Ann Lee", "John Smith", "Johnny Smithson"]
    # A later omit also removes what an earlier request found.
    found = names(data, req(("f:last_name", "smith")), req(("f:first_name", "ann")),
                  req(("f:city", "brooklyn"), omit=True))
    assert found == ["Ann Lee", "John Smith", "Johnny Smithson"]


def test_an_omit_first_starts_from_all_records(data):
    assert names(data, req(("f:state", "ny"), omit=True)) == ["John Smith", "Mary O'Brien", "Peter Pan"]


def test_omit_keeps_records_where_the_field_is_empty(data):
    """An empty field doesn't match 'brooklyn', so omitting Brooklyn keeps records with no city (NULL-safe)."""
    found = names(data, req(("f:city", "brooklyn"), omit=True))
    assert "Peter Pan" in found and "John Smith" in found  # Square's John Smith has no city


def test_constrain_extend_omit_selected_show_omitted(data):
    rs, fields, schema = data
    smith = F.find_node([req(("f:last_name", "smith"))])
    assert names(data, node=F.constrain(smith, [req(("f:city", "queens"))])) == ["Johnny Smithson"]
    assert names(data, node=F.extend(smith, [req(("f:first_name", "peter"))])) == [
        "John Smith", "John Smith", "Johnny Smithson", "Peter Pan"]
    cond, params = F.where(smith, fields)
    first = F.page(rs.db, schema, cond, params, limit=1)[0]["id"]
    assert len(names(data, node=F.omit_records(smith, [first]))) == 2
    assert names(data, node=F.show_omitted(smith)) == ["Ann Goldsmith", "Ann Lee", "Mary O'Brien", "Peter Pan"]


def test_business_and_services_picked_in_requests(data):
    """The common finds: a business; a business and one or more of its services; a business without a service."""
    toast, square = service_id(data, "Toast export"), service_id(data, "Square")
    krave = ["Ann Goldsmith", "Ann Lee", "John Smith", "Johnny Smithson", "Mary O'Brien"]
    assert names(data, req(businesses=["1001"])) == krave
    assert names(data, req(businesses=["1001"], services=[toast])) == krave
    assert names(data, req(businesses=["1001"], services=[square])) == []
    assert names(data, req(businesses=["1001", "2002"], services=[toast, square])) == sorted(krave + [
        "John Smith", "Peter Pan"])
    assert names(data, req(businesses=["1001", "2002"]), req(services=[square], omit=True)) == krave
    assert names(data, req(businesses=["2002"]), req(services=[square], omit=True)) == []
    assert names(data, req(services=[toast], omit=True)) == ["John Smith", "Peter Pan"]   # omit first: from all
    # Pickers and typed criteria together, and no business at all.
    assert names(data, req(("f:phone_number", "516*"), businesses=["1001"])) == ["Ann Lee", "John Smith",
                                                                                 "Johnny Smithson"]
    assert names(data, req(("f:phone_number", "516 555 0100"))) == ["Ann Lee", "John Smith", "John Smith"]


def test_nothing_is_required_and_a_blank_find_is_every_record(data):
    everyone = names(data, node=F.all_records())
    assert len(everyone) == 7
    assert names(data) == everyone                                  # no requests at all
    assert names(data, req()) == everyone                           # one blank request
    assert names(data, req(), req()) == everyone
    assert names(data, req(("f:last_name", "  "))) == everyone      # only spaces typed
    # A blank request among others is ignored, and so is a blank Omit request.
    assert names(data, req(("f:last_name", "smith")), req()) == ["John Smith", "John Smith", "Johnny Smithson"]
    assert names(data, req(("f:last_name", "smith")), req(omit=True)) == ["John Smith", "John Smith",
                                                                         "Johnny Smithson"]
    assert names(data, req(), req(("f:city", "brooklyn"), omit=True)) == [
        "Ann Lee", "John Smith", "Johnny Smithson", "Mary O'Brien", "Peter Pan"]
    # Constrain with nothing keeps the found set; extend with nothing finds everything (a blank find).
    smith = F.find_node([req(("f:last_name", "smith"))])
    assert names(data, node=F.constrain(smith, [req()])) == names(data, node=smith)
    assert names(data, node=F.extend(smith, [req()])) == everyone


def test_a_tag_as_found_set(data):
    rs = data[0]
    tag = rs.list_tags()[0]["id"]
    assert names(data, node=F.tag_records(tag)) == ["John Smith"]


def test_mistakes_give_messages(data):
    for key, text, msg in [("f:last_name", "!", "duplicates"), ("f:total_visits", "abc", "number"),
                           ("f:transactions_last", "...", "before or after"),
                           ("f:total_visits", "1...2...3", "once"),
                           ("f:transactions_last", "<10/9", "full date"), ("f:phone_number", "abc", "digits"),
                           ("f:birthday", "1/1...2026", "neither"), ("missing:Old field", "x", "searchable")]:
        with pytest.raises(F.FindError, match=msg):
            one(data, key, text)


# ------------------------------------------------------------------------------------------------ results

def test_counts_and_csv(data):
    rs, fields, schema = data
    cond, params = F.where(F.find_node([req(businesses=["1001"])]), fields)
    assert F.counts(rs.db, cond, params) == {"records": 5, "no_phone": 1, "businesses": 1, "numbers": 4}
    csv = F.found_set_csv(rs.db, schema, cond, params).decode("utf-8-sig")
    lines = csv.splitlines()
    assert len(lines) == 6 and lines[0].startswith("Record ID,Record created,Merchant ID,Merchant,Location ID,Location,Service,Import #")
    assert lines[1].split(",")[1].startswith(datetime.now(ZoneInfo("America/New_York")).strftime("%m/%d/%Y"))
    assert "'=HYPERLINK" not in csv  # Notes is not a field: not in the CSV
    assert "09/12/1600" in csv and ",10/09/2026," in csv and "03:45 PM" not in csv   # the last visit: a day


def test_csv_neutralises_formulas(data):
    """A value a spreadsheet would run (=, +, -, @ first) gets an apostrophe in the found-set CSV."""
    rs, fields, schema = data
    rs.db.execute("UPDATE records SET city = '=cmd()' WHERE first_name = 'Mary'")
    try:
        cond, params = F.where(F.find_node([req(("f:first_name", "mary"))]), fields)
        assert "'=cmd()" in F.found_set_csv(rs.db, schema, cond, params).decode("utf-8-sig")
    finally:
        rs.db.execute("UPDATE records SET city = 'Hoboken' WHERE first_name = 'Mary'")


def test_sorting_and_pages(data):
    rs, fields, schema = data
    cond, params = F.where(F.all_records(), fields)
    by_spend = F.page(rs.db, schema, cond, params, "Total spent (highest first)", limit=2)
    assert [r["first_name"] for r in by_spend] == ["Ann", "John"]
    p1 = F.page(rs.db, schema, cond, params, "Last name", limit=3)
    p2 = F.page(rs.db, schema, cond, params, "Last name", limit=3, offset=3)
    assert len({r["id"] for r in p1 + p2}) == 6
    assert "Last name" in F.available_sorts(schema)
