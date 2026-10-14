"""Settings of Customer Records, read from environment variables (and ``.env``, loaded by ``converter.config``).

=====================  ===========================================  ================================================
Variable               Default                                      Meaning
=====================  ===========================================  ================================================
DATABASE_URL           postgresql://localhost:5432/customer_records The PostgreSQL database. Created on first start
                                                                    when the server allows it (Homebrew's does).
RECORDS_SCHEMA_PATH    schemas/records_schema.yaml                  The record fields (what each imported row keeps)
EXPORT_SCHEMA_PATH     SCHEMA_PATH (schemas/target_schema.yaml)     The import format a tag exports into
=====================  ===========================================  ================================================

The converter's own settings (DATA_DIR, IMPORT_WORKERS, FILE_RETENTION_DAYS, Claude, logins…) still apply; see
``converter.config``. ``SCHEMA_PATH`` keeps meaning the converter's import format, which is the tag export format.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from converter.config import get_settings  # also loads .env into the environment

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = "postgresql://localhost:5432/customer_records"


@dataclass(frozen=True)
class RecordsSettings:
    """Customer Records' settings (see the module docstring)."""

    database_url: str
    records_schema_path: Path
    export_schema_path: Path


def get_records_settings() -> RecordsSettings:
    """Read the settings now (environment variables win over .env, as for the converter's settings)."""
    conv = get_settings()
    records_path = os.getenv("RECORDS_SCHEMA_PATH") or str(ROOT / "schemas" / "records_schema.yaml")
    export_path = os.getenv("EXPORT_SCHEMA_PATH") or str(conv.schema_path)
    return RecordsSettings(database_url=os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL,
                           records_schema_path=Path(records_path), export_schema_path=Path(export_path))
