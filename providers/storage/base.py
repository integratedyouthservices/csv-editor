from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

ROW_ID = "_row_id"
EditMap = dict[tuple[Any, str], Any]

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

    def write_audit(
        self, metadata: dict[str, Any], records: list[dict[str, Any]]
    ) -> None:
        pass

    def display_name(self) -> str:
        return self.name
