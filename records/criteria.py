"""One find criterion (what was typed for one field of a find request) → an SQL condition on ``records r``.

Overview
--------
The syntax follows FileMaker, which the team knows; the differences are marked ≠ below. Every condition is
wrapped in ``COALESCE(..., false)``, so an empty value never makes a request "unknown" (that would break Omit).
Values are passed as query parameters (:class:`Params`), never pasted into the SQL. Upper and lower case never
matter, also for accented letters (Åberg, MÓR): the database's own setting is used when it handles them, otherwise
an explicit collation (records.db.Database.setup picks it).

Text fields (names, email, address, city, Any field, Column: …, Service, Tag, File name)
    ``smith``            a word starting with "smith" (Smith, Smithson; not Goldsmith); case doesn't matter
    ``john smith``       both words, in any order (each must start a word)
    ``=smith``           the whole word "smith" (Smith, not Smithson)
    ``==john smith``     the whole field is exactly "john smith"
    ``"main st"``        contains this exact text anywhere (also inside words, punctuation included)
    ``sm*th``            ``*`` = any characters (≠ ``@`` and ``#`` are ordinary characters here, so email
                         addresses and "Apt #4" can be typed as they are)
    punctuation at the start or end of a word is ignored, as in FileMaker (``c++`` searches for ``c``,
    ``(516)`` for ``516``): put the text in quotes to search for it exactly; ’ and ' are the same
    ``=``                the field is empty;   ``*``  the field has any value
    ``<m`` ``>=m`` ``a...m``   alphabetical comparisons and ranges (the earlier value first)

Business
    ``1001``             the business with this ID, when there is one; otherwise businesses whose name has a word
                         starting with it · ``krave``  by name · ``100*``  IDs (or names) starting with 100
    ``1000...1999`` ``<2000``   ranges and comparisons of the IDs (as text)

Phone
    ``5165550100``, ``(516) 555-0100``, ``+1 516 555 0100``   that number (any formatting; digits are compared)
    ``5550100``          numbers containing these digits;   ``516*`` or ``+1 516*``  numbers starting with 516
    ``=``                no usable number;   ``*``  has a number

Numbers (total visits, total spent, Import # as on the Imports page, Record #, File row)
    ``5`` ``>5`` ``<=100`` ``10...20`` (inclusive, the smaller first) ``=`` (empty) ``*`` (any value);
    ``$1,200.50`` and ``#12`` are fine

Dates (activity dates, first visit, birthday, anniversary, Imported on)
    ``10/9/2026``  that day (date-times: the whole day) · ``10/2026`` that month · ``2026`` that year
    ``2026-10-09`` ``Oct 9 2026`` ``October 2026`` also work · ``//`` today (in the app's time zone)
    ``10/9/2026 3:45 PM`` that minute (date-time fields); ``>10/9/2026 9:00 AM`` after it
    ``1/1/2026...3/31/2026`` from the start of the first to the end of the second (inclusive, the earlier first)
    ``<10/1/2026`` before that day · ``<=10/1/2026`` up to and including it · ``>`` ``>=`` likewise
    ``10/9`` or ``Oct 9`` (≠ month and day, any year: birthdays in any year) · ``10/*`` or ``October``: that
    month in any year · ``12/15...1/15``: month/day range, across the new year
    ``=`` empty · ``*`` any value. Birthdays and anniversaries without a year are stored with year 1600: month/day
    criteria (``9/12``, ``10/*``) and ``1600`` find them; years, ranges and before/after with a year never do
    (``<1/1/1950`` doesn't take "year unknown" for "born in 1600").

True / false (consent)
    ``true`` / ``yes`` / ``y`` / ``1`` · ``false`` / ``no`` / ``n`` / ``0`` · ``=`` empty · ``*`` any value

Not supported (≠): ``!`` (duplicates), ``?`` (invalid dates), ``~`` (relaxed search): they give a message.
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


class CriterionError(ValueError):
    """What was typed can't be read for this field; the message says why (shown to the person)."""


class Params:
    """Collects query parameter values and hands out named placeholders (%(p0)s, %(p1)s, …).

    Also carries what every condition needs to know: ``collation`` (None = the database's own; otherwise the
    collation that makes upper/lower case of accented letters match, see records.db), ``timezone`` (the app's,
    for Imported on) and ``today`` (in that time zone, for ``//``).
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


def _range(t: str, what: str = "a...m") -> tuple[str, str]:
    """The two ends of ``a...b``, both required."""
    a, b = (x.strip() for x in t.split("...", 1))
    if not a or not b:
        raise CriterionError(f"A range needs a value on both sides of ... ({what}).")
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
        if a.isascii() and b.isascii():  # accented letters sort by the database's rules, not Python's
            _backwards(a.lower(), b.lower(), t)
        return sql.SQL("lower({}) BETWEEN lower({}) AND lower({})").format(expr, p(a), p(b))
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
        a, b = (_number(x) for x in _range(t, "10...20"))
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
    """The day, month or year as a [start, end) period; CriterionError for impossible dates."""
    try:
        if d is not None:
            s = datetime(y, m, d)
            return DateToken(s, s + timedelta(days=1))
        if m is not None:
            s = datetime(y, m, 1)
            return DateToken(s, datetime(y + (m == 12), m % 12 + 1, 1))
        return DateToken(datetime(y, 1, 1), datetime(y + 1, 1, 1))
    except ValueError:
        raise CriterionError(f"{m}/{d}/{y} isn't a real date.") from None


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
        a, b = token(sa), token(sb)
        if (a.month is None) != (b.month is None):
            raise CriterionError("Both ends of a range must have a year, or neither (10/1...10/31).")
        if a.month is not None:
            lo, hi = a.month * 100 + (a.day or 1), b.month * 100 + (b.day or 31)
            md = _md_expr(expr)
            if lo <= hi:
                return sql.SQL("{} BETWEEN {} AND {}").format(md, p(lo), p(hi))
            return sql.SQL("({md} >= {lo} OR {md} <= {hi})").format(md=md, lo=p(lo), hi=p(hi))
        _backwards(a.start, b.start, t)
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
        return _coalesce(_condition(f, text, p))
    except CriterionError as exc:
        raise CriterionError(f"{f.label}: {exc}") from None


def _condition(f: FindField, text: str, p: Params) -> sql.Composable:
    """:func:`condition` without the wrapping."""
    t = text.strip()
    col = sql.SQL("r.{}").format(sql.Identifier(f.column)) if f.column else None
    if f.kind == "text":
        return text_condition(col, t, p)
    if f.kind == "issue":
        if t == "=":
            return sql.SQL("r.phone_issue = ''")
        return text_condition(col, t, p)
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
    if f.kind == "imported":
        local = sql.SQL("(i.completed_at AT TIME ZONE {})").format(p(p.timezone))
        return sql.SQL("r.import_id IN (SELECT i.id FROM imports i WHERE {})").format(
            _coalesce(date_condition(local, t, p, with_time=True)))
    raise CriterionError(f"can't be searched (unknown kind {f.kind})")
