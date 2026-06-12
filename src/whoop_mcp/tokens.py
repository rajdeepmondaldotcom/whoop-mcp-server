"""Token persistence and lifecycle.

WHOOP rotates *both* tokens on every refresh and invalidates the old ones,
so this module is built around two rules:

1. A refreshed token set is persisted to disk before anyone can use it.
2. Refreshes are serialized behind an asyncio lock - concurrent tool calls
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
        return self._freshest()

    def _freshest(self) -> TokenSet | None:
        """Freshest of the in-memory set and tokens.json.

        Reading the file every time keeps a long-lived server in sync with
        the outside world: a `whoop-mcp-server auth` re-run (or another process
        rotating the pair) is picked up without a restart.
        """
        candidates = [t for t in (self._current, self._store.load()) if t is not None]
        if not candidates:
            return None
        return max(candidates, key=lambda t: t.expires_at)

    async def get_access_token(
        self, *, force_refresh: bool = False, rejected: str | None = None
    ) -> str:
        """Return a valid access token.

        ``rejected`` is the token a request just got a 401 with. If the
        current token already differs, a concurrent caller refreshed in the
        meantime and we hand that out instead of rotating again - WHOOP
        invalidates the old pair on every refresh, so redundant rotations
        would knock out sibling requests' retries.
        """
        if self._static:
            return self._static

        async with self._lock:
            tokens = self._freshest()
            if tokens is None:
                raise AuthRequiredError("no stored tokens")

            if force_refresh and rejected is not None and tokens.access_token != rejected:
                force_refresh = False

            needs_refresh = force_refresh or tokens.expires_within(EXPIRY_BUFFER_SECONDS)
            if needs_refresh:
                if not tokens.refresh_token:
                    self._current = None
                    raise AuthRequiredError(
                        "access token expired and no refresh token is available - "
                        "make sure the `offline` scope is enabled"
                    )
                logger.info("Refreshing WHOOP access token")
                try:
                    refreshed = await self._refresher(tokens.refresh_token)
                except Exception:
                    # Drop the in-memory copy so the next attempt re-reads
                    # tokens.json - a fresh `whoop-mcp-server auth` can then rescue a
                    # running server without a restart.
                    self._current = None
                    raise
                if refreshed.refresh_token is None:
                    # Server kept the old refresh token (allowed by RFC 6749 §6).
                    refreshed.refresh_token = tokens.refresh_token
                self._current = refreshed
                try:
                    self._store.save(refreshed)
                except OSError as exc:
                    # Keep serving from memory; losing the rotated pair would
                    # be worse than a stale file.
                    logger.error("Could not persist WHOOP tokens to %s: %s", self._store.path, exc)
                tokens = refreshed

            self._current = tokens
            return tokens.access_token
