"""The two mapping tables on New import show every row (1.0.2): the page scrolls, not the table.

Every edit in "Review column mapping" or "Columns in your file" gives that table a new widget key (app.py,
"Widget-key versioning"), and a new key draws a new table. A table that scrolls inside itself therefore jumped back
to its first row after each Skip or Goes to change (reported on 1.0.1). With height="content" there is no inner
scroll to lose. The scroll position itself can only be seen in a browser (checked with Playwright for 1.0.2); this
checks that both tables are drawn at full height.
"""

import streamlit as st

from . import test_drafts
from .test_drafts import CSV, run_app

# The app tests' data folder and records database (a pytest fixture), shared with the draft tests.
app_store = test_drafts.app_store


def test_both_mapping_tables_show_every_row(app_store, monkeypatch):
    calls = {}
    real = st.data_editor

    def recording_editor(data, *args, **kwargs):
        calls[str(kwargs.get("key", "")).rsplit("_", 1)[0]] = kwargs.get("height", "auto")
        return real(data, *args, **kwargs)

    monkeypatch.setattr(st, "data_editor", recording_editor)
    run_app(app_store.create_draft("local", "optin.csv", CSV))
    field_grid = calls.get("map_editor")
    columns_table = next((h for k, h in calls.items() if k.startswith("notes_editor")), None)
    assert (field_grid, columns_table) == ("content", "content"), calls
