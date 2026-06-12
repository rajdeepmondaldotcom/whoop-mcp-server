# Changelog

## 0.0.1 (2026-06-12)

First release.

### Data coverage
- Every read endpoint in WHOOP API v2: cycles, recoveries, sleeps, workouts,
  profile, body measurements, cycle joins, and the per-minute sleep sensor
  stream (heart rate, skin temperature)
- `include_raw` on detail tools. `export_data` writes the complete history
  (raw plus transformed JSON, daily and workout CSVs) to local files

### Analysis
- `get_health_overview`: status, trends, training load, records, and
  correlations in one call
- Trend tools with date-sorted regression, metric polarity, confidence, and
  anomaly detection
- `get_correlations` (lag-aware Pearson), `get_personal_records`
  (consecutive-day streaks), `compare_periods`, acute to chronic load ratio
- All day bucketing uses each record's own timezone offset

### Experience
- `whoop-mcp-server setup`: guided WHOOP app creation, browser OAuth, live
  verification, automatic configuration of Claude Desktop, Cursor, Windsurf,
  VS Code, and Claude Code
- Demo mode (`--demo`): 150 days of realistic generated data, no account
  needed
- In-chat authorization (`connect_whoop_account`) and diagnostics
  (`get_connection_status`, `whoop-mcp-server doctor`)
- ChatGPT connector compatibility (`search` and `fetch`), stdio and HTTP
  transports

### Reliability
- Rotation-safe OAuth: lock-serialized refresh, persisted before use,
  concurrent requests share one rotation, re-auth rescues a live server
- Rate-limit aware client, jittered retries, capped pagination, short-TTL
  caching, truncation disclosed in output
- 102 tests, including live in-process MCP sessions and demo pipeline checks
