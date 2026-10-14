"""The Streamlit pages Customer Records adds to the app: Find, Tags, Businesses and Services.

Overview
--------
``app.py`` keeps the converter's pages (New import, Imports, Templates…) with Customer Records' changes, and calls
the functions here for the new pages, passing a :class:`Ctx` with everything they need (database, schemas, the
signed-in user, how to open another page). Like ``app.py`` itself, every function redraws its page from scratch on
each click (Streamlit reruns the script); what must survive a rerun is kept in ``st.session_state``:

* Find: ``find_reqs`` (the requests on screen: ids, Find or Omit, the empty More criteria table each starts
  with; what is typed lives in the widgets, keyed ``fq_<find_ver>_<request id>_…``), ``find_ver`` (bumped by Clear
  to empty every box), ``found`` (the found set, a records.find node), ``found_note`` (how it was made),
  ``found_csv`` (a prepared CSV, with the found set and sort it was made for).
* Tags: ``tag_export_<id>`` (a prepared export, with the tag's last change time so a changed tag is prepared again).

The SQL lives in records.store / find / tags; this module only draws pages and calls them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import psycopg
from psycopg import sql

from converter.schema import TargetSchema

from . import find as F
from .criteria import SYNTAX
from .db import Database
from .fields import FORM_KEYS, PICKER_KEYS, FindField, find_fields
from .store import RecordStore, StoreError
from .tags import export_file, export_filename, export_table

ss = st.session_state

COL_FIELD, COL_CRIT = "Field", "Criterion"
# Fields shown in the found-set table unless the person picks others.
DEFAULT_SHOW = ["first_name", "last_name", "email", "city", "state", "transactions_last", "birthday",
                "total_visits", "total_spent"]


@dataclass
class Ctx:
    """What the pages need from app.py."""

    db: Database
    records: RecordStore
    schema: TargetSchema          # the record fields
    export: TargetSchema          # the import format tags export into
    user: dict
    tz: str
    open_import: Callable[[int], None]
    pages: dict[str, Any] = field(default_factory=dict)   # st.Page objects by name ("find", "tags", …)


# ---------------------------------------------------------------------------------------------------- helpers

def _biz_label(b: dict) -> str:
    return f"{b['id']} · {b['name']}"


def _when(ts, tz: str) -> str:
    """A database timestamp (with zone) in the app's time zone, e.g. "Oct 14, 2026 11:05 AM"."""
    if ts is None:
        return ""
    return ts.astimezone(ZoneInfo(tz)).strftime("%b %d, %Y %I:%M %p")


def _show_value(v) -> str:
    """A record value for the screen (dates as MM/DD/YYYY, date-times with the time)."""
    return F._cell(v)


def go_find(ctx: Ctx, node: dict, note: str) -> None:
    """Open the Find page with this found set (from a tag, an import…)."""
    ss.found, ss.found_note = node, note
    ss.pop("found_csv", None)
    st.switch_page(ctx.pages["find"])


# ---------------------------------------------------------------------------------------------------- Find

# Example shown in an empty box, by kind of field (records.criteria has the whole syntax).
PLACEHOLDER = {"date": "e.g. 1/1/2025...1/1/2026", "created": "e.g. 10/14/2026 or 10/1/2026...",
               "number": "e.g. >5 or 10...20", "phone": "e.g. 516 555 0100 or 516*", "bool": "yes / no"}
TIP = {"text": "A word it starts with (smith), =smith for the whole word, \"main st\" anywhere, = empty, * any.",
       "date": "10/9/2026 · 10/2026 · 2026 · 1/1/2025...1/1/2026 · ...1/1/2026 up to that day · 1/1/2025... from "
               "that day on · <, <=, >, >= · = empty · * any",
       "number": "5 · >5 · <=100 · 10...20 · ...20 up to 20 · 10... 10 or more · = empty · * any",
       "phone": "Any formatting; part of a number (5550100) or its start (516*); = no usable number; * has one.",
       "bool": "yes or no; = empty; * any value"}
TIP["created"] = TIP["date"]


def _new_request(omit: bool = False) -> dict:
    """A new request on screen: an id (for widget keys), Find or Omit, and its empty More criteria table."""
    ss.find_seq = ss.get("find_seq", 0) + 1
    return {"id": ss.find_seq, "omit": omit,
            "base": pd.DataFrame({COL_FIELD: pd.Series(dtype="object"), COL_CRIT: pd.Series(dtype="object")})}


def _reset_requests() -> None:
    ss.find_reqs = [_new_request()]
    ss.find_ver = ss.get("find_ver", 0) + 1


def page_find(ctx: Ctx) -> None:
    """Page "Find": find requests (FileMaker style), the found set, its records, and adding it to a tag.

    The requests are one form: typing doesn't rerun the page, and Enter in any box is the Find button (the form's
    first button). Nothing is required; a Find with everything blank shows every record.
    """
    st.header("Find records")
    totals = ctx.records.totals()
    if not totals["records"]:
        st.info("No records yet. Import a file under **New import** (pick its business and service there).")
    businesses = ctx.records.list_businesses()
    services = ctx.records.list_services()
    fields = find_fields(ctx.schema, ctx.records.columns_seen())
    by_key = {f.key: f for f in fields}
    boxes = [by_key[k] for k in FORM_KEYS] + [f for f in fields if f.key.startswith("f:")]
    extra = {f.label: f for f in fields
             if f.key not in PICKER_KEYS and f.key not in FORM_KEYS and not f.key.startswith("f:")}
    if "find_reqs" not in ss:
        _reset_requests()

    requests: list[dict] = []
    problems: list[str] = []
    with st.form("find_form"):
        st.markdown("**Find requests** — fill in only what you're looking for; **nothing is required**, and a Find "
                    "with everything blank shows every record. A record must match everything filled in within a "
                    "request; several requests add up (OR); an **Omit** request removes what it matches from the "
                    "requests above it. Enter in any box = **Find**.")
        b = st.columns(5)
        do_find = b[0].form_submit_button("Find", type="primary", width="stretch",
                                          help="A new found set from these requests (blank = every record)")
        do_constrain = b[1].form_submit_button("Constrain", width="stretch", disabled="found" not in ss,
                                               help="Keep only found records that also match these requests")
        do_extend = b[2].form_submit_button("Extend", width="stretch", disabled="found" not in ss,
                                            help="Add the records these requests match to the found set")
        do_all = b[3].form_submit_button("Show all", width="stretch", help="Every record")
        do_clear = b[4].form_submit_button("Clear", width="stretch",
                                           help="Empty the requests (the found set stays)")
        remove = None
        for n, req in enumerate(list(ss.find_reqs)):
            rkey = f"fq_{ss.find_ver}_{req['id']}"
            with st.container(border=True):
                h1, h2, h3 = st.columns([1.2, 2.2, 0.8], vertical_alignment="bottom")
                h1.markdown(f"**Request {n + 1}**")
                # A radio, not a toggle: one of the two is always chosen (a toggle clicked twice chose neither).
                kind = h2.radio("Request type", ["Find", "Omit"], index=1 if req["omit"] else 0, key=f"{rkey}_kind",
                                horizontal=True, label_visibility="collapsed")
                req["omit"] = kind == "Omit"
                if len(ss.find_reqs) > 1 and h3.form_submit_button("Remove", key=f"{rkey}_rm", width="stretch"):
                    remove = req["id"]
                c1, c2 = st.columns(2)
                biz = c1.multiselect(
                    "Business", [x["id"] for x in businesses], key=f"{rkey}_biz",
                    format_func=lambda i: next((_biz_label(x) for x in businesses if x["id"] == i), i),
                    placeholder="Any business" if businesses else "No businesses yet", select_all=False,
                    help="One or more businesses: type the ID or name and press Enter for the first match, or click "
                         "them. Empty = any business.")
                svc = c2.multiselect(
                    "Services", [x["id"] for x in services], key=f"{rkey}_svc",
                    format_func=lambda i: next((x["name"] + ("" if x["active"] else " (inactive)")
                                                for x in services if x["id"] == i), str(i)),
                    placeholder="Any service" if services else "No services yet", select_all=False,
                    help="One or more services. Empty = any service. To leave a service out, pick it in an Omit "
                         "request.")
                criteria: list[list[str]] = []
                # Request 1 shows every box; later requests (often just a business or a service to omit) keep
                # theirs folded unless something is typed in them, so the found set isn't pushed far down.
                filled = sum(1 for f in boxes if str(ss.get(f"{rkey}_{f.key}") or "").strip())
                with st.expander("Fields" + (f" — {filled} filled in" if filled else ""),
                                 expanded=n == 0 or filled > 0):
                    cols = st.columns(4)
                    for i, f in enumerate(boxes):
                        text = cols[i % 4].text_input(
                            f.label, key=f"{rkey}_{f.key}", placeholder=PLACEHOLDER.get(f.kind, ""),
                            help=TIP.get(f.kind, TIP["text"]) + (f" — {f.help}" if f.help else ""))
                        if text.strip():
                            criteria.append([f.key, text])
                with st.expander("More criteria: Any field, Full name, Tag, Import #, File name, file columns…"):
                    more = st.data_editor(
                        req["base"], key=f"{rkey}_more", num_rows="dynamic", hide_index=True, width="stretch",
                        column_config={
                            COL_FIELD: st.column_config.SelectboxColumn(options=list(extra), width="medium",
                                                                        help="What to search"),
                            COL_CRIT: st.column_config.TextColumn(width="large", help="What to look for (see How "
                                                                                      "to search below)"),
                        })
                for _, row in more.iterrows():
                    label, text = row.get(COL_FIELD), row.get(COL_CRIT)
                    if not (isinstance(text, str) and text.strip()):
                        continue
                    if not (isinstance(label, str) and label):   # an empty cell is NaN or None, not ""
                        problems.append(f"Request {n + 1}, More criteria: choose a Field for '{text.strip()}'.")
                    else:
                        f = extra.get(label)
                        criteria.append([f.key if f else f"missing:{label}", text])
                requests.append({"omit": req["omit"], "businesses": list(biz), "services": list(svc),
                                 "criteria": criteria})
        a = st.columns(5)
        add_find = a[0].form_submit_button("+ Request", width="stretch", help="Add a request (OR)")
        add_omit = a[1].form_submit_button("+ Omit request", width="stretch",
                                           help="Add a request that removes what it matches")
        with st.expander("How to search"):
            st.markdown(_help_text())

    if do_clear:
        _reset_requests()
        st.rerun()
    if remove is not None:
        ss.find_reqs = [r for r in ss.find_reqs if r["id"] != remove]
        st.rerun()
    if add_find or add_omit:
        ss.find_reqs.append(_new_request(omit=add_omit))
        st.rerun()

    step = None
    if (do_constrain or do_extend) and F.blank(requests):
        st.warning(f"Fill in something to {'constrain' if do_constrain else 'extend'} the found set with; it is "
                   "unchanged. (A blank **Find** shows every record.)")
    elif problems and (do_find or do_constrain or do_extend):
        for msg in problems:
            st.error(msg)
    elif do_find:
        step = F.find_node(requests), _describe(requests, by_key, services)
    elif do_constrain:
        step = F.constrain(ss.found, requests), ss.get("found_note", "") + " → constrained: " + _describe(
            requests, by_key, services)
    elif do_extend:
        step = F.extend(ss.found, requests), ss.get("found_note", "") + " → extended: " + _describe(
            requests, by_key, services)
    elif do_all:
        step = F.all_records(), "All records"
    if step is not None:
        # Checked before it replaces the found set: a criterion that can't be read leaves the found set as it was
        # (as FileMaker does), so correcting the typo and trying again works.
        try:
            F.where(step[0], fields, timezone=ctx.tz, collation=ctx.db.collation)
            ss.found, ss.found_note = step
            ss.pop("found_csv", None)
        except F.FindError as exc:
            st.error(str(exc) + (" The found set below is unchanged." if "found" in ss else ""))

    if "found" not in ss:
        st.caption("Fill in what you're looking for and click **Find** (or press Enter). A blank Find shows every "
                   "record.")
        return
    _found_set(ctx, fields, businesses)


def _describe(requests: list[dict], by_key: dict[str, FindField], services: list[dict]) -> str:
    """A short description of a find for the found set's caption ("business 1001; omit Catering")."""
    names = {s["id"]: s["name"] for s in services}
    parts = []
    for r in requests:
        bits = []
        if r["businesses"]:
            bits.append("business " + ", ".join(r["businesses"]))
        if r["services"]:
            bits.append(", ".join(names.get(s, str(s)) for s in r["services"]))
        bits += [f"{by_key[k].label if k in by_key else k} {t.strip()}" for k, t in r["criteria"]]
        if bits:
            parts.append(("omit " if r["omit"] else "") + " · ".join(bits))
    return "; ".join(parts) if parts else "Blank find (every record)"


def _help_text() -> str:
    """The search syntax for the How to search panel (records.criteria.SYNTAX)."""
    return SYNTAX


def _found_set(ctx: Ctx, fields: list[FindField], businesses: list[dict]) -> None:
    """The found set: counts, the records table, one record's details, tag actions and CSV."""
    try:
        cond, params = F.where(ss.found, fields, timezone=ctx.tz, collation=ctx.db.collation)
        cnt = F.counts(ctx.db, cond, params)
    except F.FindError as exc:
        st.error(str(exc))
        return
    except psycopg.Error as exc:  # a value the database refused: say so instead of the error page
        st.error(f"The database couldn't run this find ({type(exc).__name__}): {str(exc).splitlines()[0]}. "
                 "Change the criteria and click **Find**, or **Show all**.")
        return
    st.subheader("Found set")
    if ss.get("found_note"):
        st.caption(ss.found_note)
    m = st.columns(4)
    m[0].metric("Records", f"{cnt['records']:,}")
    m[1].metric("Unique phone numbers", f"{cnt['numbers']:,}")
    m[2].metric("Without a usable number", f"{cnt['no_phone']:,}")
    m[3].metric("Businesses", f"{cnt['businesses']:,}")
    if not cnt["records"]:
        st.info("No records match.")
        if st.button("Show omitted only", help="Every record that isn't in the found set"):
            ss.found, ss.found_note = F.show_omitted(ss.found), ss.get("found_note", "") + " → omitted only"
            st.rerun()
        return

    names = {f.name: f.header for f in ctx.schema.fields if f.type != "phone"}
    o1, o2, o3, o4 = st.columns([2.2, 1.6, 1, 1.2], vertical_alignment="bottom")
    show = o1.multiselect("Columns", list(names), default=[n for n in DEFAULT_SHOW if n in names],
                          format_func=lambda n: names[n], key="found_cols")
    sort = o2.selectbox("Sort by", F.available_sorts(ctx.schema), key="found_sort")
    per_page = o3.selectbox("Per page", [50, 100, 250, 500], index=1, key="found_pp")
    pages = max(1, -(-cnt["records"] // per_page))
    sig = hashlib.sha1(json.dumps([ss.found, sort], default=str).encode()).hexdigest()[:10]
    pg = o4.number_input(f"Page (of {pages:,})", 1, pages, 1, key=f"found_pg_{sig}_{per_page}")
    rows = F.page(ctx.db, ctx.schema, cond, params, sort, per_page, (pg - 1) * per_page)
    table = F.page_frame(rows, ctx.schema, show, ctx.tz)
    st.caption("Select rows to see a record or to omit them from the found set.")
    event = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun", selection_mode="multi-row",
                         key=f"found_tbl_{sig}_{per_page}_{pg}",
                         column_config={F.ID_COL: st.column_config.NumberColumn(format="%d", width="small"),
                                        "Import #": st.column_config.NumberColumn(format="%d", width="small"),
                                        "Row": st.column_config.NumberColumn(format="%d", width="small")})
    sel = list(event.selection.rows) if event and event.selection else []
    ids = [int(table.iloc[i][F.ID_COL]) for i in sel]

    a1, a2, a3 = st.columns(3)
    if a1.button(f"Omit selected ({len(ids)})", disabled=not ids, width="stretch"):
        ss.found = F.omit_records(ss.found, ids)
        ss.found_note = ss.get("found_note", "") + f" → {len(ids)} omitted"
        ss.pop("found_csv", None)
        st.rerun()
    if a2.button("Show omitted only", width="stretch", help="Every record that isn't in the found set"):
        ss.found, ss.found_note = F.show_omitted(ss.found), ss.get("found_note", "") + " → omitted only"
        ss.pop("found_csv", None)
        st.rerun()
    # The prepared CSV belongs to exactly this found set and sort (sig); any change asks for a new one.
    csv_ready = ss.get("found_csv") and ss.found_csv["sig"] == sig
    if csv_ready:
        a3.download_button("⬇ Found set (CSV)", ss.found_csv["data"], f"found_set_{datetime.now():%Y%m%d-%H%M}.csv",
                           mime="text/csv", width="stretch", on_click="ignore")
    elif a3.button("Prepare CSV of the found set", width="stretch"):
        with st.spinner("Writing the CSV…"):
            ss.found_csv = {"sig": sig, "data": F.found_set_csv(ctx.db, ctx.schema, cond, params, sort, ctx.tz)}
        st.rerun()

    if len(ids) == 1:
        _record_detail(ctx, ids[0])
    _tag_actions(ctx, cond, params, cnt, businesses)


def _record_detail(ctx: Ctx, record_id: int) -> None:
    """Everything stored for one record."""
    rec = ctx.records.record(record_id)
    if rec is None:
        return
    with st.container(border=True):
        st.markdown(f"**Record ID {rec['id']}** · created {_when(rec['created_at'], ctx.tz)} · {rec['business_id']} · "
                    f"{rec['business_name']} · {rec['service_name']}"
                    f" · import #{rec['job_id'] or '—'} ({rec['file_name']}"
                    + (f", sheet {rec['sheet']}" if rec.get("sheet") else "") + f"), row {rec['file_row']}")
        c1, c2 = st.columns(2)
        with c1:
            vals = [{"Field": f.header, "Value": _show_value(rec.get(f.name))} for f in ctx.schema.fields
                    if f.type != "phone"]
            st.dataframe(pd.DataFrame(vals), hide_index=True, width="stretch")
        with c2:
            if rec["phones"]:
                st.dataframe(pd.DataFrame([{"Number": p["phone"], "As in the file": p["original"],
                                            "Repaired": "yes — check it" if p["repaired"] else ""}
                                           for p in rec["phones"]]), hide_index=True, width="stretch")
            else:
                st.warning(f"No usable phone number: {rec['phone_issue']}")
            if rec["issues"]:
                heads = {f.name: f.header for f in ctx.schema.fields}
                st.caption("Values that couldn't be read: " + "; ".join(
                    f"{heads.get(k, k)}: {v}" for k, v in rec["issues"].items()))
            st.caption("Tags: " + (", ".join(t["name"] for t in rec["tags"]) or "none"))
            if rec.get("job_id") and st.button("Open the import", key=f"open_imp_{rec['id']}"):
                ctx.open_import(int(rec["job_id"]))
        order = (ctx.records.get_import(rec["import_id"]) or {}).get("columns") or list(rec["source"])
        st.markdown("**The row as in the file**")
        st.dataframe(pd.DataFrame([{c: rec["source"].get(c, "") for c in order}]), hide_index=True,
                     width="stretch")


def _tag_actions(ctx: Ctx, cond, params: dict, cnt: dict, businesses: list[dict]) -> None:
    """Add the found set to a tag, remove it from one, or make a new tag from it."""
    with st.container(border=True):
        st.markdown("**Tag the found set** — a tag belongs to one business; only that business's records go in. "
                    "Its export has each phone number once.")
        biz_in_found = [r["business_id"] for r in ctx.db.fetch(
            sql.SQL("SELECT DISTINCT r.business_id FROM records r WHERE {} LIMIT 20").format(cond), params)]
        tags = [t for t in ctx.records.list_tags() if t["business_id"] in biz_in_found] or ctx.records.list_tags()
        c1, c2, c3 = st.columns([2.6, 1.2, 1.2], vertical_alignment="bottom")
        tag_id = c1.selectbox("Tag", [t["id"] for t in tags], index=None, placeholder="Choose a tag",
                              format_func=lambda i: next(f"{t['name']} · {t['business_id']} {t['business_name']} "
                                                         f"({t['records']:,} records)" for t in tags if t["id"] == i))
        detail = f"found set: {ss.get('found_note', '')}"[:200]
        if c2.button("Add found set", type="primary", disabled=tag_id is None, width="stretch"):
            res = ctx.records.add_to_tag(tag_id, cond, params, ctx.user["username"], detail)
            msg = f"Added {res['added']:,} record(s)."
            if res["already"]:
                msg += f" {res['already']:,} were in the tag already."
            if res["other_business"]:
                msg += (f" {res['other_business']:,} record(s) of other businesses were left out (a tag holds one "
                        "business).")
            st.success(msg)
        if c3.button("Remove found set", disabled=tag_id is None, width="stretch"):
            n = ctx.records.remove_from_tag(tag_id, cond, params, ctx.user["username"], detail)
            st.success(f"Removed {n:,} record(s) from the tag.")
        with st.form("new_tag_from_found", clear_on_submit=True, border=False):
            n1, n2, n3 = st.columns([1.6, 2, 1.2], vertical_alignment="bottom")
            default = biz_in_found[0] if len(biz_in_found) == 1 else None
            ids = [b["id"] for b in businesses]
            biz = n1.selectbox("New tag for business", ids, index=ids.index(default) if default in ids else None,
                               format_func=lambda i: next((_biz_label(b) for b in businesses if b["id"] == i), i),
                               placeholder="Choose the business")
            name = n2.text_input("Tag name", placeholder="e.g. October promo — lapsed guests")
            if n3.form_submit_button("Create tag with found set", width="stretch"):
                try:
                    tid = ctx.records.create_tag(biz or "", name, "", ctx.user["username"])
                    res = ctx.records.add_to_tag(tid, cond, params, ctx.user["username"], detail)
                    st.success(f"Tag '{name.strip()}' created with {res['added']:,} record(s)"
                               + (f"; {res['other_business']:,} of other businesses left out." if res["other_business"]
                                  else "."))
                except StoreError as exc:
                    st.error(str(exc))
        if len(biz_in_found) > 1:
            st.caption(f"The found set has records of {cnt['businesses']} businesses; **Constrain** with one "
                       "**Business** to keep only its records.")


# ---------------------------------------------------------------------------------------------------- Tags

def page_tags(ctx: Ctx) -> None:
    """Page "Tags": every tag, a new tag, and one tag (?tag=<id>) with its export."""
    qid = st.query_params.get("tag")
    if qid and str(qid).isdigit():
        _tag_detail(ctx, int(qid))
        return
    st.header("Tags")
    st.caption("A tag is a list of records of one business. Its export — one row per unique phone number, in the "
               "import format — is what gets imported into the main system. Add records from **Find**.")
    businesses = ctx.records.list_businesses()
    ids = [b["id"] for b in businesses]
    label = {b["id"]: _biz_label(b) for b in businesses}
    pick = st.selectbox("Business", ids, index=None, placeholder="All businesses", format_func=label.get,
                        key="tags_biz")
    tags = ctx.records.list_tags(pick)
    if tags:
        table = pd.DataFrame([{"#": t["id"], "Tag": t["name"],
                               "Business": label.get(t["business_id"], t["business_id"]),
                               "Records": t["records"], "Unique numbers": t["numbers"],
                               "Changed": _when(t["updated_at"], ctx.tz), "Created by": t["created_by"],
                               "Notes": t["notes"]} for t in tags])
        st.caption("Select a tag to open it.")
        ev = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                          key="tags_tbl",
                          column_config={"#": st.column_config.NumberColumn(format="%d", width="small")})
        if ev and ev.selection and ev.selection.rows:
            st.query_params["tag"] = str(int(table.iloc[ev.selection.rows[0]]["#"]))
            st.rerun()
    else:
        st.info("No tags yet.")
    with st.form("new_tag", clear_on_submit=True):
        st.markdown("**New tag**")
        c1, c2 = st.columns(2)
        biz = c1.selectbox("Business", ids, index=ids.index(pick) if pick in ids else None, format_func=label.get,
                           placeholder="Choose the business")
        name = c2.text_input("Name")
        notes = st.text_input("Notes (optional)")
        if st.form_submit_button("Create tag", type="primary"):
            try:
                tid = ctx.records.create_tag(biz or "", name, notes, ctx.user["username"])
                st.query_params["tag"] = str(tid)
                st.rerun()
            except StoreError as exc:
                st.error(str(exc))


def _tag_detail(ctx: Ctx, tag_id: int) -> None:
    """One tag: counts, export (download + preview), its records, rename / notes / delete, history."""
    if st.button("← All tags"):
        del st.query_params["tag"]
        st.rerun()
    t = ctx.records.tag(tag_id)
    if t is None:
        st.error(f"Tag #{tag_id} not found.")
        return
    st.header(f"Tag: {t['name']}")
    st.caption(f"{t['business_id']} · {t['business_name']} · created {_when(t['created_at'], ctx.tz)} by "
               f"{t['created_by'] or '—'}" + (f" · {t['notes']}" if t["notes"] else ""))
    m = st.columns(3)
    m[0].metric("Records", f"{t['records']:,}")
    m[1].metric("Unique phone numbers (export rows)", f"{t['numbers']:,}")
    m[2].metric("Records without a usable number", f"{t['no_phone']:,}", help="Not in the export.")

    key = f"tag_export_{tag_id}"
    stamp = t["updated_at"].isoformat()
    prepared = ss.get(key)
    c1, c2, c3 = st.columns(3)
    if prepared and prepared["stamp"] == stamp:
        name = prepared["name"]
        c1.download_button(f"⬇ Export ({prepared['counts']['numbers']:,} numbers)", prepared["data"], name,
                           type="primary", width="stretch",
                           on_click=lambda: ctx.records.log_export(tag_id, ctx.user["username"],
                                                                   prepared["counts"]["numbers"], name))
    elif c1.button("Prepare export", type="primary", width="stretch", disabled=not t["numbers"]):
        with st.spinner("Building the export…"):
            data, counts = export_file(ctx.db, tag_id, ctx.schema, ctx.export)
        ss[key] = {"stamp": stamp, "data": data, "counts": counts,
                   "name": export_filename(t["business_id"], t["name"], ctx.export, f"{datetime.now():%Y%m%d-%H%M}")}
        st.rerun()
    if c2.button("Show the records in Find", width="stretch"):
        go_find(ctx, F.tag_records(tag_id), f"Tag '{t['name']}'")
    if prepared and prepared["stamp"] == stamp:
        cn = prepared["counts"]
        c3.caption(f"{ctx.export.label} · {cn['numbers']:,} numbers from {cn['records']:,} records · "
                   f"{cn['no_phone']:,} without a number left out · {cn['shared']:,} numbers in several records "
                   f"(merged: latest dates; consent left blank)")

    tabs = st.tabs(["Export preview", "Records", "Edit", "History"])
    with tabs[0]:
        if t["numbers"]:
            table, cn = export_table(ctx.db, tag_id, ctx.schema, ctx.export)
            st.caption(f"First {min(200, len(table)):,} of {len(table):,} rows, exactly as exported. Consent is "
                       "blank in tag exports for now: the main system handles opt-outs per business.")
            st.dataframe(table.head(200), hide_index=True, width="stretch")
        else:
            st.caption("No phone numbers in this tag yet.")
    with tabs[1]:
        _tag_records(ctx, t)
    with tabs[2]:
        with st.form(f"edit_tag_{tag_id}"):
            name = st.text_input("Name", value=t["name"])
            notes = st.text_input("Notes", value=t["notes"])
            if st.form_submit_button("Save"):
                try:
                    ctx.records.update_tag(tag_id, name, notes, ctx.user["username"])
                    st.rerun()
                except StoreError as exc:
                    st.error(str(exc))
        sure = st.checkbox("Yes, delete this tag (its records stay)", key=f"del_tag_ok_{tag_id}")
        if st.button("Delete tag", disabled=not sure):
            ctx.records.delete_tag(tag_id)
            del st.query_params["tag"]
            st.rerun()
    with tabs[3]:
        ev = ctx.records.tag_events(tag_id)
        st.dataframe(pd.DataFrame([{"When": _when(e["at"], ctx.tz), "Who": e["username"], "What": e["action"],
                                    "Records / numbers": e["count"] or "", "Details": e["detail"]} for e in ev]),
                     hide_index=True, width="stretch")


def _tag_records(ctx: Ctx, t: dict) -> None:
    """The tag's records, paged, with Remove selected."""
    node = F.tag_records(t["id"])
    cond, params = F.where(node, [], collation=ctx.db.collation)
    per = 100
    pages = max(1, -(-t["records"] // per))
    pg = st.number_input(f"Page (of {pages:,})", 1, pages, 1, key=f"tagrec_pg_{t['id']}_{t['records']}")
    rows = F.page(ctx.db, ctx.schema, cond, params, "Import order", per, (pg - 1) * per)
    if not rows:
        st.caption("No records in this tag yet. Add them from **Find**.")
        return
    names = [n for n in DEFAULT_SHOW if n in ctx.schema.field_names]
    table = F.page_frame(rows, ctx.schema, names, ctx.tz)
    ev = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun", selection_mode="multi-row",
                      key=f"tagrec_tbl_{t['id']}_{pg}_{t['records']}",
                      column_config={F.ID_COL: st.column_config.NumberColumn(format="%d", width="small")})
    sel = [int(table.iloc[i][F.ID_COL]) for i in (ev.selection.rows if ev and ev.selection else [])]
    if st.button(f"Remove selected from tag ({len(sel)})", disabled=not sel):
        n = ctx.records.remove_from_tag(t["id"], sql.SQL("r.id = ANY(%(sel)s)"), {"sel": sel}, ctx.user["username"],
                                        "selected on the tag")
        st.success(f"Removed {n} record(s).")
        st.rerun()


# ---------------------------------------------------------------------------------------------------- Businesses

def page_businesses(ctx: Ctx) -> None:
    """Page "Businesses": the businesses records are imported for, with the main system's ID for each."""
    st.header("Businesses")
    st.caption("Each business's **ID must be the same as in the main system**: it is how the two will be connected "
               "later. Every import is for one business.")
    businesses = ctx.records.list_businesses()
    if businesses:
        st.dataframe(pd.DataFrame([{"ID": b["id"], "Name": b["name"], "Records": b["records"],
                                    "Imports": b["imports"], "Tags": b["tags"], "Notes": b["notes"],
                                    "Added": _when(b["created_at"], ctx.tz)} for b in businesses]),
                     hide_index=True, width="stretch")
    else:
        st.info("No businesses yet. Add the first one below.")
    c1, c2 = st.columns(2)
    with c1, st.form("add_business", clear_on_submit=True):
        st.markdown("**Add a business**")
        bid = st.text_input("ID (as in the main system)")
        name = st.text_input("Name")
        notes = st.text_input("Notes (optional)")
        if st.form_submit_button("Add business", type="primary"):
            try:
                ctx.records.add_business(bid, name, notes, ctx.user["username"])
                st.rerun()
            except StoreError as exc:
                st.error(str(exc))
    if not businesses:
        return
    with c2:
        pick = st.selectbox("Change a business", [b["id"] for b in businesses],
                            format_func=lambda i: next(_biz_label(b) for b in businesses if b["id"] == i),
                            key="biz_edit_pick")
        b = next(x for x in businesses if x["id"] == pick)
        with st.form(f"edit_business_{pick}"):
            new_id = st.text_input("ID", value=b["id"], help="Correcting it updates its imports, records and tags.")
            name = st.text_input("Name", value=b["name"])
            notes = st.text_input("Notes", value=b["notes"])
            if st.form_submit_button("Save"):
                try:
                    ctx.records.update_business(pick, new_id, name, notes)
                    st.rerun()
                except StoreError as exc:
                    st.error(str(exc))
        if st.button("Delete this business", disabled=bool(b["imports"] or b["tags"]),
                     help="Only a business without imports and tags can be deleted."):
            try:
                ctx.records.delete_business(pick)
                st.rerun()
            except StoreError as exc:
                st.error(str(exc))


# ---------------------------------------------------------------------------------------------------- Services

def page_services(ctx: Ctx) -> None:
    """Page "Services": the list every import picks its service from."""
    st.header("Services")
    st.caption("Every import picks one service from this list. A service that is no longer used can be "
               "deactivated: it is then not offered on New import, and its records keep it.")
    services = ctx.records.list_services()
    if services:
        st.dataframe(pd.DataFrame([{"Service": s["name"], "Active": "yes" if s["active"] else "no",
                                    "Imports": s["imports"], "Records": s["records"],
                                    "Added": _when(s["created_at"], ctx.tz)} for s in services]),
                     hide_index=True, width="stretch")
    else:
        st.info("No services yet. Add the first one below.")
    c1, c2 = st.columns(2)
    with c1, st.form("add_service", clear_on_submit=True):
        st.markdown("**Add a service**")
        name = st.text_input("Name")
        if st.form_submit_button("Add service", type="primary"):
            try:
                ctx.records.add_service(name, ctx.user["username"])
                st.rerun()
            except StoreError as exc:
                st.error(str(exc))
    if not services:
        return
    with c2:
        pick = st.selectbox("Change a service", [s["id"] for s in services],
                            format_func=lambda i: next(s["name"] for s in services if s["id"] == i), key="svc_pick")
        s = next(x for x in services if x["id"] == pick)
        with st.form(f"edit_service_{pick}"):
            name = st.text_input("Name", value=s["name"])
            active = st.checkbox("Active (offered on New import)", value=s["active"])
            if st.form_submit_button("Save"):
                try:
                    ctx.records.update_service(pick, name, active)
                    st.rerun()
                except StoreError as exc:
                    st.error(str(exc))
        if st.button("Delete this service", disabled=bool(s["imports"]),
                     help="Only a service no import uses can be deleted; deactivate it otherwise."):
            try:
                ctx.records.delete_service(pick)
                st.rerun()
            except StoreError as exc:
                st.error(str(exc))
