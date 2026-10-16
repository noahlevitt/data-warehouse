"""The main app's merchants and locations (records.merchants, 1.6.0): read from a file or from ClickHouse.

ClickHouse is stood in for by a small local HTTP server that answers like ClickHouse's HTTP interface.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from records import merchants as M

ROWS = [  # as ClickHouse returns QUERY (JSONEachRow): numbers as numbers, NULL as null, the address as JSON text
    {"merchant_id": 63, "merchant_name": "Flakowitz Corporate", "merchant_status": "active",
     "location_id": "0B1E5C3A-0000-4000-8000-000000000001", "location_name": "Boca Raton",
     "location_address": '{"zip": "33431", "city": "Boca Raton", "state": "FL", "street": "1 A St"}',
     "location_kind": "store"},
    {"merchant_id": 63, "merchant_name": "Flakowitz Corporate", "merchant_status": "active",
     "location_id": "0b1e5c3a-0000-4000-8000-000000000002", "location_name": "Delray", "location_address": None,
     "location_kind": "store"},
    {"merchant_id": 203, "merchant_name": "La Familia Deli & Grill CORPORATE", "merchant_status": "active",
     "location_id": None, "location_name": None, "location_address": None, "location_kind": None},
    {"merchant_id": 281, "merchant_name": "delete", "merchant_status": "archived", "location_id": None,
     "location_name": None, "location_address": None, "location_kind": None},
]


def test_rows_become_merchants_and_locations():
    """One merchant per ID (also when it has several locations), IDs as digits, location IDs lower-case, the address
    as one line, a merchant without a location kept, empty values as ''."""
    merchants, locations = M.parse_rows(ROWS)
    assert merchants == [{"id": "63", "name": "Flakowitz Corporate", "status": "active"},
                         {"id": "203", "name": "La Familia Deli & Grill CORPORATE", "status": "active"},
                         {"id": "281", "name": "delete", "status": "archived"}]
    assert locations == [
        {"id": "0b1e5c3a-0000-4000-8000-000000000001", "merchant_id": "63", "name": "Boca Raton",
         "address": "1 A St, Boca Raton, FL 33431", "kind": "store"},
        {"id": "0b1e5c3a-0000-4000-8000-000000000002", "merchant_id": "63", "name": "Delray", "address": "",
         "kind": "store"}]


UUID1 = "0b1e5c3a-0000-4000-8000-000000000001"
HEAD = "merchant_id,merchant_name,merchant_status,location_id,location_name,location_address,location_kind\n"


def test_a_file_exported_from_the_console():
    """CSV or tab-separated, with a BOM, NULL written as \\N or empty, IDs a spreadsheet saved as 63.0 / 63.00,
    upper-case UUIDs, a merchant named None (a name, not an empty value) and a spreadsheet's empty trailing row."""
    csv = ("﻿" + HEAD + f"63,Flakowitz Corporate,active,{UUID1.upper()},Boca Raton,\"1 A St, Boca\",store\n"
           "203,\"La Familia, CORPORATE\",active,\\N,\\N,\\N,\\N\n"
           "281.00,delete,archived,,,,\n"
           "1001,None,active,,,,\n"
           ",,,,,,\n").encode()
    merchants, locations = M.parse_rows(M.rows_from_file(csv), start=2)
    assert [m["id"] for m in merchants] == ["63", "203", "281", "1001"]
    assert merchants[1]["name"] == "La Familia, CORPORATE" and merchants[3]["name"] == "None"
    assert locations == [{"id": UUID1, "merchant_id": "63", "name": "Boca Raton", "address": "1 A St, Boca",
                          "kind": "store"}]
    tsv = f"merchant_id\tmerchant_name\tlocation_id\n63\tFlakowitz Corporate\t{UUID1}\n".encode()
    assert M.parse_rows(M.rows_from_file(tsv), start=2)[1][0]["id"] == UUID1


def test_the_main_apps_address_becomes_one_line():
    """The main app's address is JSON (a city may be null); in the console's CSV export its quotes are doubled.
    Text that isn't a JSON object stays as it is."""
    main_app = '{"zip": "11801", "city": "Hicksville", "state": "NY", "street": "25 North Newbridge Road"}'
    assert M.address_text(main_app) == "25 North Newbridge Road, Hicksville, NY 11801"
    assert M.address_text('{"zip": "11787", "city": null, "state": "NY", "street": "59 New York 111"}') \
        == "59 New York 111, NY 11787"
    assert M.address_text('{"street": "1 A St", "unit": "Suite 4", "city": "", "state": null, "zip": "33431"}') \
        == "1 A St, Suite 4, 33431"
    assert M.address_text('{"zip": null, "city": null, "state": null, "street": null}') == ""
    for kept in ["1 A St, Boca Raton", "{not json", '["1 A St"]', "", None, "\\N"]:
        assert M.address_text(kept) == ("" if kept in ("", None, "\\N") else kept)
    exported = HEAD + (f'63,Flakowitz Corporate,active,{UUID1},Hicksville,'
                       '"{""zip"": ""11801"", ""city"": ""Hicksville"", ""state"": ""NY"", '
                       '""street"": ""25 North Newbridge Road""}",store\n')
    assert M.parse_rows(M.rows_from_file(exported.encode()), start=2)[1][0]["address"] \
        == "25 North Newbridge Road, Hicksville, NY 11801"


def test_unusual_json_addresses():
    """Numbers, flags, coordinates, line breaks and escapes the database can't store; an address already parsed."""
    assert M.address_text('{"zip": 7410, "city": "Fair Lawn", "state": "NJ", "street": "1 Main St"}') \
        == "1 Main St, Fair Lawn, NJ 07410"
    assert M.address_text('{"zip": 11801.0, "state": "NY", "street": "1 Main", "verified": true, "lat": 40.7, '
                          '"lng": -73.5, "country": "US", "Address_Line2": "Apt 2", "x": NaN}') \
        == "1 Main, Apt 2, NY 11801"
    assert M.address_text('{"street": "1 Main\\nApt 2", "city": "Hicksville\\u0000", "zip": "11801"}') \
        == "1 Main Apt 2, Hicksville, 11801"
    assert M.address_text('{"street": "1 Main \\ud800St"}') == "1 Main ?St"
    assert M.address_text({"zip": "11801", "street": "1 Main"}) == "1 Main, 11801"
    deep = '{"a":' + "[" * 30000 + "]" * 30000 + "}"
    assert M.address_text(deep) == deep
    for kept in ["{Apt 4} 1 Main St", '{"zip": 01901}', '{"street": "1 A"}{"street": "2 B"}']:
        assert M.address_text(kept) == kept


def test_a_file_as_a_spreadsheet_saves_it():
    """UTF-16 ("Unicode text"), Excel's sep= line, blank lines before the header, Mac (\\r) and Windows line endings,
    semicolons in Windows encoding."""
    text = HEAD + f"63,Flakowitz Corporate,active,{UUID1},Boca Raton,,store\n"
    for data in [text.encode("utf-16"), ("sep=,\n" + text).encode(), ("\n\n" + text).encode(),
                 text.replace("\n", "\r").encode(), text.replace("\n", "\r\n").encode(),
                 text.replace(",", ";").replace("Boca Raton", "Bocà").encode("cp1252")]:
        merchants, locations = M.parse_rows(M.rows_from_file(data), start=2)
        assert merchants[0]["id"] == "63" and locations[0]["id"] == UUID1


def test_a_wrong_file_says_what_to_do():
    """Row numbers are the spreadsheet's (its row 1 is the header)."""
    with pytest.raises(M.MerchantListError, match="no merchant_id, merchant_name, location_id column"):
        M.rows_from_file(b"id,name,status\n63,Flakowitz,active\n")
    with pytest.raises(M.MerchantListError, match="Row 1: the merchant ID 'abc' isn't a whole number"):
        M.parse_rows([{"merchant_id": "abc", "merchant_name": "x", "location_id": ""}])
    with pytest.raises(M.MerchantListError, match="Row 3: the merchant ID '²' isn't a whole number"):
        M.parse_rows(M.rows_from_file((HEAD + "63,A,active,,,,\n²,B,active,,,,\n").encode()), start=2)
    with pytest.raises(M.MerchantListError, match="Row 2: the location ID 'not-a-uuid' isn't a location ID"):
        M.parse_rows(M.rows_from_file((HEAD + "63,A,active,not-a-uuid,,,\n").encode()), start=2)
    with pytest.raises(M.MerchantListError, match="Row 1: merchant 63 has no name"):
        M.parse_rows([{"merchant_id": 63, "merchant_name": None, "location_id": None}])
    with pytest.raises(M.MerchantListError, match="Row 2: merchant 63 is listed twice"):
        M.parse_rows([{"merchant_id": 63, "merchant_name": "A", "merchant_status": "active", "location_id": None},
                      {"merchant_id": 63, "merchant_name": "A", "merchant_status": "archived", "location_id": None}])
    with pytest.raises(M.MerchantListError, match="Row 2: location .* is listed twice"):
        M.parse_rows([{"merchant_id": 63, "merchant_name": "A", "location_id": UUID1},
                      {"merchant_id": 64, "merchant_name": "B", "location_id": UUID1}])
    limit = csv.field_size_limit(1000)  # Python's own limit; the converter raises it when loaded
    try:
        with pytest.raises(M.MerchantListError, match="couldn't be read as CSV .field larger than field limit"):
            M.rows_from_file((HEAD + "63,A,active,," + "x" * 2000 + ",,\n").encode())
    finally:
        csv.field_size_limit(limit)
    with pytest.raises(M.MerchantListError, match="Row 1 isn't a row of merchants and locations"):
        M.parse_rows([[1, 2, 3]])
    with pytest.raises(M.MerchantListError, match="no merchants"):
        M.parse_rows([])


def test_clickhouse_addresses():
    assert M.clickhouse_url("abc.us-east-1.aws.clickhouse.cloud") == "https://abc.us-east-1.aws.clickhouse.cloud:8443/"
    assert M.clickhouse_url("https://ch.example.com:8443/") == "https://ch.example.com:8443/"
    assert M.clickhouse_url("http://localhost:8123") == "http://localhost:8123/"


class FakeClickHouse(BaseHTTPRequestHandler):
    """Answers like ClickHouse's HTTP interface: the user and password in its headers, the query in the body."""

    seen: list[dict] = []
    status = 200
    tail = b""        # sent after the rows (ClickHouse reports an error that happens midway like this)

    def do_POST(self):  # noqa: N802 - http.server's name
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        FakeClickHouse.seen.append({"user": self.headers["X-ClickHouse-User"], "key": self.headers["X-ClickHouse-Key"],
                                    "body": body, "path": self.path})
        if FakeClickHouse.status != 200:
            self.send_response(FakeClickHouse.status)
            self.end_headers()
            self.wfile.write(b"Code: 516. DB::Exception: app: Authentication failed. (AUTHENTICATION_FAILED)\n")
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write("\n".join(json.dumps(r) for r in ROWS).encode() + b"\n" + FakeClickHouse.tail)

    def log_message(self, *args):
        pass


@pytest.fixture
def clickhouse():
    """The URL of a fake ClickHouse answering QUERY with ROWS."""
    FakeClickHouse.seen, FakeClickHouse.status, FakeClickHouse.tail = [], 200, b""
    server = HTTPServer(("127.0.0.1", 0), FakeClickHouse)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_reading_clickhouse(clickhouse):
    """The query goes in the body, the user and password in ClickHouse's headers (never in the address)."""
    assert M.fetch_clickhouse(clickhouse, "customer_records_app", "s3cret") == ROWS
    [req] = FakeClickHouse.seen
    assert (req["user"], req["key"], req["path"]) == ("customer_records_app", "s3cret", "/")
    assert req["body"] == M.QUERY + "\nFORMAT JSONEachRow"


def test_clickhouse_problems_are_explained_without_the_password(clickhouse):
    FakeClickHouse.status = 403
    with pytest.raises(M.MerchantListError) as exc:
        M.fetch_clickhouse(clickhouse, "app", "s3cret")
    assert "HTTP 403" in str(exc.value) and "Authentication failed" in str(exc.value) and "s3cret" not in str(exc.value)
    with pytest.raises(M.MerchantListError, match="Can't reach ClickHouse at http://127.0.0.1:9/"):
        M.fetch_clickhouse("http://127.0.0.1:9", "app", "s3cret", timeout=5)
    FakeClickHouse.status, FakeClickHouse.tail = 200, b"Code: 241. DB::Exception: Memory limit exceeded\n"
    with pytest.raises(M.MerchantListError, match="Code: 241. DB::Exception: Memory limit exceeded"):
        M.fetch_clickhouse(clickhouse, "app", "s3cret")


def test_the_refresher_loads_from_clickhouse(rstore, clickhouse):
    """Now (the Merchants page's button), and in the background when due; nothing without settings."""
    off = M.MerchantRefresher(rstore, "", "", "", 60)
    off.maybe_refresh()
    assert not off.configured and rstore.list_businesses() == []
    refresher = M.MerchantRefresher(rstore, clickhouse, "app", "s3cret", 60)
    summary = refresher.refresh("noah")
    assert (summary["merchants"], summary["locations"]) == (3, 2)
    assert rstore.merchants_loaded()["source"] == "ClickHouse" and refresher.last_error == ""
    refresher.maybe_refresh()                       # just refreshed: not due yet
    assert len(FakeClickHouse.seen) == 1
    refresher.last_try -= 3600                      # an hour later
    refresher.maybe_refresh()
    end = time.monotonic() + 10
    while rstore.merchants_loaded()["source"] != "ClickHouse (automatic)" and time.monotonic() < end:
        time.sleep(0.05)
    assert len(FakeClickHouse.seen) == 2 and rstore.merchants_loaded()["by"] == ""
    FakeClickHouse.status = 500
    with pytest.raises(M.MerchantListError):
        refresher.refresh()
    assert "HTTP 500" in refresher.last_error
    assert len(rstore.list_businesses()) == 3       # a failed load changes nothing
    FakeClickHouse.status = 200
    refresher.store = None                          # anything unexpected is said too, as a MerchantListError
    with pytest.raises(M.MerchantListError, match="Loading the merchants failed"):
        refresher.refresh()
    assert refresher.last_error.startswith("Loading the merchants failed (AttributeError")
