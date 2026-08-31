from __future__ import annotations

from typing import Any, Callable

from providers.storage.base import ROW_ID as ROW_ID
from providers.storage.base import EditMap as EditMap
from providers.storage.base import StorageError, StorageProvider
from providers.storage.base import stamp_version as stamp_version
from providers.storage.base import version_of as version_of

_REGISTRY: dict[str, Callable[[], type]] = {
    "local_csv": lambda: _import(
        "providers.storage.local_csv", "LocalCsvStorageProvider"
    ),
    "local_parquet": lambda: _import(
        "providers.storage.local_parquet", "LocalParquetStorageProvider"
    ),
    "bigquery": lambda: _import(
        "providers.storage.bigquery", "BigQueryStorageProvider"
    ),
    "gcs_parquet": lambda: _import(
        "providers.storage.gcs_parquet", "GcsParquetStorageProvider"
    ),
}


def _import(module: str, cls: str) -> type:
    import importlib

    return getattr(importlib.import_module(module), cls)


def register_storage_provider(name: str, loader: Callable[[], type]) -> None:
    _REGISTRY[name] = loader


def create_storage_provider(name: str, settings: dict[str, Any]) -> StorageProvider:
    try:
        loader = _REGISTRY[name]
    except KeyError:
        raise StorageError(
            f"Unknown storage provider '{name}'. Available: {sorted(_REGISTRY)}"
        ) from None
    return loader()(settings)
