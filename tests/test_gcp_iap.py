"""Unit tests for providers.auth.gcp_iap.GcpIapAuthProvider.

Uses tests/stubs/gcp_fakes.py to stand in for google-auth (not installed
by default -- it's an optional dependency, see requirements.txt).

Run with:  python -m pytest tests/ -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.auth.base import AuthError
from tests.stubs.gcp_fakes import install_gcp_fakes, uninstall_gcp_fakes

SETTINGS = {"audience": "/projects/123/global/backendServices/456"}
ASSERTION_HEADER = "X-Goog-IAP-JWT-Assertion"


def _provider():
    from providers.auth.gcp_iap import GcpIapAuthProvider
    return GcpIapAuthProvider(dict(SETTINGS))


def test_missing_audience_raises_immediately():
    from providers.auth.gcp_iap import GcpIapAuthProvider
    try:
        GcpIapAuthProvider({})
        assert False, "expected AuthError"
    except AuthError:
        pass


def test_authenticate_is_not_supported_for_this_provider():
    provider = _provider()
    try:
        provider.authenticate("a@b.com", "pw")
        assert False, "expected AuthError"
    except AuthError:
        pass


def test_no_assertion_header_returns_none():
    provider = _provider()
    assert provider.authenticate_from_headers({}) is None


def test_valid_assertion_returns_user():
    handles = install_gcp_fakes()
    try:
        provider = _provider()
        handles.id_token_module.verify_oauth2_token = (
            lambda token, req, aud, **kw: {"email": "person@example.com"}
        )
        user = provider.authenticate_from_headers({ASSERTION_HEADER: "fake.jwt"})
        assert user is not None
        assert user.username == "person@example.com"
        assert user.display_name == "person@example.com"
        assert user.provider == "gcp_iap"
    finally:
        uninstall_gcp_fakes()


def test_assertion_verified_against_configured_audience_and_iap_certs():
    handles = install_gcp_fakes()
    try:
        provider = _provider()
        seen = {}

        def _fake(token, req, aud, **kw):
            seen["token"] = token
            seen["audience"] = aud
            seen["certs_url"] = kw.get("certs_url")
            return {"email": "person@example.com"}

        handles.id_token_module.verify_oauth2_token = _fake
        provider.authenticate_from_headers({ASSERTION_HEADER: "fake.jwt"})
        assert seen["token"] == "fake.jwt"
        assert seen["audience"] == SETTINGS["audience"]
        assert seen["certs_url"] == "https://www.gstatic.com/iap/verify/public_key"
    finally:
        uninstall_gcp_fakes()


def test_invalid_assertion_raises_autherror():
    handles = install_gcp_fakes()
    try:
        provider = _provider()

        def _boom(token, req, aud, **kw):
            raise ValueError("bad signature")

        handles.id_token_module.verify_oauth2_token = _boom
        try:
            provider.authenticate_from_headers({ASSERTION_HEADER: "fake.jwt"})
            assert False, "expected AuthError"
        except AuthError:
            pass
    finally:
        uninstall_gcp_fakes()


def test_assertion_without_email_claim_raises_autherror():
    handles = install_gcp_fakes()
    try:
        provider = _provider()
        handles.id_token_module.verify_oauth2_token = lambda token, req, aud, **kw: {}
        try:
            provider.authenticate_from_headers({ASSERTION_HEADER: "fake.jwt"})
            assert False, "expected AuthError"
        except AuthError:
            pass
    finally:
        uninstall_gcp_fakes()


def test_without_google_auth_installed_raises_friendly_autherror():
    # No install_gcp_fakes() here -- exercises the real ImportError path
    # when google-auth genuinely isn't installed (it's optional).
    import sys as _sys
    saved = {
        name: _sys.modules.pop(name, None)
        for name in ("google.oauth2", "google.oauth2.id_token",
                      "google.auth", "google.auth.transport", "google.auth.transport.requests")
    }
    try:
        provider = _provider()
        try:
            provider.authenticate_from_headers({ASSERTION_HEADER: "fake.jwt"})
            assert False, "expected AuthError"
        except AuthError as exc:
            assert "google-auth" in str(exc)
    finally:
        for name, mod in saved.items():
            if mod is not None:
                _sys.modules[name] = mod
