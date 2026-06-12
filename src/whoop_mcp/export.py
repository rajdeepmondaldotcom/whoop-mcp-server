"""Full-history export of WHOOP data to local files.

Fetches every record in a date range - chunked into windows so pagination
caps are never hit, deduplicated across chunk boundaries - and writes:

* ``data.json``  - everything, transformed (and optionally the raw API
  records), suitable for re-analysis or archival
* ``daily_summary.csv`` - one row per day (recovery, HRV, RHR, sleep,
  strain, calories, workouts), spreadsheet-ready
* ``workouts.csv`` - one row per workout

Nothing leaves the machine; files land under the data directory.
"""

from __future__ import annotations

import asyncio
import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from whoop_mcp.client import WhoopClient
from whoop_mcp.errors import RateLimitError
from whoop_mcp.summaries import (
    Bundle,
    _daily_metric_table,
    cycle_date,
    sleep_date,
    workout_date,
)
from whoop_mcp.timeutil import parse_iso, record_local_date
from whoop_mcp.transform import (
    prune,
    recovery_zone,
    rounded,
    transform_cycle,
    transform_profile,
    transform_recovery,
    transform_sleep,
    transform_workout,
)

CHUNK_DAYS = 180
CHUNK_MAX_RECORDS = 1000

DAILY_COLUMNS = (
    "date",
    "recovery",
    "zone",
    "hrv_ms",
    "rhr",
    "sleep_hours",
    "sleep_performance",
    "sleep_consistency",
    "strain",
    "calories",
    "workouts",
)

WORKOUT_COLUMNS = (
    "date",
    "sport",
    "start",
    "duration_minutes",
    "strain",
    "calories",
    "average_heart_rate",
    "max_heart_rate",
    "distance_km",
    "elevation_gain_m",
)


async def _fetch_everything(
    client: WhoopClient, start: datetime, end: datetime
) -> tuple[Bundle, bool, bool]:
    """Fetch all four collections across the range in chunked windows.

    Returns (bundle, truncated, rate_limited). On a hard rate-limit the
    already-fetched chunks are returned rather than thrown away.
    """
    bundle = Bundle()
    seen: dict[str, set] = {
        "cycles": set(),
        "recoveries": set(),
        "sleeps": set(),
        "workouts": set(),
    }
    any_truncated = False
    rate_limited = False

    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), end)
        try:
            results = await asyncio.gather(
                client.cycles(cursor, chunk_end, CHUNK_MAX_RECORDS),
                client.recoveries(cursor, chunk_end, CHUNK_MAX_RECORDS),
                client.sleeps(cursor, chunk_end, CHUNK_MAX_RECORDS),
                client.workouts(cursor, chunk_end, CHUNK_MAX_RECORDS),
            )
        except RateLimitError:
            rate_limited = True
            any_truncated = True
            break
        for (records, truncated), kind, target in zip(
            results,
            ("cycles", "recoveries", "sleeps", "workouts"),
            (bundle.cycles, bundle.recoveries, bundle.sleeps, bundle.workouts),
            strict=True,
        ):
            any_truncated = any_truncated or truncated
            for record in records:
                key = (
                    (record.get("cycle_id"), record.get("sleep_id"))
                    if kind == "recoveries"
                    else record.get("id")
                )
                if key in (None, (None, None)):
                    target.append(record)  # malformed record; keep, never dedupe
                    continue
                if key in seen[kind]:
                    continue
                seen[kind].add(key)
                target.append(record)
        cursor = chunk_end
        if cursor < end:
            await asyncio.sleep(0.2)  # be gentle with the per-minute quota
    return bundle, any_truncated, rate_limited


def _sort_key(record: dict[str, Any], field: str = "start") -> str:
    return record.get(field) or record.get("created_at") or ""


async def run_export(
    client: WhoopClient,
    *,
    start: datetime,
    end: datetime,
    data_dir: Path,
    include_raw: bool = True,
) -> dict[str, Any]:
    began = time.monotonic()
    # Pad the fetch by a day each side: WHOOP filters on record *start*, but a
    # night's sleep belongs to the wake-up day - without padding, the first
    # morning's sleep (started the previous evening) would be missing.
    bundle, truncated, rate_limited = await _fetch_everything(
        client, start - timedelta(days=1), end + timedelta(days=1)
    )
    profile, body = await asyncio.gather(client.profile(), client.body_measurement())

    # Keep records that *belong* to a day inside the requested range, by each
    # record's own local-day attribution.
    start_day, end_day = start.date(), end.date()
    bundle.cycles = [
        c for c in bundle.cycles if (d := cycle_date(c)) and start_day <= d <= end_day
    ]
    bundle.sleeps = [
        s for s in bundle.sleeps if (d := sleep_date(s)) and start_day <= d <= end_day
    ]
    bundle.workouts = [
        w for w in bundle.workouts if (d := workout_date(w)) and start_day <= d <= end_day
    ]
    kept_cycle_ids = {c.get("id") for c in bundle.cycles}
    bundle.recoveries = [
        r
        for r in bundle.recoveries
        if r.get("cycle_id") in kept_cycle_ids
        or (
            r.get("cycle_id") not in kept_cycle_ids
            and r.get("created_at")
            and start_day <= parse_iso(r["created_at"]).date() <= end_day
        )
    ]

    bundle.cycles.sort(key=_sort_key)
    bundle.sleeps.sort(key=_sort_key)
    bundle.workouts.sort(key=_sort_key)
    bundle.recoveries.sort(key=lambda r: _sort_key(r, "created_at"))

    cycle_days = {
        c.get("id"): str(d)
        for c in bundle.cycles
        if c.get("start") and (d := record_local_date(c["start"], c.get("timezone_offset")))
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = data_dir / "exports" / f"whoop-export-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "meta": {
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "source": "whoop-mcp",
            "complete": not truncated,
        },
        "profile": transform_profile(profile or {}, body or {}),
        "data": {
            "cycles": [transform_cycle(c) for c in bundle.cycles],
            "recoveries": [
                transform_recovery(r, date=cycle_days.get(r.get("cycle_id")))
                for r in bundle.recoveries
            ],
            "sleeps": [transform_sleep(s) for s in bundle.sleeps],
            "workouts": [transform_workout(w) for w in bundle.workouts],
        },
    }
    if include_raw:
        payload["raw"] = {
            "cycles": bundle.cycles,
            "recoveries": bundle.recoveries,
            "sleeps": bundle.sleeps,
            "workouts": bundle.workouts,
        }

    json_path = out_dir / "data.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    daily_path = out_dir / "daily_summary.csv"
    table = _daily_metric_table(bundle, start.date(), end.date())
    with daily_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DAILY_COLUMNS)
        writer.writeheader()
        for day, row in table.items():
            writer.writerow(
                {
                    "date": str(day),
                    "recovery": rounded(row.get("recovery"), 0),
                    "zone": recovery_zone(row.get("recovery")),
                    "hrv_ms": rounded(row.get("hrv")),
                    "rhr": rounded(row.get("rhr"), 0),
                    "sleep_hours": row.get("sleep_hours"),
                    "sleep_performance": rounded(row.get("sleep_performance"), 0),
                    "sleep_consistency": rounded(row.get("sleep_consistency"), 0),
                    "strain": rounded(row.get("strain")),
                    "calories": row.get("calories"),
                    "workouts": row.get("workouts", 0),
                }
            )

    workouts_path = out_dir / "workouts.csv"
    with workouts_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=WORKOUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for workout in payload["data"]["workouts"]:
            writer.writerow({key: workout.get(key) for key in WORKOUT_COLUMNS})

    counts = {
        "cycles": len(bundle.cycles),
        "recoveries": len(bundle.recoveries),
        "sleeps": len(bundle.sleeps),
        "workouts": len(bundle.workouts),
        "days_in_daily_csv": len(table),
    }
    result = {
        "directory": str(out_dir),
        "files": {
            "json": str(json_path),
            "daily_csv": str(daily_path),
            "workouts_csv": str(workouts_path),
        },
        "counts": counts,
        "bytes_written": sum(p.stat().st_size for p in (json_path, daily_path, workouts_path)),
        "took_seconds": round(time.monotonic() - began, 1),
    }
    if rate_limited:
        result["note"] = (
            "WHOOP's rate limit interrupted the export partway; the files contain "
            "everything fetched up to that point. Wait a minute and export the "
            "remaining range separately."
        )
    elif truncated:
        result["note"] = (
            "At least one window hit the per-window record cap; the export may be "
            "missing records in that window. Narrow the range and re-export if counts "
            "look low."
        )
    return prune(result)
