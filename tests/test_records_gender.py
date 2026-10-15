"""Customer Records: gender (1.4.0). F / M from a file's Gender or Sex column; Fb / Mb guessed from the first name
(or the email) for everyone else, on import and for records stored before. Real imports into a fresh database."""

from datetime import date

import pytest

from converter.storage import Store
from records import find as F
from records import gender
from records.fields import find_fields
from records.tags import export_table

from .records_util import import_file

GUESTS = """First Name,Last Name,Phone,Email,Gender
Mary,Smith,5165550101,,
John,Doe,5165550102,,
Jordan,Lee,5165550103,,
Taylor,Kim,5165550104,,Female
Lisa Marie Ray,,5165550105,,
"Ray, Lisa",,5165550106,,
Mr. Pat,Jones,5165550107,,
Dr. John,Watson,5165550108,,
,,5165550109,maria.lopez22@x.com,
Xyzzy,,5165550110,john.smith@x.com,
José,Muñoz,5165550111,,
Siobhan,,5165550112,,Other
Mary-Kate,Olsen,5165550113,,
Wei,Chen,5165550114,,
Sam,,5165550115,,W
Kim,,5165550116,,boy
Ann,,5165550117,,Unknown
Ann,Bee,5165550118,,Non-binary
Jordan,Fay,5165550119,mary@x.com,
"""
EXPECTED = {"5165550101": "Fb", "5165550102": "Mb", "5165550103": None, "5165550104": "F", "5165550105": "Fb",
            "5165550106": "Fb", "5165550107": "Mb", "5165550108": "Mb", "5165550109": "Fb", "5165550110": "Mb",
            "5165550111": "Mb", "5165550112": None, "5165550113": "Fb", "5165550114": None, "5165550115": "F",
            "5165550116": "M", "5165550117": "Fb", "5165550118": None, "5165550119": None}


@pytest.fixture
def data(rstore, records_schema, tmp_path):
    import_file(rstore, Store(tmp_path / "c.db"), records_schema, "guests.csv", GUESTS)
    return rstore, records_schema


def by_phone(rs) -> dict:
    return {r["phone"]: r for r in rs.db.fetch(
        "SELECT p.phone, r.gender, r.issues FROM records r JOIN record_phones p ON p.record_id = r.id")}


def test_what_a_file_says_is_f_or_m():
    for text in ["F", "f", "Female", "FEMALE", "woman", "Women", "girl", "Lady", "W", "f.", " Female ", "F - Female",
                 "Female (F)", "2 - Female", "She / Her", "weibl.", "♀", "Féminin", "Feminino", "Woman (she/her)"]:
        assert gender.stated(text) == "F", text
    for text in ["M", "male", "MALE", "Man", "boy", "Guy", "gentleman", "m.", "M (Male)", "1 - Male", "männl.", "♂",
                 "Masculino"]:
        assert gender.stated(text) == "M", text
    for text in ["M (Male)", "Masc.", "Masc", "Maschio", "Varón"]:
        assert gender.stated(text) == "M", text
    for text in ["Femmina", "Fem."]:
        assert gender.stated(text) == "F", text
    for text in ["", None, "Other", "Non-binary", "Unknown", "U", "X", "prefer not to say", "fm", "Mx",
                 "Male/Female", "they/them", "Not male", "Non-female", "M/F"]:
        assert gender.stated(text) is None, text
    for text in ["", None, "Unknown", "U", "n/a", "N/A", "N / A", "N.A.", "n.a.", "none", "?", "??", "-", "—", "1",
                 "2", "1.0", "2.0", "-1", "Not known", "Not stated", "Not Applicable", "(blank)", "Dr",
                 "U - Unknown", "Unknown (U)", "UNK - Unknown", "Unknown/Not Specified", "N/A - Not Applicable",
                 "M/F", "Male/Female", "Male or Female", "Mr. & Mrs."]:
        assert gender.unknown(text), text
    for text in ["Other", "Non-binary", "Prefer not to say", "Mx", "they/them", "Not male", "Male to Female",
                 "Female to Male", "Transgender (Male to Female)", "Intersex (male/female)", "Neither male nor female",
                 "Nonbinary woman", "Non-binary man"]:
        assert not gender.unknown(text), text
    assert gender.other_answer("x" * 3000).endswith(gender.OTHER_NOTE) and len(gender.other_answer("x" * 3000)) < 80


def test_an_import_stores_what_the_file_says_and_guesses_the_rest(data):
    rs, _ = data
    rows = by_phone(rs)
    assert {p: r["gender"] for p, r in rows.items()} == EXPECTED
    # Ambiguous first names get no guess, and a known name's email isn't used instead (Jordan, mary@x.com).
    assert rows["5165550103"]["gender"] is None and rows["5165550119"]["gender"] is None
    # The customer's own other answer is kept out of the guess and noted; "Unknown" counts as nothing said.
    assert "not guessed" in rows["5165550118"]["issues"]["gender"]
    assert "gender" not in rows["5165550117"]["issues"]
    assert "gender" in rows["5165550112"]["issues"]


def test_a_guess_never_replaces_f_or_m_but_a_new_guess_replaces_an_old_one(data):
    rs, _ = data
    rs.db.execute("UPDATE records SET gender = 'Mb' WHERE first_name = 'Mary'")      # a stale guess
    rs.db.execute("UPDATE records SET gender = 'M' WHERE first_name = 'Lisa Marie Ray'")  # as if the file said M
    with rs.db.conn() as c:
        changed = gender.guess(c)
    rows = by_phone(rs)
    assert changed == 1 and rows["5165550101"]["gender"] == "Fb" and rows["5165550105"]["gender"] == "M"
    assert rows["5165550104"]["gender"] == "F" and rows["5165550118"]["gender"] is None


def test_find_by_gender(data):
    rs, schema = data
    fields = find_fields(schema, rs.columns_seen())

    def phones(text):
        cond, params = F.where(F.find_node([{"criteria": [["f:gender", text]]}]), fields, today=date(2026, 10, 15))
        return {r["phones"] for r in F.page(rs.db, schema, cond, params, limit=100)}

    want = lambda *values: {p for p, g in EXPECTED.items() if g in values}  # noqa: E731
    assert phones("f") == want("F", "Fb")
    assert phones("=f") == want("F")
    assert phones("fb") == want("Fb")
    assert phones("m") == want("M", "Mb")
    assert phones("=") == want(None)
    assert phones("*") == want("F", "Fb", "M", "Mb")


def test_gender_is_kept_out_of_tag_exports(data, real_schema):
    rs, schema = data
    tag = rs.create_tag("1001", "All")
    cond, params = F.where(F.all_records(), [])
    rs.add_to_tag(tag, cond, params)
    table, _ = export_table(rs.db, tag, schema, real_schema)
    assert list(table.columns) == [f.name for f in real_schema.fields] and "gender" not in table.columns


def test_records_stored_before_get_a_guess_when_the_app_starts(rstore, records_schema, tmp_path):
    """A database from before 1.4.0 (no gender, no name table): the first start adds the column, loads the names
    and guesses every record. A changed name table is loaded, and guessed with, on the next start."""
    rs = rstore
    import_file(rs, Store(tmp_path / "c.db"), records_schema, "old.csv", "First Name,Phone\nMary,5165550101\n"
                                                                       "John,5165550102\n")
    rs.db.execute("ALTER TABLE records DROP COLUMN gender")
    rs.db.execute("DELETE FROM app_meta WHERE key = 'first_names'")
    rs.db.execute("TRUNCATE first_names")
    rs.db.setup(records_schema)
    assert {r["gender"] for r in rs.db.fetch("SELECT gender FROM records")} == {"Fb", "Mb"}
    assert rs.db.fetch_one("SELECT count(*) AS n FROM first_names")["n"] > 90_000
    assert rs.db.fetch_one("SELECT value FROM app_meta WHERE key = 'first_names'")["value"] == \
        gender.names_version()
    # Restarting with the same table changes nothing; a different table version guesses again.
    rs.db.execute("UPDATE records SET gender = NULL")
    rs.db.setup(records_schema)
    assert {r["gender"] for r in rs.db.fetch("SELECT gender FROM records")} == {None}
    rs.db.execute("UPDATE app_meta SET value = 'older' WHERE key = 'first_names'")
    rs.db.setup(records_schema)
    assert {r["gender"] for r in rs.db.fetch("SELECT gender FROM records")} == {"Fb", "Mb"}


def test_the_name_table():
    import csv
    import gzip
    with gzip.open(gender.NAMES_FILE, "rt") as fh:
        names = {r["name"]: (int(r["female"]), int(r["male"])) for r in csv.DictReader(fh)}
    assert len(names) > 90_000
    for name, sex in [("mary", "F"), ("john", "M"), ("maria", "F"), ("jose", "M"), ("siobhan", "F"),
                      ("mohammed", "M"), ("priya", "F")]:
        f, m = names[name]
        assert (f >= gender.SHARE * (f + m)) == (sex == "F") and (m >= gender.SHARE * (f + m)) == (sex == "M")
    f, m = names["jordan"]
    assert gender.SHARE * (f + m) > max(f, m)            # used for both: no guess


MAPPED = ["Gender", "Sex", "GENDER", "Customer Gender", "Guest Gender", "M/F", "F/M", "Gender (M/F)", "Customer Sex",
          "Guest Sex", "Member Sex", "Patient Sex", "Sex Code", "Sex of Customer", "Geschlecht", "Gender Identity",
          "Male/Female", "sex_code", "GenderCode", "Género", "GÉNERO", "Gênero", "Sexe", "Sex at Birth", "Legal Sex",
          "Sesso", "Genere", "Parent Gender", "Preferred Gender"]
NOT_MAPPED = ["Tender", "Tenders", "Sender", "Senders", "General", "Lender", "Render", "Fender", "Essex", "Female",
              "Gendered Pricing", "Child Gender", "Gender of Child", "Spouse Sex", "Customer ID", "Customer Name",
              "Sexton", "Member Since", "Title", "Salutation", "Genre", "Stylist Gender Preference",
              "Therapist Gender", "Provider Gender", "Grandchild Gender", "Infant Gender", "Recipient Gender"]


@pytest.mark.parametrize("header,mapped", [(h, True) for h in MAPPED] + [(h, False) for h in NOT_MAPPED])
def test_which_columns_feed_gender(records_schema, header, mapped):
    """Gender and Sex columns are found by the rules; look-alikes ("Tender" is 83% like "Gender") and someone else's
    gender ("Child Gender") are not (records_schema.yaml header_words / header_not_words)."""
    from converter.mapping import profile_columns
    from converter.reader import read_file
    from converter.services import initial_mapping
    rr = read_file("x.csv", f"Phone,First Name,{header}\n5165550101,Ann,F\n5165550102,Bob,M\n".encode())
    m = initial_mapping(records_schema, rr.df, profile_columns(rr.df), None, None, use_ai=False).mapping
    assert (m.get("gender").sources == [header]) == mapped



def test_a_database_from_before_1_4_0_gets_what_its_files_said(rstore, records_schema, tmp_path):
    """Upgrading: records imported before 1.4.0 get the F / M their Gender column said (from the row as written),
    an other answer stays empty and unguessed, and the rest are guessed."""
    rs = rstore
    csv = ("First Name,Phone,Gender\nAshley,5165550201,Male\nMary,5165550202,Female\nLinda,5165550203,Non-binary\n"
           "Michael,5165550204,Other\nJohn,5165550205,\nSusan,5165550206,Unknown\n")
    import_file(rs, Store(tmp_path / "c.db"), records_schema, "old.csv", csv)
    rs.db.execute("ALTER TABLE records DROP COLUMN gender")              # as 1.3.1 stored it
    rs.db.execute("UPDATE records SET issues = issues - 'gender'")
    rs.db.execute("DELETE FROM app_meta WHERE key = 'first_names'")
    rs.db.setup(records_schema)
    got = {p: r["gender"] for p, r in by_phone(rs).items()}
    assert got == {"5165550201": "M", "5165550202": "F", "5165550203": None, "5165550204": None,
                   "5165550205": "Mb", "5165550206": "Fb"}
    assert "not guessed" in by_phone(rs)["5165550203"]["issues"]["gender"]


GUESSES = """First Name,Last Name,Phone,Email,Gender
John and Mary,Smith,5165550301,,
Mr & Mrs,Jones,5165550302,,
Frau Anna,Schmidt,5165550303,,
Herr Klaus,Berg,5165550304,,
Mx.,Lee,5165550305,john@x.com,
Hans-Peter,Kurz,5165550306,,
Unknown,,5165550307,maria.lopez@x.com,
Baby,,5165550308,maria.lopez@x.com,
Šárka,Novak,5165550309,,
ÇAĞRI,Demir,5165550310,,
Mary,Smith,5165550311,,#VALUE!
ANDRÉ,Roy,5165550312,,
ZOË,Kay,5165550313,,
Jean-François,Roy,5165550314,,
Jean-Louis,Roy,5165550315,,
Sr. Mary,Joseph,5165550316,,
Mary + John,Lee,5165550317,,
Hans–Peter,Kurz,5165550318,,
Pat,Lee,5165550319,,U - Unknown
Jean-Noël,Roy,5165550320,,
Jean-Rony,Roy,5165550321,,
John,Roy,5165550322,,Male to Female
"""


def test_harder_guesses(rstore, records_schema, tmp_path):
    rs = rstore
    import_file(rs, Store(tmp_path / "c.db"), records_schema, "g.csv", GUESSES)
    got = {p: r["gender"] for p, r in by_phone(rs).items()}
    assert got["5165550301"] is None and got["5165550302"] is None       # two people: no guess
    assert got["5165550303"] == "Fb" and got["5165550304"] == "Mb"       # Frau, Herr
    assert got["5165550305"] is None                                     # Mx: neither, and not the email's John
    assert got["5165550306"] == "Mb"                                     # Hans-Peter: Hans
    assert got["5165550307"] == "Fb" and got["5165550308"] == "Fb"       # not names: the email's Maria
    assert got["5165550309"] != "Mb" and got["5165550310"] != "Fb"       # Šárka isn't "arka", Çağrı isn't "cari"
    assert got["5165550311"] == "Fb"                                     # a spreadsheet error says nothing
    assert got["5165550312"] == "Mb" and got["5165550313"] == "Fb"       # capital accents
    assert got["5165550314"] is None and got["5165550315"] is None       # Jean (US: mostly girls) + a boy's name
    assert got["5165550316"] == "Fb"                                     # "Sr." says nothing: Mary
    assert got["5165550317"] is None                                     # two people
    assert got["5165550318"] == "Mb"                                     # an en dash too
    assert "gender" not in by_phone(rs)["5165550319"]["issues"]          # "U - Unknown" says nothing
    assert got["5165550320"] is None and got["5165550321"] is None       # the parts don't both say male
    assert got["5165550322"] is None                                     # the customer's own answer: not guessed
    assert "not guessed" in by_phone(rs)["5165550322"]["issues"]["gender"]


def test_capital_accents_in_a_c_locale_database(pg_server, records_schema, tmp_path):
    """lower() of a C-locale database only knows A-Z: the accents are taken off before it."""
    from records.db import Database
    from records.store import RecordStore

    from .conftest import drop_database, fresh_database
    url = fresh_database(pg_server, icu=False)
    db = Database(url)
    try:
        db.setup(records_schema)
        rs = RecordStore(db)
        import_file(rs, Store(tmp_path / "c.db"), records_schema, "g.csv",
                    "First Name,Phone\nANDRÉ,5165550401\nZOË,5165550402\nÖMER,5165550403\nHÉCTOR,5165550404\n")
        got = {p: r["gender"] for p, r in by_phone(rs).items()}
        assert got == {"5165550401": "Mb", "5165550402": "Fb", "5165550403": "Mb", "5165550404": "Mb"}
    finally:
        db.close()
        drop_database(pg_server, url)



def test_the_upgrade_reads_the_files_as_an_import_does(rstore, records_schema, tmp_path):
    """Cells are cleaned as the converter cleans them (a spreadsheet error, "nan", "Never" say nothing), and with a
    Gender and a Sex column the upgrade reads the one an import maps."""
    rs = rstore
    csv = ("First Name,Phone,Gender,Sex\nMary,5165550501,#VALUE!,\nAnn,5165550502,nan,\nLisa,5165550503,Never,\n"
           "Robin,5165550504,Non-binary,F\nJordan,5165550505,Woman,M\nPat,5165550506,,F\n")
    import_file(rs, Store(tmp_path / "c.db"), records_schema, "old.csv", csv)
    fresh = {p: r["gender"] for p, r in by_phone(rs).items()}
    rs.db.execute("ALTER TABLE records DROP COLUMN gender")
    rs.db.execute("UPDATE records SET issues = issues - 'gender'")
    rs.db.execute("DELETE FROM app_meta WHERE key = 'first_names'")
    rs.db.setup(records_schema)
    upgraded = {p: r["gender"] for p, r in by_phone(rs).items()}
    assert upgraded == fresh
    assert upgraded["5165550501"] == "Fb" and upgraded["5165550504"] == "F"
