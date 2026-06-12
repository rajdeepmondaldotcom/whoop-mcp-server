"""End-to-end tests: a real MCP client session talking to the real server
over in-memory streams, with the WHOOP API faked at the HTTP layer."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import whoop_mcp.server as srv
from conftest import FakeWhoop, StaticTokens, result_json
from whoop_mcp.client import WhoopClient

pytestmark = pytest.mark.anyio


@pytest.fixture
async def session(fake_whoop: FakeWhoop, tmp_path):
    from whoop_mcp.config import Settings

    fake_whoop.seed_days(35)
    client = WhoopClient(
        StaticTokens(), transport=httpx.MockTransport(fake_whoop.handler), cache_ttl=300.0
    )
    settings = Settings(client_id="cid", client_secret="secret", data_dir=tmp_path)
    srv.configure_for_testing(client, tz=timezone.utc, settings=settings)
    try:
        async with create_connected_server_and_client_session(srv.mcp) as client_session:
            yield client_session
    finally:
        await srv.reset_state()


WRITE_TOOLS = {"connect_whoop_account", "export_data"}


async def test_lists_all_tools_with_correct_annotations(session):
    tools = (await session.list_tools()).tools
    assert len(tools) == 23
    for tool in tools:
        assert tool.annotations is not None, tool.name
        expected_read_only = tool.name not in WRITE_TOOLS
        assert tool.annotations.readOnlyHint is expected_read_only, tool.name
    names = {t.name for t in tools}
    assert {
        "get_daily_summary",
        "get_health_overview",
        "get_correlations",
        "get_personal_records",
        "get_sleep_stream",
        "export_data",
        "connect_whoop_account",
        "get_connection_status",
        "search",
        "fetch",
        "compare_periods",
    } <= names


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


async def test_health_overview_is_holistic(session):
    data = result_json(await session.call_tool("get_health_overview", {"days": 30}))
    assert "today" in data
    assert data["trends"]["recovery"]["recovery_score"]["average"] > 0
    assert "direction" in data["trends"]["recovery"]["recovery_score"]
    assert data["records"]["best_recovery"]["value"] >= data["records"]["worst_recovery"]["value"]
    assert data["training_load"]["ratio"] > 0
    assert isinstance(data["correlations"], list) and data["correlations"]


async def test_correlations_shape(session):
    data = result_json(await session.call_tool("get_correlations", {"days": 30}))
    assert data["days_analyzed"] >= 28
    pairs = {c["pair"] for c in data["correlations"]}
    assert "day strain vs next-morning recovery" in pairs
    for item in data["correlations"]:
        assert "note" in item or (-1.0 <= item["r"] <= 1.0 and item["n"] >= 10)


async def test_personal_records_and_streaks(session):
    data = result_json(await session.call_tool("get_personal_records", {"days": 30}))
    assert data["green_streak"]["longest_days"] >= 1
    assert data["best_recovery"]["value"] <= 100
    assert data["biggest_workout"]["sport"] in ("running", "cycling")
    assert data["totals"]["workouts"] > 0


async def test_sleep_stream_available_and_unavailable(session, fake_whoop: FakeWhoop):
    sleep = fake_whoop.sleeps[-2]
    from whoop_mcp.timeutil import parse_iso

    fake_whoop.seed_stream(sleep["id"], parse_iso(sleep["start"]), parse_iso(sleep["end"]))
    data = result_json(
        await session.call_tool(
            "get_sleep_stream", {"sleep_id": sleep["id"], "resolution_minutes": 10}
        )
    )
    assert data["available"] is True
    assert data["heart_rate"]["min"] < data["heart_rate"]["max"]
    assert data["heart_rate"]["min"] == 48
    assert len(data["series"]) >= 40  # ~7.75h at 10-minute buckets
    assert data["pct_asleep"] > 90

    no_stream = result_json(
        await session.call_tool("get_sleep_stream", {"sleep_id": fake_whoop.sleeps[0]["id"]})
    )
    assert no_stream["available"] is False
    assert "stream" in no_stream["note"]


async def test_export_data_writes_files(session, tmp_path):
    import csv as csv_module

    data = result_json(
        await session.call_tool("export_data", {"start": "14 days ago", "end": "today"})
    )
    assert data["counts"]["cycles"] == 15  # 14 days ago .. today inclusive
    # Every day's sleep must be present - including the FIRST day, whose sleep
    # started the evening before the window (the classic off-by-one-night bug).
    assert data["counts"]["sleeps"] == data["counts"]["cycles"]

    export_dir = Path(data["directory"])
    assert export_dir.is_relative_to(tmp_path)
    payload = json.loads((export_dir / "data.json").read_text())
    assert payload["meta"]["complete"] is True
    assert len(payload["data"]["cycles"]) == data["counts"]["cycles"]
    assert "raw" in payload

    with (export_dir / "daily_summary.csv").open() as handle:
        rows = list(csv_module.DictReader(handle))
    assert len(rows) == 15
    assert {"date", "recovery", "sleep_hours", "strain"} <= set(rows[0])
    assert rows[0]["sleep_hours"] != ""  # first morning's sleep included
    assert all(row["recovery"] != "" for row in rows)


async def test_connection_status_disconnected_and_connected(session, tmp_path):
    data = result_json(await session.call_tool("get_connection_status", {}))
    assert data["connected"] is False
    assert "how_to_connect" in data

    # Simulate a completed auth: tokens on disk.
    import time as time_module

    from whoop_mcp.tokens import TokenSet, TokenStore

    TokenStore(tmp_path / "tokens.json").save(
        TokenSet("test-token", "refresh", time_module.time() + 3600, scope="offline read:sleep")
    )
    data = result_json(await session.call_tool("get_connection_status", {}))
    assert data["connected"] is True
    assert data["api_reachable"] is True
    assert data["authorized_as"] == "Ada Lovelace"


async def test_connect_tool_without_credentials_returns_steps(session, fake_whoop, tmp_path):
    from whoop_mcp.config import Settings

    srv.configure_for_testing(
        srv._state.client, settings=Settings(data_dir=tmp_path)  # no client id/secret
    )
    data = result_json(await session.call_tool("connect_whoop_account", {}))
    assert data["connected"] is False
    assert any("developer-dashboard.whoop.com" in step for step in data["steps"])


async def test_include_raw_attaches_original_record(session, fake_whoop: FakeWhoop):
    sleep_id = fake_whoop.sleeps[0]["id"]
    data = result_json(
        await session.call_tool("get_sleep", {"sleep_id": sleep_id, "include_raw": True})
    )
    assert data["raw"]["id"] == sleep_id
    assert "stage_summary" in data["raw"]["score"]


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
