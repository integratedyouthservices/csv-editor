"""Google Cloud IAP (Identity-Aware Proxy) auth provider.

https://cloud.google.com/iap/docs/concepts-overview

IAP sits in front of the app: it makes the user sign in with Google before
any request reaches Streamlit, then adds a signed JWT to every request
proving who they are:
https://cloud.google.com/iap/docs/signed-headers-howto

This provider trusts *only* that signed JWT (`X-Goog-IAP-JWT-Assertion`),
verified against Google's IAP-specific public keys and the configured
audience. It deliberately ignores the plaintext `X-Goog-Authenticated-User-*`
headers IAP also sets — those are informational only and would be trivially
spoofable if the app were ever reachable without going through IAP (e.g. a
misconfigured load balancer, or someone hitting the backend service
directly), so a missing/invalid JWT is always treated as "not logged in",
never a fallback to the plaintext header.

Config (auth.gcp_iap):
    audience_env: IAP_AUDIENCE   # env var holding the expected JWT audience
    audience: "..."              # inline fallback (avoid committing)
        Format: /projects/PROJECT_NUMBER/global/backendServices/SERVICE_ID
        (external HTTPS load balancer — the typical Cloud Run + IAP setup)
        or /projects/PROJECT_NUMBER/apps/PROJECT_ID (App Engine). See
        https://cloud.google.com/iap/docs/signed-headers-howto#verifying_the_jwt_payload

Requires: pip install google-auth
(google.oauth2.id_token + google.auth.transport.requests both ship in the
`google-auth` package — same dependency google_oauth.py uses.)
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from providers.auth.base import AuthError, AuthProvider, User

# IAP signs with its own key set, distinct from Google's general OAuth certs.
_IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
_ASSERTION_HEADER = "X-Goog-IAP-JWT-Assertion"


class GcpIapAuthProvider(AuthProvider):
    name = "gcp_iap"
    header_based = True

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        self._audience = self._required_audience()

    def _required_audience(self) -> str:
        env_name = self.settings.get("audience_env")
        value = (os.environ.get(env_name) if env_name else None) or self.settings.get(
            "audience"
        )
        if not value:
            raise AuthError(
                "IAP audience not configured. Set the env var "
                f"{env_name or 'IAP_AUDIENCE'} or auth.gcp_iap.audience in "
                "config.yaml. See "
                "https://cloud.google.com/iap/docs/signed-headers-howto"
                "#verifying_the_jwt_payload"
            )
        return value

    def authenticate(self, username: str, password: str) -> Optional[User]:
        raise AuthError(
            "gcp_iap is header-based; it does not accept a username/password."
        )

    def authenticate_from_headers(self, headers: Mapping[str, str]) -> Optional[User]:
        assertion = headers.get(_ASSERTION_HEADER)
        if not assertion:
            # No signed assertion -- either IAP isn't in front of this
            # request, or the header hasn't propagated. Either way, there's
            # no identity to trust: reject rather than falling back to any
            # other header.
            return None

        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token
        except ImportError as exc:
            raise AuthError(
                "google-auth is not installed. Run: pip install google-auth"
            ) from exc

        try:
            claims = id_token.verify_oauth2_token(
                assertion,
                google_requests.Request(),
                self._audience,
                certs_url=_IAP_CERTS_URL,
            )
        except Exception as exc:  # google-auth's exact exception type varies by version
            raise AuthError(f"Invalid IAP assertion: {exc}") from exc

        email = claims.get("email")
        if not email:
            raise AuthError("IAP assertion did not include an email claim.")

        return User(username=email, display_name=email, provider=self.name)
