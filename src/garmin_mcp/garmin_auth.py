"""DI token persistence for the pinned garminconnect 0.3.6 client.

Private refresh/request hooks are intentional: persist rotation before a profile
fetch can fail or a later request can rotate again. Upgrade with contract tests.
"""

import base64
import datetime
import json
import logging
import os
import re
from pathlib import Path

from garminconnect import (
    Garmin, GarminConnectAuthenticationError, GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.client import Client

from garmin_mcp.token_store import TokenStoreError, _UpstashRedis

logger = logging.getLogger(__name__)


def safe_error(exc: Exception) -> Exception:
    """Recover wrapped causes without copying messages, URLs or response bodies."""
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TokenStoreError):
            code = str(current)
            if code in {"TOKEN_STORE_CONFIG_INVALID", "TOKEN_STORE_CONFIG_INCOMPLETE",
                        "GARMIN_TOKEN_STORE_KEY_COLLISION", "TOKEN_STORE_DATA_INVALID"}:
                return TokenStoreError(code)
            return TokenStoreError("GARMIN_TOKEN_STORE_UNAVAILABLE; retry later without replacing credentials")
        status = getattr(getattr(current, "response", None), "status_code", None)
        if status is None:
            # The pinned library puts status in fixed prefixes, without
            # retaining the response object. Read only that numeric prefix.
            match = re.match(r"(?:API Error |DI token refresh failed: )(\d{3})\b", str(current))
            status = int(match[1]) if match else None
        if status == 429 or isinstance(current, GarminConnectTooManyRequestsError):
            return GarminConnectTooManyRequestsError("GARMIN_RATE_LIMITED; retry later")
        if status in (401, 403):
            return GarminConnectAuthenticationError("GARMIN_AUTH_REJECTED; reauthentication may be required")
        if status is not None and status >= 500:
            return GarminConnectConnectionError("GARMIN_CONNECTION_FAILED; retry later")
        if isinstance(current, GarminConnectConnectionError):
            return GarminConnectConnectionError("GARMIN_CONNECTION_FAILED; retry later")
        cause = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
        if cause is None:
            if isinstance(current, GarminConnectAuthenticationError):
                code = str(current).split(";", 1)[0]
                if code in {"GARMIN_TOKEN_FORMAT_INVALID", "GARMIN_TOKEN_BASE64_INVALID",
                            "GARMIN_CREDENTIALS_MISSING"}:
                    return GarminConnectAuthenticationError(
                        f"{code}; provide DI tokens generated with garminconnect 0.3.6"
                    )
                return GarminConnectAuthenticationError("GARMIN_AUTH_REJECTED; reauthentication may be required")
            return GarminConnectConnectionError("GARMIN_REQUEST_FAILED; retry later")
        current = cause
    return GarminConnectConnectionError("GARMIN_REQUEST_FAILED; retry later")


def _validate_json(raw: str) -> str:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not all(
            isinstance(data.get(k), str) and data[k]
            for k in ("di_token", "di_refresh_token", "di_client_id")
        ):
            raise ValueError
    except Exception:
        raise GarminConnectAuthenticationError("GARMIN_TOKEN_FORMAT_INVALID; generate tokens with garminconnect 0.3.6") from None
    return raw


class _PersistentClient(Client):
    def __init__(self, store, key, **kwargs):
        super().__init__(**kwargs)
        self._store = store
        self._key = key
        self._saved = None

    def persist(self):
        if not self.di_token:
            return
        raw = self.dumps()
        if raw != self._saved:
            if self._store is not None:
                self._store.set(self._key, raw)
            elif self._tokenstore_path:
                self.dump(self._tokenstore_path)
            else:
                return
            self._saved = raw  # mark saved only after acknowledgement

    def _refresh_di_token(self):
        super()._refresh_di_token()
        self.persist()

    def _refresh_session(self):
        # Upstream suppresses refresh/save failures. Surface them, retaining
        # rotated state in memory so a subsequent call can retry persistence.
        self.persist()
        if self.di_token:
            try:
                self._refresh_di_token()
            except Exception as exc:
                raise safe_error(exc) from None
        else:
            super()._refresh_session()

    def _run_request(self, method, path, **kwargs):
        self.persist()  # retry a pending save before issuing another API call
        try:
            return super()._run_request(method, path, **kwargs)
        except Exception as exc:
            raise safe_error(exc) from None
        finally:
            self.persist()  # includes refresh followed by an API failure


def log_token_expiry(garmin):
    # DI access-token expiry is advisory; refresh-token expiry is not supplied
    # by this library. Never claim a guessed 90-day refresh lifetime.
    try:
        payload = garmin.client.di_token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expiry = datetime.datetime.fromtimestamp(data["exp"], datetime.timezone.utc)
        logger.info("Garmin access token expires at %s; refresh-token expiry is unknown.", expiry.isoformat())
    except Exception:
        logger.info("Garmin token expiry metadata unavailable.")


def initialize_garmin(is_cn=False):
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    secret = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if bool(url) != bool(secret):
        raise TokenStoreError("TOKEN_STORE_CONFIG_INCOMPLETE")
    store = _UpstashRedis(url, secret) if url and secret else None
    key = os.getenv("GARMIN_TOKEN_STORE_KEY", "mcp:garmin:credentials")
    if not key.strip() or key == os.getenv("TOKEN_STORE_KEY", "mcp:garmin:token_store"):
        raise TokenStoreError("GARMIN_TOKEN_STORE_KEY_COLLISION")

    # A store outage or corrupt record must never trigger stale-env fallback.
    raw = store.get(key) if store else None
    from_store = raw is not None
    if raw is None:
        encoded = os.getenv("GARMINTOKENS_BASE64")
        if encoded:
            try:
                raw = base64.b64decode(encoded, validate=True).decode("utf-8")
            except Exception:
                raise GarminConnectAuthenticationError("GARMIN_TOKEN_BASE64_INVALID") from None
        else:
            path = Path.home() / ".garminconnect" / "garmin_tokens.json"
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise GarminConnectAuthenticationError("GARMIN_CREDENTIALS_MISSING") from None
    raw = _validate_json(raw)
    garmin = Garmin(is_cn=is_cn)
    # Replace the unused default client; no credentials are copied into it.
    garmin.client.cs.close()
    garmin.client._api_session.close()
    garmin.client = _PersistentClient(
        store, key, domain="garmin.cn" if is_cn else "garmin.com",
    )
    garmin.client.loads(raw)
    if from_store:
        garmin.client._saved = garmin.client.dumps()
    if not store:
        # Preserve library-backed local-file refresh persistence for VPS users.
        if not os.getenv("GARMINTOKENS_BASE64"):
            garmin.client._tokenstore_path = str(path.parent)
        else:
            logger.warning("GARMIN_PERSISTENCE_DISABLED: environment tokens are not saved across restarts.")
    # Save the bootstrap before the first request, and any rotation immediately.
    garmin.client.persist()
    try:
        garmin._load_profile_and_settings()
    except Exception as exc:
        raise safe_error(exc) from None
    log_token_expiry(garmin)
    logger.info("Garmin authentication successful (source=%s).", "store" if from_store else "bootstrap")
    return garmin
