import asyncio
import json
import stat
import time

import pytest

from whoop_mcp.errors import AuthRequiredError
from whoop_mcp.tokens import TokenManager, TokenSet, TokenStore

pytestmark = pytest.mark.anyio


def fresh(expires_in: float = 3600.0) -> TokenSet:
    return TokenSet(
        access_token="access-1",
        refresh_token="refresh-1",
        expires_at=time.time() + expires_in,
        scope="offline read:profile",
    )


def test_store_round_trip_and_permissions(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    tokens = fresh()
    store.save(tokens)
    mode = stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode)
    assert mode == 0o600
    loaded = store.load()
    assert loaded == tokens


def test_store_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{not json", encoding="utf-8")
    assert TokenStore(path).load() is None
    path.write_text(json.dumps({"refresh_token": "x"}), encoding="utf-8")
    assert TokenStore(path).load() is None


async def test_missing_tokens_raise_auth_required(tmp_path):
    manager = TokenManager(TokenStore(tmp_path / "tokens.json"), refresher=_fail_refresher)
    with pytest.raises(AuthRequiredError, match="whoop-mcp auth"):
        await manager.get_access_token()


async def _fail_refresher(_token: str) -> TokenSet:
    raise AssertionError("refresher should not be called")


async def test_valid_token_returned_without_refresh(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(fresh())
    manager = TokenManager(store, refresher=_fail_refresher)
    assert await manager.get_access_token() == "access-1"


async def test_expired_token_refreshes_and_persists_rotation(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(fresh(expires_in=30))  # inside the 120s buffer

    async def refresher(refresh_token: str) -> TokenSet:
        assert refresh_token == "refresh-1"
        return TokenSet("access-2", "refresh-2", time.time() + 3600)

    manager = TokenManager(store, refresher=refresher)
    assert await manager.get_access_token() == "access-2"
    on_disk = store.load()
    assert on_disk is not None
    assert on_disk.refresh_token == "refresh-2"  # rotation persisted immediately


async def test_refresh_keeps_old_refresh_token_when_not_rotated(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(fresh(expires_in=0))

    async def refresher(_token: str) -> TokenSet:
        return TokenSet("access-2", None, time.time() + 3600)

    manager = TokenManager(store, refresher=refresher)
    await manager.get_access_token()
    on_disk = store.load()
    assert on_disk is not None
    assert on_disk.refresh_token == "refresh-1"


async def test_concurrent_calls_refresh_once(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(fresh(expires_in=0))
    calls = 0

    async def refresher(_token: str) -> TokenSet:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return TokenSet(f"access-{calls + 1}", f"refresh-{calls + 1}", time.time() + 3600)

    manager = TokenManager(store, refresher=refresher)
    results = await asyncio.gather(*(manager.get_access_token() for _ in range(5)))
    assert calls == 1
    assert set(results) == {"access-2"}


async def test_static_token_bypasses_everything(tmp_path):
    manager = TokenManager(
        TokenStore(tmp_path / "tokens.json"),
        refresher=_fail_refresher,
        static_access_token="static-abc",
    )
    assert await manager.get_access_token() == "static-abc"


async def test_expired_without_refresh_token_raises(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(TokenSet("access-1", None, time.time() - 10))
    manager = TokenManager(store, refresher=_fail_refresher)
    with pytest.raises(AuthRequiredError, match="offline"):
        await manager.get_access_token()
