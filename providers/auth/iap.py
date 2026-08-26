from __future__ import annotations

import os
from typing import Any, Optional

from providers.auth.base import AuthError, AuthProvider, User

_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"
_ASSERTION_HEADER = "X-Goog-IAP-JWT-Assertion"


class IAPAuthProvider(AuthProvider):
    """Trusts Google Cloud Identity-Aware Proxy: the app must be deployed
    behind IAP so every request already carries a signed identity assertion.
    See https://cloud.google.com/iap/docs/signed-headers-howto
    """

    name = "iap"

    def login_url(self) -> str:
        env_name = self.settings.get("login_url_env")
        url = (
            os.environ.get(env_name) if env_name else None
        ) or self.settings.get("login_url")
        # "/" is correct whenever the app is only ever reached through IAP:
        # the navigation itself is what IAP intercepts to start sign-in. It is
        # only wrong if the browser got here some other way, which is what
        # auth.iap.login_url / IAP_LOGIN_URL is for.
        return url or "/"

    def restart_login_url(self) -> str:
        # Clears IAP's login cookie and re-enters the sign-in flow, so a stale
        # or wrong-account session doesn't just get handed back.
        # https://cloud.google.com/iap/docs/sessions-howto
        url = self.login_url()
        return f"{url}{'&' if '?' in url else '?'}gcp-iap-mode=CLEAR_LOGIN_COOKIE"

    def _audience(self) -> str:
        env_name = self.settings.get("audience_env")
        audience = (
            os.environ.get(env_name) if env_name else None
        ) or self.settings.get("audience")
        if not audience:
            raise AuthError(
                "IAP audience not configured. Set the env var "
                f"{env_name or 'IAP_AUDIENCE'} or auth.iap.audience in "
                "config.yaml (format: /projects/PROJECT_NUMBER/global/"
                "backendServices/SERVICE_ID for an external HTTPS load "
                "balancer, or /projects/PROJECT_NUMBER/apps/PROJECT_ID for "
                "App Engine — see "
                "https://cloud.google.com/iap/docs/signed-headers-howto)."
            )
        return audience

    def authenticate_from_headers(self, headers: Any) -> Optional[User]:
        assertion = headers.get(_ASSERTION_HEADER)
        if not assertion:
            return None

        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token
        except ImportError as exc:
            raise AuthError(
                "google-auth is not installed. Run: pip install google-auth"
            ) from exc

        try:
            claims = id_token.verify_token(
                assertion,
                google_requests.Request(),
                audience=self._audience(),
                certs_url=_CERTS_URL,
            )
        except Exception as exc:
            raise AuthError(f"Invalid IAP identity token: {exc}") from exc

        email = claims.get("email")
        if not email:
            raise AuthError("IAP identity token did not include an email claim.")
        # IAP prefixes the subject/email claims with "accounts.google.com:".
        email = email.split(":", 1)[-1]

        return User(username=email, display_name=email, provider=self.name)
