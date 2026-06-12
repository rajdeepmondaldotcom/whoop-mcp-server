"""Demo mode: deterministic generated data through the full real pipeline."""

from datetime import date, timezone

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import whoop_mcp.server as srv
from conftest import result_json
from whoop_mcp.client import WhoopClient
from whoop_mcp.config import Settings
from whoop_mcp.demo import DemoTokens, DemoWhoop, build_demo_dataset

pytestmark = pytest.mark.anyio

FIXED_TODAY = date(2026, 6, 12)


def test_dataset_is_deterministic():
    a = build_demo_dataset(days=30, today=FIXED_TODAY)
    b = build_demo_dataset(days=30, today=FIXED_TODAY)
    assert a == b


def test_dataset_shape_and_patterns():
    data = build_demo_dataset(days=60, today=FIXED_TODAY)
    assert len(data["cycles"]) == 60
    assert len(data["recoveries"]) == 60
    main_sleeps = [s for s in data["sleeps"] if not s["nap"]]
    naps = [s for s in data["sleeps"] if s["nap"]]
    assert len(main_sleeps) == 60
    assert naps, "demo data should include naps"
    assert data["workouts"], "demo data should include workouts"
    # Today's cycle is in progress.
    newest_cycle = max(data["cycles"], key=lambda c: c["start"])
    assert newest_cycle["end"] is None
    # The trip block uses a different timezone offset.
    offsets = {c["timezone_offset"] for c in data["cycles"]}
    assert offsets == {"-04:00", "+02:00"}
    # First days are calibrating.
    oldest = min(data["recoveries"], key=lambda r: r["created_at"])
    assert oldest["score"]["user_calibrating"] is True


@pytest.fixture
async def demo_session(tmp_path):
    client = WhoopClient(DemoTokens(), transport=DemoWhoop().transport(), cache_ttl=300.0)
    settings = Settings(data_dir=tmp_path, demo_mode=True)
    srv.configure_for_testing(client, tz=timezone.utc, settings=settings)
    try:
        async with create_connected_server_and_client_session(srv.mcp) as client_session:
            yield client_session
    finally:
        await srv.reset_state()


async def test_demo_overview_end_to_end(demo_session):
    data = result_json(await demo_session.call_tool("get_health_overview", {"days": 90}))
    assert data["today"]["summary"]
    assert data["trends"]["recovery"]["recovery_score"]["average"] > 0
    assert data["records"]["totals"]["workouts"] > 20
    assert data["training_load"]["ratio"] > 0


async def test_demo_correlations_find_the_planted_pattern(demo_session):
    data = result_json(await demo_session.call_tool("get_correlations", {"days": 120}))
    strain_recovery = next(
        c for c in data["correlations"] if c["pair"] == "day strain vs next-morning recovery"
    )
    # The generator dips recovery after >14-strain days; the math must find it.
    assert strain_recovery["r"] < -0.15
    assert strain_recovery["n"] >= 100


async def test_demo_sleep_stream(demo_session):
    sleeps = result_json(await demo_session.call_tool("get_sleeps", {"limit": 2}))
    sleep_id = next(r["id"] for r in sleeps["records"] if not r["nap"])
    stream = result_json(
        await demo_session.call_tool("get_sleep_stream", {"sleep_id": sleep_id})
    )
    assert stream["available"] is True
    assert stream["heart_rate"]["min"] < stream["heart_rate"]["avg"]
    assert stream["pct_asleep"] > 80
    assert len(stream["series"]) > 30


async def test_demo_connection_tools_explain_demo_mode(demo_session):
    status = result_json(await demo_session.call_tool("get_connection_status", {}))
    assert status["connected"] is True
    assert status["mode"] == "demo"

    connect = result_json(await demo_session.call_tool("connect_whoop_account", {}))
    assert connect["mode"] == "demo"
    assert "nothing to authorize" in connect["note"]


async def test_demo_export_works_offline(demo_session, tmp_path):
    data = result_json(
        await demo_session.call_tool("export_data", {"start": "30 days ago", "end": "today"})
    )
    assert data["counts"]["cycles"] == 31
    assert data["counts"]["sleeps"] >= 31  # naps included
