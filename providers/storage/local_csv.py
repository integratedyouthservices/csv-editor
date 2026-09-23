"""Local CSV storage provider (development / fallback).

Config (storage.local_csv):
    path: sample_data/customers.csv
    id_column: null    # null → positional row ids; else a unique column
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pandas as pd

from providers.storage.base import ROW_ID, EditMap, StorageError, StorageProvider


class LocalCsvStorageProvider(StorageProvider):
    name = "local_csv"

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

    def apply_edits(self, df: pd.DataFrame, edits: EditMap) -> None:
        self.apply_changes(df, edits)

    def apply_changes(self, df, edits, inserts=(), deletes=()) -> None:
        """Rewrite the whole file with edits, new rows and deletions.

        The file is rewritten from scratch either way, so inserts and
        deletes cost no more than a plain cell edit and land in the same
        atomic replace.
        """
        id_column = self.settings.get("id_column")
        updated = df.copy()
        for (row_id, column), value in edits.items():
            updated.loc[row_id, column] = value

        if deletes:
            updated = updated.drop(index=[r for r in deletes if r in updated.index])

        if inserts:
            if id_column and any(not str(row.get(id_column, "")).strip() for row in inserts):
                # Row ids come from the file itself here; a blank one would
                # break load()'s uniqueness check on the next read.
                raise StorageError(
                    f"new rows need a value for the id column '{id_column}'"
                )
            new_rows = pd.DataFrame(
                [{c: row.get(c, "") for c in updated.columns} for row in inserts],
                columns=updated.columns,
            )
            updated = pd.concat([updated, new_rows], ignore_index=True)

        self._write(updated)

    def _write(self, updated: pd.DataFrame) -> None:
        # Atomic write: temp file in the same directory, then replace.
        target = self._path
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                updated.to_csv(fh, index=False)
            os.replace(tmp, target)
        except OSError as exc:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise StorageError(f"Failed to write {target}: {exc}") from exc

    def write_audit(self, metadata, records) -> None:
        """Append one JSON line per publish to `<csv>.audit.jsonl`."""
        audit_path = self._path.with_suffix(self._path.suffix + ".audit.jsonl")
        try:
            with open(audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({**metadata, "records": records}) + "\n")
        except OSError as exc:
            raise StorageError(f"Audit write failed ({audit_path}): {exc}") from exc

    def display_name(self) -> str:
        return self._path.name
