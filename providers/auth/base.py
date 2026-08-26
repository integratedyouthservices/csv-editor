from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class User:

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
    pass


class AuthProvider(ABC):
    """Identity comes from a signed header put on the request by a fronting
    proxy. The app never collects or verifies credentials itself, so there is
    deliberately no username/password entry point here.
    """

    name: str = "base"

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    @abstractmethod
    def authenticate_from_headers(self, headers: Any) -> Optional[User]:
        """Return the caller's identity, or None when the request carries no
        identity assertion at all. Raise AuthError when one is present but
        can't be trusted (bad signature, wrong audience, misconfiguration).
        """
        raise NotImplementedError

    def login_url(self) -> str:
        """Where the landing page's "Log in" button sends the browser —
        the proxy-protected URL, since navigating to it is what starts the
        proxy's own sign-in flow.
        """
        return "/"

    def restart_login_url(self) -> str:
        """Same, but discarding any existing proxy session first."""
        return self.login_url()
