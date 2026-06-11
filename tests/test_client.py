import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import FakeWhoop, StaticTokens, make_cycle
from whoop_mcp.client import WhoopClient
from whoop_mcp.errors import ApiError, AuthRequiredError, RateLimitError

pytestmark = pytest.mark.anyio


def client_for(handler, **kwargs) -> WhoopClient:
    return WhoopClient(StaticTokens(), transport=httpx.MockTransport(handler), **kwargs)


@pytest.fixture
def no_sleep(monkeypatch):
    """Capture retry sleeps instead of actually waiting."""
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("whoop_mcp.client.asyncio.sleep", fake_sleep)
    return delays


async def test_pagination_merges_pages(fake_whoop: FakeWhoop):
    now = datetime.now(timezone.utc)
    for i in range(60):
        fake_whoop.cycles.append(
            make_cycle(i, now - timedelta(days=i), now - timedelta(days=i - 1))
        )
    client = client_for(fake_whoop.handler)
    records, truncated = await client.cycles(max_records=100)
    assert len(records) == 60
    assert truncated is False
    list_requests = [r for r in fake_whoop.requests if "limit" in r]
    assert len(list_requests) == 3  # 25 + 25 + 10


async def test_pagination_truncates_at_cap(fake_whoop: FakeWhoop):
    now = datetime.now(timezone.utc)
    for i in range(60):
        fake_whoop.cycles.append(
            make_cycle(i, now - timedelta(days=i), now - timedelta(days=i - 1))
        )
    client = client_for(fake_whoop.handler)
    records, truncated = await client.cycles(max_records=30)
    assert len(records) == 30
    assert truncated is True


async def test_401_triggers_refresh_and_retry():
    tokens = StaticTokens(token="stale")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer stale":
            return httpx.Response(401)
        return httpx.Response(200, json={"user_id": 1})

    client = WhoopClient(tokens, transport=httpx.MockTransport(handler))
    profile = await client.profile()
    assert profile == {"user_id": 1}
    assert tokens.refreshes == 1
    assert seen == ["Bearer stale", "Bearer test-token"]


async def test_401_twice_raises_auth_required():
    client = client_for(lambda request: httpx.Response(401))
    with pytest.raises(AuthRequiredError):
        await client.profile()


async def test_429_honors_rate_limit_reset_header(no_sleep):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"X-RateLimit-Reset": "7"})
        return httpx.Response(200, json={"user_id": 1})

    client = client_for(handler)
    assert await client.profile() == {"user_id": 1}
    assert no_sleep == [7.0]


async def test_429_exhausts_into_rate_limit_error(no_sleep):
    client = client_for(lambda request: httpx.Response(429), max_retries=2)
    with pytest.raises(RateLimitError):
        await client.profile()


async def test_server_error_retries_then_succeeds(no_sleep):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"user_id": 1})

    client = client_for(handler)
    assert await client.profile() == {"user_id": 1}
    assert calls == 3
    assert len(no_sleep) == 2


async def test_network_error_retries(no_sleep):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"user_id": 1})

    client = client_for(handler)
    assert await client.profile() == {"user_id": 1}


async def test_404_raises_api_error(fake_whoop: FakeWhoop):
    client = client_for(fake_whoop.handler)
    with pytest.raises(ApiError, match="not found"):
        await client.sleep("missing-id")


async def test_403_message_mentions_scopes():
    client = client_for(lambda request: httpx.Response(403))
    with pytest.raises(ApiError, match="scope"):
        await client.profile()


async def test_cache_avoids_duplicate_requests(fake_whoop: FakeWhoop):
    client = client_for(fake_whoop.handler, cache_ttl=300.0)
    await client.profile()
    await client.profile()
    profile_hits = [r for r in fake_whoop.requests if "profile" in r]
    assert len(profile_hits) == 1
