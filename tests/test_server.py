"""End-to-end tests: a real MCP client session talking to the real server
over in-memory streams, with the WHOOP API faked at the HTTP layer."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import whoop_mcp.server as srv
from conftest import FakeWhoop, StaticTokens, result_json
from whoop_mcp.client import WhoopClient

pytestmark = pytest.mark.anyio


@pytest.fixture
async def session(fake_whoop: FakeWhoop):
    fake_whoop.seed_days(35)
    client = WhoopClient(
        StaticTokens(), transport=httpx.MockTransport(fake_whoop.handler), cache_ttl=300.0
    )
    srv.configure_for_testing(client, tz=timezone.utc)
    try:
        async with create_connected_server_and_client_session(srv.mcp) as client_session:
            yield client_session
    finally:
        await srv.reset_state()


async def test_lists_sixteen_read_only_tools(session):
    tools = (await session.list_tools()).tools
    assert len(tools) == 16
    assert all(t.annotations and t.annotations.readOnlyHint for t in tools)
    names = {t.name for t in tools}
    assert {"get_daily_summary", "search", "fetch", "compare_periods"} <= names


async def test_get_profile(session):
    result = await session.call_tool("get_profile", {})
    assert not result.isError
    data = result_json(result)
    assert data["name"] == "Ada Lovelace"
    assert data["body"]["max_heart_rate"] == 195


async def test_daily_summary_for_yesterday(session):
    result = await session.call_tool("get_daily_summary", {"day": "yesterday"})
    assert not result.isError
    data = result_json(result)
    expected = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    assert data["date"] == str(expected)
    assert "recovery" in data
    assert "sleep" in data
    assert "Recovery" in data["summary"]


async def test_collections_and_detail_round_trip(session):
    listing = result_json(await session.call_tool("get_sleeps", {"limit": 5}))
    assert listing["count"] == 5
    sleep_id = listing["records"][0]["id"]

    detail = result_json(await session.call_tool("get_sleep", {"sleep_id": sleep_id}))
    assert detail["id"] == sleep_id
    assert detail["duration"]["asleep_hours"] > 5


async def test_workout_sport_filter(session):
    result = result_json(
        await session.call_tool(
            "get_workouts", {"sport": "run", "limit": 50, "start": "30 days ago"}
        )
    )
    assert result["count"] > 0
    assert all(r["sport"] == "running" for r in result["records"])


async def test_cycle_includes_recovery_and_sleep(session):
    cycles = result_json(await session.call_tool("get_cycles", {"limit": 3}))
    cycle_id = cycles["records"][0]["id"]
    bundle = result_json(await session.call_tool("get_cycle", {"cycle_id": cycle_id}))
    assert bundle["cycle"]["id"] == cycle_id
    assert bundle["recovery"]["cycle_id"] == cycle_id
    assert bundle["sleep"]["cycle_id"] == cycle_id


async def test_recovery_trends_shape(session):
    data = result_json(await session.call_tool("get_recovery_trends", {"days": 14}))
    assert data["recovery_score"]["days_with_data"] >= 13
    assert "direction" in data["recovery_score"]["trend"]
    assert "daily" in data
    assert data["hrv_ms"]["average"] > 0


async def test_strain_trends_include_training_load(session):
    data = result_json(await session.call_tool("get_strain_trends", {"days": 30}))
    assert data["day_strain"]["days_with_data"] >= 28
    assert "training_load" in data
    assert data["training_load"]["ratio"] > 0
    assert data["workouts"]["total"] > 0
    assert "running" in data["workouts"]["by_sport"]


async def test_compare_periods(session):
    data = result_json(
        await session.call_tool(
            "compare_periods",
            {
                "period_a_start": "last week",
                "period_a_end": "last week",
                "period_b_start": "this week",
                "period_b_end": "this week",
            },
        )
    )
    assert "comparison" in data
    assert "recovery_score" in data["comparison"]


async def test_search_then_fetch_round_trip(session):
    found = result_json(await session.call_tool("search", {"query": "how did i sleep last week"}))
    assert found["results"], "search should return results"
    ids = [r["id"] for r in found["results"]]
    assert any(i.startswith("day:") for i in ids)
    assert any(i.startswith("sleep:") for i in ids)
    assert all(set(r) >= {"id", "title", "url"} for r in found["results"])

    target = next(i for i in ids if i.startswith("day:"))
    doc = result_json(await session.call_tool("fetch", {"id": target}))
    assert doc["id"] == target
    assert doc["title"].startswith("WHOOP day summary")
    parsed = json.loads(doc["text"])
    assert parsed["date"] == target.split(":", 1)[1]
    assert doc["metadata"]["type"] == "day_summary"


async def test_fetch_workout_and_profile_ids(session, fake_whoop: FakeWhoop):
    workout_id = fake_whoop.workouts[0]["id"]
    doc = result_json(await session.call_tool("fetch", {"id": f"workout:{workout_id}"}))
    assert doc["metadata"]["type"] == "workout"

    doc = result_json(await session.call_tool("fetch", {"id": "profile"}))
    assert "Ada" in doc["text"]


async def test_fetch_unknown_id_is_clean_error(session):
    result = await session.call_tool("fetch", {"id": "nonsense"})
    assert result.isError
    assert "Invalid document id" in result.content[0].text


async def test_invalid_date_expression_is_clean_error(session):
    result = await session.call_tool("get_daily_summary", {"day": "someday maybe"})
    assert result.isError
    assert "Supported forms" in result.content[0].text


async def test_resources_and_prompts(session):
    resources = (await session.list_resources()).resources
    assert {str(r.uri) for r in resources} == {
        "whoop://profile",
        "whoop://summary/today",
        "whoop://recovery/latest",
        "whoop://sleep/latest",
    }
    content = await session.read_resource("whoop://recovery/latest")
    payload = json.loads(content.contents[0].text)
    assert "recovery_score" in payload

    prompts = (await session.list_prompts()).prompts
    assert len(prompts) == 4
    prompt = await session.get_prompt("morning_readiness", {})
    assert "get_daily_summary" in prompt.messages[0].content.text
