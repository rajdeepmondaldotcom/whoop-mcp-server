# Changelog

## 1.0.0 — 2026-06-12

First stable release.

### Data coverage
- Every read endpoint in WHOOP API v2: cycles, recoveries, sleeps, workouts,
  profile, body measurements, cycle→recovery/sleep joins, and the granular
  per-minute sleep sensor stream (heart rate, skin temperature)
- `include_raw` on detail tools; `export_data` writes the complete history
  (raw + transformed JSON, daily and workout CSVs) to local files

### Analysis layer
- `get_health_overview`: status + trends + training load + records +
  correlations in one call
- Trend tools with date-sorted regression, metric polarity (rising HRV =
  improving, rising RHR = declining), confidence, and anomaly detection
- `get_correlations` (lag-aware Pearson), `get_personal_records`
  (consecutive-day streaks), `compare_periods`, acute:chronic load ratio
- All day bucketing uses each record's own timezone offset

### Experience
- `whoop-mcp setup`: guided WHOOP app creation, browser OAuth, live
  verification, automatic configuration of Claude Desktop, Cursor, Windsurf,
  VS Code, and Claude Code
- **Demo mode** (`--demo`): 150 days of realistic generated data — try every
  tool with no WHOOP account
- In-chat authorization (`connect_whoop_account`) and diagnostics
  (`get_connection_status`, `whoop-mcp doctor`)
- ChatGPT connector compatibility (`search`/`fetch` contract), stdio + HTTP
  transports

### Reliability
- Rotation-safe OAuth: lock-serialized refresh, persisted before use,
  concurrent 401s share one rotation, re-auth rescues a live server
- Rate-limit aware client (X-RateLimit-Reset), jittered retries, capped
  pagination, short-TTL caching, truncation honesty
- 102 tests, including live in-process MCP sessions and demo-pipeline checks
