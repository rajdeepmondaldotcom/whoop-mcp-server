# whoop-mcp

A Model Context Protocol (MCP) server that connects your **WHOOP** data — recovery, sleep, strain, workouts, and body measurements — to **Claude**, **ChatGPT**, and any other MCP client.

Pure Python. Read-only. Your tokens never leave your machine.

```text
You: how am I doing today?

Claude → get_daily_summary("today")
       ← "Recovery 78% (green) · Sleep 7h 41m (92%) · Strain 6.2"
       + full recovery / sleep-stage / workout detail as structured JSON
```

## Why this one

- **Answers questions, not just dumps JSON.** Daily summaries, Monday–Sunday weekly reports, recovery/sleep/strain trend analysis, period-over-period comparison, and acute:chronic training load — computed server-side so the model gets clean numbers instead of doing arithmetic on raw records.
- **Trend math that is actually right.** Series are sorted by date before regression (WHOOP returns newest-first — regressing arrival order flips every trend), and directions respect metric polarity: rising HRV is *improving*, rising resting heart rate is *declining*, strain is reported neutrally.
- **Timezone-correct days.** Every WHOOP record carries the timezone offset where you actually were; records are bucketed onto calendar days using it. A sleep belongs to the morning you woke up — even when you travel.
- **OAuth done properly.** WHOOP rotates *both* tokens on every refresh; this server persists the new pair before anyone can use it and serializes refreshes behind a lock, so concurrent tool calls can never burn a refresh token twice. Proactive refresh means requests don't pay 401 round-trips.
- **A polite API citizen.** Honors `X-RateLimit-Reset` on 429s, retries 5xx/network errors with jittered backoff, paginates with hard caps, and short-TTL caches repeated queries.
- **Works with ChatGPT out of the box.** Implements the `search` / `fetch` contract ChatGPT connectors require, on top of the regular tool set.
- **Token-efficient output.** Milliseconds become `"7h 37m"`, kilojoules become calories, heart-rate zones become minutes and percentages. Structured content plus readable text, with ids kept so everything is traceable.

## What the model gets

**16 read-only tools**

| Tool | What it answers |
| --- | --- |
| `get_daily_summary` | "How am I doing today?" — recovery + sleep + strain + workouts for one day |
| `get_weekly_report` | Monday–Sunday grid with averages and workout totals |
| `get_recovery_trends` | Recovery %, HRV, RHR over 7–180 days: stats, direction, unusual days |
| `get_sleep_trends` | Hours, performance, efficiency, consistency, debt over time |
| `get_strain_trends` | Daily strain, calories, per-sport totals, acute:chronic load ratio |
| `compare_periods` | "This month vs last month" across every key metric, polarity-aware |
| `get_recoveries` / `get_sleeps` / `get_workouts` / `get_cycles` | Filterable record lists (date expressions, sport filter, nap filter) |
| `get_sleep` / `get_workout` / `get_cycle` | Single records; `get_cycle` joins its recovery + sleep |
| `get_profile` | Name, email, height, weight, max HR |
| `search` / `fetch` | ChatGPT-connector contract over all of the above |

**4 resources** — `whoop://profile`, `whoop://summary/today`, `whoop://recovery/latest`, `whoop://sleep/latest`

**4 prompts** — `morning_readiness`, `weekly_review`, `sleep_coach`, `training_planner`

All date parameters accept human expressions: `today`, `yesterday`, `7 days ago`, `last 30 days`, `this week`, `last week`, `this month`, `last month`, `2026-05`, `2026-05-12`, or full ISO timestamps.

## Setup

### 1. Create a WHOOP developer app (one time, ~2 minutes)

1. Sign in at the [WHOOP Developer Dashboard](https://developer-dashboard.whoop.com) with your regular WHOOP account and create a team, then an app.
2. Enable **all scopes**: `read:recovery`, `read:cycles`, `read:sleep`, `read:workout`, `read:profile`, `read:body_measurement`, and `offline` (required for refresh tokens).
3. Add a redirect URI of exactly: `http://localhost:8765/callback`
4. Copy the **Client ID** and **Client Secret**.

### 2. Install

```bash
# with uv (recommended)
git clone https://github.com/rajdeepmondaldotcom/whoop-mcp.git
cd whoop-mcp
uv tool install .

# or: pipx install . / pip install .
```

This puts a `whoop-mcp` command on your PATH (`which whoop-mcp` to confirm the absolute path — you'll want it for client configs).

### 3. Connect your WHOOP account

```bash
whoop-mcp auth
```

Paste your Client ID/Secret when prompted (or pass `--client-id` / `--client-secret`). A browser opens for WHOOP consent; tokens land in `~/.whoop-mcp/tokens.json` (0600) and the credentials are saved to `~/.whoop-mcp/config.json` so you never need environment variables again. The command finishes by fetching your profile to prove the whole pipeline works.

Run `whoop-mcp doctor` any time to diagnose the setup.

### 4a. Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows), then fully restart Claude:

```json
{
  "mcpServers": {
    "whoop": {
      "command": "/absolute/path/to/whoop-mcp",
      "args": ["serve"]
    }
  }
}
```

(Use the path from `which whoop-mcp`; Claude Desktop doesn't inherit your shell PATH.)

### 4b. Claude Code

```bash
claude mcp add whoop -- whoop-mcp serve
```

### 4c. ChatGPT

ChatGPT connects to *remote* MCP servers, so run the HTTP transport and expose it:

```bash
whoop-mcp serve --transport http --port 8000   # serves at /mcp
ngrok http 8000                                 # or any tunnel / small VPS
```

In ChatGPT: **Settings → Apps & Connectors → Advanced settings → enable Developer mode**, then create a connector pointing at `https://<your-tunnel>/mcp` with no authentication. The server also implements the `search`/`fetch` contract that non-developer-mode connectors and Deep Research require.

> ⚠️ A no-auth tunnel means anyone with the URL can read your health data. Use an ephemeral tunnel URL, keep it private, and shut it down when done. For stdio clients (Claude) nothing is ever exposed.

The same HTTP endpoint works as a claude.ai custom connector.

### 4d. Anything else (MCP Inspector, etc.)

```bash
npx @modelcontextprotocol/inspector whoop-mcp serve
```

## CLI

```text
whoop-mcp auth      Connect your WHOOP account (browser OAuth flow)
whoop-mcp serve     Run the server (--transport stdio|http|sse, --host, --port)
whoop-mcp status    Show config, token expiry, scopes
whoop-mcp doctor    Check python/sdk/credentials/connectivity, with fixes
whoop-mcp logout    Delete local tokens (--revoke also revokes at WHOOP)
```

`whoop-mcp` with no subcommand serves stdio, so it drops straight into MCP client configs.

## Configuration

Everything works with zero configuration after `whoop-mcp auth`. Overrides, in priority order — process env, `./.env`, `~/.whoop-mcp/.env`, `~/.whoop-mcp/config.json`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET` | — | WHOOP app credentials |
| `WHOOP_REDIRECT_URI` | `http://localhost:8765/callback` | Must exactly match the dashboard |
| `WHOOP_MCP_DIR` | `~/.whoop-mcp` | Token/config directory |
| `WHOOP_MCP_TZ` | system zone | IANA timezone for "today", week bounds, etc. |
| `WHOOP_MCP_CACHE_TTL` | `60` | Seconds to cache collection responses |
| `WHOOP_MCP_TIMEOUT` | `30` | Per-request timeout (seconds) |
| `WHOOP_MCP_LOG_LEVEL` | `INFO` | Logging (stderr only — stdout belongs to MCP) |
| `WHOOP_ACCESS_TOKEN` | — | Static token escape hatch (testing; no refresh) |

## Privacy & security

- Read-only: every tool carries `readOnlyHint`; the only write anywhere is optional `logout --revoke` (revokes this app's own access).
- Tokens and credentials are stored locally with `0600` permissions; nothing is logged, no telemetry, no third-party services — traffic goes to `api.prod.whoop.com` and nowhere else.
- Health data is sensitive. The aggregate views (trends, anomalies) can reveal more than single records — connect this only to AI clients you trust with that.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest        # 70 tests: unit + live in-process MCP sessions over a fake WHOOP API
ruff check .
```

Architecture, briefly: `client.py` (httpx wrapper: auth, retries, rate limits, pagination, cache) → `transform.py` (raw records → clean shapes) → `summaries.py`/`analytics.py` (day bucketing, trends, comparisons) → `server.py` (FastMCP tools/resources/prompts). `oauth.py` + `tokens.py` own the auth lifecycle; `cli.py` is the human surface.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "WHOOP authorization required" | `whoop-mcp auth` (token was revoked or never created) |
| Browser opens but redirect fails | Redirect URI in the dashboard must be exactly `http://localhost:8765/callback`; port 8765 must be free |
| `403 forbidden — missing scope` | Enable all read scopes on the app, then `whoop-mcp auth` again |
| Tools missing in Claude Desktop | Use the absolute binary path; fully quit and reopen Claude; check `~/Library/Logs/Claude/mcp-server-whoop.log` |
| Today shows no recovery | WHOOP scores recovery after you wake and sync — the summary says so explicitly |
| Anything else | `whoop-mcp doctor` |

## License

MIT — see [LICENSE](LICENSE).

*Not affiliated with or endorsed by WHOOP. WHOOP is a trademark of WHOOP, Inc.*
