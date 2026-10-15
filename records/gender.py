"""Gender: what a file says (F, M), and a guess from the first name when it says nothing (Fb, Mb).

Values (the record field ``gender``, decided 2026-10-15)
--------------------------------------------------------
======  =================================================================================================
``F``   the file said so: a Gender / Sex column with Female, Woman, Girl, F, W, ♀, "F - Female"… (:func:`stated`)
``M``   the file said so: Male, Man, Boy, M, ♂, "1 - Male"…
``Fb``  likely female: guessed (:func:`guess`), never from the file
``Mb``  likely male: guessed
empty   nothing said and nothing to guess from (no first name, or one used for girls and boys alike)
======  =================================================================================================

What a file's cell says: female or male words (English, Spanish, French, German, Portuguese, Italian; accents,
case and punctuation don't matter; a negated one, "Not male", "Neither male nor female", says neither); "nothing"
(empty, Unknown, U - Unknown, N/A, Not stated, a number code such as 1 / 2, a title without a gender such as Dr, an
unfilled form's "M/F" or "Male or Female": :func:`unknown`), and then the record can get a guess; or the customer's own other answer (Other, Non-binary, Mx, Male to Female, Intersex, Prefer not to say…): the gender stays empty, the
record's issues say so (ending :data:`OTHER_NOTE`), and it is never guessed. Which column feeds the field is decided
by the converter's rules: a header naming gender or sex, and not someone else's (``header_words`` and
``header_not_words`` in records_schema.yaml).

How a guess is made
-------------------
In this order, from the record's own values:

1. Two people in First name ("John and Mary", "Mr & Mrs", "Mary + John"): no guess.
2. A title in First name: Mr, Mister, Sir, Herr, Señor → Mb; Mrs, Ms, Miss, Mme, Mlle, Madam, Frau, Sra, Señora → Fb;
   Mx → no guess; Dr, Prof, Rev and Sr (Señor or Sister) say nothing: the next word is the first name.
3. The first name (its first word; "Ray, Lisa" kept whole in First name: Lisa; "Dr. John": John; accents taken off;
   "Mary-Kate" as the name table writes it, else both parts, which must agree: "Hans-Peter" → Mb, "Jean-François"
   and "Jean-Noël" → none) looked up in ``first_names``
   (US Social Security baby names since 1930, ``names/first_names.csv.gz``): at least :data:`SHARE` (90%) of the
   people with it female → Fb, male → Mb. A name given to fewer than :data:`MIN_PEOPLE` people, or to both, gives
   no guess.
4. Only when the first name is missing or not a known name: the email's first word (``maria.lopez@…`` → maria).

Guesses only ever replace an empty gender or an earlier guess. They are made by one SQL statement (:func:`guess`):
for an import's records when it is stored (records.importer; about 1.5 seconds per 20,000 rows), and for every
record when the app starts with a database whose name table is missing or older (records.db.setup; about 20
seconds per 300,000 records, about 5 when no guess changes). A database from before 1.4.0 first gets the F / M its files said
(:func:`read_stored_files`, from each record's row as written).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import re
import unicodedata
from pathlib import Path

from psycopg import sql

# The record field this module fills (records_schema.yaml); nothing happens when the record fields don't have it.
FIELD = "gender"
STATED = ("F", "M")
GUESSED = ("Fb", "Mb")
# The end of the issue noted for a file's own other answer: such records are never guessed.
OTHER_NOTE = "(not guessed)"

NAMES_FILE = Path(__file__).parent / "names" / "first_names.csv.gz"
# A guess needs at least this share of the name's people to be of one sex, and at least this many people.
SHARE = 0.9
MIN_PEOPLE = 10

_FEMALE = {"f", "female", "fem", "feminine", "w", "woman", "women", "girl", "girls", "lady", "ladies", "she", "her",
           "hers", "mrs", "ms", "miss", "femenino", "femenina", "mujer", "feminin", "femme", "feminino", "weiblich",
           "weibl", "frau", "donna", "femmina", "♀"}
_MALE = {"m", "male", "masc", "masculine", "man", "men", "boy", "boys", "guy", "gentleman", "gent", "he", "him", "his",
         "mr", "masculino", "hombre", "varon", "masculin", "homme", "mannlich", "mannl", "herr", "uomo", "maschio",
         "♂"}
# Words of a cell that says nothing: "U - Unknown", "N/A - Not Applicable", "Unknown or not specified", "Dr".
_UNKNOWN_WORDS = {"u", "unk", "unknown", "n", "a", "na", "none", "null", "nil", "blank", "unspecified", "not",
                  "specified", "stated", "known", "applicable", "provided", "given", "set", "no", "answer", "or",
                  "dr", "doctor", "prof", "rev"}
_NEGATIONS = {"not", "non", "no", "neither", "nor", "nonbinary"}
# What an unfilled form's cell may hold besides female and male words: "M/F", "Male or Female", "Mr. & Mrs.".
_FORM_WORDS = {"or", "and"}
_TITLES = {"mr": "Mb", "mister": "Mb", "sir": "Mb", "herr": "Mb", "senor": "Mb", "signor": "Mb", "signore": "Mb",
           "mrs": "Fb", "ms": "Fb", "miss": "Fb", "mme": "Fb", "mlle": "Fb", "madam": "Fb", "madame": "Fb",
           "frau": "Fb", "sra": "Fb", "senora": "Fb", "srta": "Fb", "signora": "Fb", "sigra": "Fb", "mx": None}
# Titles that say nothing about the person: the next word is the first name ("Sr." is Señor or Sister).
_SKIP_TITLES = ("dr", "doctor", "prof", "rev", "sr")
# The rules' threshold for a text field (converter.heuristics.auto_map step 1).
_TEXT_THRESHOLD = 72


def _letter_map() -> tuple[str, str]:
    """Accented Latin letters (both cases) → plain lower-case ones, one to one (for SQL's translate)."""
    src, dst = [], []
    for cp in list(range(0xC0, 0x250)) + list(range(0x1E00, 0x1F00)):
        ch = chr(cp)
        plain = "".join(c for c in unicodedata.normalize("NFKD", ch) if c.isascii() and c.isalpha())
        if len(plain) == 1:
            src.append(ch)
            dst.append(plain.lower())
    for ch, plain in {"ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ı": "i", "ß": "s", "æ": "a",
                      "Æ": "a", "œ": "o", "Œ": "o", "þ": "t", "Þ": "t", "ð": "d", "Ð": "d", "ħ": "h", "Ħ": "h",
                      "ŧ": "t", "Ŧ": "t", "ŀ": "l", "Ŀ": "l"}.items():
        if ch not in src:
            src.append(ch)
            dst.append(plain)
    return "".join(src), "".join(dst)


_ACCENTED, _PLAIN = _letter_map()


def _cell_words(text: str) -> tuple[str, list[str]]:
    """A gender cell as (its words joined by spaces, its words): plain lower-case letters and the ♀ ♂ signs."""
    folded = str(text).translate(str.maketrans(_ACCENTED, _PLAIN)).lower()
    words = re.findall(r"[a-z]+|[♀♂]", folded)
    return " ".join(words), words


def available(schema) -> bool:
    """Whether the record fields have what guessing needs: the gender field, first and last name, email."""
    return {FIELD, "first_name", "last_name", "email"} <= set(schema.field_names)


def _says(text: str) -> tuple[bool, bool, list[str]]:
    """(names female, names male, words) of a gender cell; a negated cell ("Not male", "Non-binary") names neither."""
    joined, words = _cell_words(text)
    if set(words) & _NEGATIONS:
        return False, False, words
    female = joined in _FEMALE or any(w in _FEMALE for w in words)
    male = joined in _MALE or any(w in _MALE for w in words)
    return female, male, words


def unknown(text: str | None) -> bool:
    """Whether a file's gender cell says nothing: empty, Unknown, U - Unknown, N/A, Not stated, ?, -, a number (a
    code such as 1 / 2 whose meaning the file doesn't give, also 1.0 or -1), a title without a gender (Dr), or both
    sexes at once (M/F, Male or Female: an unfilled form)."""
    if text is None or not str(text).strip():
        return True
    female, male, words = _says(text)
    if not words:
        return True
    if female and male:
        # Both sexes and nothing else is an unfilled form ("M/F"); with any other word it is the customer's own
        # answer ("Male to Female", "Intersex (male/female)").
        return all(w in _FEMALE or w in _MALE or w in _FORM_WORDS for w in words)
    return all(w in _UNKNOWN_WORDS for w in words)


def stated(text: str | None) -> str | None:
    """F or M for what a file's gender cell says ("Female", "WOMAN", "F - Female", "Féminin", "♀", "she/her" → F;
    "Male", "boy", "1 - Male", "M (Male)", "Masc." → M); None when it says neither, both, or negates ("Not male")."""
    if not text:
        return None
    female, male, _ = _says(text)
    if female != male:
        return "F" if female else "M"
    return None


def other_answer(text: str) -> str:
    """The issue noted for a file's own other answer (Other, Non-binary…): short, ending :data:`OTHER_NOTE`."""
    shown = " ".join(str(text).split())
    shown = shown if len(shown) <= 40 else shown[:39] + "…"
    return f"'{shown}' isn't female or male {OTHER_NOTE}"


def header_column(columns: list[str], field) -> str | None:
    """The column of these headers that the converter's rules map to the gender ``field`` (header evidence only,
    as for every text field; accents folded as heuristics.score does for ``header_words``), or None."""
    from converter.heuristics import _words, header_score, plain_letters

    best: tuple[float, str] | None = None
    for col in columns:
        words, hs = set(_words(plain_letters(col))), header_score(col, field)
        if words & set(field.header_not_words):
            continue
        if field.header_words and not words & set(field.header_words) and hs < 100:
            continue
        if hs >= _TEXT_THRESHOLD and (best is None or (hs, col) > best):
            best = (hs, col)          # equal scores: the later column name, as auto_map's sort picks
    return best[1] if best else None


def names_version() -> str:
    """A fingerprint of the shipped name table (the database reloads it, and guesses again, when it changes)."""
    return hashlib.sha1(NAMES_FILE.read_bytes()).hexdigest()[:12]


def load_names(c) -> int:
    """Replace the ``first_names`` table with the shipped one (``c``: a connection in a transaction). Returns the
    number of names."""
    c.execute("TRUNCATE first_names")
    with gzip.open(NAMES_FILE, "rt", encoding="utf-8") as fh:
        rows = list(csv.reader(io.StringIO(fh.read())))[1:]
    with c.cursor().copy("COPY first_names (name, female, male) FROM STDIN") as cp:
        for name, female, male in rows:
            cp.write_row([name, int(female), int(male)])
    return len(rows)


def _fold_sql(expr: str) -> str:
    """SQL: the text with accents taken off and lower-cased ("Šárka" → "šárka" → "sarka"), also in a C-locale
    database, whose lower() only knows A-Z: translate runs first and knows both cases."""
    return f"lower(translate({expr}, '{_ACCENTED}', '{_PLAIN}'))"


def _plain(expr: str) -> str:
    """SQL: :func:`_fold_sql` with every character that isn't a letter or a space taken out ("Mary-Kate" →
    "marykate", as the name table writes it; "Mr." → "mr"). Letters of other scripts stay, so such a name matches
    no name (no guess) instead of becoming another one."""
    return f"regexp_replace({_fold_sql(expr)}, '[^[:alpha:][:space:]]', '', 'g')"


def _guess_sql(where: sql.Composable) -> sql.Composed:
    """The UPDATE that guesses the gender of the records matching ``where`` (a condition on ``records r``).

    Each record's name is folded once (a materialized step: PostgreSQL would otherwise repeat the folding in every
    join and test, three times slower)."""
    first = ("CASE WHEN coalesce(r.last_name, '') = '' AND strpos(r.first_name, ',') > 0 "
             "THEN split_part(r.first_name, ',', 2) ELSE r.first_name END")
    couple = ("(lower(coalesce(r.first_name, '')) ~ '(^|[[:space:]])(and|y|et|und)([[:space:]]|$)' "
              "OR r.first_name ~ '[&+]')")
    # The email's first word of letters: "maria.lopez22@x.com" → maria.
    mail = "split_part(btrim(regexp_replace(lower(split_part(r.email, '@', 1)), '[^a-z]+', ' ', 'g')), ' ', 1)"
    titles = " ".join(f"WHEN '{t}' THEN {'NULL' if g is None else repr(g)}" for t, g in _TITLES.items())
    skip = ", ".join(f"'{t}'" for t in _SKIP_TITLES)

    def part(n: int) -> str:
        # The n-th part of a hyphenated first name ("Hans-Peter": hans, peter), letters only.
        return (f"regexp_replace(split_part(regexp_replace(s.raw1, '[–—]', '-', 'g'), '-', {n}), '[^[:alpha:]]', "
                "'', 'g')")

    def by_share(n: str) -> str:
        return (f"(CASE WHEN {n}.female + {n}.male < {MIN_PEOPLE} THEN NULL "
                f"WHEN {n}.female >= {SHARE} * ({n}.female + {n}.male) THEN 'Fb' "
                f"WHEN {n}.male >= {SHARE} * ({n}.female + {n}.male) THEN 'Mb' END)")

    # A hyphenated name the table doesn't have whole: both parts must give the same guess ("Hans-Peter" → Mb).
    # "Jean-Noël", "Jean-Rony": Jean is mostly a girl's name in the US but not in French compounds, and the second
    # part is unclear or unknown: no guess.
    hyphen = f"CASE WHEN {by_share('n3')} = {by_share('n4')} THEN {by_share('n3')} END"
    return sql.SQL(f"""
        WITH a AS MATERIALIZED (
            SELECT r.id, {first} AS first, {couple} AS couple, {mail} AS m
            FROM records r
            WHERE (r.{FIELD} IS NULL OR r.{FIELD} IN ('Fb', 'Mb'))
              AND strpos(coalesce(r.issues ->> '{FIELD}', ''), '{OTHER_NOTE}') = 0 AND {{}}
        ),
        b AS MATERIALIZED (
            SELECT a.id, a.couple, a.m,
                   CASE WHEN octet_length(a.first) = char_length(a.first) THEN lower(a.first)
                        ELSE {_fold_sql('a.first')} END AS folded
            FROM a
        ),
        c AS MATERIALIZED (
            SELECT b.id, b.couple, b.m,
                   regexp_split_to_array(btrim(regexp_replace(b.folded, '[^[:alpha:][:space:]]', '', 'g')),
                                         '[[:space:]]+') AS w,
                   regexp_split_to_array(btrim(b.folded), '[[:space:]]+') AS rw
            FROM b
        ),
        s AS MATERIALIZED (
            SELECT c.id, c.couple, c.m, c.w[1] AS w1,
                   CASE WHEN c.w[1] IN ({skip}) THEN c.w[2] ELSE c.w[1] END AS k1,
                   CASE WHEN c.w[1] IN ({skip}) THEN c.rw[2] ELSE c.rw[1] END AS raw1
            FROM c
        ),
        h AS MATERIALIZED (
            SELECT s.*, {part(1)} AS part1, {part(2)} AS part2 FROM s
        ),
        g AS (
            SELECT h.id,
                   CASE WHEN h.couple THEN NULL ELSE
                       CASE h.w1 {titles} ELSE
                           CASE WHEN n1.name IS NOT NULL THEN {by_share('n1')}
                                WHEN n3.name IS NOT NULL THEN {hyphen}
                                ELSE {by_share('n2')} END
                       END
                   END AS guess
            FROM h
            LEFT JOIN first_names n1 ON n1.name = h.k1
            LEFT JOIN first_names n3 ON n1.name IS NULL AND n3.name = h.part1
            LEFT JOIN first_names n4 ON n1.name IS NULL AND h.part2 <> '' AND n4.name = h.part2
            LEFT JOIN first_names n2 ON n2.name = h.m AND length(h.m) >= 3
        )
        UPDATE records t SET {FIELD} = g.guess FROM g
        WHERE t.id = g.id AND t.{FIELD} IS DISTINCT FROM g.guess""").format(where)


def guess(c, where: sql.Composable | None = None, params: dict | None = None) -> int:
    """Guess the gender of the records matching ``where`` (every record when None) whose gender is empty or an
    earlier guess; F and M, and a file's own other answer (Other…), are never touched. ``c``: a connection in a
    transaction. Returns how many records changed."""
    cur = c.execute(_guess_sql(where if where is not None else sql.SQL("true")), params or {})
    return cur.rowcount


def read_stored_files(c, schema) -> int:
    """For a database from before 1.4.0: give its records what their files said (F, M, or an other answer), from
    each record's row as written, cleaned as the converter cleans a cell (spreadsheet errors and null words are
    nothing), in the column the rules map to the gender field now (:func:`header_column`; header evidence only, so
    the rare file whose values would decide differently may differ from a new import). Run before the first
    :func:`guess`. Returns how many records got F, M or an other answer."""
    from converter.normalizers import clean_text, sheet_error

    field = schema.field(FIELD)
    changed = 0
    for imp in c.execute("SELECT id, columns FROM imports WHERE status = 'completed'").fetchall():
        col = header_column(list(imp["columns"] or []), field)
        if col is None:
            continue
        raw = [r["v"] for r in c.execute(
            f"SELECT DISTINCT source ->> %s AS v FROM records WHERE import_id = %s AND {FIELD} IS NULL",
            (col, imp["id"])).fetchall() if r["v"]]
        values, genders, notes = [], [], []
        for v in raw:
            text = clean_text(v)
            if not text or sheet_error(text):
                continue
            said = stated(text)
            if said or not unknown(text):
                values.append(v)
                genders.append(said)
                notes.append(None if said else other_answer(text))
        if values:
            cur = c.execute(
                f"""UPDATE records r SET {FIELD} = v.g,
                        issues = CASE WHEN v.note IS NULL THEN r.issues
                                      ELSE r.issues || jsonb_build_object('{FIELD}', v.note) END
                    FROM unnest(%s::text[], %s::text[], %s::text[]) AS v(val, g, note)
                    WHERE r.import_id = %s AND r.{FIELD} IS NULL AND r.source ->> %s = v.val""",
                (values, genders, notes, imp["id"], col))
            changed += cur.rowcount
    return changed
