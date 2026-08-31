"""Unit tests for providers.storage.local_parquet.LocalParquetStorageProvider.

Round-trips a real parquet file through a temp directory: no GCP, no
network, no stubs — pyarrow is a hard requirement of this provider.

Run with:  python -m pytest tests/ -q
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from providers.storage.base import ROW_ID, StorageError
from providers.storage.local_parquet import LocalParquetStorageProvider

FRAME = pd.DataFrame(
    {
        "name": ["Alpha", "Beta", "Gamma"],
        "city_town": ["Toronto", None, "Halifax"],
        "latitude": [43.6532, 45.5019, 44.6488],
    }
)


def _provider(tmpdir, id_column=None, frame=FRAME):
    path = os.path.join(tmpdir, "data.parquet")
    frame.to_parquet(path, engine="pyarrow", index=False)
    return LocalParquetStorageProvider({"path": path, "id_column": id_column}), path


def test_load_returns_strings_indexed_by_row_id():
    with tempfile.TemporaryDirectory() as tmp:
        provider, _ = _provider(tmp)
        df = provider.load()
        assert df.index.name == ROW_ID
        assert list(df.index) == [0, 1, 2]
        assert set(map(str, df.dtypes)) == {"str"}
        assert df.at[0, "latitude"] == "43.6532"


def test_nulls_become_blanks_not_literal_na_text():
    with tempfile.TemporaryDirectory() as tmp:
        provider, _ = _provider(tmp)
        df = provider.load()
        assert df.at[1, "city_town"] == ""


def test_id_column_indexes_by_that_column():
    with tempfile.TemporaryDirectory() as tmp:
        provider, _ = _provider(tmp, id_column="name")
        df = provider.load()
        assert df.index.name == ROW_ID
        assert list(df.index) == ["Alpha", "Beta", "Gamma"]


def test_duplicate_id_column_raises():
    dupes = FRAME.copy()
    dupes["name"] = ["Alpha", "Alpha", "Gamma"]
    with tempfile.TemporaryDirectory() as tmp:
        provider, _ = _provider(tmp, id_column="name", frame=dupes)
        try:
            provider.load()
            assert False, "expected StorageError"
        except StorageError:
            pass


def test_missing_file_raises_storage_error():
    with tempfile.TemporaryDirectory() as tmp:
        provider = LocalParquetStorageProvider({"path": os.path.join(tmp, "nope.parquet")})
        try:
            provider.load()
            assert False, "expected StorageError"
        except StorageError:
            pass


def test_apply_edits_round_trips_to_disk():
    with tempfile.TemporaryDirectory() as tmp:
        provider, path = _provider(tmp)
        df = provider.load()
        provider.apply_edits(df, {(1, "city_town"): "Montreal", (2, "name"): "Delta"})

        reloaded = provider.load()
        assert reloaded.at[1, "city_town"] == "Montreal"
        assert reloaded.at[2, "name"] == "Delta"
        assert reloaded.at[0, "name"] == "Alpha"       # untouched
        assert len(os.listdir(tmp)) == 1               # temp file cleaned up
        assert os.path.basename(path) in os.listdir(tmp)


def test_replace_all_swaps_the_whole_dataset():
    with tempfile.TemporaryDirectory() as tmp:
        provider, _ = _provider(tmp)
        provider.replace_all(
            pd.DataFrame({"name": ["Solo"], "city_town": ["Regina"], "latitude": ["50.4"]})
        )
        reloaded = provider.load()
        assert len(reloaded) == 1
        assert reloaded.at[0, "name"] == "Solo"


def test_write_audit_appends_jsonl_beside_the_file():
    import json

    with tempfile.TemporaryDirectory() as tmp:
        provider, path = _provider(tmp)
        provider.write_audit({"last_updated_by": "a@b.c"}, [{"row": 1}])
        provider.write_audit({"last_updated_by": "a@b.c"}, [{"row": 2}])

        with open(path + ".audit.jsonl", encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        assert len(lines) == 2
        assert lines[1]["records"] == [{"row": 2}]


def test_missing_path_setting_raises():
    provider = LocalParquetStorageProvider({})
    try:
        provider.load()
        assert False, "expected StorageError"
    except StorageError:
        pass
