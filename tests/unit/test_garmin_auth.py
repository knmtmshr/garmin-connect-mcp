"""Exercise real pinned client refresh hooks with fake HTTP and Redis only."""

import base64
import json
import threading
import time
from unittest.mock import Mock
from pathlib import Path

import httpx
import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError, GarminConnectTooManyRequestsError
from garminconnect.client import Client

from garmin_mcp import _GarminProxy, init_api
from garmin_mcp.garmin_auth import initialize_garmin, safe_error
from garmin_mcp.token_store import TokenStoreError, _UpstashRedis


def jwt(exp, marker="initial"):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp, "marker": marker}).encode()).decode().rstrip("=")
    return f"e30.{payload}.signature"


def tokens(exp, marker="initial"):
    return json.dumps({"di_token": jwt(exp, marker), "di_refresh_token": marker, "di_client_id": "test-client"})


@pytest.fixture
def backend(monkeypatch):
    for key in ("GARMINTOKENS_BASE64", "TOKEN_STORE_FILE", "TOKEN_STORE_KEY", "GARMIN_TOKEN_STORE_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://redis.example.invalid")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "test-only-redis-secret")
    records = {}
    monkeypatch.setattr(_UpstashRedis, "get", lambda self, key: records.get(key))
    monkeypatch.setattr(_UpstashRedis, "set", lambda self, key, value: records.__setitem__(key, value))
    return records


@pytest.fixture
def api(monkeypatch):
    requests = []
    def request(session, method, url, **kwargs):
        requests.append(kwargs["headers"])
        payload = {"displayName": "test-user"} if "socialProfile" in url else {"userData": {}}
        return Mock(status_code=200, json=lambda: payload)
    monkeypatch.setattr("requests.Session.request", request)
    return requests


def bootstrap(monkeypatch, raw):
    monkeypatch.setenv("GARMINTOKENS_BASE64", base64.b64encode(raw.encode()).decode())


def test_actual_refresh_survives_restart_with_stale_env(backend, api, monkeypatch):
    bootstrap(monkeypatch, tokens(1))
    refreshes = []
    def refresh(self, url, **kwargs):
        refreshes.append(kwargs["data"]["refresh_token"])
        return Mock(ok=True, json=lambda: {
            "access_token": jwt(time.time() + 3600, "rotated"), "refresh_token": "rotated",
        })
    monkeypatch.setattr(Client, "_http_post", refresh)
    first = initialize_garmin()
    assert first.client.di_refresh_token == "rotated"
    restarted = initialize_garmin()  # environment still contains expired initial token
    assert restarted.client.di_refresh_token == "rotated"
    assert refreshes == ["initial"]
    assert json.loads(backend["mcp:garmin:credentials"])["di_refresh_token"] == "rotated"


def test_runtime_rotation_saved_before_profile_failure(backend, api, monkeypatch):
    bootstrap(monkeypatch, tokens(time.time() + 3600))
    garmin = initialize_garmin()
    garmin.client.di_token = jwt(1)
    monkeypatch.setattr(Client, "_http_post", lambda *a, **k: Mock(ok=True, json=lambda: {
        "access_token": jwt(time.time() + 3600), "refresh_token": "rotated-on-failure",
    }))
    monkeypatch.setattr("requests.Session.request", lambda *a, **k: Mock(
        status_code=503, json=lambda: {"message": "test-secret-response"}, text="test-secret-response",
    ))
    with pytest.raises(GarminConnectConnectionError):
        garmin.client.connectapi("/test")
    assert json.loads(backend["mcp:garmin:credentials"])["di_refresh_token"] == "rotated-on-failure"


def test_read_outage_never_falls_back_to_environment(backend, monkeypatch):
    bootstrap(monkeypatch, tokens(time.time() + 3600))
    monkeypatch.setattr(_UpstashRedis, "get", Mock(side_effect=TokenStoreError("offline")))
    with pytest.raises(TokenStoreError):
        initialize_garmin()
    assert not backend


def test_corrupt_saved_record_never_falls_back(backend, monkeypatch):
    bootstrap(monkeypatch, tokens(time.time() + 3600))
    backend["mcp:garmin:credentials"] = "{}"
    with pytest.raises(GarminConnectAuthenticationError):
        initialize_garmin()
    assert backend["mcp:garmin:credentials"] == "{}"


def test_failed_save_retains_rotation_and_retries_before_next_api(backend, api, monkeypatch):
    bootstrap(monkeypatch, tokens(time.time() + 3600))
    garmin = initialize_garmin()
    monkeypatch.setattr(Client, "_token_expires_soon", lambda self: self.di_refresh_token == "initial")
    monkeypatch.setattr(Client, "_http_post", lambda *a, **k: Mock(ok=True, json=lambda: {
        "access_token": jwt(time.time() + 3600), "refresh_token": "pending-save",
    }))
    original_set = _UpstashRedis.set
    monkeypatch.setattr(_UpstashRedis, "set", Mock(side_effect=TokenStoreError("offline")))
    count = len(api)
    with pytest.raises(TokenStoreError):
        garmin.client.connectapi("/test")
    assert garmin.client.di_refresh_token == "pending-save"
    assert len(api) == count  # no Garmin request after rotation couldn't be saved
    monkeypatch.setattr(_UpstashRedis, "set", original_set)
    garmin.client.connectapi("/test")
    assert json.loads(backend["mcp:garmin:credentials"])["di_refresh_token"] == "pending-save"


@pytest.mark.parametrize("status,kind", [(401, GarminConnectAuthenticationError), (403, GarminConnectAuthenticationError),
                                              (429, GarminConnectTooManyRequestsError), (503, GarminConnectConnectionError)])
def test_profile_failure_classified_without_secrets(backend, monkeypatch, caplog, status, kind):
    bootstrap(monkeypatch, tokens(time.time() + 3600))
    monkeypatch.setattr("requests.Session.request", lambda *a, **k: Mock(
        status_code=status, json=lambda: {"message": "test-secret-response"}, text="test-secret-response",
    ))
    monkeypatch.setattr("garminconnect.time.sleep", lambda *_: None)
    with pytest.raises(kind) as caught:
        initialize_garmin()
    assert "test-secret" not in str(caught.value)
    with pytest.raises(SystemExit):
        init_api()
    assert "test-secret" not in caplog.text


def test_redis_http_200_error_is_failure_and_redacted(monkeypatch):
    def post(*args, **kwargs):
        return httpx.Response(200, json={"error": "test-secret-response"}, request=httpx.Request("POST", "https://redis.example.invalid"))
    monkeypatch.setattr(httpx.Client, "post", post)
    store = _UpstashRedis("https://redis.example.invalid", "test-only-redis-secret")
    with pytest.raises(TokenStoreError) as caught:
        store.set("key", "test-garmin-secret")
    assert "test-secret" not in str(caught.value)


def test_local_file_refresh_survives_without_redis(tmp_path, monkeypatch, api):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("GARMINTOKENS_BASE64", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / ".garminconnect" / "garmin_tokens.json"
    path.parent.mkdir()
    path.write_text(tokens(1))
    monkeypatch.setattr(Client, "_http_post", lambda *a, **k: Mock(ok=True, json=lambda: {
        "access_token": jwt(time.time() + 3600), "refresh_token": "local-rotated",
    }))
    initialize_garmin()
    assert json.loads(path.read_text())["di_refresh_token"] == "local-rotated"
    assert path.stat().st_mode & 0o777 == 0o600
    assert initialize_garmin().client.di_refresh_token == "local-rotated"


def test_store_keys_must_not_collide(backend, monkeypatch):
    monkeypatch.setenv("GARMIN_TOKEN_STORE_KEY", "mcp:garmin:token_store")
    with pytest.raises(TokenStoreError, match="COLLISION"):
        initialize_garmin()


def test_proxy_serializes_concurrent_calls():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def method():
        calls.append(len(calls))
        entered.set()
        release.wait(2)
    proxy = _GarminProxy(Mock(method=method), timeout=0.01)
    try:
        with pytest.raises(TimeoutError):
            proxy.method()
        assert entered.wait(1)
        with pytest.raises(TimeoutError):
            proxy.method()
        assert len(calls) == 1
    finally:
        release.set()


def test_legacy_garth_tokens_rejected_without_echo(backend, monkeypatch):
    bootstrap(monkeypatch, json.dumps({"oauth1_token": "test-legacy-secret"}))
    with pytest.raises(GarminConnectAuthenticationError) as caught:
        initialize_garmin()
    assert "test-legacy-secret" not in str(caught.value)


@pytest.mark.parametrize("status,kind", [(400, GarminConnectAuthenticationError),
                                        (429, GarminConnectTooManyRequestsError),
                                        (503, GarminConnectConnectionError)])
def test_refresh_failures_preserve_type_and_hide_response(backend, monkeypatch, status, kind):
    bootstrap(monkeypatch, tokens(1))
    monkeypatch.setattr("garminconnect.time.sleep", lambda *_: None)
    monkeypatch.setattr(Client, "_http_post", lambda *a, **k: Mock(
        ok=False, status_code=status, text="test-secret-response",
    ))
    with pytest.raises(kind) as caught:
        initialize_garmin()
    assert "test-secret" not in str(caught.value)


def test_shared_redis_get_set_and_one_transient_retry(monkeypatch):
    calls = []
    def post(self, url, **kwargs):
        command = json.loads(kwargs["content"])
        calls.append(command)
        if len(calls) == 1:
            raise httpx.ConnectError("test-secret-network-error")
        result = "OK" if command[0] == "SET" else "saved-state"
        return httpx.Response(200, json={"result": result}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.Client, "post", post)
    store = _UpstashRedis("https://redis.example.invalid", "test-redis-secret")
    store.set("test-key", "saved-state")
    assert store.get("test-key") == "saved-state"
    assert calls == [["SET", "test-key", "saved-state"], ["SET", "test-key", "saved-state"], ["GET", "test-key"]]


@pytest.mark.parametrize("encoded", ["not base64!", base64.b64encode(b"not json").decode()])
def test_invalid_bootstrap_is_redacted(backend, monkeypatch, caplog, encoded):
    monkeypatch.setenv("GARMINTOKENS_BASE64", encoded)
    with pytest.raises(SystemExit):
        init_api()
    assert encoded not in caplog.text
    assert "not json" not in caplog.text


def test_garmin_migration_preserves_claude_redis_state(backend, api, monkeypatch):
    import asyncio
    from mcp.server.auth.provider import AccessToken, RefreshToken
    from mcp.shared.auth import OAuthClientInformationFull
    from garmin_mcp.github_oauth_provider import GitHubOAuthProvider

    def provider():
        return GitHubOAuthProvider("test-client", "test-secret", "https://example.invalid")

    first = provider()
    asyncio.run(first.register_client(OAuthClientInformationFull(
        client_id="claude-test", redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
    )))
    first._access_tokens["test-access"] = AccessToken(
        token="test-access", client_id="claude-test", scopes=["mcp"], expires_at=int(time.time()) + 3600,
    )
    first._refresh_tokens["test-refresh"] = RefreshToken(
        token="test-refresh", client_id="claude-test", scopes=["mcp"], expires_at=int(time.time()) + 3600,
    )
    first._save_store()
    before = backend["mcp:garmin:token_store"]
    bootstrap(monkeypatch, tokens(time.time() + 3600))
    initialize_garmin()
    assert backend["mcp:garmin:token_store"] == before
    restarted = provider()
    assert asyncio.run(restarted.load_access_token("test-access")) is not None
    client = asyncio.run(restarted.get_client("claude-test"))
    assert asyncio.run(restarted.load_refresh_token(client, "test-refresh")) is not None
