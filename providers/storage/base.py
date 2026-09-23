"""Storage provider interface.

A provider loads the dataset into a pandas DataFrame and applies a set
of cell edits back to the underlying store.

Contract:
  * `load()` must return a DataFrame whose index is a stable, unique
    row id (`df.index.name == ROW_ID`). All edits are keyed by
    (row_id, column_name), so the id must survive filtering/sorting
    and round-trip to `apply_edits()`.
  * `apply_edits()` receives {(row_id, column): new_value} where every
    value has already passed validation, and must persist atomically
    where the backend allows it.
  * `apply_changes()` is the same thing plus whole-row inserts and
    deletes. Override it to support adding/deleting rows; the default
    handles the edits-only case and refuses the rest rather than
    silently dropping them.
  * `replace_all()` swaps the whole dataset out (the CSV import).
    Optional — set `supports_import = True` when you implement it.

To add a provider:
  1. Subclass StorageProvider, implement load() + apply_edits().
  2. Register it in providers/storage/__init__.py.
  3. Point `storage.provider` at it in config.yaml.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

ROW_ID = "_row_id"
EditMap = dict[tuple[Any, str], Any]
RowValues = dict[str, Any]      # one whole row, {column: value}


class StorageError(Exception):
    """Raised when a storage backend cannot load or persist data."""


class StorageProvider(ABC):
    name: str = "base"

    # True => the app calls write_audit() BEFORE the data write, and a failed
    # data write after a successful audit write is surfaced as a distinct,
    # blocking error (an audit trail slightly ahead of reality is the safer
    # failure mode for a compliance log than data changing with no record of
    # it at all). False (default) preserves the existing behavior: data write
    # first, a failed write_audit() is only a non-blocking warning.
    audit_before_data_write: bool = False

    # True => this provider implements replace_all() for bulk dataset
    # replacement (e.g. CSV import). Lets the UI gate the Import feature
    # generically instead of catching NotImplementedError at click time.
    supports_import: bool = False

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    @abstractmethod
    def load(self) -> pd.DataFrame:
        """Load the full dataset. Index = stable unique row id."""
        raise NotImplementedError

    @abstractmethod
    def apply_edits(self, df: pd.DataFrame, edits: EditMap) -> None:
        """Persist `edits` ({(row_id, column): new_value}) to the backend.

        `df` is the current in-memory dataset (original values), provided
        for backends that rewrite whole objects (e.g. a CSV file).
        """
        raise NotImplementedError

    def replace_all(self, new_df: pd.DataFrame) -> None:
        """Replace the entire dataset with `new_df` (e.g. a CSV import).

        Unlike apply_edits(), this isn't a patch — every row in the backend
        is replaced by every row in `new_df`. Optional: only providers with
        supports_import = True need to implement this.
        """
        raise StorageError(f"{self.name} does not support replacing the whole dataset")

    def apply_changes(
        self,
        df: pd.DataFrame,
        edits: EditMap,
        inserts: list[RowValues] | tuple = (),
        deletes: list[Any] | tuple = (),
    ) -> None:
        """Persist cell edits plus whole-row inserts and deletes.

        `df` is the current in-memory dataset WITHOUT the pending new
        rows (they arrive as `inserts`, one {column: value} dict each,
        already stripped of rows that were left entirely blank).
        `deletes` is a list of row ids to remove.

        The default supports edits only: a backend that cannot add or
        delete rows raises instead of quietly publishing a partial
        change set.
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
        """The whole-dataset rewrite behind apply_changes for every
        file-backed provider: apply the cell edits, drop the deleted
        rows, append the new ones. A provider that rewrites its file
        wholesale (CSV, parquet) just hands the result to its writer —
        inserts and deletes then cost no more than a plain cell edit and
        land in the same atomic replace.
        """
        updated = df.copy()
        for (row_id, column), value in edits.items():
            updated.loc[row_id, column] = value

        if deletes:
            updated = updated.drop(index=[r for r in deletes if r in updated.index])

        if inserts:
            id_column = self.settings.get("id_column")
            if id_column and any(not str(row.get(id_column, "")).strip() for row in inserts):
                # Row ids come from the data itself here; a blank one would
                # break load()'s uniqueness check on the next read.
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
        """Optional audit write. Ordering relative to the data write is
        controlled by `audit_before_data_write` (see above), not by this
        method — implementations just persist whatever `records` they're
        given.

        `metadata`: {"last_updated_at": iso-timestamp, "last_updated_by": email}
        `records`:  a list of caller-defined row dicts — this base class
                    does not prescribe their shape (existing implementations
                    treat it as an opaque passthrough), so callers are free
                    to pass per-cell diffs, full before/after row snapshots,
                    or anything else a given deployment's audit store needs.
                    This app passes the change-log rows built by
                    core/audit.py — see that module for their shape.

        A failing write_audit() is non-blocking (see audit_before_data_write
        for the exception). Default: no-op for backends without an audit
        store.
        """

    def display_name(self) -> str:
        """Human-readable source name for the toolbar (override freely)."""
        return self.name
