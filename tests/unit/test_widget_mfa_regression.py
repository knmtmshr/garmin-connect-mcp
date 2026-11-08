"""Contract tests for the upstream GH-386 fix; no Garmin credentials/network."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from garminconnect import client as client_mod
from garminconnect import GarminConnectConnectionError, GarminConnectTooManyRequestsError


def page(method=None, sent=False):
    variables = ""
    if method:
        variables = (f'var mfaMethod = "{method}"; var customerGuid = "test-guid"; '
                     'var locale = "en-US"; var clientId = "test-client";')
    if sent:
        variables += 'var codeSentTo = "test@example.invalid";'
    return ('<title>GARMIN Authentication Application</title>'
            f'<script>{variables}</script>')


def widget(monkeypatch, html, delivery_status=200):
    def response(text, status=200):
        return SimpleNamespace(text=text, status_code=status, ok=status < 400,
                               url="https://sso.garmin.com/sso/signin")

    session = Mock()
    session.get.return_value = response('<input name="_csrf" value="test-csrf">')
    session.post.side_effect = lambda url, **kwargs: response(
        "" if url.endswith("/mfaCode") else html,
        delivery_status if url.endswith("/mfaCode") else 200,
    )
    monkeypatch.setattr(client_mod.cffi_requests, "Session", lambda **kwargs: session)
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    return client_mod.Client(), session


def test_plain_signin_title_does_not_trigger_mfa(monkeypatch):
    client, _ = widget(monkeypatch, page())
    with pytest.raises(GarminConnectConnectionError, match="unexpected title"):
        client._widget_web_login("test@example.invalid", "test-password")
    assert not hasattr(client, "_mfa_flow")


@pytest.mark.parametrize("method,sent,requests_delivery", [
    ("email", False, True), ("sms", False, True),
    ("email", True, False), ("totp", False, False),
])
def test_real_mfa_delivery_precedes_prompt(monkeypatch, method, sent, requests_delivery):
    client, session = widget(monkeypatch, page(method, sent))
    with pytest.raises(client_mod._MFARequired):
        client._widget_web_login("test@example.invalid", "test-password")
    calls = [c for c in session.post.call_args_list if c.args[0].endswith("/mfaCode")]
    assert len(calls) == int(requests_delivery)
    assert client._mfa_method == method


def test_failed_delivery_does_not_prompt_for_nonexistent_code(monkeypatch):
    client, _ = widget(monkeypatch, page("email"), delivery_status=429)
    with pytest.raises(GarminConnectTooManyRequestsError):
        client._widget_web_login("test@example.invalid", "test-password")
