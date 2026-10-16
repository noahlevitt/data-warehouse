"""Settings of Customer Records, read from environment variables (and ``.env``, loaded by ``converter.config``).

=====================  ===========================================  ================================================
Variable               Default                                      Meaning
=====================  ===========================================  ================================================
DATABASE_URL           postgresql://localhost:5432/customer_records The PostgreSQL database. Created on first start
                                                                    when the server allows it (Homebrew's does).
RECORDS_SCHEMA_PATH    schemas/records_schema.yaml                  The record fields (what each imported row keeps)
EXPORT_SCHEMA_PATH     SCHEMA_PATH (schemas/target_schema.yaml)     The import format a tag exports into
IMPORT_WORKERS         5 (minimum 1)                                Imports that run at the same time, each in its
                                                                    own worker process (records.runner); more wait
                                                                    queued. The converter alone defaults to 2.
DATABASE_POOL_SIZE     16 (minimum 2)                               The most PostgreSQL connections the web server
                                                                    opens for the pages. Each running import uses up
                                                                    to 2 more.
CLICKHOUSE_URL         —                                            The main app's ClickHouse (``https://<host>:8443``
                                                                    or just the host), where its merchants and
                                                                    locations are read (records.merchants).
CLICKHOUSE_USER        —                                            Its app user (SELECT on pg.merchants and
CLICKHOUSE_PASSWORD    —                                            pg.locations). Without these three, the merchants
                                                                    are loaded from a file on the Merchants page.
MERCHANTS_REFRESH_     60                                           Minutes between automatic loads (0 = only with
MINUTES                                                             the Merchants page's button).
=====================  ===========================================  ================================================

The converter's other settings (DATA_DIR, FILE_RETENTION_DAYS, Claude, logins…) still apply; see
``converter.config``. ``SCHEMA_PATH`` keeps meaning the converter's import format, which is the tag export format.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from converter.config import get_settings  # also loads .env into the environment

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = "postgresql://localhost:5432/customer_records"
DEFAULT_IMPORT_WORKERS = 5
DEFAULT_POOL_SIZE = 16


@dataclass(frozen=True)
class RecordsSettings:
    """Customer Records' settings (see the module docstring)."""

    database_url: str
    records_schema_path: Path
    export_schema_path: Path
    import_workers: int = DEFAULT_IMPORT_WORKERS
    database_pool_size: int = DEFAULT_POOL_SIZE
    clickhouse_url: str = ""
    clickhouse_user: str = ""
    clickhouse_password: str = field(default="", repr=False)  # never printed
    merchants_refresh_minutes: int = 60


def get_records_settings() -> RecordsSettings:
    """Read the settings now (environment variables win over .env, as for the converter's settings).

    Raises ValueError when IMPORT_WORKERS, DATABASE_POOL_SIZE or MERCHANTS_REFRESH_MINUTES isn't a whole number.
    """
    conv = get_settings()
    records_path = os.getenv("RECORDS_SCHEMA_PATH") or str(ROOT / "schemas" / "records_schema.yaml")
    export_path = os.getenv("EXPORT_SCHEMA_PATH") or str(conv.schema_path)
    return RecordsSettings(database_url=os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL,
                           records_schema_path=Path(records_path), export_schema_path=Path(export_path),
                           import_workers=max(1, int(os.getenv("IMPORT_WORKERS") or DEFAULT_IMPORT_WORKERS)),
                           database_pool_size=max(2, int(os.getenv("DATABASE_POOL_SIZE") or DEFAULT_POOL_SIZE)),
                           clickhouse_url=(os.getenv("CLICKHOUSE_URL") or "").strip(),
                           clickhouse_user=(os.getenv("CLICKHOUSE_USER") or "").strip(),
                           clickhouse_password=os.getenv("CLICKHOUSE_PASSWORD") or "",
                           merchants_refresh_minutes=max(0, int(os.getenv("MERCHANTS_REFRESH_MINUTES") or 60)))
