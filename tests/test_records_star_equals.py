"""Customer Records: a sole `*` in a box finds the records where that field has a value, a sole `=` the records
where it is blank, for every field a find can search (records.criteria). Anything else typed with them keeps its
meaning (`sm*th` is a wildcard, `=smith` the whole word)."""

from datetime import date

import pytest

from converter.mapping import FieldMapping
from converter.storage import Store
from records import find as F
from records.fields import find_fields

from .records_util import import_file

HEADERS = ["First", "Last", "Phone", "Email", "Addr", "Addr2", "City", "State", "Zip", "Visits", "Spent", "Joined",
           "LastVisit", "LastCall", "LastText", "LastRedeem", "LastSpin", "LastContact", "Bday", "Anniv", "OptIn",
           "Extra", "Notes"]
FIELD_OF = {"First": "first_name", "Last": "last_name", "Email": "email", "Addr": "address1", "Addr2": "address2",
            "City": "city", "State": "state", "Zip": "zip", "Visits": "total_visits", "Spent": "total_spent",
            "Joined": "first_visit", "LastVisit": "transactions_last", "LastCall": "calls_last",
            "LastText": "in_sms_last", "LastRedeem": "redemptions_last", "LastSpin": "spin_last",
            "LastContact": "last_contacted", "Bday": "birthday", "Anniv": "anniversary", "OptIn": "consent"}
FULL = ["Ann", "Lee", "5165550101", "ann@example.com", "1 Main St", "Apt 2", "Bayside", "NY", "11361", "3",
        "45.50", "1/5/2024", "10/1/2026 7:00 PM", "10/2/2026 1:00 PM", "10/3/2026 2:00 PM", "10/4/2026 3:00 PM",
        "10/5/2026 4:00 PM", "10/6/2026 5:00 PM", "5/6/1990", "6/7/2015", "Y", "e1", ""]
# Every field blank; only the Notes column (which feeds no field) has something, so the row is kept.
BLANK = [""] * (len(HEADERS) - 1) + ["n2"]


@pytest.fixture
def two(rstore, records_schema, tmp_path):
    """Record "full" (every field filled) and record "blank" (every field empty), each the only row of its file."""
    def by_hand(m):
        for h, name in FIELD_OF.items():
            # consent: a person also says what its column records (file-converter 0.23.0)
            m.fields[name] = FieldMapping(sources=[h], transform="copy", origin="user",
                                          params={"modes": {h: "both"}} if name == "consent" else {})
        return m
    jobs = Store(tmp_path / "c.db")
    csv = ",".join(HEADERS) + "\n" + ",".join(FULL) + "\n" + ",".join(BLANK) + "\n"
    import_file(rstore, jobs, records_schema, "every.csv", csv, edit=by_hand)
    with rstore.db.conn() as c:
        rows = {r["notes"]: r["id"] for r in c.execute("SELECT id, source ->> 'Notes' AS notes FROM records")}
        full, blank = rows[None], rows["n2"]
        record = c.execute("SELECT * FROM records WHERE id = %s", (full,)).fetchone()
    assert all(record[name] is not None for name in FIELD_OF.values()), "every field of 'full' must be filled"
    tag = rstore.create_tag("1001", "promo")
    cond, params = F.where(F.find_node([{"criteria": [["f:first_name", "ann"]]}]),
                           find_fields(records_schema, rstore.columns_seen()))
    rstore.add_to_tag(tag, cond, params)
    return rstore, records_schema, {"full": full, "blank": blank}


def found(rs, schema, key, text):
    fields = find_fields(schema, rs.columns_seen())
    cond, params = F.where(F.find_node([{"criteria": [[key, text]]}]), fields, collation=rs.db.collation,
                           today=date(2026, 10, 14))
    return {r["id"] for r in F.page(rs.db, schema, cond, params, limit=100)}


def test_a_sole_star_finds_filled_and_a_sole_equals_finds_blank_for_every_field(two):
    rs, schema, ids = two
    full, blank = {ids["full"]}, {ids["blank"]}
    both = full | blank
    # Which of the two records has a value in each field (default: "full" only).
    has = {"col:Notes": blank, "col:Extra": full,
           "business": both, "service": both, "file": both, "import": both, "created": both, "record": both}
    keys = [f.key for f in find_fields(schema, rs.columns_seen())]
    assert {"f:phone_number", "f:first_name", "name", "f:consent", "tag", "col:Notes"} <= set(keys)
    wrong = []
    for key in keys:
        if key.startswith("col:") and key not in has:
            continue
        expect = has.get(key, full)
        for text, want in (("*", expect), ("=", both - expect), (" * ", expect), (" = ", both - expect)):
            got = found(rs, schema, key, text)
            if got != want:
                wrong.append(f"{key} {text!r}: {sorted(got)} instead of {sorted(want)}")
    assert not wrong, "\n".join(wrong)


def test_star_and_equals_with_other_text_keep_their_meaning(two):
    rs, schema, ids = two
    full = {ids["full"]}
    for key, text in [("f:city", "bay*"), ("f:city", "=bayside"), ("f:city", "==bayside"), ("f:last_name", "l*e"),
                      ("f:phone_number", "516*"), ("f:total_visits", "=3"), ("f:birthday", "5/*")]:
        assert found(rs, schema, key, text) == full, (key, text)
    for key, text in [("f:city", "=bay"), ("f:city", "*x*")]:
        assert found(rs, schema, key, text) == set(), (key, text)


def test_a_value_the_import_couldnt_read_counts_as_blank(rstore, records_schema, tmp_path):
    """Visits "abc" isn't a number: the record's Total visits is blank (`=` finds it, `*` doesn't); the text is
    still in the row as written."""
    import_file(rstore, Store(tmp_path / "c.db"), records_schema, "odd.csv",
                "Phone,Visits\n5165550199,abc\n5165550198,4\n")
    assert len(found(rstore, records_schema, "f:total_visits", "=")) == 1
    assert len(found(rstore, records_schema, "f:total_visits", "*")) == 1
    assert len(found(rstore, records_schema, "col:Visits", "*")) == 2


def test_an_omit_keeps_the_records_where_the_field_is_blank_for_every_field(two):
    """Text, number and date criteria (the Date too) are made NULL-safe with "value IS NOT NULL" instead of COALESCE
    (1.3.0, so indexes can be used): an Omit of what the full record matches must still keep the blank one."""
    rs, schema, ids = two
    matching = {"text": ">=0", "number": ">=0", "date": "1/1/1000...12/31/9998"}
    checked = []
    for f in find_fields(schema, rs.columns_seen()):
        if f.kind not in matching or not (f.column or f.expr) or f.key.startswith("col:"):
            continue
        fields = find_fields(schema, rs.columns_seen())
        cond, params = F.where(F.find_node([{"omit": True, "criteria": [[f.key, matching[f.kind]]]}]), fields,
                               collation=rs.db.collation)
        kept = {r["id"] for r in F.page(rs.db, schema, cond, params, limit=100)}
        filled = found(rs, schema, f.key, matching[f.kind])
        assert kept == {ids["full"], ids["blank"]} - filled, f.key
        if f.key != "record":                               # every record has an ID
            assert filled == {ids["full"]} and kept == {ids["blank"]}, f.key
        checked.append(f.key)
    assert {"f:city", "f:total_visits", "f:birthday", "f:transactions_last", "date", "record"} <= set(checked)
