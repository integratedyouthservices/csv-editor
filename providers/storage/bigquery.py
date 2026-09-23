"""BigQuery storage provider.

Config (storage.bigquery):
    project:   my-gcp-project
    dataset:   my_dataset
    table:     customers
    id_column: id          # REQUIRED — stable unique key for row identity
    location:  US

Credentials come from Application Default Credentials:
    gcloud auth application-default login
or  GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

Requires: pip install google-cloud-bigquery
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from providers.storage.base import ROW_ID, EditMap, StorageError, StorageProvider


class BigQueryStorageProvider(StorageProvider):
    name = "bigquery"

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        for key in ("project", "dataset", "table", "id_column"):
            if not settings.get(key):
                raise StorageError(f"storage.bigquery.{key} is required")
        try:
            from google.cloud import bigquery  # noqa: F401
        except ImportError as exc:
            raise StorageError(
                "google-cloud-bigquery is not installed. "
                "Run: pip install google-cloud-bigquery"
            ) from exc

    def _client(self):
        from google.cloud import bigquery

        return bigquery.Client(
            project=self.settings["project"],
            location=self.settings.get("location"),
        )

    @property
    def _table_ref(self) -> str:
        s = self.settings
        return f"`{s['project']}.{s['dataset']}.{s['table']}`"

    def load(self) -> pd.DataFrame:
        id_column = self.settings["id_column"]
        try:
            df = self._client().query(
                f"SELECT * FROM {self._table_ref} ORDER BY {id_column}"
            ).to_dataframe()
        except Exception as exc:  # google api errors vary by version
            raise StorageError(f"BigQuery load failed: {exc}") from exc

        if df[id_column].duplicated().any():
            raise StorageError(f"id_column '{id_column}' has duplicate values")
        df = df.set_index(df[id_column].rename(ROW_ID), drop=False)
        # Editing works on strings; providers normalise on the way out.
        return df.astype(str)

    def apply_edits(self, df: pd.DataFrame, edits: EditMap) -> None:
        """Apply all edits in a single MERGE so the publish is atomic."""
        from google.cloud import bigquery

        id_column = self.settings["id_column"]

        # Reshape {(row_id, col): value} → one patch row per edited row.
        patches: dict[Any, dict[str, Any]] = {}
        for (row_id, column), value in edits.items():
            patches.setdefault(row_id, {})[column] = value
        if not patches:
            return

        edited_columns = sorted({c for p in patches.values() for c in p})
        rows = [
            {id_column: row_id, **{c: patch.get(c) for c in edited_columns}}
            for row_id, patch in patches.items()
        ]

        set_clauses = ", ".join(
            f"{c} = COALESCE(S.{c}, T.{c})" for c in edited_columns
        )
        sql = f"""
        MERGE {self._table_ref} T
        USING UNNEST(@patches) S
        ON CAST(T.{id_column} AS STRING) = CAST(S.{id_column} AS STRING)
        WHEN MATCHED THEN UPDATE SET {set_clauses}
        """

        struct_fields = [
            bigquery.StructQueryParameter(
                None,
                *[
                    bigquery.ScalarQueryParameter(k, "STRING", None if v is None else str(v))
                    for k, v in row.items()
                ],
            )
            for row in rows
        ]
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("patches", "STRUCT", struct_fields)
            ]
        )
        try:
            self._client().query(sql, job_config=job_config).result()
        except Exception as exc:
            raise StorageError(f"BigQuery publish failed: {exc}") from exc

    def apply_changes(self, df, edits, inserts=(), deletes=()) -> None:
        """Cell edits + row deletes. Inserts are not supported yet.

        Deleting is unambiguous (the row id is already known), so it runs
        as a parameterised DELETE before the edits MERGE. The two are
        separate statements: the MERGE is still atomic in itself, but a
        publish that both deletes and edits is not all-or-nothing across
        the pair. Wrap them in a BEGIN/COMMIT transaction if that
        matters for the real table. Inserting is
        not: `id_column` is the table's own key and the editor has no way
        to mint one for a brand-new row. Wire up whatever the real table
        uses (an autoincrement/DEFAULT column, a UUID, a sequence table)
        here before enabling row adds against BigQuery.
        """
        from google.cloud import bigquery

        if inserts:
            raise StorageError(
                "adding rows isn't supported on BigQuery yet — the new row has "
                f"no value for the key column '{self.settings['id_column']}'. "
                "See BigQueryStorageProvider.apply_changes."
            )

        if deletes:
            sql = (
                f"DELETE FROM {self._table_ref} "
                f"WHERE CAST({self.settings['id_column']} AS STRING) IN UNNEST(@ids)"
            )
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter(
                        "ids", "STRING", [str(r) for r in deletes]
                    )
                ]
            )
            try:
                self._client().query(sql, job_config=job_config).result()
            except Exception as exc:
                raise StorageError(f"BigQuery delete failed: {exc}") from exc

        self.apply_edits(df, edits)

    def write_audit(self, metadata, records) -> None:
        """Insert one row per changed cell into `audit_table` (if set).

        Rows carry the change (row_id, column, old_value, new_value,
        timestamp, user) plus last_updated_at / last_updated_by.
        Skipped silently when audit_table isn't configured.
        """
        audit_table = self.settings.get("audit_table")
        if not audit_table:
            return
        s = self.settings
        ref = f"{s['project']}.{s['dataset']}.{audit_table}"
        rows = [{**{k: (None if v is None else str(v)) for k, v in r.items()},
                 **metadata} for r in records]
        try:
            bq_errors = self._client().insert_rows_json(ref, rows)
        except Exception as exc:
            raise StorageError(f"Audit write to {ref} failed: {exc}") from exc
        if bq_errors:
            raise StorageError(f"Audit write to {ref} rejected rows: {bq_errors}")

    def display_name(self) -> str:
        return f"{self.settings['dataset']}.{self.settings['table']}"
