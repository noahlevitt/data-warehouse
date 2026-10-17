"""Start Customer Records afresh: delete every merchant, location, import, record and tag (1.6.0).

  python scripts/reset_records.py            say what would be deleted
  python scripts/reset_records.py --yes      delete it

In Docker:  docker compose exec app python scripts/reset_records.py --yes

Overview
--------
Made for the update to 1.6.0, when merchants and locations started coming from the main app: a database that
only held test data typed in by hand (businesses with made-up IDs, their imports and tags) can start clean. It
works on the same PostgreSQL database and data folder as the app (``DATABASE_URL``, ``DATA_DIR``, from the same
``.env`` / environment settings). Stop the app first, so no import is running.

Deleted: in PostgreSQL every merchant, location, import, record (and its phone numbers) and tag (with its records
and history), what was sent to the main app's bucket (1.7.0: the list only; the files sent stay there, and the
script says how many), and when the merchants were last loaded; in the data folder every import job, its stored files, the
New import drafts, and the converter's number history (``number_log``, not used by Customer Records). Kept: the
services, users, templates (mappings), display preferences and the name table. It refuses to run when the data
folder has no ``converter.db`` (a wrong ``DATA_DIR`` would otherwise empty the database and leave the jobs).
Afterwards load the merchants on the Merchants page (or let the app read them from ClickHouse).

Exit status: 0 on success (also when only listing); 1 when the data folder isn't the app's; non-zero with Python's
error when PostgreSQL can't be reached.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from converter.config import get_settings  # noqa: E402
from records.config import get_records_settings  # noqa: E402

TABLES = ["bucket_sends", "tag_events", "tag_records", "tags", "record_phones", "records", "imports", "locations",
          "businesses"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete every merchant, location, import, record and tag.")
    parser.add_argument("--yes", action="store_true", help="really delete (without it, only say what would go)")
    args = parser.parse_args(argv)
    url = get_records_settings().database_url
    db_path = Path(get_settings().db_path)
    data = db_path.parent
    if not db_path.exists():
        print(f"No app data in {data} (DATA_DIR): run this with the same settings as the app. Nothing deleted.")
        return 1
    with psycopg.connect(url) as c:
        have = {r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
        tables = [t for t in TABLES if t in have]
        counts = {t: c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
        with sqlite3.connect(db_path) as s:
            jobs = s.execute("SELECT count(*) FROM imports").fetchone()[0]
        print(f"PostgreSQL ({url.rsplit('@', 1)[-1]}): " + ", ".join(f"{n:,} {t}" for t, n in counts.items()))
        print(f"Data folder ({data}): {jobs:,} import jobs with their files, and the New import drafts")
        if "bucket_sends" in tables:
            sent = c.execute("SELECT count(*) FROM bucket_sends WHERE status = 'sent'").fetchone()[0]
            if sent:
                print(f"Note: {sent:,} file(s) were already sent to the main app's bucket (imports, deletions, tags). "
                      "This doesn't remove them there, or from the main app's ClickHouse.")
        if not args.yes:
            print("Nothing deleted. Run again with --yes to delete all of it.")
            return 0
        if tables:
            c.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")
        if "app_meta" in have:
            c.execute("DELETE FROM app_meta WHERE key = 'merchants_loaded'")
    with sqlite3.connect(db_path) as s:
        s.execute("DELETE FROM imports")
        s.execute("DELETE FROM number_log")
    for folder in (data / "imports", data / "drafts"):
        if folder.exists():
            shutil.rmtree(folder)
    print("Deleted. Load the merchants on the Merchants page (or let the app read them from ClickHouse).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
