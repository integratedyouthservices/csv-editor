from __future__ import annotations

import base64
import html
import json
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
import streamlit as st

from core import audit
from core.config import AppConfig, ColumnRule, load_config
from core.csv_import import extra_columns, missing_columns, validate_dataframe
from core.publish import publish_with_audit
from core.validation import (
    errors_only,
    normalize_value,
    validate_edits,
    warnings_only,
)
from providers.auth import AuthError, create_auth_provider
from providers.storage import (
    ROW_ID,
    StorageError,
    create_storage_provider,
    stamp_version,
    version_of,
)

GREEN = "rgb(3,149,121)"
GREEN_HOVER = "rgb(2,119,97)"
EDIT_TINT = "rgb(230,245,241)"
ERROR_RED = "rgb(231,0,11)"
ERROR_FILL = "rgb(255,247,237)"
MATCH_YELLOW = "#fff3a6"
FILTER_BLUE = "#eef4fb"
MUTED = "rgb(161,161,161)"
SECONDARY = "rgb(82,82,82)"

st.set_page_config(page_title="Data Editor", page_icon="🗂️", layout="wide")


@st.cache_resource
def get_config() -> AppConfig:
    return load_config()


@st.cache_resource
def get_auth_provider():
    cfg = get_config()
    return create_auth_provider(cfg.auth_provider_name, cfg.auth_settings())


@st.cache_resource
def get_storage_provider():
    cfg = get_config()
    return create_storage_provider(cfg.storage_provider_name, cfg.storage_settings())


def init_state() -> None:
    ss = st.session_state
    ss.setdefault("user", None)
    ss.setdefault("original_df", None)
    ss.setdefault("edits", {})
    ss.setdefault("view", "editing")
    ss.setdefault("grid_ver", 0)
    ss.setdefault("undo_stack", [])
    ss.setdefault("redo_stack", [])
    ss.setdefault("just_published", None)
    ss.setdefault("audit_warning", None)
    ss.setdefault("import_df", None)
    ss.setdefault("import_edits", {})
    ss.setdefault("export_ready", None)
    ss.setdefault("export_error", None)
    ss.setdefault("publish_busy", False)
    ss.setdefault("publish_blocked", False)
    ss.setdefault("publish_error", None)
    for key, value in _ROW_STATE.items():
        ss.setdefault(key, value() if callable(value) else value)


_ROW_STATE = {
    "added_rows": list,          # row ids added this session, in order
    "deleted_rows": list,        # row ids pending deletion
    "new_row_seq": 0,            # counter behind the "new-N" row ids
    "pending_delete": None,      # row id the confirm modal is asking about
    "focus_row": None,           # row whose first cell should open for editing
    "focus_token": 0,            # bumped per focus request, see render_html_grid
}


def clear_row_state() -> None:
    # A pending new row is a blank row parked in original_df, so forgetting
    # added_rows without dropping it would strand it in the frame as an
    # ordinary row -- and publish it next time round.
    ss = st.session_state
    df = ss.get("original_df")
    stranded = [r for r in (ss.get("added_rows") or []) if df is not None and r in df.index]
    if stranded:
        ss.original_df = df.drop(index=stranded)
    for key, value in _ROW_STATE.items():
        ss[key] = value() if callable(value) else value


def bump_grid() -> None:
    st.session_state.grid_ver += 1


def load_data(force: bool = False) -> pd.DataFrame:
    ss = st.session_state
    if ss.original_df is None or force:
        ss.original_df = get_storage_provider().load()
        ss.edits = {}
        ss.undo_stack, ss.redo_stack = [], []
        clear_row_state()
        bump_grid()
    return ss.original_df


def rules_by_column() -> dict[str, ColumnRule]:
    return {r.name: r for r in get_config().columns}


def merge_edits(df: pd.DataFrame, edits: dict[tuple, str]) -> pd.DataFrame:
    out = df.copy()
    for (row_id, column), value in edits.items():
        if row_id in out.index and column in out.columns:
            out.at[row_id, column] = "" if value is None else str(value)
    return out


def df_with_edits(df: pd.DataFrame) -> pd.DataFrame:
    return merge_edits(df, st.session_state.edits)


def set_edit(row_id: Any, column: str, new_value: str) -> None:
    original = str(st.session_state.original_df.at[row_id, column])
    new = "" if new_value is None else str(new_value)
    if new == original:
        st.session_state.edits.pop((row_id, column), None)
    else:
        st.session_state.edits[(row_id, column)] = new


def row_number(df: pd.DataFrame, row_id: Any) -> int:
    return int(df.index.get_indexer([row_id])[0]) + 1


# --------------------------------------------------------- whole rows
# A pending new row is a blank row in original_df listed in added_rows;
# its typed values live in `edits` like any other cell. A pending delete
# keeps the row and lists it in deleted_rows. Both survive undo.

NEW_ROW_PREFIX = "new-"


def data_columns() -> list[str]:
    return [r.name for r in get_config().columns]


def row_values(df: pd.DataFrame, row_id: Any) -> dict[str, str]:
    """Every data column of a row, pending edits applied."""
    edits = st.session_state.edits
    values = {}
    for column in data_columns():
        if (row_id, column) in edits:
            values[column] = str(edits[(row_id, column)])
        elif row_id in df.index and column in df.columns:
            values[column] = str(df.at[row_id, column])
        else:
            values[column] = ""
    return values


def row_is_blank(df: pd.DataFrame, row_id: Any) -> bool:
    """A row of nothing but whitespace — never worth saving."""
    return all(not v.strip() for v in row_values(df, row_id).values())


def _insert_blank_row(row_id: Any, at: int, seq: int) -> None:
    ss = st.session_state
    df = ss.original_df
    blank = pd.DataFrame(
        [{c: "" for c in df.columns}],
        index=pd.Index([row_id], name=df.index.name),
        columns=df.columns,
    )
    # concat drops `attrs` unless every input carries the same ones, and the
    # blank row carries none -- so re-stamp the storage baseline the frame was
    # loaded at, or adding a row would make the next publish look unbacked.
    combined = pd.concat([df.iloc[:at], blank, df.iloc[at:]])
    combined.attrs = dict(df.attrs)
    ss.original_df = combined
    ss.added_rows.insert(seq, row_id)


def _drop_added_row(row_id: Any) -> None:
    """Remove an added row and everything hanging off it."""
    ss = st.session_state
    if row_id in ss.original_df.index:
        ss.original_df = ss.original_df.drop(index=[row_id])
    if row_id in ss.added_rows:
        ss.added_rows.remove(row_id)
    if row_id in ss.deleted_rows:
        ss.deleted_rows.remove(row_id)
    for key in [k for k in ss.edits if k[0] == row_id]:
        ss.edits.pop(key)


def focus_row(row_id: Any) -> None:
    """Ask the grid script to open this row's first cell editor."""
    ss = st.session_state
    ss.focus_row = row_id
    ss.focus_token += 1


def add_row() -> None:
    """Append a blank row — or, if the bottom row is already blank, just
    put the cursor in it (per spec: never stack empty rows)."""
    ss = st.session_state
    df = ss.original_df
    if df is None:
        return
    visible = [rid for rid in df.index if rid not in ss.deleted_rows]
    if visible and row_is_blank(df, visible[-1]):
        focus_row(visible[-1])
        bump_grid()
        return
    ss.new_row_seq += 1
    row_id = f"{NEW_ROW_PREFIX}{ss.new_row_seq}"
    record_row_action("add", row_id)
    _insert_blank_row(row_id, len(df), len(ss.added_rows))
    focus_row(row_id)
    bump_grid()


def delete_row(row_id: Any) -> None:
    """Mark a row for deletion — or, for a row added in this session
    (never published), simply take it back out again."""
    ss = st.session_state
    if row_id in ss.added_rows:
        drop_added_row(row_id)
        return
    record_row_action("del", row_id)
    if row_id not in ss.deleted_rows:
        ss.deleted_rows.append(row_id)
    bump_grid()


def drop_added_row(row_id: Any) -> None:
    """Take a pending new row back out again, undoably."""
    record_row_action("add", row_id)
    _drop_added_row(row_id)
    bump_grid()


def row_summary(df: pd.DataFrame, row_id: Any, limit: int = 3) -> str:
    """A few of a row's values, enough to recognise it in the change list."""
    values = [v.strip() for v in row_values(df, row_id).values() if v.strip()]
    if not values:
        return "(blank)"
    return " · ".join(values[:limit]) + (" …" if len(values) > limit else "")


def restore_row(row_id: Any) -> None:
    """Undo a pending deletion from the review screen."""
    record_row_action("del", row_id)
    if row_id in st.session_state.deleted_rows:
        st.session_state.deleted_rows.remove(row_id)
    bump_grid()


def pending_changes(df: pd.DataFrame) -> tuple[dict, list, list]:
    """(cell edits, added, deleted). Edits on an added/deleted row are left
    out -- the row travels as a unit. A still-blank added row isn't a change."""
    ss = st.session_state
    added = [rid for rid in ss.added_rows if rid in df.index and not row_is_blank(df, rid)]
    deleted = [rid for rid in ss.deleted_rows if rid in df.index]
    whole_rows = set(ss.added_rows) | set(deleted)
    cells = {
        k: v for k, v in ss.edits.items()
        if k[0] not in whole_rows and k[0] in df.index
    }
    return cells, added, deleted


def pending_count(df: pd.DataFrame) -> int:
    """The number on the "Review changes" badge: one per changed cell,
    one per added row, one per deleted row."""
    cells, added, deleted = pending_changes(df)
    return len(cells) + len(added) + len(deleted)


def validation_map(df: pd.DataFrame) -> dict[tuple, str]:
    """Cells to validate: every pending cell edit, plus every cell of an
    added row (a new row is validated whole, like the wireframes' edited
    cells). Deleted rows are on their way out, so they're skipped."""
    cells, added, _deleted = pending_changes(df)
    out = dict(cells)
    for row_id in added:
        for column, value in row_values(df, row_id).items():
            out[(row_id, column)] = value
    return out


def esc(text: Any) -> str:
    """HTML-escape a value for the hand-rolled markup, and encode any real
    newlines as numeric character references.

    The newline part is load-bearing, not cosmetic: st.markdown(
    unsafe_allow_html=True) runs the string through a Markdown parser
    before it ever reaches the DOM, and a blank line inside a raw-HTML
    block *ends* that block — everything after it gets parsed as Markdown
    prose instead. A single multi-paragraph cell would therefore shatter
    the whole grid, dumping the rest of the table markup onto the page as
    visible text (the 988 dataset has 105 such description cells; the
    older CSV sample had none, which is why this stayed hidden).

    &#10; keeps the emitted markup on one physical line while still
    decoding back to a real newline in the DOM, so `cell.dataset.value`
    hands the textarea editor the original text and multi-paragraph
    values round-trip through an edit unchanged.
    """
    escaped = html.escape("" if text is None else str(text))
    return escaped.replace("\r", "&#13;").replace("\n", "&#10;")


def inject_css() -> None:
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Open+Sans:ital,wght@0,400;0,600;1,400&display=swap');
        html, body, [class*="css"], .stApp {{ font-family: 'Open Sans', Arial, sans-serif; }}
        .stApp {{ background: rgb(241,239,234); }}
        /* Streamlit disables *every* widget whenever the websocket is not
           CONNECTED, so a routine reconnect greys out the whole toolbar --
           Export, Import, Publish -- for about a second. The status widget
           is the only thing that tells a user "reconnecting" rather than
           "this button is broken", so the header stays in the DOM to carry
           it and only the rest of its chrome is hidden. It cannot be
           display:none: the status widget lives inside it and would go with
           it. The header is position:absolute and would otherwise overlay
           the app's own toolbar row, so it is collapsed to zero height and
           made click-through, and the status widget is re-pinned to the
           bottom-right corner where it can't cover anything. */
        header[data-testid="stHeader"] {{
            background: transparent; height: 0; min-height: 0; pointer-events: none;
        }}
        header[data-testid="stHeader"] [data-testid="stToolbarActions"],
        header[data-testid="stHeader"] [data-testid="stAppDeployButton"],
        header[data-testid="stHeader"] [data-testid="stMainMenu"],
        header[data-testid="stHeader"] [data-testid="stDecoration"] {{ display: none; }}
        [data-testid="stStatusWidget"] {{
            position: fixed; right: 16px; bottom: 16px; z-index: 9999;
            pointer-events: auto;
        }}
        .block-container {{ padding-top: 1.5rem; max-width: 100%; padding-bottom: 0.5rem; }}

        .de-card {{
            background: #fff; border: 1px solid rgb(229,229,229); border-radius: 12px;
            box-shadow: 0 7px 8px -4px rgba(0,0,0,.1), 0 12px 17px 2px rgba(0,0,0,.08),
                        0 5px 22px 4px rgba(0,0,0,.06);
            padding: 14px 18px;
        }}
        .de-title {{ font-size: 17px; font-weight: 600; }}
        .de-fileinfo {{ font-size: 12px; color: {SECONDARY}; }}
        .de-note {{ font-style: italic; font-size: 12px; color: {SECONDARY}; }}

        .de-filterbar {{
            background: {FILTER_BLUE}; border: 1px solid #dbe7f6; border-radius: 6px;
            padding: 6px 12px; font-size: 13px; margin: 2px 0 8px;
        }}
        .de-filterbar mark {{ background: {MATCH_YELLOW}; padding: 0 2px; }}

        .de-legend {{ font-size: 12px; color: {SECONDARY}; }}
        .de-swatch {{
            display:inline-block; width:12px; height:12px; border:1px solid rgb(212,212,212);
            border-radius:3px; vertical-align:-2px; margin-right:5px;
        }}

        .de-diff-old {{ text-decoration: line-through; color: {MUTED}; font-size: 12px; }}
        .de-diff-new {{ font-weight: 600; }}
        .de-diff-err {{ color: {ERROR_RED}; font-size: 11px; }}
        .de-summary-err {{ color: {ERROR_RED}; font-weight: 600; }}

        .de-avatar {{
            display:inline-flex; align-items:center; justify-content:center;
            width:32px; height:32px; border-radius:50%; background:{GREEN};
            color:#fff; font-size:12px; font-weight:600; margin-top:2px;
        }}

        div.stButton > button[kind="primary"] {{
            background: {GREEN}; border-color: {GREEN}; color: #fff;
            border-radius: 6px; font-weight: 600;
        }}
        div.stButton > button[kind="primary"]:hover {{
            background: {GREEN_HOVER}; border-color: {GREEN_HOVER};
        }}
        /* Faded green rather than the old grey-and-dashed-border treatment.
           A button can be disabled for a second at a time through no fault
           of the user's (see the header comment above), and the dashed
           border read as "broken" where a fade reads as "not right now".
           The transition delay is what makes a short connection blip almost
           invisible: transitions only run on *changes*, so a button that is
           genuinely disabled when the page paints still shows up disabled
           immediately -- only a mid-life toggle waits out the delay. */
        div.stButton > button[kind="primary"]:disabled {{
            background: {GREEN}; border-color: {GREEN}; color: #fff; opacity: .45;
        }}
        div.stButton > button {{
            border-radius: 6px; transition: opacity .3s ease .15s, background .3s ease;
        }}
        div.stButton > button p {{ white-space: nowrap; }}
        @keyframes de-spin {{ to {{ transform: rotate(360deg); }} }}
        div[class*="st-key-publish_go_busy"] button::before {{
            content: ""; display: inline-block; flex: none;
            width: 13px; height: 13px; margin-right: 8px; border-radius: 50%;
            border: 2px solid rgb(212,212,212); border-top-color: {MUTED};
            animation: de-spin .7s linear infinite;
        }}
        div[data-testid="stTextInput"] div[data-baseweb="input"] {{ border-radius: 6px; }}
        div[data-testid="stTextInput"] div[data-baseweb="input"]:focus-within {{
            border-color: {GREEN}; box-shadow: 0 0 0 1px {GREEN};
        }}
        div[data-testid="stTextInput"] input {{
            border: none !important; box-shadow: none !important; outline: none !important;
        }}
        input::-ms-reveal, input::-ms-clear {{ display: none !important; }}

        [data-testid="stElementToolbar"] {{ display: none !important; }}

        div[data-testid="stPopover"] button[data-testid="stPopoverButton"] {{
            border-radius: 50%; width: 36px; height: 36px; min-height: 36px;
            padding: 0; background: {GREEN}; border: none; color: #fff;
            font-weight: 600; font-size: 12px;
        }}
        div[data-testid="stPopover"] button[data-testid="stPopoverButton"]:hover {{
            background: {GREEN_HOVER}; color: #fff;
        }}
        div[data-testid="stPopover"] button[data-testid="stPopoverButton"] svg {{
            display: none;
        }}
        div[data-testid="stForm"] {{
            border: 2px solid {GREEN}; border-radius: 8px; background: #fff;
        }}

        div[class*="st-key-cell_bridge"] {{
            position: absolute; width: 1px; height: 1px; overflow: hidden;
            opacity: 0; pointer-events: none;
        }}
        div[data-testid="stElementContainer"]:has(> iframe) {{
            height: 0; min-height: 0; margin: 0; padding: 0;
        }}
        div[data-testid="stElementContainer"] > iframe {{ height: 0; border: 0; display: block; }}

        .de-grid-wrap {{
            border: 1px solid rgb(229,229,229); border-radius: 8px;
            height: var(--de-grid-h, auto);
        }}
        table.de-grid {{
            border-collapse: collapse; width: 100%; font-size: 13px; table-layout: auto;
        }}
        table.de-grid th, table.de-grid td {{
            border: 1px solid rgb(235,235,235); padding: 5px 8px; text-align: left;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 260px;
        }}
        table.de-grid thead th {{
            position: sticky; top: 0; background: rgb(248,248,247);
            font-weight: 600; font-size: 11px; color: {SECONDARY}; z-index: 2;
        }}
        table.de-grid td.de-cell-pin, table.de-grid th.de-th-pin {{
            position: sticky; left: 0; background: #fff; z-index: 1;
            color: {MUTED}; text-align: right; width: 40px;
        }}
        table.de-grid thead th.de-th-pin {{ z-index: 3; background: rgb(248,248,247); }}
        table.de-grid tbody tr:hover td:not(.de-cell-pin) {{ background: rgb(250,250,249); }}
        table.de-grid td.de-cell[data-editable="1"] {{ cursor: text; }}
        table.de-grid td.de-cell-locked {{ color: {MUTED}; cursor: not-allowed; }}
        table.de-grid td.de-cell-err {{
            background: {ERROR_FILL}; color: {ERROR_RED}; font-weight: 600;
            box-shadow: inset 0 0 0 1px {ERROR_RED};
        }}
        table.de-grid td.de-cell-warn {{
            background: #fef3c7; color: #b45309; font-weight: 600;
            box-shadow: inset 0 0 0 1px #f59e0b;
        }}
        table.de-grid td.de-cell-edit {{ background: {EDIT_TINT}; }}
        table.de-grid td.de-cell-match {{ background: {MATCH_YELLOW}; }}

        /* ---- pending whole-row changes ---- */
        table.de-grid td.de-cell-pin-added {{
            box-shadow: inset 3px 0 0 {GREEN}; color: {GREEN}; font-weight: 600;
        }}
        table.de-grid td.de-cell-pin-deleted {{
            box-shadow: inset 3px 0 0 {ERROR_RED}; color: {ERROR_RED}; font-weight: 600;
        }}
        /* a row on its way out reads as dead: greyed, struck through and
           not editable (the :hover twin keeps the row-hover tint off it) */
        table.de-grid tbody tr.de-row-deleted td,
        table.de-grid tbody tr.de-row-deleted:hover td {{
            background: rgb(245,245,245); color: rgb(190,190,190);
            text-decoration: line-through; cursor: not-allowed; box-shadow: none;
            font-weight: 400;
        }}
        table.de-grid tbody tr.de-row-deleted td.de-cell-pin,
        table.de-grid tbody tr.de-row-deleted:hover td.de-cell-pin {{
            box-shadow: inset 3px 0 0 {ERROR_RED}; color: {ERROR_RED};
            text-decoration: none; font-weight: 600;
        }}

        /* right-click row menu — appended to <body> by the grid script */
        .de-row-menu {{
            position: fixed; z-index: 10001; background: #fff; padding: 4px;
            border: 1px solid rgb(229,229,229); border-radius: 8px; min-width: 160px;
            box-shadow: 0 6px 18px rgba(0,0,0,.18);
        }}
        .de-row-menu-item {{
            display: block; width: 100%; text-align: left; background: none;
            border: 0; border-radius: 6px; padding: 8px 12px; cursor: pointer;
            font: 400 13px 'Open Sans', Arial, sans-serif; color: rgb(38,39,48);
        }}
        .de-row-menu-item:hover {{ background: rgb(245,245,245); }}

        /* double-click popup editor — appended to <body>, position:fixed
           over the clicked cell (see render_grid_script) */
        .de-cell-editor {{
            position: fixed; z-index: 10000; font-family: 'Open Sans', Arial, sans-serif;
            font-size: 13px; padding: 4px 7px; box-sizing: border-box;
            border: 2px solid {GREEN}; border-radius: 3px; outline: none;
            box-shadow: 0 4px 14px rgba(0,0,0,.18); background: #fff; resize: none;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _landing_header(cfg: AppConfig) -> None:
    st.markdown(
        f"""
        <div style="max-width:420px;margin:48px auto 12px;text-align:center;">
          <div style="width:56px;height:56px;border-radius:50%;background:{GREEN};margin:0 auto 14px;
                      display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600;">DE</div>
          <div style="font-size:22px;font-weight:600;">{esc(cfg.title)}</div>
          <div class="de-note">{esc(cfg.subtitle)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_landing() -> None:
    """Landing page for a visitor with no verified identity yet.

    There is no login form: the app never sees credentials. Identity comes from
    the IAP-signed request header, so "Log in" just navigates the browser to the
    IAP-protected URL and lets IAP run the Google sign-in flow.
    """
    cfg = get_config()
    provider = get_auth_provider()

    error = None
    try:
        user = provider.authenticate_from_headers(st.context.headers)
    except AuthError as exc:
        user, error = None, str(exc)

    if user is not None:
        st.session_state.user = user
        st.rerun()

    _, mid, _ = st.columns([1, 1.05, 1])
    with mid:
        _landing_header(cfg)
        if error:
            # A header was there but could not be trusted (bad signature,
            # wrong audience, IAP misconfigured). Say so rather than offer a
            # Log in button that would land the visitor straight back here.
            st.error(f"Sign-in is unavailable: {error}")
            return
        st.link_button(
            "Log in with Google", provider.login_url(), type="primary", width="stretch"
        )


def render_header_login(provider) -> None:
    """Header-based providers (e.g. Google Cloud IAP): identity arrives
    already verified on the request headers of the session that opened the
    app -- there's no form to submit and no redirect to send the browser
    on, so this just reads the identity and, if present, logs it straight
    in.
    """
    cfg = get_config()
    try:
        user = provider.authenticate_from_headers(st.context.headers)
    except AuthError as exc:
        st.error(f"Sign-in is unavailable: {exc}")
        return

    if user is not None:
        st.session_state.user = user
        st.rerun()
        return

    _, mid, _ = st.columns([1, 1.05, 1])
    with mid:
        _landing_header(cfg)
        st.error(
            "This app must be accessed through its Identity-Aware Proxy "
            "URL. If you're already doing that and still see this, "
            "contact your administrator -- IAP isn't passing a valid "
            "identity header."
        )


# --------------------------------------------------------------- toolbar


@st.dialog("Discard unpublished edits?")
def confirm_discard_and_import(n_edits: int) -> None:
    st.markdown(
        f"You have **{n_edits} unpublished edit{'s' if n_edits != 1 else ''}**. "
        "Importing a new file will discard them — this can't be undone."
    )
    c1, c2 = st.columns(2)
    if c1.button("Cancel", width="stretch"):
        st.rerun()
    if c2.button("Discard and import", type="primary", width="stretch"):
        st.session_state.edits = {}
        st.session_state.undo_stack, st.session_state.redo_stack = [], []
        clear_row_state()
        st.session_state.view = "import_upload"
        bump_grid()
        st.rerun()


def render_toolbar(subtitle: str) -> None:
    cfg = get_config()
    user = st.session_state.user
    provider = get_storage_provider()
    c_title, c_search, c_export, c_import, c_review, c_avatar = st.columns(
        [2.1, 1.9, 1.05, 1.15, 1.6, 0.55]
    )
    with c_title:
        st.markdown(
            f'<div style="padding-top:6px;"><span class="de-title">{esc(cfg.title)}</span>'
            f'&nbsp;&nbsp;<span class="de-fileinfo">{esc(subtitle)}</span></div>',
            unsafe_allow_html=True,
        )
    with c_search:
        st.text_input(
            "Search",
            key="search",
            placeholder="🔍  Search all columns…",
            label_visibility="collapsed",
            on_change=bump_grid,
        )
    with c_export:
        if st.button("Export CSV", type="primary", width="stretch",
                      help="Download the latest published data as CSV"):
            try:
                fresh = provider.load()
            except StorageError as exc:
                st.session_state.export_ready = None
                st.session_state.export_error = str(exc)
            else:
                cols = [c.name for c in cfg.columns]
                csv_bytes = fresh.reindex(columns=cols).to_csv(index=False).encode("utf-8")
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                st.session_state.export_ready = (csv_bytes, f"988-Export-{ts}.csv")
                st.session_state.export_error = None
    with c_import:
        if st.button(
            "Import CSV", type="primary", width="stretch", disabled=not provider.supports_import,
            help=None if provider.supports_import
            else "This storage backend doesn't support importing a full dataset",
        ):
            if st.session_state.edits:
                confirm_discard_and_import(len(st.session_state.edits))
            else:
                st.session_state.view = "import_upload"
                st.rerun()
    with c_review:
        n_edits = pending_count(st.session_state.original_df)
        label = f"Review changes ({n_edits})" if n_edits else "Review changes"
        if st.button(label, width="stretch", disabled=n_edits == 0,
                     help=None if n_edits else "Edit a cell to enable review"):
            st.session_state.view = "review"
            bump_grid()
            st.rerun()
    with c_avatar:
        with st.popover(user.initials, help=user.display_name):
            st.markdown(f"**{esc(user.display_name)}**", unsafe_allow_html=True)
            # Auth is IAP-only, so there is no local sign-out to offer: IAP
            # re-authenticates from the request header on every rerun, and
            # clearing session state alone would just log the same identity
            # straight back in. Ending IAP's own session is the only real way
            # to switch accounts -- restart_login_url() builds that URL from
            # the configured login_url, so it still points at the external
            # load balancer rather than assuming a same-origin path.
            # https://cloud.google.com/iap/docs/sessions-howto
            st.caption("Signed in via Identity-Aware Proxy.")
            st.link_button(
                "Switch account",
                get_auth_provider().restart_login_url(),
                width="stretch",
            )

    if st.session_state.export_error:
        st.error(st.session_state.export_error)
        st.session_state.export_error = None
    if st.session_state.export_ready:
        data, filename = st.session_state.export_ready
        st.session_state.export_ready = None
        b64 = base64.b64encode(data).decode()
        st.iframe(
            f"""<script>
            const link = document.createElement('a');
            link.href = "data:text/csv;base64,{b64}";
            link.download = "{filename}";
            link.click();
            </script>""",
            height=1,
        )


def column_header(rule: ColumnRule) -> str:
    glyph = {"enum": " \u25be", "textarea": " \u00b6", "float": " #.#"}.get(rule.type, "")
    star = " *" if rule.required else ""
    return f"{rule.label}{star}{glyph}"


def cell_editor_kind(rule: ColumnRule) -> str:
    if rule.type in ("enum", "textarea", "float"):
        return rule.type
    return "text"


def render_html_grid(
    page_df: pd.DataFrame,
    base_df: pd.DataFrame,
    row_ids: list,
    rules: list[ColumnRule],
    edits: dict[tuple, str],
    errors: dict[tuple, str],
    warnings: dict[tuple, str],
    term: str,
    page: str,
    max_height: Optional[int],
    added: Optional[set] = None,
    deleted: Optional[set] = None,
    focus_pos: Optional[int] = None,
) -> None:
    """`page` + positional `data-pos` map a committed edit, or a right-click
    Delete Row, back to a row id; `focus_pos` opens that row's first editor."""
    added = added or set()
    deleted = deleted or set()
    term_l = term.strip().lower()
    head = ['<th class="de-th de-th-pin">#</th>']
    head += [f'<th class="de-th">{esc(column_header(r))}</th>' for r in rules]

    rows_html = []
    for pos, row_id in enumerate(row_ids):
        is_added, is_deleted = row_id in added, row_id in deleted
        pin_classes = ["de-cell", "de-cell-pin"]
        if is_deleted:
            pin_classes.append("de-cell-pin-deleted")
        elif is_added:
            pin_classes.append("de-cell-pin-added")
        cells = [
            f'<td class="{" ".join(pin_classes)}">{esc(row_number(base_df, row_id))}</td>'
        ]
        for r in rules:
            raw = str(page_df.at[row_id, r.name]) if row_id in page_df.index else ""
            key = (row_id, r.name)
            editable = r.editable and not is_deleted
            classes = ["de-cell"]
            if is_deleted:
                pass                         # the row's own class styles it dead
            elif key in errors:
                classes.append("de-cell-err")
            elif key in warnings:
                classes.append("de-cell-warn")
            elif key in edits or is_added:
                # every cell of a new row reads as edited, typed in or not
                classes.append("de-cell-edit")
            elif term_l and term_l in raw.lower():
                classes.append("de-cell-match")
            if not editable:
                classes.append("de-cell-locked")
            opts_attr = ""
            if r.type == "enum":
                opts_attr = f' data-options="{esc(json.dumps(list(r.options)))}"'
            cells.append(
                f'<td class="{" ".join(classes)}" data-pos="{pos}" data-col="{esc(r.name)}" '
                f'data-editable="{"1" if editable else "0"}" '
                f'data-type="{cell_editor_kind(r)}"{opts_attr} '
                f'data-value="{esc(raw)}">{esc(raw)}</td>'
            )
        row_classes = ["de-row"]
        if is_deleted:
            row_classes.append("de-row-deleted")
        elif is_added:
            row_classes.append("de-row-added")
        rows_html.append(
            f'<tr class="{" ".join(row_classes)}" data-pos="{pos}">{"".join(cells)}</tr>'
        )

    wrap_style = f"max-height:{max_height}px;overflow-y:auto;" if max_height else ""
    focus_attr = ""
    if focus_pos is not None:
        focus_attr = (
            f' data-focus-pos="{focus_pos}"'
            f' data-focus-token="{st.session_state.focus_token}"'
        )
    st.markdown(
        f'<div class="de-grid-wrap" style="{wrap_style}" data-gridpage="{page}"{focus_attr}>'
        f'<table class="de-grid"><thead><tr>{"".join(head)}</tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table></div>',
        unsafe_allow_html=True,
    )


def render_cell_bridge() -> None:
    st.text_input("cell bridge", key="cell_bridge", label_visibility="collapsed",
                   on_change=apply_bridge_edit)


def apply_bridge_edit() -> None:
    ss = st.session_state
    raw = ss.get("cell_bridge") or ""
    if not raw:
        return
    try:
        payload = json.loads(raw)
        page, pos = payload["page"], int(payload["pos"])
    except (ValueError, KeyError, TypeError):
        return
    row_ids = ss.get(f"_row_ids_{page}")
    if not row_ids or not (0 <= pos < len(row_ids)):
        return
    row_id = row_ids[pos]

    # The same bridge carries the row menu's "Delete Row": it only arms
    # the confirm modal, which is what actually deletes (see main()).
    if payload.get("action") == "delete_row":
        if row_id not in ss.deleted_rows:
            ss.pending_delete = row_id
        return

    try:
        column, value = payload["col"], payload["value"]
    except (KeyError, TypeError):
        return
    rule = rules_by_column().get(column)
    if rule is None or not rule.editable:
        return
    new = normalize_value(value, rule)

    if page == "import":
        if ss.import_df is None or column not in ss.import_df.columns:
            return
        original_value = str(ss.import_df.at[row_id, column])
        current = ss.import_edits.get((row_id, column), original_value)
        if new == str(current):
            return
        if new == original_value:
            ss.import_edits.pop((row_id, column), None)
        else:
            ss.import_edits[(row_id, column)] = new
        bump_grid()
        return

    original = ss.original_df
    current = ss.edits.get((row_id, column), str(original.at[row_id, column]))
    if new == str(current):
        return
    record_action(row_id, column)
    if new == str(original.at[row_id, column]):
        ss.edits.pop((row_id, column), None)
    else:
        ss.edits[(row_id, column)] = new
    bump_grid()


def render_grid_script(autosize: bool) -> None:
    """Cell editor, right-click row menu, autosizer and new-row focus.

    Handlers re-bind on every run: switching views swaps this component out,
    and a listener left behind by the torn-down iframe is a dead function.
    """
    fit_fn = """
        function fit() {
          const grid = doc.querySelector('.de-grid-wrap');
          if (!grid) return;
          const top = grid.getBoundingClientRect().top;
          const h = Math.max(P.innerHeight - top - 16, 220);
          doc.documentElement.style.setProperty('--de-grid-h', h + 'px');
        }
    """ if autosize else """
        // Not the editing page. Clear the height the editing grid fitted:
        // --de-grid-h is still set for a 290-row table, which this grid
        // would otherwise inherit as dead space below its last row.
        function fit() { doc.documentElement.style.removeProperty('--de-grid-h'); }
    """

    st.iframe(
        f"""<script>
        const P = window.parent, doc = P.document;

        // rebind every run -- see the docstring
        for (const [target, type, fn, cap] of (P.__deHandlers || [])) {{
          try {{ target.removeEventListener(type, fn, cap); }} catch (err) {{}}
        }}
        P.__deHandlers = [];
        function bind(target, type, fn, cap) {{
          target.addEventListener(type, fn, !!cap);
          P.__deHandlers.push([target, type, fn, !!cap]);
        }}

        {fit_fn}

        function findBridgeInput() {{
          const wrap = doc.querySelector('div[class*="st-key-cell_bridge"]');
          return wrap ? wrap.querySelector('input') : null;
        }}
        function setNativeValue(el, value) {{
          const proto = Object.getPrototypeOf(el);
          const desc = Object.getOwnPropertyDescriptor(proto, 'value');
          desc.set.call(el, value);
          el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        }}
        function send(payload) {{
          const input = findBridgeInput();
          if (!input) return;
          // `n` makes every payload distinct: Streamlit only fires the
          // widget's on_change when the value actually changes, so two
          // identical actions in a row would otherwise count as one.
          payload.n = Date.now();
          setNativeValue(input, JSON.stringify(payload));
          input.focus();
          input.blur();
        }}
        function pageOf(el) {{
          const wrap = el.closest('[data-gridpage]');
          return wrap ? wrap.dataset.gridpage : '';
        }}

        // ---------------------------------------- cell editor
        function commitAndClose(ed, save) {{
          if (!ed || !ed.isConnected) return;
          ed.onblur = null;
          const cell = ed.__deCell;
          if (save) {{
            const val = ed.value;
            cell.textContent = val;
            cell.dataset.value = val;
            send({{
              page: pageOf(cell),
              pos: parseInt(cell.dataset.pos, 10),
              col: cell.dataset.col,
              value: val,
            }});
          }}
          ed.remove();
        }}

        function openEditor(cell) {{
          const existing = doc.querySelector('.de-cell-editor');
          if (existing) commitAndClose(existing, true);

          const rect = cell.getBoundingClientRect();
          const type = cell.dataset.type;
          let ed;
          if (type === 'enum') {{
            ed = doc.createElement('select');
            const options = JSON.parse(cell.dataset.options || '[]');
            for (const opt of options) {{
              const o = doc.createElement('option');
              o.value = opt; o.textContent = opt;
              if (opt === cell.dataset.value) o.selected = true;
              ed.appendChild(o);
            }}
          }} else if (type === 'textarea') {{
            ed = doc.createElement('textarea');
            ed.value = cell.dataset.value;
          }} else {{
            ed = doc.createElement('input');
            ed.type = type === 'float' ? 'number' : 'text';
            if (type === 'float') ed.step = 'any';
            ed.value = cell.dataset.value;
          }}
          ed.className = 'de-cell-editor';
          ed.__deCell = cell;

          doc.body.appendChild(ed);
          ed.style.position = 'fixed';
          ed.style.left = rect.left + 'px';
          ed.style.top = rect.top + 'px';
          const natW = ed.scrollWidth, natH = ed.scrollHeight;
          ed.style.width = Math.max(rect.width, natW, 80) + 'px';
          ed.style.height = Math.max(rect.height, natH) + 'px';

          ed.addEventListener('keydown', (e) => {{
            if (e.key === 'Escape') {{
              e.preventDefault();
              commitAndClose(ed, false);
            }} else if (e.key === 'Enter' && !(type === 'textarea' && e.shiftKey)) {{
              e.preventDefault();
              commitAndClose(ed, true);
            }} else if (e.key === 'Tab') {{
              e.preventDefault();
              commitAndClose(ed, true);
            }}
          }});
          ed.onblur = () => commitAndClose(ed, true);

          ed.focus();
          if (ed.select) ed.select();
        }}

        // ---------------------------------------- right-click row menu
        function closeRowMenu() {{
          const menu = doc.querySelector('.de-row-menu');
          if (menu) menu.remove();
        }}
        function openRowMenu(tr, x, y) {{
          closeRowMenu();
          const menu = doc.createElement('div');
          menu.className = 'de-row-menu';
          const item = doc.createElement('button');
          item.type = 'button';
          item.className = 'de-row-menu-item';
          item.textContent = '✕  Delete Row';
          item.addEventListener('click', () => {{
            closeRowMenu();
            send({{ action: 'delete_row', page: pageOf(tr),
                    pos: parseInt(tr.dataset.pos, 10) }});
          }});
          menu.appendChild(item);
          menu.style.left = '-9999px';      // measure off-screen, then place
          doc.body.appendChild(menu);
          const r = menu.getBoundingClientRect();
          menu.style.left = Math.min(x, P.innerWidth - r.width - 8) + 'px';
          menu.style.top = Math.min(y, P.innerHeight - r.height - 8) + 'px';
        }}

        // ---------------------------------------- focus a new row
        // Python marks the row to open on the grid wrapper; the token
        // makes each request fire exactly once.
        function checkFocus() {{
          const wrap = doc.querySelector('.de-grid-wrap[data-focus-token]');
          if (!wrap) return;
          if (P.__deFocusToken === wrap.dataset.focusToken) return;
          P.__deFocusToken = wrap.dataset.focusToken;
          const cell = wrap.querySelector(
            'tbody tr[data-pos="' + wrap.dataset.focusPos + '"] td.de-cell[data-editable="1"]');
          if (!cell) return;
          cell.scrollIntoView({{ block: 'nearest', inline: 'nearest' }});
          openEditor(cell);
        }}

        function tick() {{ fit(); checkFocus(); }}

        // ---------------------------------------- wiring
        bind(doc, 'dblclick', (e) => {{
          const cell = e.target.closest('.de-cell[data-editable="1"]');
          if (!cell || cell.classList.contains('de-cell-pin')) return;
          e.preventDefault();
          openEditor(cell);
        }});

        bind(doc, 'contextmenu', (e) => {{
          // inside a cell editor the browser's own menu is the useful one
          if (e.target.closest('input, textarea, select, .de-cell-editor')) return;
          const tr = e.target.closest('table.de-grid tbody tr');
          if (!tr || tr.classList.contains('de-row-deleted')) return;
          e.preventDefault();
          openRowMenu(tr, e.clientX, e.clientY);
        }});
        bind(doc, 'mousedown', (e) => {{
          if (!e.target.closest('.de-row-menu')) closeRowMenu();
        }}, true);
        bind(doc, 'scroll', closeRowMenu, true);

        // Keyboard shortcuts: Ctrl/Cmd+Z = undo, Ctrl/Cmd+Shift+Z or
        // Ctrl+Y = redo. Skipped while a cell editor / input has focus
        // so native text-field undo still works there.
        bind(doc, 'keydown', (e) => {{
          if (e.key === 'Escape') closeRowMenu();
          if (!(e.ctrlKey || e.metaKey)) return;
          const k = e.key.toLowerCase();
          if (k !== 'z' && k !== 'y') return;
          const t = e.target;
          if (t && t.closest && t.closest('input, textarea, [contenteditable="true"]')) return;
          const wantRedo = k === 'y' || (k === 'z' && e.shiftKey);
          const btn = [...doc.querySelectorAll('button')].find(b =>
            b.textContent.trim().startsWith(wantRedo ? '↷' : '↶'));
          if (btn && !btn.disabled) {{ e.preventDefault(); btn.click(); }}
        }}, true);

        bind(P, 'resize', () => {{ fit(); P.requestAnimationFrame(fit); }});
        if (P.__deObserver) P.__deObserver.disconnect();
        P.__deObserver = new P.MutationObserver(() => {{
          P.clearTimeout(P.__deTickTimer);
          P.__deTickTimer = P.setTimeout(tick, 80);
        }});
        P.__deObserver.observe(doc.body, {{ childList: true, subtree: true }});

        tick();
        P.requestAnimationFrame(tick);
        </script>""",
        height=1,
    )


def _cell_instruction(row_id, column) -> dict:
    edits = st.session_state.edits
    if (row_id, column) in edits:
        return {"row": row_id, "col": column, "inst": "MODIFY",
                "value": edits[(row_id, column)]}
    return {"row": row_id, "col": column, "inst": "DELETE"}


def _row_instruction(kind: str, row_id) -> dict:
    """Instruction that restores this row's CURRENT pending state."""
    ss = st.session_state
    if kind == "add":
        if row_id in ss.added_rows:
            return {
                "kind": "add", "row": row_id, "inst": "PRESENT",
                "at": int(ss.original_df.index.get_indexer([row_id])[0]),
                "seq": ss.added_rows.index(row_id),
                "edits": {c: v for (r, c), v in ss.edits.items() if r == row_id},
                "deleted": row_id in ss.deleted_rows,
            }
        return {"kind": "add", "row": row_id, "inst": "ABSENT"}
    return {
        "kind": "del", "row": row_id,
        "inst": "DELETED" if row_id in ss.deleted_rows else "LIVE",
    }


def _state_instruction(instr: dict) -> dict:
    """The instruction that would restore whatever `instr` targets to
    the state it is in right now — i.e. the inverse of applying it."""
    kind = instr.get("kind", "cell")
    if kind == "cell":
        return _cell_instruction(instr["row"], instr["col"])
    return _row_instruction(kind, instr["row"])


def _apply_instruction(instr: dict) -> None:
    ss = st.session_state
    kind = instr.get("kind", "cell")

    if kind == "add":
        if instr["inst"] == "ABSENT":
            _drop_added_row(instr["row"])
        elif instr["row"] not in ss.added_rows:
            _insert_blank_row(instr["row"], instr["at"], instr["seq"])
            for column, value in instr.get("edits", {}).items():
                ss.edits[(instr["row"], column)] = value
            if instr.get("deleted") and instr["row"] not in ss.deleted_rows:
                ss.deleted_rows.append(instr["row"])
        return

    if kind == "del":
        if instr["inst"] == "DELETED":
            if instr["row"] not in ss.deleted_rows:
                ss.deleted_rows.append(instr["row"])
        elif instr["row"] in ss.deleted_rows:
            ss.deleted_rows.remove(instr["row"])
        return

    key = (instr["row"], instr["col"])
    if key[0] not in ss.original_df.index:
        return                           # row was undone out from under it
    if instr["inst"] == "DELETE":
        ss.edits.pop(key, None)
    else:
        value = str(instr["value"])
        if value == str(ss.original_df.at[key[0], key[1]]):
            ss.edits.pop(key, None)
        else:
            ss.edits[key] = value


def record_action(row_id, column) -> None:
    ss = st.session_state
    ss.undo_stack.append(_cell_instruction(row_id, column))
    ss.redo_stack.clear()


def record_row_action(kind: str, row_id) -> None:
    """record_action's whole-row twin — `kind` is "add" or "del"."""
    ss = st.session_state
    ss.undo_stack.append(_row_instruction(kind, row_id))
    ss.redo_stack.clear()


def undo() -> None:
    ss = st.session_state
    if not ss.undo_stack:
        return
    instr = ss.undo_stack.pop()
    ss.redo_stack.append(_state_instruction(instr))
    _apply_instruction(instr)
    bump_grid()


def redo() -> None:
    ss = st.session_state
    if not ss.redo_stack:
        return
    instr = ss.redo_stack.pop()
    ss.undo_stack.append(_state_instruction(instr))
    _apply_instruction(instr)
    bump_grid()


def revert_edit(row_id, column) -> None:
    record_action(row_id, column)
    st.session_state.edits.pop((row_id, column), None)
    bump_grid()


def render_grid_toolbar() -> None:
    ss = st.session_state
    left, right = st.columns([6, 2], vertical_alignment="center")
    with left, st.container(horizontal=True, gap="small"):
        st.button(
            "↶ Undo",
            width="content",
            disabled=not ss.undo_stack,
            help=f"{len(ss.undo_stack)} step{'s' if len(ss.undo_stack) != 1 else ''} to undo",
            on_click=undo,
        )
        st.button(
            "↷ Redo",
            width="content",
            disabled=not ss.redo_stack,
            help=f"{len(ss.redo_stack)} step{'s' if len(ss.redo_stack) != 1 else ''} to redo",
            on_click=redo,
        )
    with right, st.container(horizontal=True, horizontal_alignment="right"):
        st.button(
            "＋ Add Row",
            width="content",
            help="Add a blank row at the bottom of the table",
            on_click=add_row,
        )


def filter_rows(display_df: pd.DataFrame, term: str, always_keep=()) -> pd.DataFrame:
    if not term:
        return display_df
    t = term.lower()
    mask = (
        display_df.drop(columns=[ROW_ID], errors="ignore")
        .apply(lambda col: col.astype(str).str.lower().str.contains(t, regex=False))
        .any(axis=1)
    )
    if len(always_keep):
        # rows added this session are pending work, not search results —
        # hiding one behind a filter would make Add Row look broken
        mask = mask | pd.Series(
            display_df.index.isin(list(always_keep)), index=display_df.index
        )
    return display_df[mask]


def render_editing() -> None:
    ss = st.session_state
    cfg = get_config()
    df = load_data()
    deleted = [rid for rid in ss.deleted_rows if rid in df.index]
    total = len(df) - len(deleted)

    render_toolbar(f"{cfg.dataset_display_name} · {total:,} rows")

    if st.session_state.just_published:
        st.success(st.session_state.just_published)
        st.session_state.just_published = None
    if st.session_state.get("audit_warning"):
        st.warning(st.session_state.audit_warning)
        st.session_state.audit_warning = None

    # rows pending deletion are out of the editor until publish (undo,
    # or ↩ on the review screen, brings them back)
    display = df_with_edits(df).drop(index=deleted)
    term = (st.session_state.get("search") or "").strip()
    filtered = filter_rows(display, term, always_keep=ss.added_rows)

    rules = rules_by_column()
    edits = st.session_state.edits
    findings = validate_edits(validation_map(df), rules)
    errors = errors_only(findings)
    warnings = warnings_only(findings)

    if term:
        def _clear_search():
            st.session_state.search = ""

        bar = st.columns([5.5, 1])
        bar[0].markdown(
            f'<div class="de-filterbar">Showing <b>{len(filtered):,}</b> of {total:,} rows '
            f'matching "<mark>{esc(term)}</mark>"</div>',
            unsafe_allow_html=True,
        )
        bar[1].button("Clear search", width="stretch", on_click=_clear_search)

    render_grid_toolbar()

    row_ids = list(filtered.index)
    st.session_state["_row_ids_editing"] = row_ids
    focus_pos = row_ids.index(ss.focus_row) if ss.focus_row in row_ids else None
    render_cell_bridge()
    render_html_grid(
        filtered, df, row_ids, cfg.columns, edits, errors, warnings, term,
        page="editing", max_height=560,
        added=set(ss.added_rows), focus_pos=focus_pos,
    )
    render_grid_script(autosize=True)
    ss.focus_row = None                   # one grid render per focus request


def render_review() -> None:
    ss = st.session_state
    cfg = get_config()
    df = load_data()
    rules = rules_by_column()
    cells, added, deleted = pending_changes(df)
    findings = validate_edits(validation_map(df), rules)
    errors = errors_only(findings)
    warnings = warnings_only(findings)

    # every row with something pending, in dataset order
    edited_rows = {k[0] for k in cells}
    touched = edited_rows | set(added) | set(deleted)
    review_row_ids = [rid for rid in df.index if rid in touched]
    n_rows, n_cells = len(edited_rows), len(cells)
    n_changes = n_cells + len(added) + len(deleted)

    c_back, c_title, c_publish = st.columns([1.5, 4, 1.7])
    with c_back:
        if st.button("← Back to editing", width="stretch"):
            st.session_state.view = "editing"
            bump_grid()
            st.rerun()
    with c_title:
        if errors:
            summary = (
                f'<span class="de-summary-err">{len(errors)} '
                f'cell{"s" if len(errors) != 1 else ""} invalid</span>'
            )
        else:
            parts = []
            if n_cells or not (added or deleted):
                parts.append(
                    f'{n_rows} row{"s" if n_rows != 1 else ""} · '
                    f'{n_cells} cell{"s" if n_cells != 1 else ""} changed'
                )
            if added:
                parts.append(f'{len(added)} row{"s" if len(added) != 1 else ""} added')
            if deleted:
                parts.append(f'{len(deleted)} row{"s" if len(deleted) != 1 else ""} deleted')
            summary = " · ".join(parts)
        if warnings:
            summary += (
                f' <span style="color:#b45309;">· {len(warnings)} required '
                f'cell{"s" if len(warnings) != 1 else ""} blank</span>'
            )
        st.markdown(
            f'<div style="padding-top:6px;"><span class="de-title">Review changes</span>'
            f'&nbsp;&nbsp;<span class="de-fileinfo">{summary}</span></div>',
            unsafe_allow_html=True,
        )
    with c_publish:
        if st.button(
            "Publish changes",
            type="primary",
            width="stretch",
            disabled=bool(errors) or n_changes == 0,
            help="Fix invalid cells to enable publishing" if errors else None,
        ):
            reset_publish_gate()
            publish_dialog(n_rows, n_cells, len(added), len(deleted))

    if n_changes == 0:
        st.markdown('<div class="de-note">No pending changes — go back and edit some cells.</div>',
                    unsafe_allow_html=True)
        return

    page_df = df_with_edits(df.loc[review_row_ids])
    st.session_state["_row_ids_review"] = review_row_ids
    render_cell_bridge()
    render_html_grid(
        page_df, df, review_row_ids, cfg.columns, ss.edits, errors, warnings, "",
        page="review", max_height=None,
        added=set(added), deleted=set(deleted),
    )
    render_grid_script(autosize=False)

    st.markdown(
        '<div class="de-note" style="margin-top:4px;">Pending changes (old → new)</div>',
        unsafe_allow_html=True,
    )
    for row_id in added:
        values = row_values(df, row_id)
        filled = sum(1 for v in values.values() if v.strip())
        c1, c2 = st.columns([8, 0.6])
        c1.markdown(
            f'<div style="font-size:13px;padding-top:4px;'
            f'border-left:3px solid {GREEN};padding-left:8px;">'
            f'<span style="color:{MUTED};">row {row_number(df, row_id)}</span> · '
            f'<b>New row</b>: <span class="de-diff-old">(blank)</span> → '
            f'<span class="de-diff-new">{filled} of {len(values)} '
            f'cell{"s" if filled != 1 else ""} filled in</span></div>',
            unsafe_allow_html=True,
        )
        c2.button("↩", key=f"rev_added_{row_id}", help="Remove this new row",
                  on_click=drop_added_row, args=(row_id,))

    for row_id in deleted:
        c1, c2 = st.columns([8, 0.6])
        c1.markdown(
            f'<div style="font-size:13px;padding-top:4px;'
            f'border-left:3px solid {ERROR_RED};padding-left:8px;">'
            f'<span style="color:{MUTED};">row {row_number(df, row_id)}</span> · '
            f'<b>Row deleted</b>: '
            f'<span class="de-diff-old">{esc(row_summary(df, row_id))}</span> → '
            f'<span class="de-diff-new" style="color:{ERROR_RED};">(removed)</span></div>',
            unsafe_allow_html=True,
        )
        c2.button("↩", key=f"rev_deleted_{row_id}", help="Keep this row after all",
                  on_click=restore_row, args=(row_id,))

    for (row_id, column), new in sorted(
        cells.items(), key=lambda kv: (row_number(df, kv[0][0]), kv[0][1])
    ):
        rule = rules.get(column)
        label = rule.label if rule else column.upper()
        old = str(df.at[row_id, column])
        old_disp = esc(old) if old.strip() else '<span style="color:#9ca3af;">(blank)</span>'
        new_disp = esc(new) if str(new).strip() else '<span style="color:#9ca3af;">(blank)</span>'
        err = errors.get((row_id, column))
        warn = warnings.get((row_id, column))
        note_html = ""
        accent = ""
        new_style = ""
        if err:
            note_html = f'<div class="de-diff-err">✗ {esc(err)}</div>'
            accent = f'border-left:3px solid {ERROR_RED};padding-left:8px;'
            new_style = (
                f'background:{ERROR_FILL};color:{ERROR_RED};border:1px solid {ERROR_RED};'
                'border-radius:4px;padding:1px 6px;'
            )
        elif warn:
            note_html = f'<div style="color:#b45309;font-size:11px;">⚠ {esc(warn)}</div>'
            accent = 'border-left:3px solid #f59e0b;padding-left:8px;'
            new_style = (
                'background:#fef3c7;color:#b45309;border:1px solid #f59e0b;'
                'border-radius:4px;padding:1px 6px;'
            )
        c1, c2 = st.columns([8, 0.6])
        c1.markdown(
            f'<div style="font-size:13px;padding-top:4px;{accent}">'
            f'<span style="color:{MUTED};">row {row_number(df, row_id)}</span> · '
            f'<b>{esc(label)}</b>: '
            f'<span class="de-diff-old">{old_disp}</span> → '
            f'<span class="de-diff-new" style="{new_style}">{new_disp}</span>{note_html}</div>',
            unsafe_allow_html=True,
        )
        c2.button(
            "↩",
            key=f"rev_review_{row_id}_{column}",
            help=f'Revert to "{old}"',
            on_click=revert_edit,
            args=(row_id, column),
        )


def reset_publish_gate() -> None:
    """Clear the confirm-button gate so a freshly opened dialog can publish."""
    st.session_state.publish_busy = False
    st.session_state.publish_blocked = False
    st.session_state.publish_error = None


def publish_gate() -> bool:
    """Render the Cancel / Yes, publish row and say whether to publish now.

    The first click only flips the gate to busy and reruns the dialog, so the
    disabled, spinning button paints before the write blocks the script. The
    button stays disabled after a failure, so a failed publish can't be
    double-submitted.
    """
    busy = st.session_state.publish_busy
    c1, c2 = st.columns(2)
    if c1.button("Cancel", width="stretch", disabled=busy):
        st.rerun()
    clicked = c2.button(
        "Yes, publish",
        type="primary",
        width="stretch",
        disabled=busy or st.session_state.publish_blocked,
        key="publish_go_busy" if busy else "publish_go",
    )
    if st.session_state.publish_error:
        st.error(st.session_state.publish_error)
    if clicked:
        st.session_state.publish_busy = True
        st.rerun(scope="fragment")
    return busy


def fail_publish(message: str) -> None:
    """Stop the spinner, keep the button disabled, show why. Does not return."""
    st.session_state.publish_error = message
    st.session_state.publish_busy = False
    st.session_state.publish_blocked = True
    st.rerun(scope="fragment")
@st.dialog("Delete this row?", dismissible=False)
def delete_row_dialog(row_id: Any) -> None:
    df = st.session_state.original_df
    st.markdown(
        f"Row **{row_number(df, row_id)}** will be removed from the editor.\n\n"
        f"`{row_summary(df, row_id, limit=4)}`"
    )
    st.markdown(
        '<div class="de-note">a pending change like any other — undo brings it '
        "back, and nothing is written until you publish</div>",
        unsafe_allow_html=True,
    )
    c1, c2 = st.columns(2)
    if c1.button("Cancel", width="stretch"):
        st.session_state.pending_delete = None
        st.rerun()
    if c2.button("OK", type="primary", width="stretch"):
        delete_row(row_id)
        st.session_state.pending_delete = None
        st.rerun()


@st.dialog("Publish these changes?")
def publish_dialog(n_rows: int, n_cells: int, n_added: int = 0, n_deleted: int = 0) -> None:
    cfg = get_config()
    user = st.session_state.user
    what = []
    if n_cells or not (n_added or n_deleted):
        what.append(
            f"update **{n_cells} cell{'s' if n_cells != 1 else ''}** across "
            f"**{n_rows} row{'s' if n_rows != 1 else ''}**"
        )
    if n_added:
        what.append(f"add **{n_added} row{'s' if n_added != 1 else ''}**")
    if n_deleted:
        what.append(f"delete **{n_deleted} row{'s' if n_deleted != 1 else ''}**")
    st.markdown(
        f"You're about to {', '.join(what[:-1]) + ' and ' + what[-1] if len(what) > 1 else what[0]}"
        f" in **{cfg.dataset_display_name}**. This can't be undone."
    )
    st.markdown(
        f'<div class="de-note">saved as {esc(user.username)} · a second request '
        "records who changed what and when</div>",
        unsafe_allow_html=True,
    )
    if not publish_gate():
        return

    provider = get_storage_provider()
    # Catch a concurrent publish before the change log is written, so a
    # losing publish doesn't leave the log describing edits that never
    # reached the data. The write itself re-checks.
    try:
        provider.check_writable(st.session_state.original_df)
    except StorageError as exc:
        fail_publish(str(exc))
    ss = st.session_state
    df = ss.original_df
    cells, added, deleted = pending_changes(df)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data_columns = [c.name for c in cfg.columns]

    # An edited row logs a before/after pair under one change_id; an added
    # row logs one "after" row, a deleted row one "before" row.
    audit_records = audit.rows_for_edits(df, cells, data_columns, user.username, now)
    inserts = []
    for row_id in added:
        values = row_values(df, row_id)
        inserts.append(values)
        audit_records.append(audit.insert_row(values, data_columns, user.username, now))
    for row_id in deleted:
        audit_records.append(
            audit.delete_row(row_values(df, row_id), data_columns, user.username, now)
        )

    # The pending new rows aren't published data yet, so they travel as
    # `inserts` rather than as rows of the frame.
    base = df.drop(index=[r for r in ss.added_rows if r in df.index])
    outcome = publish_with_audit(
        provider,
        lambda: provider.apply_changes(base, cells, inserts, deleted),
        metadata={"last_updated_at": now, "last_updated_by": user.username},
        audit_records=audit_records,
    )
    if not outcome.ok:
        fail_publish(outcome.blocking_error)
    # apply_changes advanced the baseline on `base`; carry it back so the
    # next publish in this session isn't rejected as stale.
    stamp_version(ss.original_df, version_of(base))
    if inserts or deleted:
        # Row ids are positional for some backends, so adding or removing
        # rows renumbers everything — reload rather than patch in memory.
        load_data(force=True)
    else:
        ss.original_df = df_with_edits(ss.original_df)
        ss.edits = {}
        ss.undo_stack, ss.redo_stack = [], []
        clear_row_state()
    ss.view = "editing"
    done = []
    if n_cells:
        done.append(f"{n_cells} cell{'s' if n_cells != 1 else ''} across "
                    f"{n_rows} row{'s' if n_rows != 1 else ''}")
    if inserts:
        done.append(f"{len(inserts)} new row{'s' if len(inserts) != 1 else ''}")
    if deleted:
        done.append(f"{len(deleted)} deleted row{'s' if len(deleted) != 1 else ''}")
    ss.just_published = "Published " + (
        ", ".join(done[:-1]) + " and " + done[-1] if len(done) > 1 else done[0]
    ) + "."
    if outcome.audit_warning:
        ss.audit_warning = outcome.audit_warning
    bump_grid()
    st.rerun()


def render_import_upload() -> None:
    cfg = get_config()
    provider = get_storage_provider()
    if not provider.supports_import:
        st.error("This storage backend doesn't support importing a full dataset.")
        if st.button("Back to editor"):
            st.session_state.view = "editing"
            st.rerun()
        return

    st.markdown(
        f'<div style="padding-top:6px;"><span class="de-title">Import data</span>'
        f'&nbsp;&nbsp;<span class="de-fileinfo">Upload a CSV to fully replace '
        f'{esc(cfg.dataset_display_name)}</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="de-note" style="margin:4px 0 12px;">The uploaded file must have exactly '
        "the same columns as the current dataset. Every row in it will be validated, and the "
        "whole file replaces what's published today.</div>",
        unsafe_allow_html=True,
    )
    uploaded = st.file_uploader("Choose a CSV file", type=["csv"], key="import_uploader")

    c1, c2, _ = st.columns([1, 1, 4])
    if c1.button("Cancel", width="stretch"):
        st.session_state.view = "editing"
        st.rerun()
    validate = c2.button(
        "Validate & Continue", type="primary", width="stretch", disabled=uploaded is None
    )

    if not validate or uploaded is None:
        return

    try:
        raw_df = pd.read_csv(uploaded, dtype=str, keep_default_na=False)
    except Exception as exc:
        st.error(f"Could not read that file as a CSV: {exc}")
        return

    expected = [c.name for c in cfg.columns]
    got = list(raw_df.columns)
    missing = missing_columns(got, expected)
    extra = extra_columns(got, expected)
    if missing:
        st.error("Missing required column(s): " + ", ".join(missing))
    if extra:
        st.error("Unexpected column(s) not in the schema: " + ", ".join(extra))
    if missing or extra:
        return

    import_df = raw_df.reindex(columns=expected)
    import_df.index = pd.RangeIndex(len(import_df), name=ROW_ID)
    st.session_state.import_df = import_df
    st.session_state.import_edits = {}
    st.session_state.view = "import_review"
    bump_grid()
    st.rerun()


def render_import_review() -> None:
    cfg = get_config()
    rules = rules_by_column()
    if st.session_state.import_df is None:
        st.session_state.view = "import_upload"
        st.rerun()
        return

    display_df = merge_edits(st.session_state.import_df, st.session_state.import_edits)
    findings = validate_dataframe(display_df, rules)
    errors = errors_only(findings)
    warnings = warnings_only(findings)
    n_rows = len(display_df)

    c_title, c_discard, c_publish = st.columns([4, 1.6, 1.8])
    with c_title:
        if errors:
            summary = (
                f'<span class="de-summary-err">{len(errors)} '
                f'cell{"s" if len(errors) != 1 else ""} invalid</span>'
            )
        else:
            summary = f'{n_rows:,} row{"s" if n_rows != 1 else ""} ready to import'
        if warnings:
            summary += (
                f' <span style="color:#b45309;">· {len(warnings)} required '
                f'cell{"s" if len(warnings) != 1 else ""} blank</span>'
            )
        st.markdown(
            f'<div style="padding-top:6px;"><span class="de-title">Review import</span>'
            f'&nbsp;&nbsp;<span class="de-fileinfo">{summary}</span></div>',
            unsafe_allow_html=True,
        )
    with c_discard:
        if st.button("Discard import", width="stretch"):
            st.session_state.import_df = None
            st.session_state.import_edits = {}
            st.session_state.view = "editing"
            st.rerun()
    with c_publish:
        if st.button(
            "Publish Changes", type="primary", width="stretch",
            disabled=bool(errors),
            help="Fix invalid cells to enable publishing" if errors else None,
        ):
            reset_publish_gate()
            import_publish_dialog(display_df)

    row_ids = list(display_df.index)
    st.session_state["_row_ids_import"] = row_ids
    render_cell_bridge()
    render_html_grid(
        display_df, display_df, row_ids, cfg.columns, st.session_state.import_edits,
        errors, warnings, "", page="import", max_height=560,
    )
    render_grid_script(autosize=True)


@st.dialog("Publish imported data?")
def import_publish_dialog(final_df: pd.DataFrame) -> None:
    cfg = get_config()
    user = st.session_state.user
    n_rows = len(final_df)
    st.markdown(
        f"You're about to **replace the entire dataset** with **{n_rows:,} "
        f"row{'s' if n_rows != 1 else ''}** from the imported file, in "
        f"**{cfg.dataset_display_name}**. This can't be undone."
    )
    st.markdown(
        f'<div class="de-note">saved as {esc(user.username)} · the change log records what '
        "actually moved — rows are matched against what's published</div>",
        unsafe_allow_html=True,
    )
    if not publish_gate():
        return

    provider = get_storage_provider()
    # final_df was built from the uploaded file, so it carries no baseline
    # of its own; the version to replace is the one this session loaded.
    stamp_version(final_df, version_of(st.session_state.original_df))
    try:
        provider.check_writable(final_df)
    except StorageError as exc:
        fail_publish(str(exc))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data_columns = [c.name for c in cfg.columns]
    # Matched on dataset.identity_columns, so the log records real
    # updates/inserts/deletes rather than one insert per uploaded row.
    audit_records = audit.rows_for_replace_diff(
        st.session_state.original_df, final_df, data_columns,
        cfg.identity_columns, user.username, now,
    )
    outcome = publish_with_audit(
        provider,
        lambda: provider.replace_all(final_df),
        metadata={},
        audit_records=audit_records,
    )
    if not outcome.ok:
        fail_publish(outcome.blocking_error)
    st.session_state.original_df = final_df
    st.session_state.import_df = None
    st.session_state.import_edits = {}
    st.session_state.edits = {}
    st.session_state.undo_stack, st.session_state.redo_stack = [], []
    # a full replace invalidates any pending row adds/deletes
    clear_row_state()
    st.session_state.view = "editing"
    st.session_state.just_published = (
        f"Imported and published {n_rows:,} row{'s' if n_rows != 1 else ''}."
    )
    if outcome.audit_warning:
        st.session_state.audit_warning = outcome.audit_warning
    bump_grid()
    st.rerun()


def main() -> None:
    init_state()
    inject_css()

    if st.session_state.user is None:
        render_landing()
        return

    try:
        view = st.session_state.view
        if view == "review":
            render_review()
        elif view == "import_upload":
            render_import_upload()
        elif view == "import_review":
            render_import_review()
        else:
            render_editing()
        # armed by the grid's right-click menu (see apply_bridge_edit);
        # opened after the view so the modal lands on top of the grid
        pending = st.session_state.pending_delete
        if pending is not None:
            if pending in st.session_state.original_df.index:
                delete_row_dialog(pending)
            else:
                st.session_state.pending_delete = None
    except StorageError as exc:
        st.error(f"Storage error: {exc}")
        if st.button("Retry"):
            st.session_state.original_df = None
            st.rerun()


if __name__ == "__main__":
    main()
