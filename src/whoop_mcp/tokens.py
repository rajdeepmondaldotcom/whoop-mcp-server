"""Token persistence and lifecycle.

WHOOP rotates *both* tokens on every refresh and invalidates the old ones,
so this module is built around two rules:

1. A refreshed token set is persisted to disk before anyone can use it.
2. Refreshes are serialized behind an asyncio lock — concurrent tool calls
   never race each other into burning the same refresh token twice.

Access tokens are refreshed proactively (within ``EXPIRY_BUFFER_SECONDS`` of
expiry) so requests almost never pay a 401 round-trip.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from whoop_mcp.errors import AuthRequiredError

logger = logging.getLogger(__name__)

EXPIRY_BUFFER_SECONDS = 120.0


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float  # unix epoch seconds
    scope: str = ""
    token_type: str = "bearer"

    @classmethod
    def from_token_response(cls, payload: dict, *, now: float | None = None) -> TokenSet:
        issued = now if now is not None else time.time()
        try:
            expires_in = float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600.0
        return cls(
            access_token=str(payload["access_token"]),
            refresh_token=(str(payload["refresh_token"]) if payload.get("refresh_token") else None),
            expires_at=issued + expires_in,
            scope=str(payload.get("scope", "")),
            token_type=str(payload.get("token_type", "bearer")),
        )

    def expires_within(self, seconds: float, *, now: float | None = None) -> bool:
        current = now if now is not None else time.time()
        return self.expires_at - current <= seconds


class TokenStore:
    """Atomic, 0600-permission JSON storage for a TokenSet."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> TokenSet | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable token file %s: %s", self.path, exc)
            return None
        if not isinstance(raw, dict) or not raw.get("access_token"):
            logger.warning("Ignoring malformed token file %s", self.path)
            return None
        try:
            return TokenSet(
                access_token=str(raw["access_token"]),
                refresh_token=(str(raw["refresh_token"]) if raw.get("refresh_token") else None),
                expires_at=float(raw.get("expires_at", 0)),
                scope=str(raw.get("scope", "")),
                token_type=str(raw.get("token_type", "bearer")),
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Ignoring malformed token file %s", self.path)
            return None

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(tokens), indent=2) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


Refresher = Callable[[str], Awaitable[TokenSet]]


class TokenManager:
    """Hands out a valid access token, refreshing and persisting as needed."""

    def __init__(
        self,
        store: TokenStore,
        refresher: Refresher,
        *,
        static_access_token: str | None = None,
    ) -> None:
        self._store = store
        self._refresher = refresher
        self._static = static_access_token
        self._current: TokenSet | None = None
        self._lock = asyncio.Lock()

    @property
    def current(self) -> TokenSet | None:
        return self._current or self._store.load()

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        if self._static:
            return self._static

        async with self._lock:
            tokens = self._current or self._store.load()
            if tokens is None:
                raise AuthRequiredError("no stored tokens")

            needs_refresh = force_refresh or tokens.expires_within(EXPIRY_BUFFER_SECONDS)
            if needs_refresh:
                if not tokens.refresh_token:
                    raise AuthRequiredError(
                        "access token expired and no refresh token is available — "
                        "make sure the `offline` scope is enabled"
                    )
                logger.info("Refreshing WHOOP access token")
                refreshed = await self._refresher(tokens.refresh_token)
                if refreshed.refresh_token is None:
                    # Server kept the old refresh token (allowed by RFC 6749 §6).
                    refreshed.refresh_token = tokens.refresh_token
                self._store.save(refreshed)
                tokens = refreshed

            self._current = tokens
            return tokens.access_token
