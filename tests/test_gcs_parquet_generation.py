"""Concurrency tests for GcsParquetStorageProvider's generation preconditions.

GCS objects are immutable: overwriting blob_path writes a *new generation* at
the same path. These tests drive the provider against an in-memory fake of
that model to prove a publish only lands on the generation the editor loaded,
so two people editing at once cannot silently clobber each other.

No network and no google-cloud-* install: fake modules are injected into
sys.modules for the duration of each test.

Run with:  python -m pytest tests/ -q
"""
import io
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from providers.storage.base import StorageError, stamp_version, version_of

SETTINGS = {
    "bucket": "a-bucket",
    "blob_path": "some/dir/data.parquet",
    "id_column": None,
    "change_log": {"project": "p", "dataset": "d", "table": "t"},
}

FRAME = pd.DataFrame({"name": ["Alpha", "Beta"], "city_town": ["Toronto", "Ottawa"]})


class FakePrecondition(Exception):
    """Stands in for google.api_core.exceptions.PreconditionFailed."""

    code = 412


class FakeNotFound(Exception):
    code = 404


class FakeStore:
    """One GCS object path: bytes plus a monotonically increasing generation."""

    def __init__(self, df=None):
        self.generation = None
        self.data = None
        self.uploads = []
        if df is not None:
            self.put(df)

    def put(self, df):
        self.generation = (self.generation or 1000) + 1
        self.data = df.to_parquet(engine="pyarrow", index=False)


class FakeBlob:
    def __init__(self, store):
        self._store = store
        self.generation = None

    def reload(self):
        if self._store.generation is None:
            raise FakeNotFound("No such object")
        self.generation = self._store.generation

    def download_as_bytes(self, if_generation_match=None):
        if self._store.generation is None:
            raise FakeNotFound("No such object")
        if (
            if_generation_match is not None
            and if_generation_match != self._store.generation
        ):
            raise FakePrecondition("generation mismatch")
        return self._store.data

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        if (
            if_generation_match is not None
            and if_generation_match != self._store.generation
        ):
            raise FakePrecondition("generation mismatch")
        self._store.uploads.append((data, if_generation_match))
        self._store.generation = (self._store.generation or 1000) + 1
        self._store.data = data
        self.generation = self._store.generation


@pytest.fixture
def provider(monkeypatch):
    """A GcsParquetStorageProvider wired to a fake object holding FRAME."""
    store = FakeStore(FRAME)

    class FakeBucket:
        def blob(self, path):
            assert path == SETTINGS["blob_path"]
            return FakeBlob(store)

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def bucket(self, name):
            assert name == SETTINGS["bucket"]
            return FakeBucket()

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

    from providers.storage.gcs_parquet import GcsParquetStorageProvider

    p = GcsParquetStorageProvider(dict(SETTINGS))
    p._store = store
    return p


def test_load_stamps_the_generation_it_read(provider):
    df = provider.load()
    assert version_of(df) == provider._store.generation
    assert list(df["name"]) == ["Alpha", "Beta"]


def test_publish_sends_the_loaded_generation_as_the_precondition(provider):
    df = provider.load()
    loaded_at = version_of(df)
    provider.apply_edits(df, {(0, "city_town"): "Hamilton"})
    _, precondition = provider._store.uploads[-1]
    assert precondition == loaded_at


def test_publish_advances_the_baseline_so_a_second_publish_works(provider):
    df = provider.load()
    provider.apply_edits(df, {(0, "city_town"): "Hamilton"})
    assert version_of(df) == provider._store.generation

    # app.py then rebuilds original_df from the published edits (a copy) and
    # clears the edit set. The second publish must not be rejected as stale
    # against the generation this same session just wrote.
    republished = df.copy()
    republished.at[0, "city_town"] = "Hamilton"
    provider.apply_edits(republished, {(1, "city_town"): "Kingston"})

    assert len(provider._store.uploads) == 2
    assert [gen for _, gen in provider._store.uploads] == [1001, 1002]
    written = pd.read_parquet(io.BytesIO(provider._store.data))
    assert written["city_town"].tolist() == ["Hamilton", "Kingston"]


def test_publish_is_refused_when_someone_else_published_first(provider):
    df = provider.load()
    provider._store.put(FRAME)  # a concurrent editor publishes
    with pytest.raises(StorageError) as exc:
        provider.apply_edits(df, {(0, "city_town"): "Hamilton"})
    assert "Someone else published" in str(exc.value)
    assert provider._store.uploads == []  # their write survived untouched


def test_check_writable_passes_when_nothing_changed(provider):
    provider.check_writable(provider.load())


def test_check_writable_catches_a_concurrent_publish(provider):
    df = provider.load()
    provider._store.put(FRAME)
    with pytest.raises(StorageError, match="Someone else published"):
        provider.check_writable(df)


def test_check_writable_rejects_an_unstamped_frame(provider):
    with pytest.raises(StorageError, match="no baseline version"):
        provider.check_writable(FRAME.copy())


def test_replace_all_honours_the_stamped_baseline(provider):
    df = provider.load()
    loaded_at = version_of(df)
    incoming = pd.DataFrame({"name": ["Gamma"], "city_town": ["Guelph"]})
    stamp_version(incoming, loaded_at)
    provider.replace_all(incoming)
    assert provider._store.uploads[-1][1] == loaded_at
    assert version_of(incoming) == provider._store.generation


def test_replace_all_is_refused_when_the_baseline_is_stale(provider):
    df = provider.load()
    incoming = pd.DataFrame({"name": ["Gamma"], "city_town": ["Guelph"]})
    stamp_version(incoming, version_of(df))
    provider._store.put(FRAME)
    with pytest.raises(StorageError, match="Someone else published"):
        provider.replace_all(incoming)
    assert provider._store.uploads == []


def test_write_without_a_baseline_never_reaches_gcs(provider):
    with pytest.raises(StorageError, match="no baseline version"):
        provider.apply_edits(FRAME.copy(), {(0, "city_town"): "Hamilton"})
    assert provider._store.uploads == []


def test_load_pins_the_download_to_the_generation_it_saw(provider):
    # A torn read (metadata from one generation, bytes from the next) would
    # hand back a baseline newer than the data and mask a concurrent write.
    seen = {}
    real_download = FakeBlob.download_as_bytes

    def spy(self, if_generation_match=None):
        seen["pinned"] = if_generation_match
        return real_download(self, if_generation_match=if_generation_match)

    FakeBlob.download_as_bytes = spy
    try:
        provider.load()
    finally:
        FakeBlob.download_as_bytes = real_download
    assert seen["pinned"] == provider._store.generation


def test_the_edit_merge_round_trip_carries_the_stamp(provider):
    # app.py rebuilds original_df through merge_edits (a DataFrame.copy) after
    # every publish; the baseline has to survive that or the next publish
    # fails as unstamped.
    df = provider.load()
    merged = df.copy()
    merged.at[0, "city_town"] = "Hamilton"
    assert version_of(merged) == version_of(df)


def test_providers_without_versioning_are_unaffected():
    from providers.storage.local_parquet import LocalParquetStorageProvider

    p = LocalParquetStorageProvider.__new__(LocalParquetStorageProvider)
    assert p.check_writable(FRAME.copy()) is None
    assert version_of(FRAME.copy()) is None
