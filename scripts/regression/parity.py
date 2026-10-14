"""Parity check: a tag made of one file's records exports what the file converter makes of that file.

What it does
------------
For every file of the regression corpus (``scripts/regression/corpus.zip`` + ``sample_data/``, the same 406 files as
regression.py) it compares two paths, both mapped automatically by the rules (no Claude, no templates):

* converter: map with ``schemas/target_schema.yaml``, convert → the import file;
* Customer Records: map with ``schemas/records_schema.yaml``, convert, store the records (as an import does), put
  every record in a tag → the tag's export.

Expected differences, left out of the comparison:
* consent: a tag export leaves it blank (business decision), so the converter's consent is blanked too;
* a number on several rows of the file: the converter keeps a row per different combination of dates, the tag
  export merges them into one row (latest dates); only such numbers' rows can differ, and they are counted apart.

Everything else must be identical: the same set of numbers, and for every number on one row the same value in every
column. Prints one line per file that differs and a summary; exit code 1 when any file differs.

Usage (from the repository root, with PostgreSQL running)::

    python scripts/regression/parity.py                 # uses DATABASE_URL's server
    python scripts/regression/parity.py --limit 50      # first 50 files only

A temporary database (``<name>_parity``) is created on the same server and dropped at the end.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import psycopg  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

from converter.engine import convert  # noqa: E402
from converter.mapping import profile_columns  # noqa: E402
from converter.reader import ReadError, read_file  # noqa: E402
from converter.schema import load_schema  # noqa: E402
from converter.services import initial_mapping  # noqa: E402
from records import find as F  # noqa: E402
from records.config import get_records_settings  # noqa: E402
from records.db import Database  # noqa: E402
from records.importer import RecordSink  # noqa: E402
from records.store import RecordStore  # noqa: E402
from records.tags import export_table  # noqa: E402
from regression import corpus  # noqa: E402


class _Rec:
    """The bits of an app import record that RecordSink.save reads."""

    def __init__(self, records_import_id: int, mapping):
        self.read_options = {"records_import_id": records_import_id}
        self.mapping = mapping


def compare_file(name: str, data: bytes, target, records_schema, rs: RecordStore, sid: int) -> dict:
    """Run one file both ways; {"numbers": n, "multi": numbers on several rows, "diffs": [text, …]}."""
    rr = read_file(name, data)
    rows = [rr.excel_row_number(i) for i in range(len(rr.df))]
    prof = profile_columns(rr.df)
    mt = initial_mapping(target, rr.df, prof, None, None, use_ai=False).mapping
    conv = convert(rr.df, target, mt, row_numbers=rows).output.copy()
    consent = next((f.header for f in target.fields if f.name == "consent"), None)
    if consent:
        conv[consent] = ""
    phone = next(f.header for f in target.fields if f.type == "phone")

    mr = initial_mapping(records_schema, rr.df, prof, None, None, use_ai=False).mapping
    result = convert(rr.df, records_schema, mr, row_numbers=rows)
    biz = f"P{hashlib.sha1(name.encode()).hexdigest()[:10]}"
    rs.add_business(biz, name)
    imp = rs.create_import(biz, sid, name, hashlib.sha1(data).hexdigest(), rr.sheet, "parity")
    RecordSink(rs, None).save(0, _Rec(imp, mr), rr, result, records_schema)
    tag = rs.create_tag(biz, "all")
    cond, params = F.where(F.all_records(), [], businesses=[biz])
    rs.add_to_tag(tag, cond, params)
    exp, _ = export_table(rs.db, tag, records_schema, target)

    diffs = []
    counts = conv[phone].value_counts()
    multi = set(counts[counts > 1].index)
    if set(conv[phone]) != set(exp[phone]):
        only_c, only_e = set(conv[phone]) - set(exp[phone]), set(exp[phone]) - set(conv[phone])
        diffs.append(f"numbers: only in converter {sorted(only_c)[:3]}, only in tag {sorted(only_e)[:3]}")
    e_by = {r[phone]: r for r in exp.to_dict("records")}
    for r in conv.to_dict("records"):
        if r[phone] in multi or r[phone] not in e_by:
            continue
        for col, v in r.items():
            if e_by[r[phone]][col] != v:
                diffs.append(f"{r[phone]} {col}: converter {v!r}, tag {e_by[r[phone]][col]!r}")
    return {"numbers": len(counts), "multi": len(multi), "diffs": diffs}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="only the first N files")
    args = ap.parse_args(argv)
    url = get_records_settings().database_url
    name = f"{conninfo_to_dict(url).get('dbname') or 'customer_records'}_parity"
    admin = make_conninfo(url, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}"')
        c.execute(f'CREATE DATABASE "{name}"')
    db = Database(make_conninfo(url, dbname=name))
    try:
        records_schema = load_schema(REPO / "schemas" / "records_schema.yaml")
        target = load_schema(REPO / "schemas" / "target_schema.yaml")
        db.setup(records_schema)
        rs = RecordStore(db)
        sid = rs.add_service("parity")
        files = corpus()
        if args.limit:
            files = files[:args.limit]
        bad, numbers, multi, skipped = 0, 0, 0, 0
        for fname, data in files:
            try:
                res = compare_file(fname, data, target, records_schema, rs, sid)
            except ReadError:
                skipped += 1
                continue
            numbers += res["numbers"]
            multi += res["multi"]
            if res["diffs"]:
                bad += 1
                print(f"{fname}: " + "; ".join(res["diffs"][:3]) + (" …" if len(res["diffs"]) > 3 else ""))
        print(f"{len(files) - skipped} files, {numbers:,} numbers ({multi:,} on several rows, merged in tags, "
              f"not compared); {'identical' if not bad else f'{bad} file(s) differ'}"
              + (f"; {skipped} unreadable file(s) skipped" if skipped else ""))
        return 1 if bad else 0
    finally:
        db.close()
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}"')


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
