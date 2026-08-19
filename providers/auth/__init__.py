"""Auth provider registry.

Providers are imported lazily so an unused provider's dependencies
(e.g. `requests` for gcloud) are never required just to run the app.
"""
from __future__ import annotations

from typing import Any, Callable

from providers.auth.base import AuthError, AuthProvider
from providers.auth.base import User as User  # re-export

_REGISTRY: dict[str, Callable[[], type]] = {
    "mock": lambda: _import("providers.auth.mock", "MockAuthProvider"),
    "gcloud_identity": lambda: _import(
        "providers.auth.gcloud_identity", "GcloudIdentityAuthProvider"
    ),
    "google_oauth": lambda: _import(
        "providers.auth.google_oauth", "GoogleOAuthProvider"
    ),
    "gcp_iap": lambda: _import("providers.auth.gcp_iap", "GcpIapAuthProvider"),
}


def _import(module: str, cls: str) -> type:
    import importlib

    return getattr(importlib.import_module(module), cls)


def register_auth_provider(name: str, loader: Callable[[], type]) -> None:
    """Register a custom provider: register_auth_provider('okta', lambda: OktaProvider)."""
    _REGISTRY[name] = loader


def create_auth_provider(name: str, settings: dict[str, Any]) -> AuthProvider:
    try:
        loader = _REGISTRY[name]
    except KeyError:
        raise AuthError(
            f"Unknown auth provider '{name}'. Available: {sorted(_REGISTRY)}"
        ) from None
    return loader()(settings)
