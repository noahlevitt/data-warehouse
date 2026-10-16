"""The main app's merchants and locations, read from its ClickHouse or from a file exported there (1.6.0).

Customer Records doesn't make up its own businesses: every import is for a **location** of a **merchant** of the
main app, with the main app's IDs, so the records and tags can be connected to it. Both come from the main app's
ClickHouse, where ``pg.merchants`` and ``pg.locations`` show the main app's own database. :data:`QUERY` reads them
in one list, one row per location plus one per merchant without a location:

    merchant_id, merchant_name, merchant_status, location_id, location_name, location_address, location_kind

Two ways in, both ending in :meth:`records.store.RecordStore.load_merchants`:

* **Automatically** (:class:`MerchantRefresher`), when ``CLICKHOUSE_URL``, ``CLICKHOUSE_USER`` and
  ``CLICKHOUSE_PASSWORD`` are set (an app user with SELECT on those two tables): at start, then every
  ``MERCHANTS_REFRESH_MINUTES`` (60), in the background, and from the Merchants page's button.
* **From a file** (:func:`rows_from_file`): run :data:`QUERY` in the ClickHouse console, export the result as CSV,
  and upload it on the Merchants page. That works with read access only, until the app has its own user.

Merchant IDs are whole numbers in the main app and are kept as their digits (``"63"``); location IDs are UUIDs,
kept lower-case. A merchant without a location is kept (it shows on the Merchants page) but can't be imported for.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Iterable

log = logging.getLogger(__name__)

QUERY = """SELECT m.id AS merchant_id, m.name AS merchant_name, m.status AS merchant_status,
       l.id AS location_id, l.name AS location_name, l.address AS location_address, l.kind AS location_kind
FROM pg.merchants AS m
LEFT JOIN pg.locations AS l ON l.merchant_id = m.id
ORDER BY m.id, l.name
SETTINGS join_use_nulls = 1"""

COLUMNS = ["merchant_id", "merchant_name", "merchant_status", "location_id", "location_name", "location_address",
           "location_kind"]
REQUIRED = ["merchant_id", "merchant_name", "location_id"]
# How an empty value (SQL NULL) looks in an export: ClickHouse writes \N in CSV and TSV (null in JSON: None here).
NULLS = {"", "\\N"}
# A merchant ID as the main app has it (a whole number), also as a spreadsheet may have saved it (63.0).
ID_RE = re.compile(r"^([0-9]+)(?:\.0+)?$")


class MerchantListError(ValueError):
    """The list couldn't be read or fetched; the message says why and what to do."""


def _text(value) -> str:
    """A cell as trimmed text, '' for an empty value however the export wrote it."""
    t = "" if value is None else str(value).strip()
    return "" if t in NULLS else t


def parse_rows(rows: Iterable[dict], start: int = 1) -> tuple[list[dict], list[dict]]:
    """(merchants, locations) from rows of :data:`COLUMNS` (from ClickHouse or a file). Raises MerchantListError.

    Merchants: {"id", "name", "status"}; locations: {"id" (a UUID, lower-case), "merchant_id", "name", "address",
    "kind"}. ``start``: the number of the first row in messages (2 for a file: its row 1 is the header). Rows with
    nothing in them (a spreadsheet's trailing ",,,,") are skipped; a merchant or location listed twice must say the
    same both times.
    """
    merchants: dict[str, dict] = {}
    locations: dict[str, dict] = {}
    for n, raw in enumerate(rows, start=start):
        if not isinstance(raw, dict):
            raise MerchantListError(f"Row {n} isn't a row of merchants and locations: {str(raw)[:100]}")
        row = {str(k).strip().lower(): v for k, v in raw.items() if k is not None}
        if not any(_text(v) for v in row.values()):
            continue
        mid = _text(row.get("merchant_id"))
        match = ID_RE.match(mid) if mid.isascii() else None
        if not match:
            raise MerchantListError(f"Row {n}: the merchant ID {mid!r} isn't a whole number.")
        mid = str(int(match.group(1)))
        name = _text(row.get("merchant_name"))
        if not name:
            raise MerchantListError(f"Row {n}: merchant {mid} has no name.")
        merchant = {"id": mid, "name": name, "status": _text(row.get("merchant_status")).lower() or "active"}
        if merchants.setdefault(mid, merchant) != merchant:
            raise MerchantListError(f"Row {n}: merchant {mid} is listed twice with a different name or status.")
        lid = _text(row.get("location_id"))
        if lid:
            try:
                lid = str(uuid.UUID(lid))
            except ValueError:
                raise MerchantListError(f"Row {n}: the location ID {lid!r} isn't a location ID of the main app "
                                        "(a UUID).") from None
            location = {"id": lid, "merchant_id": mid, "name": _text(row.get("location_name")),
                        "address": _text(row.get("location_address")),
                        "kind": _text(row.get("location_kind")).lower()}
            if locations.setdefault(lid, location) != location:
                raise MerchantListError(f"Row {n}: location {lid} is listed twice with different details.")
    if not merchants:
        raise MerchantListError("The list has no merchants.")
    return list(merchants.values()), list(locations.values())


def rows_from_file(data: bytes) -> list[dict]:
    """The rows of a CSV (or tab-separated) export of :data:`QUERY`, with its header row. Raises MerchantListError.

    Also as a spreadsheet saves it: UTF-16 ("Unicode text"), Windows encoding, semicolons, blank lines and Excel's
    ``sep=,`` line before the header. Give the rows to :func:`parse_rows` with ``start=2``.
    """
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    lines = text.splitlines(keepends=True)
    while lines and (not lines[0].strip() or lines[0].strip().lower().startswith("sep=")):
        lines.pop(0)
    text = "".join(lines)
    first = lines[0] if lines else ""
    delimiter = max([",", "\t", ";"], key=first.count) if first else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    header = [(h or "").strip().lower() for h in reader.fieldnames or []]
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise MerchantListError(
            f"The file has no {', '.join(missing)} column. Export the result of the query shown on this page, with "
            "its column names (CSV).")
    return list(reader)


def clickhouse_url(url: str) -> str:
    """The HTTPS address of a ClickHouse service: a bare host name gets ``https://`` and ClickHouse Cloud's port."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = f"https://{url}" + ("" if ":" in url else ":8443")
    return url + "/"


def fetch_clickhouse(url: str, user: str, password: str, timeout: float = 30) -> list[dict]:
    """Run :data:`QUERY` on the main app's ClickHouse (HTTP interface) and return its rows. Raises MerchantListError.

    The user name and password go in ClickHouse's own headers, never in the address, so they don't end up in logs.
    """
    request = urllib.request.Request(
        clickhouse_url(url), data=(QUERY + "\nFORMAT JSONEachRow").encode(), method="POST",
        headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password, "Content-Type": "text/plain"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip().splitlines()
        raise MerchantListError(f"ClickHouse refused the query (HTTP {exc.code}): "
                                f"{detail[0][:300] if detail else exc.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise MerchantListError(f"Can't reach ClickHouse at {clickhouse_url(url)}: {reason}") from None
    rows = []
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # ClickHouse reports an error that happens after it started sending rows as a line of text.
            raise MerchantListError(f"ClickHouse's answer wasn't the expected list of rows: {line.strip()[:300]}") \
                from None
    return rows


class MerchantRefresher:
    """Keeps Customer Records' merchants and locations those of the main app's ClickHouse (one per server process).

    :meth:`maybe_refresh` is called on every page run and starts a background refresh when the last one is older
    than ``every_minutes`` (0 = only by hand); :meth:`refresh` runs one now (the Merchants page's button). Without
    ClickHouse settings it does nothing. The last error is kept for the Merchants page.
    """

    def __init__(self, store, url: str, user: str, password: str, every_minutes: int = 60):
        self.store = store
        self.url, self.user, self.password = url, user, password
        self.every = every_minutes * 60
        self.last_try = 0.0
        self.last_error = ""
        self._lock = threading.Lock()
        self._running = False

    @property
    def configured(self) -> bool:
        """True when the ClickHouse address, user and password are all set."""
        return bool(self.url and self.user and self.password)

    def refresh(self, username: str = "") -> dict:
        """Fetch the lists now and load them (RecordStore.load_merchants). Raises MerchantListError (or StoreError)."""
        self.last_try = time.monotonic()
        try:
            merchants, locations = parse_rows(fetch_clickhouse(self.url, self.user, self.password))
            summary = self.store.load_merchants(merchants, locations,
                                                "ClickHouse" + ("" if username else " (automatic)"), username)
        except ValueError as exc:  # MerchantListError, StoreError
            self.last_error = str(exc)
            raise
        except Exception as exc:  # anything else (the database…): said on the page too, as a MerchantListError
            self.last_error = f"Loading the merchants failed ({type(exc).__name__}: {exc})"
            raise MerchantListError(self.last_error) from exc
        self.last_error = ""
        return summary

    def maybe_refresh(self) -> None:
        """Start a background refresh if one is due (see the class docstring). Never raises."""
        if not self.configured or not self.every or self._running:
            return
        if self.last_try and time.monotonic() - self.last_try < self.every:
            return
        with self._lock:
            if self._running:
                return
            self._running = True
            self.last_try = time.monotonic()
        threading.Thread(target=self._background, name="merchant-refresh", daemon=True).start()

    def _background(self) -> None:
        try:
            self.refresh()
        except Exception as exc:  # noqa: BLE001 - shown on the Merchants page
            log.warning("Loading the merchants from ClickHouse failed: %s", exc)
        finally:
            self._running = False
