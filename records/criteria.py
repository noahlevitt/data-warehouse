"""One find criterion (what was typed for one field of a find request) → an SQL condition on ``records r``.

Overview
--------
The syntax follows FileMaker, which the team knows; it is written out for people in :data:`SYNTAX` (shown under
"How to search" on the Find page; the README has the same as a table), with FileMaker's differences marked ≠.
Every condition is wrapped in ``COALESCE(..., false)``, so an empty value never makes a request "unknown" (that
would break Omit). Values are passed as query parameters (:class:`Params`), never pasted into the SQL. Upper and
lower case never matter, also for accented letters (Åberg, MÓR): the database's own setting is used when it handles
them, otherwise an explicit collation (records.db.Database.setup picks it).

Nothing is ever required: a field left blank isn't searched (records.find ignores blank requests). Before reading a
criterion, ``…`` (one character, as phones and Macs type it) and ``..`` become the range operator ``...``; four or
more dots are refused (:func:`normalize`). Every range may leave one end open. Typed Business and Service criteria
(``business_condition``, kind ``service``) are still understood, for found sets kept from before 1.1.0 and for code;
the Find page has pickers for them instead.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg import sql

from converter import dateparse
from converter.normalizers import FALSE_TOKENS, TRUE_TOKENS

from .fields import FindField



# What people can type, for the Find page's "How to search" panel (Markdown).
SYNTAX = """
Leave a box empty to ignore it — **nothing is required**. A Find with everything blank shows every record. A range
is written with three dots: `...` (`…` and `..` are read the same); either end may be left open.

**`*` alone** in a box finds the records where that field has a value; **`=` alone** finds the records where it is
blank. This works in every box: Phone `=` is no usable number, Tag `=` is in no tag, and a file's column `=` also
finds records from files without that column. A value the import couldn't read (Visits `abc`) counts as blank.
Typed with anything else, `*` and `=` keep their meanings below (`sm*th`, `=smith`).

**Names**: First name and Last name work as text below. Some files give the whole name in their First Name column,
kept as it is with no Last name ("Lisa Marie Ray"). First name and Last name also read such a name as split at its
first space (first name Lisa, last name Marie Ray), or at its comma ("Ray, Lisa": last name Ray, first name Lisa):
First name `lisa` and Last name `ray` both find it. `=` and `*` look at the stored names only. **Full name** (under
Add a field) searches first and last together: `lisa ray`.

**Text** (names, email, address, city, Any field, file columns, Tag, File name)
- `smith` — a word starting with "smith" (Smith, Smithson; not Goldsmith); upper/lower case never matters
- `john smith` — both words, in any order · `=smith` — the whole word · `==john smith` — exactly the whole field
- `"main st"` — this exact text anywhere, punctuation included · `sm*th` — `*` is any characters
- `=` — empty · `*` — any value
- `a...m` — from a through everything starting with m · `...m` up to m · `m...` from m on · `<m`, `>=m`
- Punctuation at the start or end of a word is ignored (`c++` looks for words starting with c; ≠ FileMaker, `@`
  and `#` are ordinary characters, so emails and "Apt #4" can be typed as they are); ’ and ' are the same

**Phone** (any of the record's numbers, typed in any format)
- `516-555-0100`, `(516) 555 0100`, `+1 516 555 0100` — that number
- `5550100` — numbers containing these digits · `516*` or `+1 516*` — numbers starting with 516
- `=` — no usable number · `*` — has a number

**Numbers** (visits, spend, Record ID, Import #, File row)
- `5` · `>5` · `<=100` · `10...20` (both included) · `...20` (20 or less) · `10...` (10 or more)
- `$1,200.50` and `#12` are fine · `=` empty · `*` any value

**Dates** (last visit and the other activity dates, first visit, birthday, anniversary, Record created). The last
visit is a date only: type days, never times.
- `10/9/2026` — that day · `10/2026` — that month · `2026` — that year · `//` — today
- `1/1/2025...1/1/2026` — from the first day through the last (both included)
- `...1/1/2026` — up to and including that day · `1/1/2025...` — from that day on (also `...2025`, `10/2026...`)
- `<10/1/2026` before · `<=10/1/2026` up to and including · `>` after · `>=` on or after
- `10/9/2026 3:45 PM` — that minute (fields with a time); `>10/9/2026 9:00 AM` — after it
- `10/9` or `Oct 9` — that day in **any year** (birthdays) · `10/*` or `October` — that month in any year ·
  `12/15...1/15` — across the new year · `...3/31` — January 1 to March 31 of any year
- `=` empty · `*` any value
- Birthdays and anniversaries without a year are stored as year 1600: day-and-month criteria (`9/12`, `10/*`)
  find them, and so does asking for 1600 itself; a range or before/after with a real year never does

**Yes / no** (consent): `yes` · `no` · `=` empty · `*` any value

**Business and Services**: pick them in the request (one or more; empty = any). To leave a service out, pick it in
an **Omit** request.

Not supported (≠ FileMaker): `!` duplicates, `?` invalid dates, `~` relaxed search — they say so.
"""

class CriterionError(ValueError):
    """What was typed can't be read for this field; the message says why (shown to the person)."""


class Params:
    """Collects query parameter values and hands out named placeholders (%(p0)s, %(p1)s, …).

    Also carries what every condition needs to know: ``collation`` (None = the database's own; otherwise the
    collation that makes upper/lower case of accented letters match, see records.db), ``timezone`` (the app's,
    for Record created) and ``today`` (in that time zone, for ``//``).
    """

    def __init__(self, collation: str | None = None, timezone: str = "America/New_York",
                 today: date | None = None) -> None:
        self.values: dict[str, Any] = {}
        self.collation = collation
        self.timezone = timezone
        self.today = today or date.today()

    def __call__(self, value: Any) -> sql.Placeholder:
        name = f"p{len(self.values)}"
        self.values[name] = value
        return sql.Placeholder(name)


# First and last name as searched. Some files give whole names in their First Name column, kept whole in First name
# with no Last name ("Lisa Marie Ray" | ""). Such a name is also searched as if split: at a comma when it has one
# ("Smith, John": last Smith, first John), otherwise at its first space (first Lisa, last Marie Ray). First name
# matches the stored first name OR that first part; Last name the stored last name OR the rest. Records with a last
# name are searched as they are; "=" (empty) and "*" (any value) are about the stored values only.
def name_expr(part: str, table: str = "r.") -> sql.SQL:
    """The split part ("first" or "last") of a whole name kept in First name, else NULL; ``table`` "" for an index
    expression (records.db creates one per part)."""
    first, last = f"btrim({table}first_name)", f"{table}last_name"
    whole = f"coalesce({last}, '') = '' AND (strpos({first}, ',') > 0 OR strpos({first}, ' ') > 0)"
    if part == "first":
        split = (f"CASE WHEN strpos({first}, ',') > 0 THEN btrim(split_part({first}, ',', 2)) "
                 f"ELSE split_part({first}, ' ', 1) END")
    else:
        split = (f"CASE WHEN strpos({first}, ',') > 0 THEN btrim(split_part({first}, ',', 1)) "
                 f"ELSE btrim(substr({first}, strpos({first}, ' ') + 1)) END")
    return sql.SQL(f"(CASE WHEN {whole} THEN {split} END)")


def name_condition(part: str, text: str, p: "Params") -> sql.Composable:
    """First or Last name: the stored value, or the split part of a whole name (see above)."""
    stored = sql.SQL(f"r.{part}_name")
    t = text.strip()
    if t in ("=", "*"):
        return text_condition(stored, t, p)
    return sql.SQL("({} OR {})").format(_coalesce(text_condition(stored, t, p)),
                                        _coalesce(text_condition(name_expr(part), t, p)))


TRUE = sql.SQL("true")
FALSE = sql.SQL("false")
_REGEX_SPECIAL = set(".^$|?*+()[]{}\\")
_APOSTROPHES = "'‘’"
_UNSUPPORTED = {"!": "duplicates (!)", "?": "invalid values (?)", "~": "relaxed search (~)"}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9


def _coalesce(cond: sql.Composable) -> sql.Composable:
    """``COALESCE((cond), false)``: NULL counts as "doesn't match" (see the module docstring)."""
    return sql.SQL("COALESCE(({}), false)").format(cond)


def _and(parts: list[sql.Composable]) -> sql.Composable:
    return sql.SQL("(") + sql.SQL(" AND ").join(parts) + sql.SQL(")") if parts else TRUE


def _check_supported(text: str) -> None:
    """Refuse FileMaker operators this tool doesn't implement, with a message."""
    for op, what in _UNSUPPORTED.items():
        if text == op or (text.startswith(op) and op != "?"):
            raise CriterionError(f"Searching for {what} isn't supported yet.")


def _regex(text: str) -> str:
    """Text → a PostgreSQL regular expression fragment: ``*`` = any characters, the three apostrophes are one,
    everything else literal."""
    out = []
    for ch in text:
        if ch == "*":
            out.append(".*")
        elif ch in _APOSTROPHES:
            out.append("[" + _APOSTROPHES + "]")
        elif ch in _REGEX_SPECIAL:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _like(text: str) -> str:
    """Text → a LIKE pattern fragment: ``*`` = any characters, %, _ and \\ literal."""
    out = []
    for ch in text:
        if ch == "*":
            out.append("%")
        elif ch in "%_\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _words(text: str) -> list[str]:
    """The words of a criterion; punctuation at the edges of a word is dropped ("(516)" → "516")."""
    out = []
    for w in text.split():
        w = re.sub(r"^[^\w*]+|[^\w*]+$", "", w)
        if w.strip("*"):
            out.append(w)
    return out


def normalize(text: str) -> str:
    """The criterion with its range operator spelled ``...``: ``…`` (one character) and ``..`` mean the same. Four or
    more dots are refused (a typo: which part is the range?). Text in quotes is left as it is."""
    t = text.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t
    t = t.replace("…", "...")
    if re.search(r"\.{4,}", t):
        raise CriterionError("Type the range with three dots, once (e.g. 1/1/2025...1/1/2026).")
    return re.sub(r"(?<!\.)\.\.(?!\.)", "...", t)


def _range(t: str, what: str = "a...m") -> tuple[str, str]:
    """The two ends of ``a...b``; one of them may be "" (an open end: ``...b`` up to b, ``a...`` from a on)."""
    a, b = (x.strip() for x in t.split("...", 1))
    if not a and not b:
        raise CriterionError(f"Type a value before or after ... ({what}).")
    if "..." in b:
        raise CriterionError(f"Use ... once: {what}.")
    return a, b


def _backwards(a, b, t: str) -> None:
    """Refuse a range whose first end is after its second (it would find nothing, silently)."""
    if a > b:
        raise CriterionError(f"The range {t} goes backwards: put the earlier (smaller) value first.")


def text_condition(expr: sql.Composable, text: str, p: Params) -> sql.Composable:
    """Condition for a text expression (see "Text fields" in the module docstring). ``text`` is non-empty."""
    t = text.strip()
    _check_supported(t)
    if t == "=":
        return sql.SQL("({e} IS NULL OR {e} = '')").format(e=expr)
    if t == "*":
        return sql.SQL("({e} IS NOT NULL AND {e} <> '')").format(e=expr)
    if p.collation:  # case-insensitive matching of accented letters (see Params)
        expr = sql.SQL("({} COLLATE {})").format(expr, sql.Identifier(p.collation))
    if t.startswith("=="):
        v = t[2:].strip()
        if not v:
            raise CriterionError("Type the exact value after ==.")
        if "*" in v or any(a in v for a in _APOSTROPHES):
            return sql.SQL("{} ~* {}").format(expr, p("^" + _regex(v) + "$"))
        return sql.SQL("lower({}) = lower({})").format(expr, p(v))
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        v = t[1:-1]
        if not v:
            raise CriterionError('Type some text between the quotes.')
        return sql.SQL("{} ILIKE {}").format(expr, p("%" + _like(v) + "%"))
    for op in ("<=", ">=", "<", ">"):
        if t.startswith(op):
            v = t[len(op):].strip()
            if not v:
                raise CriterionError(f"Type a value after {op}.")
            return sql.SQL("lower({}) {} lower({})").format(expr, sql.SQL(op), p(v))
    if "..." in t:
        a, b = _range(t)
        if not b:
            return sql.SQL("lower({}) >= lower({})").format(expr, p(a))
        # The upper end takes in everything starting with it: a...m is A through M, Miller included.
        upto = sql.SQL("(lower({e}) <= lower({b}) OR {e} ILIKE {pre})").format(e=expr, b=p(b), pre=p(_like(b) + "%"))
        if not a:
            return upto
        if a.isascii() and b.isascii():  # accented letters sort by the database's rules, not Python's
            _backwards(a.lower(), b.lower(), t)
        return sql.SQL("(lower({}) >= lower({}) AND {})").format(expr, p(a), upto)
    whole = t.startswith("=")
    words = _words(t[1:] if whole else t)
    if not words:
        raise CriterionError(f"'{t}' has no letters or digits to search for (put it in quotes to search for it "
                             "exactly).")
    parts = []
    for w in words:
        pattern = "\\m" + _regex(w) + ("\\M" if whole and not w.endswith("*") else "")
        parts.append(sql.SQL("{} ~* {}").format(expr, p(pattern)))
    return _and(parts)


def phone_condition(text: str, p: Params) -> sql.Composable:
    """Condition on the record's phone numbers (see "Phone" in the module docstring)."""
    raw = text.strip()
    t = raw if raw in ("=", "*") else raw.lstrip("=").strip()
    _check_supported(t)
    has = sql.SQL("EXISTS (SELECT 1 FROM record_phones rp WHERE rp.record_id = r.id")
    if t == "=":
        return sql.SQL("NOT ") + has + sql.SQL(")")
    if t == "*":
        return has + sql.SQL(")")
    if "..." in t or t.startswith(("<", ">")):
        raise CriterionError("phone numbers can't be searched by range or comparison; type the number, part of it "
                             "(5550100) or its start (516*).")
    # A typed country code: "+1 …", "1-…", "1 …", "(+1)…". US numbers never start with 1, so it can't be part of one.
    country = bool(re.match(r"^\(?\+1\)?|^1[\s\-.(]", t))
    if "*" in t:
        pattern = "".join("%" if ch == "*" else ch for ch in t if ch.isdigit() or ch == "*")
        # Anchored at the start of the number: a leading 1 is always the country code.
        if pattern.startswith("1"):
            pattern = pattern[1:]
        if not pattern.strip("%"):
            raise CriterionError("Type the digits of the phone number.")
        return has + sql.SQL(" AND rp.phone LIKE {})").format(p(pattern))
    digits = re.sub(r"\D", "", t)
    if not digits:
        raise CriterionError("Type the digits of the phone number.")
    if digits[0] == "1" and (len(digits) == 11 or country):
        digits = digits[1:]
    if not digits:
        raise CriterionError("Type the digits after +1.")
    if len(digits) == 10:
        return has + sql.SQL(" AND rp.phone = {})").format(p(digits))
    if len(digits) > 10:
        raise CriterionError(f"'{t}' has more digits than a phone number.")
    if country:  # "+1 516" = numbers starting with 516
        return has + sql.SQL(" AND rp.phone LIKE {})").format(p(digits + "%"))
    return has + sql.SQL(" AND rp.phone LIKE {})").format(p("%" + digits + "%"))


def _number(text: str) -> Decimal:
    """A typed number ("$1,200.50" → 1200.50, "#12" → 12); CriterionError when it isn't one."""
    s = re.sub(r"[,$\s]", "", text).lstrip("#")
    if not s:
        raise CriterionError("Type a number.")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise CriterionError(f"'{text.strip()}' isn't a number.") from None
    if not d.is_finite():
        raise CriterionError(f"'{text.strip()}' isn't a number.")
    if d and not -1000 <= d.adjusted() <= 1000:   # 1e1000000 would be an error in the database, not a search
        raise CriterionError(f"'{text.strip()}' is too large or too small.")
    return d


def number_condition(expr: sql.Composable, text: str, p: Params) -> sql.Composable:
    """Condition for a number expression (see "Numbers" in the module docstring)."""
    t = text.strip()
    _check_supported(t)
    if t == "=":
        return sql.SQL("{} IS NULL").format(expr)
    if t == "*":
        return sql.SQL("{} IS NOT NULL").format(expr)
    for op in ("<=", ">=", "<", ">"):
        if t.startswith(op):
            return sql.SQL("{} {} {}").format(expr, sql.SQL(op), p(_number(t[len(op):])))
    if "..." in t:
        sa, sb = _range(t, "10...20")
        if not sb:
            return sql.SQL("{} >= {}").format(expr, p(_number(sa)))
        if not sa:
            return sql.SQL("{} <= {}").format(expr, p(_number(sb)))
        a, b = _number(sa), _number(sb)
        _backwards(a, b, t)
        return sql.SQL("{} BETWEEN {} AND {}").format(expr, p(a), p(b))
    return sql.SQL("{} = {}").format(expr, p(_number(t.lstrip("="))))


@dataclass(frozen=True)
class DateToken:
    """A typed date: a period [start, end) (a day, month, year or minute), or a month (and day) in any year."""

    start: datetime | None = None
    end: datetime | None = None
    month: int | None = None   # month/day in any year (start/end are None then)
    day: int | None = None


def _period(y: int, m: int | None = None, d: int | None = None) -> DateToken:
    """The day, month or year as a [start, end) period; CriterionError for impossible dates and for years the
    app can't search (before 1 or from 9999 on)."""
    shown = f"{m}/{d}/{y}" if d is not None else f"{m}/{y}" if m is not None else str(y)
    if not 1 <= y <= 9998:
        raise CriterionError(f"{shown}: the year must be between 1 and 9998.")
    try:
        if d is not None:
            s = datetime(y, m, d)
            return DateToken(s, s + timedelta(days=1))
        if m is not None:
            s = datetime(y, m, 1)
            return DateToken(s, datetime(y + (m == 12), m % 12 + 1, 1))
        return DateToken(datetime(y, 1, 1), datetime(y + 1, 1, 1))
    except (ValueError, OverflowError):
        raise CriterionError(f"{shown} isn't a real date.") from None


def _year(text: str, today: date) -> int:
    """A typed year; two digits are 20yy unless that is more than a year ahead, then 19yy (as the converter)."""
    y = int(text)
    if len(text) <= 2:
        y += 2000
        if y > today.year + 1:
            y -= 100
    return y


def _month_day(m: int, d: int | None) -> DateToken:
    """Month (and day) in any year, checked."""
    if not 1 <= m <= 12 or (d is not None and not 1 <= d <= calendar.monthrange(2000, m)[1]):
        raise CriterionError(f"{m}/{d if d is not None else '*'} isn't a real month/day.")
    return DateToken(month=m, day=d)


def date_token(text: str, today: date | None = None, with_time: bool = False) -> DateToken:
    """Read one typed date (see "Dates" in the module docstring).

    ``with_time``: the field holds date-times, so a typed time ("10/9/2026 3:45 PM") means that minute; for a
    date field the time is ignored (the day).
    """
    today = today or date.today()
    s = text.strip()
    if s == "//":
        return _period(today.year, today.month, today.day)
    if m := re.fullmatch(r"\d{4}", s):
        return _period(int(s))
    if m := re.fullmatch(r"(\d{1,2})[/.\-](\d{4})", s):
        return _period(int(m[2]), int(m[1]))
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", s):
        return _period(int(m[1]), int(m[2]))
    if m := re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})", s):
        return _period(_year(m[3], today), int(m[1]), int(m[2]))
    if m := re.fullmatch(r"(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})", s):
        return _period(int(m[1]), int(m[2]), int(m[3]))
    if m := re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})", s):
        return _month_day(int(m[1]), int(m[2]))
    if m := re.fullmatch(r"(\d{1,2})[/.\-]\*", s):
        return _month_day(int(m[1]), None)
    words = s.lower().replace(",", " ").replace(".", " ").split()
    if len(words) == 1 and words[0] in MONTHS:
        return _month_day(MONTHS[words[0]], None)
    if len(words) == 2 and words[0] in MONTHS and re.fullmatch(r"\d{4}", words[1]):
        return _period(int(words[1]), MONTHS[words[0]])
    if len(words) == 2 and words[0] in MONTHS and re.fullmatch(r"\d{1,2}(st|nd|rd|th)?", words[1]):
        return _month_day(MONTHS[words[0]], int(re.sub(r"\D", "", words[1])))
    try:
        dt, has_time = dateparse.parse(s, today=today, convert_zones=False)
    except dateparse.DateError:
        raise CriterionError(f"'{s}' isn't a date (try 10/9/2026, 10/2026, 2026 or 10/9).") from None
    if has_time and with_time:
        minute = dt.replace(second=0, microsecond=0)
        if not 1 <= minute.year <= 9998:
            raise CriterionError(f"{s}: the year must be between 1 and 9998.")
        return DateToken(minute, minute + timedelta(minutes=1))
    return _period(dt.year, dt.month, dt.day)


def _md_expr(expr: sql.Composable) -> sql.Composable:
    """month*100 + day of a date expression, for month/day comparisons."""
    return sql.SQL("(EXTRACT(MONTH FROM {e}) * 100 + EXTRACT(DAY FROM {e}))").format(e=expr)


def _known_year(expr: sql.Composable, no_year: int | None, p: Params) -> sql.Composable | None:
    """"The year is known" for birthday-type fields (not the no-year value 1600), else None."""
    return sql.SQL("EXTRACT(YEAR FROM {}) <> {}").format(expr, p(no_year)) if no_year is not None else None


def date_condition(expr: sql.Composable, text: str, p: Params, with_time: bool = False,
                   no_year: int | None = None) -> sql.Composable:
    """Condition for a date or date-time expression (see "Dates" in the module docstring).

    ``no_year``: the field's year for "no year" (1600 for birthday/anniversary): such dates are only found by
    month/day criteria or by asking for that year itself.
    """
    t = text.strip()
    _check_supported(t)
    if t == "=":
        return sql.SQL("{} IS NULL").format(expr)
    if t == "*":
        return sql.SQL("{} IS NOT NULL").format(expr)

    def token(s: str) -> DateToken:
        return date_token(s, p.today, with_time)

    def with_year(cond: sql.Composable, tok_years: set[int]) -> sql.Composable:
        known = _known_year(expr, no_year, p)
        if known is None or no_year in tok_years:
            return cond
        return sql.SQL("({} AND {})").format(cond, known)

    for op in ("<=", ">=", "<", ">"):
        if t.startswith(op):
            if not t[len(op):].strip():
                raise CriterionError(f"Type a date after {op}.")
            tok = token(t[len(op):])
            if tok.month is not None:
                raise CriterionError(f"{op} needs a full date, month or year (e.g. {op}10/1/2026).")
            bound = {"<": tok.start, "<=": tok.end, ">": tok.end, ">=": tok.start}[op]
            cmp = {"<": "<", "<=": "<", ">": ">=", ">=": ">="}[op]
            return with_year(sql.SQL("{} {} {}").format(expr, sql.SQL(cmp), p(bound)), set())
    if "..." in t:
        sa, sb = _range(t, "1/1/2026...3/31/2026")
        if not sa or not sb:  # an open end: up to and including b, or from a on
            tok = token(sa or sb)
            if tok.month is not None:  # month/day in any year: from January 1 or up to December 31
                lo = tok.month * 100 + (tok.day or 1) if sa else 101
                hi = tok.month * 100 + (tok.day or 31) if sb else 1231
                return sql.SQL("{} BETWEEN {} AND {}").format(_md_expr(expr), p(lo), p(hi))
            cond = (sql.SQL("{} >= {}").format(expr, p(tok.start)) if sa
                    else sql.SQL("{} < {}").format(expr, p(tok.end)))
            return with_year(cond, set())
        a, b = token(sa), token(sb)
        if (a.month is None) != (b.month is None):
            raise CriterionError("Both ends of a range must have a year, or neither (10/1...10/31).")
        if a.month is not None:
            lo, hi = a.month * 100 + (a.day or 1), b.month * 100 + (b.day or 31)
            md = _md_expr(expr)
            if lo <= hi:
                return sql.SQL("{} BETWEEN {} AND {}").format(md, p(lo), p(hi))
            return sql.SQL("({md} >= {lo} OR {md} <= {hi})").format(md=md, lo=p(lo), hi=p(hi))
        if a.start >= b.end:  # the first end starts after the second is over: nothing could match
            raise CriterionError(f"The range {t} goes backwards: put the earlier (smaller) value first.")
        cond = sql.SQL("({e} >= {s} AND {e} < {n})").format(e=expr, s=p(a.start), n=p(b.end))
        return with_year(cond, {a.start.year} if a.start.year == b.start.year else set())
    tok = token(t.lstrip("="))
    if tok.month is not None:
        if tok.day is None:
            return sql.SQL("EXTRACT(MONTH FROM {}) = {}").format(expr, p(tok.month))
        return sql.SQL("{} = {}").format(_md_expr(expr), p(tok.month * 100 + tok.day))
    cond = sql.SQL("({e} >= {s} AND {e} < {n})").format(e=expr, s=p(tok.start), n=p(tok.end))
    return with_year(cond, {tok.start.year})


def bool_condition(expr: sql.Composable, text: str, p: Params) -> sql.Composable:
    """Condition for a true/false expression (see "True / false" in the module docstring)."""
    t = text.strip().lower()
    if t == "=":
        return sql.SQL("{} IS NULL").format(expr)
    if t == "*":
        return sql.SQL("{} IS NOT NULL").format(expr)
    v = t.lstrip("=").strip()
    if v in TRUE_TOKENS:
        return sql.SQL("{} = true").format(expr)
    if v in FALSE_TOKENS:
        return sql.SQL("{} = false").format(expr)
    raise CriterionError(f"'{text}' isn't true or false (yes/no, y/n, 1/0).")


def business_condition(text: str, p: Params) -> sql.Composable:
    """Condition on the record's business (see "Business" in the module docstring)."""
    t = text.strip()
    if t in ("*", "="):
        return TRUE if t == "*" else FALSE
    bid = sql.SQL("b.id")
    if t.startswith(("<", ">")) or "..." in t:
        cond = text_condition(bid, t, p)       # ranges and comparisons are about the IDs
    elif "*" in t:
        v = t.lstrip("=")
        cond = sql.SQL("(b.id ILIKE {} OR {})").format(p(_like(v)), _coalesce(text_condition(sql.SQL("b.name"), t, p)))
    else:
        # An exact ID wins: "1001" is business 1001, not also "1001 Nights Cafe". Otherwise the name.
        v = t.lstrip("=").strip()
        cond = sql.SQL("(CASE WHEN EXISTS (SELECT 1 FROM businesses x WHERE lower(x.id) = lower({v1})) "
                       "THEN lower(b.id) = lower({v2}) ELSE {name} END)").format(
            v1=p(v), v2=p(v), name=_coalesce(text_condition(sql.SQL("b.name"), t, p)))
    return sql.SQL("r.business_id IN (SELECT b.id FROM businesses b WHERE {})").format(_coalesce(cond))


def condition(f: FindField, text: str, p: Params) -> sql.Composable:
    """The SQL condition for criterion ``text`` on field ``f`` (wrapped in COALESCE, see the module docstring).

    Raises :class:`CriterionError` with a message naming the field when the text can't be read.
    """
    try:
        return _coalesce(_condition(f, normalize(text), p))
    except CriterionError as exc:
        raise CriterionError(f"{f.label}: {exc}") from None


def _condition(f: FindField, text: str, p: Params) -> sql.Composable:
    """:func:`condition` without the wrapping."""
    t = text.strip()
    col = sql.SQL("r.{}").format(sql.Identifier(f.column)) if f.column else None
    if f.kind == "text":
        return text_condition(col, t, p)
    if f.kind == "issue":
        # A record with a usable number has an empty problem (''): read as no value, so comparisons and ranges
        # only find records that have a problem, as with any other empty field.
        return text_condition(sql.SQL("NULLIF(r.phone_issue, '')"), t, p)
    if f.kind == "phone":
        return phone_condition(t, p)
    if f.kind == "number":
        return number_condition(col, t, p)
    if f.kind == "date":
        return date_condition(col, t, p, with_time=f.datetime, no_year=f.no_year_value)
    if f.kind == "bool":
        return bool_condition(col, t, p)
    if f.kind == "name":
        return text_condition(sql.SQL("concat_ws(' ', r.first_name, r.last_name)"), t, p)
    if f.kind in ("first", "last"):
        return name_condition(f.kind, t, p)
    if f.kind == "any":
        return text_condition(sql.SQL("r.search_text"), t, p)
    if f.kind == "column":
        return text_condition(sql.SQL("(r.source ->> {})").format(p(f.column)), t, p)
    if f.kind == "business":
        return business_condition(t, p)
    if f.kind == "service":
        if t in ("*", "="):
            return TRUE if t == "*" else FALSE
        return sql.SQL("r.service_id IN (SELECT s.id FROM services s WHERE {})").format(
            _coalesce(text_condition(sql.SQL("s.name"), t, p)))
    if f.kind == "tag":
        any_tag = sql.SQL("EXISTS (SELECT 1 FROM tag_records tr WHERE tr.record_id = r.id)")
        if t == "*":
            return any_tag
        if t == "=":
            return sql.SQL("NOT ") + any_tag
        return sql.SQL("EXISTS (SELECT 1 FROM tag_records tr JOIN tags t ON t.id = tr.tag_id "
                       "WHERE tr.record_id = r.id AND {})").format(_coalesce(text_condition(sql.SQL("t.name"), t, p)))
    if f.kind == "file":
        return sql.SQL("r.import_id IN (SELECT i.id FROM imports i WHERE {})").format(
            _coalesce(text_condition(sql.SQL("i.file_name"), t, p)))
    if f.kind == "job":
        return sql.SQL("r.import_id IN (SELECT i.id FROM imports i WHERE {})").format(
            _coalesce(number_condition(sql.SQL("i.job_id"), t, p)))
    if f.kind == "created":
        # When the record was stored, on the app's clock (APP_TIMEZONE), like the times shown on the pages.
        local = sql.SQL("(r.created_at AT TIME ZONE {})").format(p(p.timezone))
        return date_condition(local, t, p, with_time=True)
    raise CriterionError(f"can't be searched (unknown kind {f.kind})")
