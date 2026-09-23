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

    def write_audit(
        self, metadata: dict[str, Any], records: list[dict[str, Any]]
    ) -> None:
        """Optional second write, issued AFTER a successful publish.

        `metadata`: {"last_updated_at": iso-timestamp, "last_updated_by": email}
        `records`:  one dict per changed cell:
                    {row_id, column, old_value, new_value, change_type,
                     timestamp, user}
                    `change_type` is insert | update | delete. An added
                    row logs every column with a blank old_value, a
                    deleted row every column with a blank new_value.

        The CSV publish stands even if this fails — the app surfaces a
        non-blocking warning. Default: no-op for backends without an
        audit store.
        """

    def display_name(self) -> str:
        """Human-readable source name for the toolbar (override freely)."""
        return self.name
