"""GCS parquet storage provider, with a BigQuery change-log audit trail.

Config (storage.gcs_parquet):
    bucket:     collab-nprod-data
    blob_path:  collaborator_988_raw/raw_crisis_988_data/geo_coded_988_data.parquet
    id_column:  null                      # null -> positional row ids (like local_csv)
    change_log:
        project:  collab-infra-nprod
        dataset:  collaborator_988_raw
        table:    988_change_log
        location: US                      # optional

Credentials come from Application Default Credentials (same as bigquery.py):
    gcloud auth application-default login
or  GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

Requires: pip install google-cloud-storage google-cloud-bigquery pyarrow

Every read/write moves through an in-memory `io.BytesIO` buffer only —
never a local temp file — so there's no file handle left open, half-written,
or orphaned on disk if something fails partway through. Writes are a single
`upload_from_string` call, which is a single atomic GCS object PUT: readers
never observe a partially-written object, so this needs no temp-file/rename
dance the way a local filesystem write does.
"""
from __future__ import annotations

import io
from typing import Any

import pandas as pd

from providers.storage.base import ROW_ID, EditMap, StorageError, StorageProvider


class GcsParquetStorageProvider(StorageProvider):
    name = "gcs_parquet"
    audit_before_data_write = True   # change_log write happens BEFORE the parquet write
    supports_import = True

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        for key in ("bucket", "blob_path"):
            if not settings.get(key):
                raise StorageError(f"storage.gcs_parquet.{key} is required")
        change_log = settings.get("change_log") or {}
        for key in ("project", "dataset", "table"):
            if not change_log.get(key):
                raise StorageError(f"storage.gcs_parquet.change_log.{key} is required")

        try:
            from google.cloud import bigquery, storage  # noqa: F401
        except ImportError as exc:
            raise StorageError(
                "google-cloud-storage and google-cloud-bigquery are not installed. "
                "Run: pip install google-cloud-storage google-cloud-bigquery"
            ) from exc
        try:
            import pyarrow  # noqa: F401
        except ImportError as exc:
            raise StorageError(
                "pyarrow is not installed. Run: pip install pyarrow"
            ) from exc

    # ------------------------------------------------------------- clients

    def _storage_client(self):
        from google.cloud import storage

        return storage.Client()

    def _bigquery_client(self):
        from google.cloud import bigquery

        change_log = self.settings["change_log"]
        return bigquery.Client(
            project=change_log["project"], location=change_log.get("location")
        )

    def _blob(self):
        return self._storage_client().bucket(self.settings["bucket"]).blob(
            self.settings["blob_path"]
        )

    @property
    def _change_log_ref(self) -> str:
        c = self.settings["change_log"]
        return f"{c['project']}.{c['dataset']}.{c['table']}"

    # -------------------------------------------------------------- I/O

    def load(self) -> pd.DataFrame:
        try:
            data = self._blob().download_as_bytes()
        except Exception as exc:  # google api errors vary by version
            raise StorageError(f"GCS read failed: {exc}") from exc

        buf = io.BytesIO(data)
        try:
            df = pd.read_parquet(buf, engine="pyarrow")
        except Exception as exc:
            raise StorageError(f"Could not parse parquet data: {exc}") from exc
        finally:
            buf.close()

        id_column = self.settings.get("id_column")
        if id_column:
            if df[id_column].duplicated().any():
                raise StorageError(f"id_column '{id_column}' has duplicate values")
            df = df.set_index(df[id_column].rename(ROW_ID), drop=False)
        else:
            df.index = pd.RangeIndex(len(df), name=ROW_ID)
        # Editing works on strings; providers normalise on the way out.
        return df.astype(str)

    def _write_parquet(self, df: pd.DataFrame) -> None:
        buf = io.BytesIO()
        try:
            df.to_parquet(buf, engine="pyarrow", index=False)
            data = buf.getvalue()
        finally:
            buf.close()
        try:
            self._blob().upload_from_string(data, content_type="application/octet-stream")
        except Exception as exc:
            raise StorageError(f"GCS write failed: {exc}") from exc

    def apply_edits(self, df: pd.DataFrame, edits: EditMap) -> None:
        self.apply_changes(df, edits)

    def apply_changes(self, df, edits, inserts=(), deletes=()) -> None:
        self._write_parquet(self._frame_with_changes(df, edits, inserts, deletes))

    def replace_all(self, new_df: pd.DataFrame) -> None:
        self._write_parquet(new_df)

    def write_audit(self, metadata, records) -> None:
        """Append rows to the BigQuery change-log. `metadata` is ignored —
        the rows from core/audit.py are already fully shaped (17 data
        columns + change_id/change_state/change_type/changed_by/changed_at);
        merging in generic {last_updated_at, last_updated_by} keys would add
        columns the real change-log table's schema doesn't have, and
        insert_rows_json would reject the row.
        """
        if not records:
            return
        try:
            bq_errors = self._bigquery_client().insert_rows_json(
                self._change_log_ref, records
            )
        except Exception as exc:
            raise StorageError(f"Change log write to {self._change_log_ref} failed: {exc}") from exc
        if bq_errors:
            raise StorageError(
                f"Change log write to {self._change_log_ref} rejected rows: {bq_errors}"
            )

    def display_name(self) -> str:
        return self.settings["blob_path"].rsplit("/", 1)[-1]
