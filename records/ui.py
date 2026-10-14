"""The Streamlit pages Customer Records adds to the app: Find, Tags, Businesses and Services.

Overview
--------
``app.py`` keeps the converter's pages (New import, Imports, Templates…) with Customer Records' changes, and calls
the functions here for the new pages, passing a :class:`Ctx` with everything they need (database, schemas, the
signed-in user, how to open another page). Like ``app.py`` itself, every function redraws its page from scratch on
each click (Streamlit reruns the script); what must survive a rerun is kept in ``st.session_state``:

* Find: ``find_reqs`` (the requests on screen: ids and their starting criteria), ``find_ver`` (bumped to clear the
  request editors), ``found`` (the found set, a records.find node), ``found_note`` (how it was made),
  ``found_csv`` (a prepared CSV, with the found set, Limit to and sort it was made for).
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
from psycopg import sql

from converter.schema import TargetSchema

from . import find as F
from .criteria import __doc__ as CRITERIA_HELP
from .db import Database
from .fields import FindField, find_fields
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

# Fields a new request starts with (like a FileMaker find layout); any row can be changed to another field, and
# rows without a criterion are ignored.
STARTING_FIELDS = ["Phone", "First name", "Last name", "Any field"]


def _new_request(omit: bool = False) -> dict:
    """A new request on screen: an id (for widget keys), Find or Omit, and its starting rows."""
    ss.find_seq = ss.get("find_seq", 0) + 1
    return {"id": ss.find_seq, "omit": omit,
            "base": pd.DataFrame({COL_FIELD: STARTING_FIELDS, COL_CRIT: [""] * len(STARTING_FIELDS)})}


def _reset_requests() -> None:
    ss.find_reqs = [_new_request()]
    ss.find_ver = ss.get("find_ver", 0) + 1


def page_find(ctx: Ctx) -> None:
    """Page "Find": find requests (FileMaker style), the found set, its records, and adding it to a tag."""
    st.header("Find records")
    totals = ctx.records.totals()
    if not totals["records"]:
        st.info("No records yet. Import a file under **New import** (pick its business and service there).")
    businesses = ctx.records.list_businesses()
    services = ctx.records.list_services()
    fields = find_fields(ctx.schema, ctx.records.columns_seen())
    by_label = {f.label: f for f in fields}

    # Limit to: applies to everything below, always.
    c1, c2 = st.columns(2)
    biz_pick = c1.multiselect("Limit to businesses", [b["id"] for b in businesses], key="find_biz",
                              format_func=lambda i: next((_biz_label(b) for b in businesses if b["id"] == i), i),
                              placeholder="All businesses")
    svc_pick = c2.multiselect("Limit to services", [s["id"] for s in services], key="find_svc",
                              format_func=lambda i: next((s["name"] for s in services if s["id"] == i), str(i)),
                              placeholder="All services")

    if "find_reqs" not in ss:
        _reset_requests()
    current: dict[int, pd.DataFrame] = {}
    with st.container(border=True):
        st.markdown("**Find requests** — a record must match every criterion of a request; several requests add "
                    "up (OR); an **Omit** request removes what it matches from the requests above it.")
        for n, req in enumerate(list(ss.find_reqs)):
            h1, h2, h3 = st.columns([1.2, 2.2, 0.8], vertical_alignment="bottom")
            h1.markdown(f"**Request {n + 1}**")
            kind = h2.segmented_control("Request type", ["Find", "Omit"], default="Omit" if req["omit"] else "Find",
                                        key=f"freq_kind_{ss.find_ver}_{req['id']}", label_visibility="collapsed")
            req["omit"] = kind == "Omit"
            if len(ss.find_reqs) > 1 and h3.button("Remove", key=f"freq_rm_{ss.find_ver}_{req['id']}",
                                                   width="stretch"):
                ss.find_reqs = [r for r in ss.find_reqs if r["id"] != req["id"]]
                st.rerun()
            current[req["id"]] = st.data_editor(
                req["base"], key=f"freq_{ss.find_ver}_{req['id']}", num_rows="dynamic", hide_index=True,
                width="stretch",
                column_config={
                    COL_FIELD: st.column_config.SelectboxColumn(options=list(by_label), width="medium",
                                                                help="What to search"),
                    COL_CRIT: st.column_config.TextColumn(width="large", help="What to look for (see How to "
                                                                              "search below)"),
                })
        b = st.columns(7)
        if b[6].button("Clear", width="stretch", help="Start the requests over (the found set stays)"):
            _reset_requests()
            st.rerun()
        add_find = b[0].button("+ Request", width="stretch", help="Add a request (OR)")
        add_omit = b[1].button("+ Omit request", width="stretch", help="Add a request that removes records")
        do_find = b[2].button("Find", type="primary", width="stretch", help="A new found set from these requests")
        do_constrain = b[3].button("Constrain", width="stretch", disabled="found" not in ss,
                                   help="Keep only found records that also match these requests")
        do_extend = b[4].button("Extend", width="stretch", disabled="found" not in ss,
                                help="Add the records these requests match to the found set")
        do_all = b[5].button("Show all", width="stretch", help="Every record (within Limit to)")
        with st.expander("How to search"):
            st.markdown(_help_text())

    if add_find or add_omit:
        _keep_edits(current)
        ss.find_reqs.append(_new_request(omit=add_omit))
        st.rerun()

    requests = _requests(current, by_label)
    try:
        if do_find:
            ss.found, ss.found_note = F.find_node(requests), "Find"
        elif do_constrain:
            ss.found, ss.found_note = F.constrain(ss.found, requests), ss.get("found_note", "") + " → constrained"
        elif do_extend:
            ss.found, ss.found_note = F.extend(ss.found, requests), ss.get("found_note", "") + " → extended"
        elif do_all:
            ss.found, ss.found_note = F.all_records(), "All records"
        if do_find or do_constrain or do_extend or do_all:
            ss.pop("found_csv", None)
    except F.FindError as exc:
        st.error(str(exc))

    if "found" not in ss:
        st.caption("Type criteria and click **Find**, or **Show all**.")
        return
    _found_set(ctx, fields, biz_pick, svc_pick, businesses)


def _keep_edits(current: dict[int, pd.DataFrame]) -> None:
    """Make what was typed the requests' starting values (before their editors are rebuilt)."""
    for req in ss.find_reqs:
        if req["id"] in current:
            req["base"] = current[req["id"]].reset_index(drop=True)
    ss.find_ver += 1


def _requests(current: dict[int, pd.DataFrame], by_label: dict[str, FindField]) -> list[dict]:
    """The requests on screen as records.find requests: {"omit", "criteria": [[field key, text], …]}."""
    out = []
    for req in ss.find_reqs:
        df = current.get(req["id"], req["base"])
        crit = []
        for _, row in df.iterrows():
            label, text = row.get(COL_FIELD), row.get(COL_CRIT)
            if label and isinstance(text, str) and text.strip():
                f = by_label.get(label)
                crit.append([f.key if f else f"missing:{label}", text])
        out.append({"omit": req["omit"], "criteria": crit})
    return out


def _help_text() -> str:
    """The search syntax, from records.criteria's documentation, for the How to search panel."""
    doc = CRITERIA_HELP.split("Overview\n--------\n", 1)[-1]
    lines = []
    for line in doc.splitlines():
        if line and not line.startswith(" "):
            lines.append(f"\n**{line.strip()}**\n")
        elif line.strip():
            lines.append("- " + line.strip())
    return "\n".join(lines)


def _found_set(ctx: Ctx, fields: list[FindField], biz_pick: list[str], svc_pick: list[int],
               businesses: list[dict]) -> None:
    """The found set: counts, the records table, one record's details, tag actions and CSV."""
    try:
        cond, params = F.where(ss.found, fields, biz_pick, svc_pick, timezone=ctx.tz, collation=ctx.db.collation)
        cnt = F.counts(ctx.db, cond, params)
    except F.FindError as exc:
        st.error(str(exc))
        return
    st.subheader("Found set")
    if ss.get("found_note"):
        st.caption(ss.found_note + (" · limited to the chosen businesses/services" if biz_pick or svc_pick else ""))
    m = st.columns(4)
    m[0].metric("Records", f"{cnt['records']:,}")
    m[1].metric("Unique phone numbers", f"{cnt['numbers']:,}")
    m[2].metric("Without a usable number", f"{cnt['no_phone']:,}")
    m[3].metric("Businesses", f"{cnt['businesses']:,}")
    if not cnt["records"]:
        st.info("No records match.")
        if st.button("Show omitted only", help="Every record (within Limit to) that isn't in the found set"):
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
    sig = hashlib.sha1(json.dumps([ss.found, biz_pick, svc_pick, sort], default=str).encode()).hexdigest()[:10]
    pg = o4.number_input(f"Page (of {pages:,})", 1, pages, 1, key=f"found_pg_{sig}_{per_page}")
    rows = F.page(ctx.db, ctx.schema, cond, params, sort, per_page, (pg - 1) * per_page)
    table = F.page_frame(rows, ctx.schema, show)
    st.caption("Select rows to see a record or to omit them from the found set.")
    event = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun", selection_mode="multi-row",
                         key=f"found_tbl_{sig}_{per_page}_{pg}",
                         column_config={"Record #": st.column_config.NumberColumn(format="%d", width="small"),
                                        "Import #": st.column_config.NumberColumn(format="%d", width="small"),
                                        "Row": st.column_config.NumberColumn(format="%d", width="small")})
    sel = list(event.selection.rows) if event and event.selection else []
    ids = [int(table.iloc[i]["Record #"]) for i in sel]

    a1, a2, a3 = st.columns(3)
    if a1.button(f"Omit selected ({len(ids)})", disabled=not ids, width="stretch"):
        ss.found = F.omit_records(ss.found, ids)
        ss.found_note = ss.get("found_note", "") + f" → {len(ids)} omitted"
        ss.pop("found_csv", None)
        st.rerun()
    if a2.button("Show omitted only", width="stretch", help="Every record (within Limit to) not in the found set"):
        ss.found, ss.found_note = F.show_omitted(ss.found), ss.get("found_note", "") + " → omitted only"
        ss.pop("found_csv", None)
        st.rerun()
    # The prepared CSV belongs to exactly this found set, Limit to and sort (sig); any change asks for a new one.
    csv_ready = ss.get("found_csv") and ss.found_csv["sig"] == sig
    if csv_ready:
        a3.download_button("⬇ Found set (CSV)", ss.found_csv["data"], f"found_set_{datetime.now():%Y%m%d-%H%M}.csv",
                           mime="text/csv", width="stretch", on_click="ignore")
    elif a3.button("Prepare CSV of the found set", width="stretch"):
        with st.spinner("Writing the CSV…"):
            ss.found_csv = {"sig": sig, "data": F.found_set_csv(ctx.db, ctx.schema, cond, params, sort)}
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
        st.markdown(f"**Record #{rec['id']}** · {rec['business_id']} · {rec['business_name']} · {rec['service_name']}"
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
            st.caption(f"The found set has records of {cnt['businesses']} businesses; use **Limit to** to keep one.")


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
    table = F.page_frame(rows, ctx.schema, names)
    ev = st.dataframe(table, hide_index=True, width="stretch", on_select="rerun", selection_mode="multi-row",
                      key=f"tagrec_tbl_{t['id']}_{pg}_{t['records']}",
                      column_config={"Record #": st.column_config.NumberColumn(format="%d", width="small")})
    sel = [int(table.iloc[i]["Record #"]) for i in (ev.selection.rows if ev and ev.selection else [])]
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
