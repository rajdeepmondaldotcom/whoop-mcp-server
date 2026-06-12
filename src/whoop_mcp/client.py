"""Async WHOOP API v2 client.

Wraps httpx with the behaviors a well-mannered WHOOP integration needs:

* Bearer auth with proactive refresh, plus a single forced refresh + retry
  if a request still hits 401.
* Retries with exponential backoff and jitter on 5xx/network errors, and
  429 handling that honors WHOOP's ``X-RateLimit-Reset`` header.
* Transparent pagination (`nextToken`) with an overall record cap so a
  single tool call can't run away with the daily quota.
* A small in-memory TTL cache keyed by path+params, sized for the access
  pattern of an LLM session (the same windows get re-queried constantly).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime
from typing import Any, Protocol

import httpx

from whoop_mcp.config import API_BASE_URL
from whoop_mcp.errors import ApiError, AuthRequiredError, RateLimitError
from whoop_mcp.timeutil import to_api_iso

logger = logging.getLogger(__name__)

PAGE_SIZE = 25  # WHOOP's maximum per-page limit
MAX_PAGES = 60  # hard backstop regardless of requested record cap
DEFAULT_MAX_RECORDS = 100
ABSOLUTE_MAX_RECORDS = 1000

PROFILE_TTL = 3600.0
DETAIL_TTL = 300.0


class TokenProvider(Protocol):
    async def get_access_token(
        self, *, force_refresh: bool = False, rejected: str | None = None
    ) -> str: ...


class _TTLCache:
    """Tiny TTL cache. Not thread-safe; lives on one event loop like the client."""

    def __init__(self, max_entries: int = 256) -> None:
        self._data: dict[Any, tuple[float, Any]] = {}
        self._max = max_entries

    def get(self, key: Any) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        deadline, value = entry
        if time.monotonic() >= deadline:
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: Any, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        if len(self._data) >= self._max:
            # Drop the stalest half rather than tracking LRU order.
            cutoff = sorted(deadline for deadline, _ in self._data.values())[self._max // 2]
            self._data = {k: v for k, v in self._data.items() if v[0] > cutoff}
        self._data[key] = (time.monotonic() + ttl, value)

    def clear(self) -> None:
        self._data.clear()


class WhoopClient:
    """Typed access to every WHOOP API v2 read endpoint."""

    def __init__(
        self,
        tokens: TokenProvider,
        *,
        base_url: str = API_BASE_URL,
        timeout: float = 30.0,
        cache_ttl: float = 60.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._tokens = tokens
        self._cache_ttl = cache_ttl
        self._max_retries = max_retries
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "whoop-mcp (+https://github.com/rajdeepmondaldotcom/whoop-mcp-server)"},
        )
        self._cache = _TTLCache()

    async def aclose(self) -> None:
        await self._http.aclose()

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------------ core

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        attempts = 0
        auth_retried = False
        while True:
            token = await self._tokens.get_access_token()
            try:
                response = await self._http.request(
                    method, path, params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                attempts += 1
                if attempts > self._max_retries:
                    raise ApiError(0, f"network error talking to WHOOP: {exc}") from exc
                await asyncio.sleep(self._backoff(attempts))
                continue

            if response.status_code == 401:
                if auth_retried:
                    raise AuthRequiredError("WHOOP rejected the access token even after refresh")
                auth_retried = True
                # Passing the rejected token lets the manager skip the rotation
                # when a concurrent request already refreshed past it.
                await self._tokens.get_access_token(force_refresh=True, rejected=token)
                continue

            if response.status_code == 429:
                attempts += 1
                if attempts > self._max_retries:
                    raise RateLimitError()
                delay = self._rate_limit_delay(response, attempts)
                logger.warning("WHOOP rate limit hit; retrying in %.1fs", delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 500:
                attempts += 1
                if attempts > self._max_retries:
                    raise ApiError(response.status_code, "WHOOP server error; try again later")
                await asyncio.sleep(self._backoff(attempts))
                continue

            if response.is_error:
                raise ApiError(response.status_code, self._error_detail(response))

            if not response.content:
                return None
            return response.json()

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.25), 10.0)

    @staticmethod
    def _rate_limit_delay(response: httpx.Response, attempt: int) -> float:
        for header in ("Retry-After", "X-RateLimit-Reset"):
            raw = response.headers.get(header)
            if raw:
                try:
                    return min(max(float(raw), 1.0), 30.0)
                except ValueError:
                    continue
        return min(2.0 * attempt, 30.0)

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        snippets = {
            400: "bad request",
            403: (
                "forbidden - your WHOOP app is missing a scope for this data, or the "
                "WHOOP membership is inactive. Re-run `whoop-mcp auth` after enabling "
                "all read scopes in the developer dashboard"
            ),
            404: "not found - check the id",
        }
        base = snippets.get(response.status_code, "unexpected response")
        body = response.text[:200].strip()
        return f"{base}: {body}" if body and response.status_code == 400 else base

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, *, ttl: float | None = None
    ) -> Any:
        key = (path, tuple(sorted((params or {}).items())))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        value = await self._request("GET", path, params)
        self._cache.put(key, value, self._cache_ttl if ttl is None else ttl)
        return value

    async def _collect(
        self,
        path: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        max_records: int = DEFAULT_MAX_RECORDS,
        ttl: float | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fetch a paginated collection. Returns (records, truncated)."""
        max_records = max(1, min(max_records, ABSOLUTE_MAX_RECORDS))
        base_params: dict[str, Any] = {}
        if start is not None:
            base_params["start"] = to_api_iso(start)
        if end is not None:
            base_params["end"] = to_api_iso(end)

        cache_key = (path, tuple(sorted(base_params.items())), max_records)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        records: list[dict[str, Any]] = []
        next_token: str | None = None
        truncated = False
        for _ in range(MAX_PAGES):
            params = dict(base_params)
            params["limit"] = min(PAGE_SIZE, max_records - len(records))
            if next_token:
                params["nextToken"] = next_token
            payload = await self._request("GET", path, params) or {}
            records.extend(payload.get("records") or [])
            next_token = payload.get("next_token") or None
            if len(records) >= max_records:
                truncated = next_token is not None or len(records) > max_records
                records = records[:max_records]
                break
            if not next_token:
                break
        else:
            truncated = True

        result = (records, truncated)
        self._cache.put(cache_key, result, self._cache_ttl if ttl is None else ttl)
        return result

    # ------------------------------------------------------------- endpoints

    async def profile(self) -> dict[str, Any]:
        return await self._get("/v2/user/profile/basic", ttl=PROFILE_TTL)

    async def body_measurement(self) -> dict[str, Any]:
        return await self._get("/v2/user/measurement/body", ttl=PROFILE_TTL)

    async def cycles(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> tuple[list[dict[str, Any]], bool]:
        return await self._collect("/v2/cycle", start=start, end=end, max_records=max_records)

    async def cycle(self, cycle_id: int) -> dict[str, Any]:
        return await self._get(f"/v2/cycle/{int(cycle_id)}", ttl=DETAIL_TTL)

    async def cycle_recovery(self, cycle_id: int) -> dict[str, Any]:
        return await self._get(f"/v2/cycle/{int(cycle_id)}/recovery", ttl=DETAIL_TTL)

    async def cycle_sleep(self, cycle_id: int) -> dict[str, Any]:
        return await self._get(f"/v2/cycle/{int(cycle_id)}/sleep", ttl=DETAIL_TTL)

    async def recoveries(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> tuple[list[dict[str, Any]], bool]:
        return await self._collect("/v2/recovery", start=start, end=end, max_records=max_records)

    async def sleeps(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> tuple[list[dict[str, Any]], bool]:
        return await self._collect(
            "/v2/activity/sleep", start=start, end=end, max_records=max_records
        )

    async def sleep(self, sleep_id: str) -> dict[str, Any]:
        return await self._get(f"/v2/activity/sleep/{sleep_id}", ttl=DETAIL_TTL)

    async def sleep_stream(self, sleep_id: str) -> dict[str, Any]:
        """Granular in-sleep sensor stream (heart rate, skin temperature).

        Present in WHOOP's OpenAPI spec but sparsely documented; some accounts
        or app configurations may get 403/404 here - callers should degrade
        gracefully.
        """
        return await self._get(f"/v2/activity/sleep/{sleep_id}/stream", ttl=PROFILE_TTL)

    async def workouts(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> tuple[list[dict[str, Any]], bool]:
        return await self._collect(
            "/v2/activity/workout", start=start, end=end, max_records=max_records
        )

    async def workout(self, workout_id: str) -> dict[str, Any]:
        return await self._get(f"/v2/activity/workout/{workout_id}", ttl=DETAIL_TTL)

    async def revoke_access(self) -> None:
        await self._request("DELETE", "/v2/user/access")
