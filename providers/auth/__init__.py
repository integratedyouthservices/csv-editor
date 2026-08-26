from __future__ import annotations

from typing import Any, Callable

from providers.auth.base import AuthError, AuthProvider
from providers.auth.base import User as User

_REGISTRY: dict[str, Callable[[], type]] = {
    "iap": lambda: _import("providers.auth.iap", "IAPAuthProvider"),
}


def _import(module: str, cls: str) -> type:
    import importlib

    return getattr(importlib.import_module(module), cls)


def register_auth_provider(name: str, loader: Callable[[], type]) -> None:
    _REGISTRY[name] = loader


def create_auth_provider(name: str, settings: dict[str, Any]) -> AuthProvider:
    try:
        loader = _REGISTRY[name]
    except KeyError:
        raise AuthError(
            f"Unknown auth provider '{name}'. Available: {sorted(_REGISTRY)}"
        ) from None
    return loader()(settings)
