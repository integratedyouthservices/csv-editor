from __future__ import annotations

import io
import os
from typing import Any

import pandas as pd

from providers.storage.base import (
    ROW_ID,
    EditMap,
    StorageError,
    StorageProvider,
    blanks_as_null,
    stamp_version,
    version_of,
)

_CONFLICT = (
    "Someone else published to this dataset after you loaded it, so nothing "
    "was overwritten and their changes are intact. Reload the page to pick up "
    "the latest data, then re-apply and publish your changes."
)

_NO_BASELINE = (
    "Cannot publish: this data was not loaded from GCS, so there is no "
    "baseline version to write against. Reload the page and try again."
)


def _is_precondition_failure(exc: Exception) -> bool:
    # google.api_core.exceptions.PreconditionFailed, i.e. the 412 the JSON API
    # returns when if_generation_match does not hold. Matched on .code so this
    # module keeps its lazy-import discipline for the google packages.
    return getattr(exc, "code", None) == 412


def _resolve_env(settings: dict[str, Any]) -> dict[str, Any]:
    """Settings with every `<key>_env` resolved into `<key>`.

    Same convention as the auth providers: `bucket_env: GCS_BUCKET` in
    config.yaml means "read the bucket from $GCS_BUCKET", and a non-empty env
    var wins over any inline `bucket:` value. That is what lets one container
    image be promoted between environments — the non-prod and prod bucket and
    change-log project are supplied by the runtime, not baked into the image.
    Applied to the top level and to the nested `change_log` block.
    """

    def resolve(block: dict[str, Any]) -> dict[str, Any]:
        out = dict(block)
        for key, env_name in block.items():
            if not key.endswith("_env") or not env_name:
                continue
            value = os.environ.get(str(env_name))
            if value:
                out[key[: -len("_env")]] = value
        return out

    resolved = resolve(settings)
    change_log = settings.get("change_log")
    if isinstance(change_log, dict):
        resolved["change_log"] = resolve(change_log)
    return resolved


def _missing(setting_path: str, env_name: Any) -> str:
    """Startup error for a setting that resolved to nothing.

    Deliberately blunt: these are per-environment values with no fallback in
    config.yaml, so an unset env var must stop the app here rather than let a
    prod deployment quietly read and write the non-prod bucket.
    """
    if env_name:
        return (
            f"{setting_path} is not set. Set the {env_name} environment variable "
            f"on the container (docker run -e {env_name}=..., or Cloud Run "
            f"--set-env-vars {env_name}=...). There is no default: see the "
            "README's \"Secrets / environment variables\" section."
        )
    return f"{setting_path} is required"


class GcsParquetStorageProvider(StorageProvider):
    name = "gcs_parquet"
    audit_before_data_write = True
    supports_import = True

    def __init__(self, settings: dict[str, Any]):
        super().__init__(_resolve_env(settings))
        for key in ("bucket", "blob_path"):
            if not self.settings.get(key):
                raise StorageError(_missing(f"storage.gcs_parquet.{key}", settings.get(f"{key}_env")))
        change_log = self.settings.get("change_log") or {}
        raw_change_log = settings.get("change_log") or {}
        for key in ("project", "dataset", "table"):
            if not change_log.get(key):
                raise StorageError(
                    _missing(
                        f"storage.gcs_parquet.change_log.{key}",
                        raw_change_log.get(f"{key}_env"),
                    )
                )

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

    def _live_generation(self, blob) -> Any:
        """The generation currently at blob_path, from a metadata-only fetch."""
        try:
            blob.reload()
        except Exception as exc:
            raise StorageError(f"GCS read failed: {exc}") from exc
        return blob.generation

    @property
    def _change_log_ref(self) -> str:
        c = self.settings["change_log"]
        return f"{c['project']}.{c['dataset']}.{c['table']}"


    def load(self) -> pd.DataFrame:
        blob = self._blob()
        # Read the generation first, then pin the download to it. The reverse
        # order would hand back a generation newer than the bytes in hand, and
        # a later publish would silently overwrite the write that bumped it.
        generation = self._live_generation(blob)
        try:
            data = blob.download_as_bytes(if_generation_match=generation)
        except Exception as exc:
            if _is_precondition_failure(exc):
                raise StorageError(
                    "The dataset was republished while it was loading. Try again."
                ) from exc
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
        return stamp_version(df.astype(str), generation)

    def check_writable(self, df: pd.DataFrame) -> None:
        expected = version_of(df)
        if expected is None:
            raise StorageError(_NO_BASELINE)
        if self._live_generation(self._blob()) != expected:
            raise StorageError(_CONFLICT)

    def _write_parquet(self, df: pd.DataFrame, expected_generation: Any) -> Any:
        """Overwrite blob_path, but only if it is still at expected_generation.

        GCS objects are immutable, so this replaces the object with a new
        generation at the same path. Returns that new generation.
        """
        if expected_generation is None:
            raise StorageError(_NO_BASELINE)

        buf = io.BytesIO()
        try:
            df.to_parquet(buf, engine="pyarrow", index=False)
            data = buf.getvalue()
        finally:
            buf.close()

        blob = self._blob()
        try:
            blob.upload_from_string(
                data,
                content_type="application/octet-stream",
                if_generation_match=expected_generation,
            )
        except Exception as exc:
            if _is_precondition_failure(exc):
                raise StorageError(_CONFLICT) from exc
            raise StorageError(f"GCS write failed: {exc}") from exc

        if blob.generation is None:
            # The upload response normally carries it; if it somehow didn't,
            # go and ask rather than leave the session without a baseline.
            return self._live_generation(blob)
        return blob.generation

    def apply_edits(self, df: pd.DataFrame, edits: EditMap) -> None:
        self.apply_changes(df, edits)

    def apply_changes(self, df, edits, inserts=(), deletes=()) -> None:
        updated = self._frame_with_changes(df, edits, inserts, deletes)
        # Advance the caller's baseline, so a second publish in the same
        # session isn't rejected as stale against the generation we just wrote.
        stamp_version(df, self._write_parquet(updated, version_of(df)))

    def replace_all(self, new_df: pd.DataFrame) -> None:
        stamp_version(new_df, self._write_parquet(new_df, version_of(new_df)))

    def _change_log_field_types(self, client) -> dict[str, str]:
        """{column: BigQuery type} for the change-log table.

        Read on every publish rather than cached: the provider is a
        process-wide singleton, so a cached schema would outlive a column
        type change until the container restarts, and one metadata call per
        publish is nothing next to the write it guards.
        """
        try:
            table = client.get_table(self._change_log_ref)
        except Exception as exc:
            raise StorageError(
                f"Could not read the change log schema for {self._change_log_ref}: {exc}"
            ) from exc
        return {field.name: field.field_type for field in table.schema}

    def write_audit(self, metadata, records) -> None:
        if not records:
            return
        client = self._bigquery_client()
        rows = blanks_as_null(records, self._change_log_field_types(client))
        try:
            bq_errors = client.insert_rows_json(self._change_log_ref, rows)
        except Exception as exc:
            raise StorageError(f"Change log write to {self._change_log_ref} failed: {exc}") from exc
        if bq_errors:
            raise StorageError(
                f"Change log write to {self._change_log_ref} rejected rows: {bq_errors}"
            )

    def display_name(self) -> str:
        return self.settings["blob_path"].rsplit("/", 1)[-1]
