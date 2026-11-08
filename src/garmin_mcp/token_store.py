"""Shared Redis transport. Never include credentials or response bodies in errors."""

import json
from urllib.parse import urlsplit

import httpx


class TokenStoreError(RuntimeError):
    """A token store is unavailable; its contents must not be treated as missing."""


class _UpstashRedis:
    def __init__(self, url: str, token: str) -> None:
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment
                or parts.path not in ("", "/")):
            raise TokenStoreError("TOKEN_STORE_CONFIG_INVALID")
        self._url = url.rstrip("/")
        self._token = token

    def _cmd(self, *args) -> object:
        # Only GET and idempotent SET are used. Never replay Garmin API writes.
        for attempt in range(2):
            try:
                with httpx.Client(timeout=5) as client:
                    response = client.post(
                        self._url,
                        headers={"Authorization": f"Bearer {self._token}",
                                 "Content-Type": "application/json"},
                        content=json.dumps(list(args)),
                    )
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict) or "error" in data or "result" not in data:
                        raise ValueError("Invalid Redis response")
                    return data["result"]
            except Exception:
                if attempt:
                    raise TokenStoreError("TOKEN_STORE_UNAVAILABLE") from None

    def get(self, key: str) -> str | None:
        result = self._cmd("GET", key)
        if result is not None and not isinstance(result, str):
            raise TokenStoreError("TOKEN_STORE_DATA_INVALID")
        return result

    def set(self, key: str, value: str) -> None:
        if self._cmd("SET", key, value) != "OK":
            raise TokenStoreError("TOKEN_STORE_WRITE_FAILED")
