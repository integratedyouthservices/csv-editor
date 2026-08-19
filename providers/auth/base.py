"""Auth provider interface.

Three login shapes are supported:
  * Credential providers (the default): a synchronous authenticate(username,
    password) call, driven by a form. Subclass AuthProvider and implement
    authenticate() — see mock.py / gcloud_identity.py.
  * Redirect providers (e.g. an OAuth code-exchange flow): login spans a
    browser redirect away from and back to the app, so it can't be a single
    call. Set redirect_based = True and implement get_login_url()/
    complete_login() instead; authenticate() is never called for these. The
    CSRF `state` value is generated/stored/compared by the caller (app.py's
    st.session_state) — providers only ever see a plain string, which keeps
    this method provider-agnostic and unit-testable without Streamlit.
  * Header providers (e.g. a fronting proxy like Google Cloud IAP): identity
    arrives already verified on the request headers of the session that
    opened the app — there's no form and no redirect for the app to drive.
    Set header_based = True and implement authenticate_from_headers()
    instead; authenticate() is never called for these.

To add a provider:
  1. Subclass AuthProvider and implement authenticate() (or the redirect
     pair, or authenticate_from_headers(), depending on the login shape).
  2. Register it in providers/auth/__init__.py.
  3. Point `auth.provider` at it in config.yaml.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class User:
    """Minimal authenticated identity, provider-agnostic."""

    username: str
    display_name: str
    provider: str

    @property
    def initials(self) -> str:
        parts = [p for p in self.display_name.replace("@", " ").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


class AuthError(Exception):
    """Raised for provider/config failures (not for bad credentials)."""


class AuthProvider(ABC):
    """Authentication backend — credential form or redirect-based."""

    name: str = "base"
    redirect_based: bool = False   # True => use get_login_url/complete_login
    header_based: bool = False     # True => use authenticate_from_headers

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    @abstractmethod
    def authenticate(self, username: str, password: str) -> Optional[User]:
        """Return a User on success, None on bad credentials.

        Raise AuthError for infrastructure problems (bad config,
        network failure) so the UI can distinguish them from a wrong
        password. Not used by redirect_based or header_based providers.
        """
        raise NotImplementedError

    def get_login_url(self, state: str) -> str:
        """Redirect-based providers only: the URL to send the browser to,
        embedding `state` (an opaque CSRF token the caller will verify on
        the way back)."""
        raise NotImplementedError

    def complete_login(self, code: str) -> Optional[User]:
        """Redirect-based providers only: exchange the authorization `code`
        from the redirect-back for a User. Same None/AuthError convention
        as authenticate() — None for a rejected login, AuthError for an
        infrastructure failure."""
        raise NotImplementedError

    def authenticate_from_headers(self, headers: Mapping[str, str]) -> Optional[User]:
        """Header-based providers only: derive a User from the (already
        verified by the caller, e.g. a signed JWT) request headers of the
        session that opened the app. Same None/AuthError convention as
        authenticate() — None when there's no valid identity to read (e.g.
        the app was reached without going through the fronting proxy),
        AuthError for an infrastructure failure."""
        raise NotImplementedError
