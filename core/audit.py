from __future__ import annotations

import uuid
from typing import Any, Mapping

import pandas as pd

from providers.storage.base import EditMap

CHANGE_STATE_BEFORE = "before"
CHANGE_STATE_AFTER = "after"
CHANGE_TYPE_INSERT = "insert"
CHANGE_TYPE_UPDATE = "update"
CHANGE_TYPE_DELETE = "delete"


def new_change_id() -> str:
    return f"CHG-{uuid.uuid4().hex[:8].upper()}"


def _snapshot(values: Mapping[str, Any], data_columns: list[str]) -> dict[str, Any]:
    return {col: "" if values.get(col) is None else str(values.get(col, "")) for col in data_columns}


def _row(
    values: Mapping[str, Any],
    data_columns: list[str],
    change_id: str,
    change_state: str,
    change_type: str,
    changed_by: str,
    changed_at: str,
) -> dict[str, Any]:
    return {
        **_snapshot(values, data_columns),
        "change_id": change_id,
        "change_state": change_state,
        "change_type": change_type,
        "changed_by": changed_by,
        "changed_at": changed_at,
    }


def update_rows(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    data_columns: list[str],
    changed_by: str,
    changed_at: str,
) -> list[dict[str, Any]]:
    change_id = new_change_id()
    return [
        _row(before, data_columns, change_id, CHANGE_STATE_BEFORE, CHANGE_TYPE_UPDATE, changed_by, changed_at),
        _row(after, data_columns, change_id, CHANGE_STATE_AFTER, CHANGE_TYPE_UPDATE, changed_by, changed_at),
    ]


def insert_row(
    after: Mapping[str, Any], data_columns: list[str], changed_by: str, changed_at: str
) -> dict[str, Any]:
    return _row(
        after, data_columns, new_change_id(), CHANGE_STATE_AFTER, CHANGE_TYPE_INSERT, changed_by, changed_at
    )


def delete_row(
    before: Mapping[str, Any], data_columns: list[str], changed_by: str, changed_at: str
) -> dict[str, Any]:
    return _row(
        before, data_columns, new_change_id(), CHANGE_STATE_BEFORE, CHANGE_TYPE_DELETE, changed_by, changed_at
    )


def rows_for_edits(
    original_df: pd.DataFrame,
    edits: EditMap,
    data_columns: list[str],
    changed_by: str,
    changed_at: str,
) -> list[dict[str, Any]]:
    by_row: dict[Any, dict[str, Any]] = {}
    for (row_id, column), value in edits.items():
        by_row.setdefault(row_id, {})[column] = value

    rows: list[dict[str, Any]] = []
    for row_id, patch in by_row.items():
        before = {col: original_df.at[row_id, col] for col in data_columns}
        after = {**before, **patch}
        rows.extend(update_rows(before, after, data_columns, changed_by, changed_at))
    return rows


def rows_for_full_replace(
    new_df: pd.DataFrame, data_columns: list[str], changed_by: str, changed_at: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _, row in new_df.iterrows():
        after = {col: row.get(col) for col in data_columns}
        rows.append(insert_row(after, data_columns, changed_by, changed_at))
    return rows


def _identity_key(values: Mapping[str, Any], identity_columns: list[str]) -> tuple:
    snap = _snapshot(values, identity_columns)
    return tuple(snap[col] for col in identity_columns)


def _by_identity(
    df: pd.DataFrame, data_columns: list[str], identity_columns: list[str]
) -> dict[tuple, list[dict[str, Any]]]:
    """Group a frame's rows by their identity key, preserving row order.

    Values are snapshotted here so both sides of a diff are compared as the
    same stringified form that gets written to the change log -- otherwise a
    float read back from parquet as 45.0 would look like a change against the
    "45.0" that came in from a CSV.
    """
    grouped: dict[tuple, list[dict[str, Any]]] = {}
    for _, row in df.iterrows():
        values = {col: row.get(col) for col in data_columns}
        grouped.setdefault(_identity_key(values, identity_columns), []).append(
            _snapshot(values, data_columns)
        )
    return grouped


def rows_for_replace_diff(
    original_df: pd.DataFrame,
    new_df: pd.DataFrame,
    data_columns: list[str],
    identity_columns: list[str],
    changed_by: str,
    changed_at: str,
) -> list[dict[str, Any]]:
    """Change-log rows for a full-file replace, diffed against what's published.

    Rows are matched on `identity_columns` (a composite natural key -- this
    dataset has no id column). A matched pair whose other values differ logs
    one before/after update pair; a key only in the new file logs an insert;
    a key only in the current data logs a delete; an unchanged row logs
    nothing at all.

    Identity keys are not guaranteed unique -- neither the import validator
    nor the parquet enforces it -- so rows sharing a key are paired off in
    file order within that group, and whichever side has more rows left over
    contributes inserts or deletes. That keeps the diff total and
    deterministic instead of failing a publish over duplicate data the editor
    didn't create.

    With no identity_columns configured there is nothing to match on, so this
    falls back to logging every row as an insert (see rows_for_full_replace).
    """
    if not identity_columns:
        return rows_for_full_replace(new_df, data_columns, changed_by, changed_at)

    old_groups = _by_identity(original_df, data_columns, identity_columns)
    new_groups = _by_identity(new_df, data_columns, identity_columns)

    rows: list[dict[str, Any]] = []
    for key, new_rows in new_groups.items():
        old_rows = old_groups.get(key, [])
        for i, after in enumerate(new_rows):
            if i < len(old_rows):
                before = old_rows[i]
                if before != after:
                    rows.extend(
                        update_rows(before, after, data_columns, changed_by, changed_at)
                    )
            else:
                rows.append(insert_row(after, data_columns, changed_by, changed_at))

    for key, old_rows in old_groups.items():
        surplus = old_rows[len(new_groups.get(key, [])):]
        for before in surplus:
            rows.append(delete_row(before, data_columns, changed_by, changed_at))

    return rows
