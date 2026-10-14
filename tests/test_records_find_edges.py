"""Customer Records: finds in the cases the independent test of 1.0.0 found wrong (all fixed before release).

Each test states the case. The records are imported through the real import path into a fresh database.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from converter.storage import Store
from records import find as F
from records.criteria import CriterionError, Params, date_token
from records.db import Database
from records.fields import find_fields
from records.store import RecordStore

from .conftest import drop_database, fresh_database
from .records_util import import_file

GUESTS = """First Name,Last Name,Phone,City,Visits,Last Visit,Birthday
Zoë,Åberg,516-555-0101,Åre,3,10/09/2026 9:00 AM,9/12
José,Muñoz,516-555-0102,Mór,4,10/09/2026 3:45 PM,1985-03-04
Sean,O'Brien,516-555-0103,Queens,5,10/08/2026,4/1
Pat,C++,516-555-0104,Albany,6,,12/25/1940
"""


def load(rs, jobs, schema):
    rs.add_business("1001", "Krave It Bayside")
    rs.add_business("5005", "1001 Nights Cafe")
    import_file(rs, jobs, schema, "guests.csv", GUESTS, business="1001")
    import_file(rs, jobs, schema, "nights.csv", "First Name,Phone\nAli,9175550199\n", business="5005")


@pytest.fixture
def data(rstore, records_schema, tmp_path):
    load(rstore, Store(tmp_path / "c.db"), records_schema)
    return rstore, find_fields(records_schema, rstore.columns_seen()), records_schema


def names(data, key, text, **kw):
    rs, fields, schema = data
    cond, params = F.where(F.find_node([{"omit": False, "criteria": [[key, text]]}]), fields,
                           collation=rs.db.collation, today=date(2026, 10, 14), **kw)
    return sorted(f"{r['first_name']}" for r in F.page(rs.db, schema, cond, params, limit=100))


def test_accented_letters_in_any_case(data):
    """'åberg', 'ÅBERG', 'MUÑOZ', 'zoë' find the accented names (an ICU database, as the app creates)."""
    assert data[0].db.collation is None  # the database's own rules fold Å/å
    for key, text, who in [("f:last_name", "åberg", ["Zoë"]), ("f:last_name", "ÅBERG", ["Zoë"]),
                           ("f:last_name", "Åb*", ["Zoë"]), ("f:last_name", '"åberg"', ["Zoë"]),
                           ("f:last_name", "MUÑOZ", ["José"]), ("name", "zoë åberg", ["Zoë"]),
                           ("any", "mór", ["José"]), ("f:city", "åre", ["Zoë"])]:
        assert names(data, key, text) == who, (key, text)


def test_accented_letters_in_a_c_locale_database(pg_server, records_schema, tmp_path):
    """A database made with the C locale (Homebrew's default for databases made by hand) gets an explicit
    collation, with a warning, and finds still match accented letters in any case."""
    url = fresh_database(pg_server, icu=False)
    db = Database(url, max_size=2)
    try:
        warnings = db.setup(records_schema)
        assert db.collation and any("collation" in w for w in warnings)
        rs = RecordStore(db)
        load(rs, Store(tmp_path / "c.db"), records_schema)
        d = (rs, find_fields(records_schema, rs.columns_seen()), records_schema)
        assert names(d, "f:last_name", "ÅBERG") == ["Zoë"] and names(d, "business", "TARA") == []
        assert names(d, "f:last_name", "åberg...b") == ["Zoë"]
    finally:
        db.close()
        drop_database(pg_server, url)


def test_a_business_id_wins_over_names_starting_with_it(data):
    """'1001' is business 1001, not also '1001 Nights Cafe'; the name is searched when no ID matches."""
    assert len(names(data, "business", "1001")) == 4
    assert names(data, "business", "nights") == ["Ali"]
    assert names(data, "business", "5005") == ["Ali"]
    assert len(names(data, "business", "1000...2000")) == 4  # ranges are about IDs
    assert names(data, "business", ">=5") == ["Ali"]


def test_times_typed_in_a_date_criterion_count(data):
    assert names(data, "f:transactions_last", ">10/9/2026 9:00 AM") == ["José"]
    assert names(data, "f:transactions_last", "<10/9/2026 3:00 PM") == ["Sean", "Zoë"]
    assert names(data, "f:transactions_last", "10/9/2026 9:00 AM...10/9/2026 10:00 AM") == ["Zoë"]
    assert names(data, "f:transactions_last", "10/9/2026 3:45 PM") == ["José"]
    assert names(data, "f:transactions_last", "10/9/2026") == ["José", "Zoë"]


def test_today_is_the_app_time_zones():
    """'//' is today where the app is (APP_TIMEZONE), not on the server's clock."""
    for tz in ("Pacific/Kiritimati", "America/New_York", "Pacific/Pago_Pago"):
        comp = F.Compiler({}, timezone=tz)
        assert comp.p.today == datetime.now(ZoneInfo(tz)).date()


def test_phone_with_country_code_and_wildcard(data):
    for typed in ("+1 516*", "1-516*", "1516*", "+1 (516) 555-01*", "516*"):
        assert len(names(data, "f:phone_number", typed)) == 4, typed
    assert names(data, "f:phone_number", "+1 917") == ["Ali"]


def test_birthdays_without_a_year_only_by_month_and_day(data):
    """'Year unknown' (1600) isn't 'born before 1950'; month/day and 1600 itself still find them."""
    assert names(data, "f:birthday", "<1/1/1950") == ["Pat"]
    assert names(data, "f:birthday", "1/1/1500...12/31/1950") == ["Pat"]
    assert names(data, "f:birthday", "9/12") == ["Zoë"]
    assert names(data, "f:birthday", "1600") == ["Sean", "Zoë"]
    assert names(data, "f:birthday", "*") == ["José", "Pat", "Sean", "Zoë"]


def test_apostrophes_and_punctuation(data):
    assert names(data, "f:last_name", "o’brien") == ["Sean"]   # a curly apostrophe, as phones type it
    assert names(data, "f:last_name", "==o’brien") == ["Sean"]
    assert names(data, "f:last_name", '"c++"') == ["Pat"]
    assert names(data, "f:last_name", "c++") == ["Pat"]         # punctuation ignored: words starting with c


def test_backward_and_half_ranges_say_so(data):
    for key, text, msg in [("f:transactions_last", "12/31/2026...1/1/2026", "backwards"),
                           ("f:total_visits", "5...1", "backwards"), ("f:last_name", "m...a", "backwards"),
                           ("f:total_visits", "5...", "both sides"), ("f:total_visits", "<", "number"),
                           ("f:transactions_last", "<", "date after"), ("f:total_visits", "NaN", "isn't a number"),
                           ("f:total_visits", "Infinity", "isn't a number")]:
        with pytest.raises(F.FindError, match=msg):
            names(data, key, text)
    assert names(data, "f:birthday", "9/30...9/1")  # month/day ranges wrap around the year (by design)


def test_record_numbers_with_a_hash(data):
    rs = data[0]
    first = rs.db.fetch_one("SELECT min(id) AS i FROM records")["i"]
    assert names(data, "record", f"#{first}") == ["Zoë"]


def test_date_token_with_time():
    tok = date_token("10/9/2026 3:45 PM", date(2026, 10, 14), with_time=True)
    assert (tok.start.hour, tok.start.minute, (tok.end - tok.start).seconds) == (15, 45, 60)
    assert date_token("10/9/2026 3:45 PM", date(2026, 10, 14)).start.hour == 0  # date fields: the day
    with pytest.raises(CriterionError):
        date_token("13/13/2026")
    assert Params().today == date.today()
