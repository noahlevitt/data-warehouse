"""Customer Records: every imported customer row kept as a searchable record, grouped into tags for export.

Overview
--------
Customer Records is built on the file converter (the ``converter`` package, file-converter 0.21.2): a file is read,
its columns are matched and the values cleaned exactly as the converter does, but instead of producing one import
file the rows are stored in PostgreSQL, one record per file row, tied to the business and service chosen for the
import. Records are found with FileMaker-style find requests, put into tags (one business per tag), and a tag
exports its unique phone numbers in the converter's import format.

::

    New import (business + service) ─► converter.jobs.run_import ─► engine.convert (schemas/records_schema.yaml)
        ─► records.importer.RecordSink.save ─► PostgreSQL: imports, records, record_phones
    Find ─► records.find (requests → SQL) ─► found set ─► records.store.add_to_tag ─► tags, tag_records
    Tag ─► records.tags.export_table ─► converter.output.write_output (schemas/target_schema.yaml)

Modules
    * ``config``   — settings (DATABASE_URL, the record fields and export format files).
    * ``db``       — the connection pool and the tables, created and extended from the record fields at start.
    * ``store``    — businesses, services, imports, records, tags (all SQL outside finds and exports).
    * ``importer`` — the import sink: a converted file → records and their phone numbers.
    * ``fields``   — what can be searched: the record fields plus business, service, import, tag, other columns.
    * ``criteria`` — one find criterion (FileMaker syntax) → an SQL condition, per kind of field.
    * ``find``     — find requests, omit, constrain / extend, the found set, its pages, counts and CSV.
    * ``tags``     — a tag's export: one row per unique number, in the converter's import format.
    * ``ui``       — the Streamlit pages added by Customer Records (Find, Tags, Businesses, Services).

Everything except ``ui`` is free of Streamlit and is tested against a real PostgreSQL server (tests/records/).
"""

# Customer Records' own version (shown in the sidebar, tagged vX.Y.Z in git). The conversion engine keeps its own
# number (converter.__version__, file-converter's) so it is clear which converter release the engine came from.
__version__ = "1.1.0"
