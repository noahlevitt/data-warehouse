"""Helpers for Customer Records' tests: run a file through a real import (converter.jobs.run_import with the
records sink), exactly as the app does, against the test database."""

from __future__ import annotations

import hashlib

from converter.jobs import run_import
from converter.mapping import Mapping, profile_columns
from converter.reader import read_file
from converter.services import initial_mapping
from converter.storage import Store
from records.importer import RecordSink
from records.store import RecordStore, StoreError


def ensure_business(rs: RecordStore, business: str, name: str | None = None) -> None:
    """Add the business unless it exists."""
    if rs.business(business) is None:
        rs.add_business(business, name or f"Business {business}")


def ensure_service(rs: RecordStore, name: str) -> int:
    """The id of the service with this name, added if needed."""
    for s in rs.list_services():
        if s["name"] == name:
            return s["id"]
    return rs.add_service(name)


def import_file(rs: RecordStore, jobs: Store, schema, name: str, data: bytes | str, business: str = "1001",
                service: str = "Toast export", mapping: Mapping | None = None, edit=None,
                timezone: str = "America/New_York"):
    """Import one file for a business and service; returns (job record, Customer Records import id).

    ``mapping``: use this mapping instead of the automatic one; ``edit(mapping) -> mapping``: change the automatic
    one (e.g. map consent by hand, as a person would).
    """
    if isinstance(data, str):
        data = data.encode()
    ensure_business(rs, business)
    sid = ensure_service(rs, service)
    rr = read_file(name, data)
    m = mapping or initial_mapping(schema, rr.df, profile_columns(rr.df), None, None, use_ai=False).mapping
    if edit:
        m = edit(m)
    imp = rs.create_import(business, sid, name, hashlib.sha1(data).hexdigest(), rr.sheet, "tester")
    job = jobs.create_import(username="tester", filename=name, data=data, schema_label=schema.label,
                             schema_json=schema.model_dump_json(), mapping=m,
                             read_options={"sheet": rr.sheet, "header_row": rr.header_row, "timezone": timezone,
                                           "records_import_id": imp}, template_name=None, ai_used=False)
    rs.set_job(imp, job)
    rec = run_import(jobs, job, RecordSink(rs, jobs))
    return rec, imp


def phones_of(rs: RecordStore, record_id: int) -> list[str]:
    """A record's numbers in their stored order (the phone columns' order)."""
    return [p["phone"] for p in rs.record(record_id)["phones"]]


__all__ = ["ensure_business", "ensure_service", "import_file", "phones_of", "StoreError"]
