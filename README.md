<!-- mcp-name: io.github.rajdeepmondaldotcom/whoop-mcp -->

<div align="center">

# whoop-mcp

**Ask your AI anything about your body.**

The complete WHOOP → AI connector: recovery, sleep, strain, workouts, and overnight sensor data in Claude, ChatGPT, Cursor, and every MCP client — with the analysis layer that turns records into answers.

[![CI](https://github.com/rajdeepmondaldotcom/whoop-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/rajdeepmondaldotcom/whoop-mcp/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://github.com/rajdeepmondaldotcom/whoop-mcp)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-8A2BE2)](https://modelcontextprotocol.io)

</div>

```text
You: should I train hard today?

AI → get_daily_summary · get_strain_trends
   ← Recovery 81% (green), HRV trending up, sleep debt cleared.
   ← Acute:chronic load 0.94 — you're conditioned for more.
   → "Yes — your body is primed. Last 3 times you trained hard on a
      green day like this, recovery stayed green. Go."
```

Your data never leaves your machine. Read-only. No telemetry. MIT.

## Try it in 30 seconds — no WHOOP account needed

```bash
uv tool install git+https://github.com/rajdeepmondaldotcom/whoop-mcp
```

(No `uv`? `pipx install git+https://github.com/rajdeepmondaldotcom/whoop-mcp` — or clone and `uv tool install .`)

<!-- TODO once on PyPI, this becomes: uvx whoop-mcp setup -->

Add to Claude Desktop (or any client) in **demo mode** and start asking:

```bash
claude mcp add whoop-demo -- whoop-mcp serve --demo
```

Demo mode serves 150 days of realistic generated data — periodized training, travel across timezones, the occasional terrible night — through the exact same pipeline as real data. Every tool works, the correlations are really there, and you can kick the tires before touching OAuth.

## Connect your real WHOOP (3 minutes)

```bash
whoop-mcp setup
```

One guided command does everything:

1. **WHOOP app** — walks you through creating your free developer app (opens the dashboard, tells you exactly what to click), stores credentials with `0600` perms.
2. **Authorize** — opens WHOOP's consent page; tokens auto-refresh forever after.
3. **Verify** — makes a live API call and shows your actual latest recovery before you leave the terminal.
4. **Connect your AI** — detects **Claude Desktop, Cursor, Windsurf, VS Code, and Claude Code** and configures them *for you* (safely: config preserved, backup written, idempotent).

Already added the server but skipped auth? Just tell your AI *"connect my WHOOP account"* — it opens the consent page from inside the chat.

## What your AI can do with it

**23 tools** covering every read endpoint in WHOOP API v2, plus the analysis most integrations make the model do badly by hand:

| Ask… | Tool behind it |
| --- | --- |
| "Give me the full picture of my health" | `get_health_overview` — status + trends + training load + records + correlations, one call |
| "How am I doing today?" / "How did I sleep?" | `get_daily_summary` — recovery, sleep, strain, workouts for any day |
| "What actually affects my recovery?" | `get_correlations` — lag-aware Pearson: strain → next-morning recovery, sleep duration/consistency → recovery, with plain-English readings |
| "Am I overtraining?" | `get_strain_trends` — acute:chronic load ratio, per-sport breakdown |
| "Is my HRV improving?" | `get_recovery_trends` — stats, direction, confidence, unusual days |
| "Show my overnight heart-rate curve" | `get_sleep_stream` — minute-level HR + skin temp, lowest HR and when |
| "This month vs last month?" | `compare_periods` — every metric, improved/declined/unchanged |
| "My records this year?" | `get_personal_records` — bests, worsts, green streaks (real consecutive days) |
| "Export everything" | `export_data` — complete history to local JSON + CSVs |
| Week review, raw records, profile… | `get_weekly_report`, `get_sleeps/workouts/cycles/recoveries` (+ by-id with `include_raw`), `get_profile` |
| Connection trouble? | `get_connection_status`, `connect_whoop_account` |

Plus ChatGPT's required `search`/`fetch` contract, 4 resources, and 4 ready-made prompts (`morning_readiness`, `weekly_review`, `sleep_coach`, `training_planner`).

Dates are plain English everywhere: `yesterday`, `last 30 days`, `this week`, `2 years ago`, `2026-05`…

**Steal ideas:** [40+ prompts worth asking →](docs/PROMPTS.md)

## Works with everything

| Client | Setup |
| --- | --- |
| **Claude Desktop** | `whoop-mcp setup` configures it automatically |
| **Claude Code** | `claude mcp add whoop -- whoop-mcp serve` (setup offers this too) |
| **Cursor** | auto via setup — or [![Install in Cursor](https://img.shields.io/badge/Cursor-install-black)](cursor://anysphere.cursor-deeplink/mcp/install?name=whoop&config=eyJjb21tYW5kIjoid2hvb3AtbWNwIiwiYXJncyI6WyJzZXJ2ZSJdfQ==) |
| **Windsurf** | auto via setup |
| **VS Code** | auto via setup — or [![Install in VS Code](https://img.shields.io/badge/VS_Code-install-0078d4)](https://insiders.vscode.dev/redirect/mcp/install?name=whoop&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22whoop-mcp%22%2C%22args%22%3A%5B%22serve%22%5D%7D) |
| **ChatGPT** | remote connector — see below |
| **Anything MCP** | `command: whoop-mcp` `args: ["serve"]` · Inspector: `npx @modelcontextprotocol/inspector whoop-mcp serve` |

<details>
<summary><b>ChatGPT setup</b></summary>

ChatGPT connects to remote MCP servers, so expose the HTTP transport and tunnel it:

```bash
whoop-mcp serve --transport http --port 8000    # endpoint: /mcp
ngrok http 8000
```

Then **Settings → Apps & Connectors → Advanced settings → Developer mode → create connector** with `https://<your-tunnel>/mcp`, no auth. The `search`/`fetch` tools mean it also works with plain connectors and Deep Research.

⚠️ A no-auth tunnel means anyone with the URL can read your health data. Keep it private and short-lived; Claude's stdio setup never exposes anything.

</details>

## Why this one

- **It computes, the model narrates.** Trends, correlations, training load, and records are calculated server-side — sorted by date, polarity-aware (rising HRV = improving, rising resting HR = declining), timezone-correct (records bucket onto days using *their own* offset, so travel doesn't scramble your history). Models are bad at arithmetic over 90 JSON records; this never asks them to do any.
- **Exhaustive.** Every v2 read endpoint including the relational cycle joins and the minute-level sleep sensor stream most integrations don't know exist. `include_raw` and `export_data` mean you can always get the untouched bytes. *(WHOOP Peak note: app-only features like stress monitor and healthspan have no public API yet — the moment WHOOP ships endpoints, they land here.)*
- **OAuth that survives reality.** WHOOP rotates both tokens on every refresh; this server persists before use, lock-serializes refreshes, shares one rotation across concurrent requests, and picks up a re-auth without a restart.
- **A polite API citizen.** Honors `X-RateLimit-Reset`, jittered retries, capped pagination, brief caching — and says so when a result is truncated rather than pretending it's complete.
- **Tested like health data deserves.** 102 tests: live in-process MCP sessions over a faked WHOOP API, token-rotation races, timezone edges, and a demo-pipeline check that the analysis layer finds deliberately planted patterns. CI on Python 3.10–3.13.

## CLI

```text
whoop-mcp setup     Guided setup: app → authorize → auto-configure clients
whoop-mcp serve     Run the server (--demo · --transport stdio|http|sse · --host/--port)
whoop-mcp status    Config + token state        whoop-mcp doctor   Diagnose everything
whoop-mcp auth      Scriptable OAuth flow       whoop-mcp logout   Delete tokens (--revoke)
```

## Configuration

Nothing required after `whoop-mcp setup`. Overrides (env > `./.env` > `~/.whoop-mcp/.env` > `~/.whoop-mcp/config.json`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET` | — | WHOOP app credentials |
| `WHOOP_REDIRECT_URI` | `http://localhost:8765/callback` | Must exactly match the dashboard |
| `WHOOP_MCP_DEMO` | — | `1` = demo mode (same as `--demo`) |
| `WHOOP_MCP_DIR` | `~/.whoop-mcp` | Tokens, config, exports |
| `WHOOP_MCP_TZ` | system zone | IANA timezone for "today", week bounds |
| `WHOOP_MCP_CACHE_TTL` / `WHOOP_MCP_TIMEOUT` | `60` / `30` | Seconds |
| `WHOOP_MCP_LOG_LEVEL` | `INFO` | stderr only — stdout belongs to MCP |
| `WHOOP_ACCESS_TOKEN` | — | Static-token escape hatch (no refresh) |

## Privacy & security

Read-only against WHOOP. Tokens local with `0600` perms. The only network peer is `api.prod.whoop.com`. No telemetry. Aggregates (trends, correlations) can reveal more than single records — connect this only to AI clients you trust with health data. Vulnerabilities: see [SECURITY.md](SECURITY.md).

## Contributing & development

```bash
uv venv && uv pip install -e ".[dev]" && pytest && ruff check .
```

The whole test suite runs offline. See [CONTRIBUTING.md](CONTRIBUTING.md) for the invariants that keep the data honest. Architecture: `client.py` (httpx: auth/retries/rate-limits/pagination/cache) → `transform.py` (clean shapes) → `summaries.py`/`analytics.py` (bucketing, trends, correlations) → `server.py` (FastMCP). `oauth.py`+`tokens.py` own auth; `demo.py` the generated account; `export.py` bulk export; `clients.py` client auto-config; `cli.py` the human surface.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "WHOOP authorization required" | `whoop-mcp setup` — or ask your AI to connect your WHOOP account |
| Redirect fails after consent | Dashboard redirect URI must be exactly `http://localhost:8765/callback` |
| `403 — missing scope` | Enable **all** read scopes + `offline` on the app, re-run `whoop-mcp auth` |
| Tools missing in a client | Re-run `whoop-mcp setup`, then restart the client fully |
| No recovery shown today | WHOOP scores it after you wake and sync; the summary says so |
| Sleep stream "not available" | WHOOP doesn't expose it for every account/app — summaries still work |
| Anything else | `whoop-mcp doctor` · `get_connection_status` from chat · [open an issue](https://github.com/rajdeepmondaldotcom/whoop-mcp/issues) |

## License

MIT — see [LICENSE](LICENSE). *Not affiliated with or endorsed by WHOOP. WHOOP is a trademark of WHOOP, Inc.*
