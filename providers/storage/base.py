from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

import pandas as pd

ROW_ID = "_row_id"
EditMap = dict[tuple[Any, str], Any]
RowValues = dict[str, Any]      # one whole row, {column: value}

# Where a loaded frame carries the backend version it was read at, for
# providers that can detect a concurrent write (see stamp_version).
VERSION_KEY = "_storage_version"


class StorageError(Exception):
    pass


def stamp_version(df: pd.DataFrame, version: Any) -> pd.DataFrame:
    """Record the backend version `df` was loaded at, and return `df`.

    The value rides in ``DataFrame.attrs``, which ``copy()`` propagates, so it
    survives the edit/merge round trip and stays bound to one session's data.
    That matters: the provider object itself is a process-wide singleton
    (``@st.cache_resource``), so a baseline stored on ``self`` would be shared
    between concurrent editors and guard nothing.
    """
    df.attrs[VERSION_KEY] = version
    return df


def version_of(df: pd.DataFrame) -> Any:
    """The version `df` was loaded at, or None if the provider doesn't stamp one."""
    return df.attrs.get(VERSION_KEY)


def blanks_as_null(
    records: list[dict[str, Any]], field_types: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Audit records with empty values turned into NULL for typed columns.

    Every value in an audit record is a string: the editor loads the dataset
    with `.astype(str)` and the before/after snapshot stringifies whatever it
    finds. A change log whose columns mirror the data's real types therefore
    gets `""` for an empty cell, and BigQuery rejects that outright — "Cannot
    convert value to floating point (bad value): " for an unset longitude.
    An empty cell means "no value", so that is what gets recorded. STRING
    columns keep their empty string, which is a value they can hold and one
    that reads back differently from a NULL. Columns the schema doesn't
    mention are passed through untouched for the backend to complain about.
    """
    rows: list[dict[str, Any]] = []
    for record in records:
        row: dict[str, Any] = {}
        for key, value in record.items():
            typed = (field_types.get(key) or "STRING").upper() != "STRING"
            if typed and (value is None or not str(value).strip()):
                value = None
            row[key] = value
        rows.append(row)
    return rows


class StorageProvider(ABC):
    name: str = "base"

    audit_before_data_write: bool = False

    supports_import: bool = False

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    @abstractmethod
    def load(self) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def apply_edits(self, df: pd.DataFrame, edits: EditMap) -> None:
        raise NotImplementedError

    def replace_all(self, new_df: pd.DataFrame) -> None:
        raise StorageError(f"{self.name} does not support replacing the whole dataset")

    def check_writable(self, df: pd.DataFrame) -> None:
        """Pre-flight a publish: raise StorageError if `df` is stale.

        Called before any audit/change-log write so a doomed publish fails
        clean, instead of leaving a change log describing edits that never
        landed. It narrows the race but does not close it — the write itself
        stays the authoritative guard. Default is a no-op.
        """
        return None

    def apply_changes(
        self,
        df: pd.DataFrame,
        edits: EditMap,
        inserts: list[RowValues] | tuple = (),
        deletes: list[Any] | tuple = (),
    ) -> None:
        """Cell edits plus whole-row inserts/deletes. `df` excludes the
        pending new rows; they arrive as `inserts`. A backend that can't
        add or delete rows refuses rather than publishing part of the set.
        """
        if inserts or deletes:
            raise StorageError(
                f"the '{self.name}' storage provider cannot add or delete rows"
            )
        self.apply_edits(df, edits)

    def _frame_with_changes(
        self,
        df: pd.DataFrame,
        edits: EditMap,
        inserts: list[RowValues] | tuple = (),
        deletes: list[Any] | tuple = (),
    ) -> pd.DataFrame:
        """The rewritten frame behind apply_changes for any provider that
        replaces its file wholesale, so a row add costs no more than an edit.
        """
        updated = df.copy()
        for (row_id, column), value in edits.items():
            updated.loc[row_id, column] = value

        if deletes:
            updated = updated.drop(index=[r for r in deletes if r in updated.index])

        if inserts:
            id_column = self.settings.get("id_column")
            if id_column and any(not str(row.get(id_column, "")).strip() for row in inserts):
                raise StorageError(
                    f"new rows need a value for the id column '{id_column}'"
                )
            new_rows = pd.DataFrame(
                [{c: row.get(c, "") for c in updated.columns} for row in inserts],
                columns=updated.columns,
            )
            updated = pd.concat([updated, new_rows], ignore_index=True)

        return updated

    def write_audit(
        self, metadata: dict[str, Any], records: list[dict[str, Any]]
    ) -> None:
        pass

    def display_name(self) -> str:
        return self.name
