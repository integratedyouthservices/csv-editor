"""Local parquet storage provider (development / fallback).

The on-disk twin of gcs_parquet.py: same parquet payload and the same
string-typed editing contract, read from a local file instead of a GCS
object, so `streamlit run app.py` works against the real 988 dataset with
zero GCP setup.

Config (storage.local_parquet):
    path: sample_data/geo_coded_988_data.parquet
    id_column: null    # null -> positional row ids; else a unique column

Requires: pip install pyarrow
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pandas as pd

from providers.storage.base import ROW_ID, EditMap, StorageError, StorageProvider


class LocalParquetStorageProvider(StorageProvider):
    name = "local_parquet"
    supports_import = True

    def __init__(self, settings):
        super().__init__(settings)
        try:
            import pyarrow  # noqa: F401
        except ImportError as exc:
            raise StorageError(
                "pyarrow is not installed. Run: pip install pyarrow"
            ) from exc

    @property
    def _path(self) -> Path:
        raw = self.settings.get("path")
        if not raw:
            raise StorageError("storage.local_parquet.path is not configured")
        p = Path(raw)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent.parent / p
        return p

    def load(self) -> pd.DataFrame:
        try:
            df = pd.read_parquet(self._path, engine="pyarrow")
        except FileNotFoundError as exc:
            raise StorageError(f"Parquet file not found: {self._path}") from exc
        except Exception as exc:
            raise StorageError(f"Could not parse parquet file {self._path}: {exc}") from exc

        id_column = self.settings.get("id_column")
        if id_column:
            if id_column not in df.columns:
                raise StorageError(f"id_column '{id_column}' is not a column in {self._path.name}")
            if df[id_column].duplicated().any():
                raise StorageError(f"id_column '{id_column}' has duplicate values")
            df = df.set_index(df[id_column].rename(ROW_ID), drop=False)
        else:
            df.index = pd.RangeIndex(len(df), name=ROW_ID)

        # Editing works on strings everywhere in the app (local_csv reads
        # dtype=str, keep_default_na=False). Nulls have to be masked to ""
        # BEFORE astype(str), or pandas stringifies them into literal
        # "<NA>"/"nan" cells that would then fail validation and get
        # published back as real text.
        return df.astype(object).where(df.notna(), "").astype(str)

    def _write_parquet(self, df: pd.DataFrame) -> None:
        # Atomic write: temp file in the same directory, then replace.
        target = self._path
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        os.close(fd)
        try:
            df.to_parquet(tmp, engine="pyarrow", index=False)
            os.replace(tmp, target)
        except Exception as exc:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise StorageError(f"Failed to write {target}: {exc}") from exc

    def apply_edits(self, df: pd.DataFrame, edits: EditMap) -> None:
        updated = df.copy()
        for (row_id, column), value in edits.items():
            updated.loc[row_id, column] = value
        self._write_parquet(updated)

    def replace_all(self, new_df: pd.DataFrame) -> None:
        self._write_parquet(new_df)

    def write_audit(self, metadata, records) -> None:
        """Append one JSON line per publish to `<file>.audit.jsonl`."""
        audit_path = self._path.with_suffix(self._path.suffix + ".audit.jsonl")
        try:
            with open(audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({**metadata, "records": records}) + "\n")
        except OSError as exc:
            raise StorageError(f"Audit write failed ({audit_path}): {exc}") from exc

    def display_name(self) -> str:
        return self._path.name
