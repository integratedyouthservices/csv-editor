"""`<key>_env` resolution for the gcs_parquet provider.

The bucket and the change-log project are the only per-environment difference
between non-prod and prod, so they come from env vars and one container image
can be promoted without a rebuild. These tests pin that precedence: env var
wins, inline value is the fallback, and neither present is a startup error
that names the env var.

Run with:  python -m pytest tests/ -q
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from providers.storage.base import StorageError
from providers.storage.gcs_parquet import _resolve_env

SETTINGS = {
    "bucket_env": "GCS_BUCKET",
    "bucket": "inline-bucket",
    "blob_path": "some/dir/data.parquet",
    "id_column": None,
    "change_log": {
        "project_env": "CHANGE_LOG_PROJECT",
        "project": "inline-project",
        "dataset": "d",
        "table": "t",
    },
}


@pytest.fixture
def fake_google(monkeypatch):
    """Satisfy the provider's constructor-time google/pyarrow import check."""

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

    storage_mod = types.ModuleType("google.cloud.storage")
    storage_mod.Client = FakeClient
    bigquery_mod = types.ModuleType("google.cloud.bigquery")
    bigquery_mod.Client = FakeClient
    cloud_mod = types.ModuleType("google.cloud")
    cloud_mod.storage = storage_mod
    cloud_mod.bigquery = bigquery_mod

    monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bigquery_mod)
    import google

    monkeypatch.setattr(google, "cloud", cloud_mod, raising=False)


def build(settings, fake_google):
    from providers.storage.gcs_parquet import GcsParquetStorageProvider

    return GcsParquetStorageProvider(settings)


def test_env_var_wins_over_inline_value(monkeypatch, fake_google):
    monkeypatch.setenv("GCS_BUCKET", "collab-prod-data")
    monkeypatch.setenv("CHANGE_LOG_PROJECT", "collab-infra-prod")

    p = build(dict(SETTINGS), fake_google)

    assert p.settings["bucket"] == "collab-prod-data"
    assert p.settings["change_log"]["project"] == "collab-infra-prod"
    assert p._change_log_ref == "collab-infra-prod.d.t"


def test_inline_value_used_when_env_unset(monkeypatch, fake_google):
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    monkeypatch.delenv("CHANGE_LOG_PROJECT", raising=False)

    p = build(dict(SETTINGS), fake_google)

    assert p.settings["bucket"] == "inline-bucket"
    assert p.settings["change_log"]["project"] == "inline-project"


def test_empty_env_var_does_not_blank_the_inline_value(monkeypatch, fake_google):
    monkeypatch.setenv("GCS_BUCKET", "")

    p = build(dict(SETTINGS), fake_google)

    assert p.settings["bucket"] == "inline-bucket"


def test_missing_env_and_inline_names_the_env_var(monkeypatch, fake_google):
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    settings = dict(SETTINGS)
    settings.pop("bucket")

    with pytest.raises(StorageError) as exc:
        build(settings, fake_google)

    assert "GCS_BUCKET" in str(exc.value)


def test_missing_change_log_project_names_the_env_var(monkeypatch, fake_google):
    monkeypatch.delenv("CHANGE_LOG_PROJECT", raising=False)
    settings = dict(SETTINGS)
    settings["change_log"] = {
        k: v for k, v in SETTINGS["change_log"].items() if k != "project"
    }

    with pytest.raises(StorageError) as exc:
        build(settings, fake_google)

    assert "CHANGE_LOG_PROJECT" in str(exc.value)


def test_resolution_does_not_mutate_the_caller_settings(monkeypatch):
    monkeypatch.setenv("GCS_BUCKET", "collab-prod-data")
    monkeypatch.setenv("CHANGE_LOG_PROJECT", "collab-infra-prod")
    original = {
        "bucket_env": "GCS_BUCKET",
        "bucket": "inline-bucket",
        "change_log": {"project_env": "CHANGE_LOG_PROJECT", "project": "inline-project"},
    }

    resolved = _resolve_env(original)

    assert resolved["bucket"] == "collab-prod-data"
    assert original["bucket"] == "inline-bucket"
    assert original["change_log"]["project"] == "inline-project"


def test_settings_without_env_keys_pass_through(monkeypatch):
    plain = {"bucket": "b", "blob_path": "p", "change_log": {"project": "q"}}
    assert _resolve_env(plain) == plain


def test_shipped_config_has_no_inline_fallback():
    """config.yaml must not carry the values the env vars supply.

    An inline `bucket:`/`project:` would turn a forgotten env var into a
    silent non-prod default in a prod deployment, which is exactly what the
    env vars exist to prevent.
    """
    import yaml

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "config.yaml"), encoding="utf-8") as fh:
        gcs = yaml.safe_load(fh)["storage"]["gcs_parquet"]

    assert gcs["bucket_env"] == "GCS_BUCKET"
    assert "bucket" not in gcs
    assert gcs["change_log"]["project_env"] == "CHANGE_LOG_PROJECT"
    assert "project" not in gcs["change_log"]
