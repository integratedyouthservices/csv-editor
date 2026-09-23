from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pandas as pd

from providers.storage.base import ROW_ID, EditMap, StorageError, StorageProvider


class LocalCsvStorageProvider(StorageProvider):
    name = "local_csv"
    supports_import = True

    @property
    def _path(self) -> Path:
        raw = self.settings.get("path")
        if not raw:
            raise StorageError("storage.local_csv.path is not configured")
        p = Path(raw)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent.parent / p
        return p

    def load(self) -> pd.DataFrame:
        try:
            df = pd.read_csv(self._path, dtype=str, keep_default_na=False)
        except FileNotFoundError as exc:
            raise StorageError(f"CSV file not found: {self._path}") from exc

        id_column = self.settings.get("id_column")
        if id_column:
            if df[id_column].duplicated().any():
                raise StorageError(f"id_column '{id_column}' has duplicate values")
            df = df.set_index(df[id_column].rename(ROW_ID), drop=False)
        else:
            df.index = pd.RangeIndex(len(df), name=ROW_ID)
        return df

    def _write_csv(self, df: pd.DataFrame) -> None:
        target = self._path
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                df.to_csv(fh, index=False)
            os.replace(tmp, target)
        except OSError as exc:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise StorageError(f"Failed to write {target}: {exc}") from exc

    def apply_edits(self, df: pd.DataFrame, edits: EditMap) -> None:
        self.apply_changes(df, edits)

    def apply_changes(self, df, edits, inserts=(), deletes=()) -> None:
        self._write_csv(self._frame_with_changes(df, edits, inserts, deletes))

    def replace_all(self, new_df: pd.DataFrame) -> None:
        self._write_csv(new_df)

    def write_audit(self, metadata, records) -> None:
        audit_path = self._path.with_suffix(self._path.suffix + ".audit.jsonl")
        try:
            with open(audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({**metadata, "records": records}) + "\n")
        except OSError as exc:
            raise StorageError(f"Audit write failed ({audit_path}): {exc}") from exc

    def display_name(self) -> str:
        return self._path.name
