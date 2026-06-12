<!-- mcp-name: io.github.rajdeepmondaldotcom/whoop-mcp -->

# whoop-mcp

WHOOP measures your recovery, sleep, and strain all day. Then it shows you charts. This server connects the data to Claude, ChatGPT, and any MCP client, so you can ask real questions and get answers computed from your own records.

[![CI](https://github.com/rajdeepmondaldotcom/whoop-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/rajdeepmondaldotcom/whoop-mcp/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://github.com/rajdeepmondaldotcom/whoop-mcp)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-8A2BE2)](https://modelcontextprotocol.io)

```text
You: should I train hard today?

AI:  Recovery 81% (green). HRV trending up. Sleep debt cleared.
     Your 7-day load is 0.94x your 28-day base.
     You are conditioned for more.
```

Runs on your machine. Read-only. The only network peer is api.prod.whoop.com. No telemetry. MIT.

## Try it in 30 seconds

You don't need a WHOOP account to see it work. Demo mode serves 150 days of realistic generated data through the exact pipeline real data uses.

```bash
uv tool install git+https://github.com/rajdeepmondaldotcom/whoop-mcp
claude mcp add whoop-demo -- whoop-mcp serve --demo
```

Ask "how did I sleep last week?" and watch. The patterns in the demo data are real: hard training days dent the next morning's recovery, and the correlation tool finds it.

## Connect your WHOOP

One command. About 3 minutes, most of it WHOOP's consent screen.

```bash
whoop-mcp setup
```

The wizard walks you through WHOOP's free developer app (it opens the dashboard and tells you exactly what to click), runs the OAuth in your browser, proves the connection with a live API call, then configures Claude Desktop, Cursor, Windsurf, VS Code, and Claude Code for you. Existing configs are backed up before any edit.

Added the server but skipped auth? Tell your AI "connect my WHOOP account". It opens the consent page from chat.

## What you can ask

23 tools cover every read endpoint in WHOOP's v2 API, plus the analysis layer that turns records into answers.

| Ask | Tool behind it |
| --- | --- |
| "Give me the full picture of my health" | `get_health_overview`: status, trends, training load, records, and correlations in one call |
| "How am I doing today?" | `get_daily_summary`: recovery, sleep, strain, workouts for any day |
| "What actually affects my recovery?" | `get_correlations`: strain vs next-morning recovery, sleep vs recovery, with plain readings |
| "Am I overtraining?" | `get_strain_trends`: acute vs chronic load, per-sport breakdown |
| "Is my HRV improving?" | `get_recovery_trends`: direction, confidence, unusual days |
| "Show my overnight heart rate curve" | `get_sleep_stream`: minute-level HR and skin temp, lowest point and when |
| "This month vs last month?" | `compare_periods`: every metric, improved or declined |
| "My records this year?" | `get_personal_records`: bests, worsts, green streaks |
| "Export everything" | `export_data`: full history to local JSON and CSV |
| Week grids, raw records, profile | `get_weekly_report`, `get_sleeps/workouts/cycles/recoveries`, by-id tools with `include_raw`, `get_profile` |
| Connection trouble | `get_connection_status`, `connect_whoop_account` |

Dates are plain English everywhere: yesterday, last 30 days, this week, 2 years ago, 2026-05.

ChatGPT's required `search` and `fetch` tools are implemented too, plus 4 resources and 4 ready-made prompts (`morning_readiness`, `weekly_review`, `sleep_coach`, `training_planner`).

More questions worth asking: [docs/PROMPTS.md](docs/PROMPTS.md).

## Works with

| Client | Setup |
| --- | --- |
| Claude Desktop | `whoop-mcp setup` configures it for you |
| Claude Code | `claude mcp add whoop -- whoop-mcp serve` (setup offers this too) |
| Cursor | auto via setup, or [![Install in Cursor](https://img.shields.io/badge/Cursor-install-black)](cursor://anysphere.cursor-deeplink/mcp/install?name=whoop&config=eyJjb21tYW5kIjoid2hvb3AtbWNwIiwiYXJncyI6WyJzZXJ2ZSJdfQ==) |
| Windsurf | auto via setup |
| VS Code | auto via setup, or [![Install in VS Code](https://img.shields.io/badge/VS_Code-install-0078d4)](https://insiders.vscode.dev/redirect/mcp/install?name=whoop&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22whoop-mcp%22%2C%22args%22%3A%5B%22serve%22%5D%7D) |
| ChatGPT | remote connector, see below |
| Any MCP client | `command: whoop-mcp` with `args: ["serve"]`. Inspector: `npx @modelcontextprotocol/inspector whoop-mcp serve` |

<details>
<summary><b>ChatGPT setup</b></summary>

ChatGPT connects to remote MCP servers, so expose the HTTP transport and tunnel it:

```bash
whoop-mcp serve --transport http --port 8000    # endpoint: /mcp
ngrok http 8000
```

Then in ChatGPT: Settings, Apps & Connectors, Advanced settings, enable Developer mode, create a connector with `https://<your-tunnel>/mcp` and no auth.

Be careful here: a no-auth tunnel means anyone with the URL can read your health data. Keep the URL private and the tunnel short-lived. Claude's stdio setup never exposes anything.

</details>

## Why the answers hold up

Models are bad at arithmetic over 90 days of records. So this server computes first and lets the model explain.

- Trend lines are fit to date-sorted series. WHOOP returns records newest-first. Fit arrival order instead and every trend reads backwards.
- Direction respects what the metric means. Rising HRV is improvement. Rising resting heart rate is not.
- Records land on calendar days using their own timezone offset. A sleep belongs to the morning you woke up, even when you travel.
- WHOOP rotates both OAuth tokens on every refresh. Refreshes here are serialized, saved before use, and shared across concurrent requests. Re-running auth rescues a live server without a restart.
- The client honors WHOOP's rate-limit headers, retries with backoff, and caps pagination. When a result is truncated or approximate, the output says so instead of pretending it's complete.

What it can't do: WHOOP's public API has no endpoints yet for Peak features like the stress monitor and healthspan. When WHOOP ships them, they land here. The overnight sensor stream isn't enabled for every account, and the tool reports that instead of failing.

102 tests, all offline against a faked WHOOP API. CI on Python 3.10 to 3.13.

## CLI

```text
whoop-mcp setup     Guided setup: app, authorize, auto-configure clients
whoop-mcp serve     Run the server (--demo, --transport stdio|http|sse, --host, --port)
whoop-mcp status    Config and token state
whoop-mcp doctor    Diagnose setup and connectivity
whoop-mcp auth      Scriptable OAuth flow
whoop-mcp logout    Delete tokens (--revoke also revokes at WHOOP)
```

## Configuration

Nothing required after `whoop-mcp setup`. Overrides, in priority order: process env, `./.env`, `~/.whoop-mcp/.env`, `~/.whoop-mcp/config.json`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET` | none | WHOOP app credentials |
| `WHOOP_REDIRECT_URI` | `http://localhost:8765/callback` | Must exactly match the dashboard |
| `WHOOP_MCP_DEMO` | off | `1` serves demo data (same as `--demo`) |
| `WHOOP_MCP_DIR` | `~/.whoop-mcp` | Tokens, config, exports |
| `WHOOP_MCP_TZ` | system zone | IANA timezone for "today" and week bounds |
| `WHOOP_MCP_CACHE_TTL` / `WHOOP_MCP_TIMEOUT` | `60` / `30` | Seconds |
| `WHOOP_MCP_LOG_LEVEL` | `INFO` | Logs go to stderr. stdout belongs to MCP |
| `WHOOP_ACCESS_TOKEN` | none | Static token for testing, no refresh |

## Privacy

Read-only against WHOOP. Tokens stored locally with 0600 permissions. The only writes are local files you ask for (`export_data`) and the OAuth flow you trigger. One thing worth knowing: aggregates like trends and correlations can reveal more about you than single records. Connect this only to AI clients you trust with health data. Vulnerabilities: [SECURITY.md](SECURITY.md).

## Contributing

```bash
uv venv && uv pip install -e ".[dev]" && pytest && ruff check .
```

The whole test suite runs offline. [CONTRIBUTING.md](CONTRIBUTING.md) lists the invariants that keep the data honest. The short version of the architecture: `client.py` talks to WHOOP, `transform.py` cleans the records, `summaries.py` and `analytics.py` do the math, `server.py` exposes the tools. `oauth.py` and `tokens.py` own auth. `demo.py` is the generated account.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "WHOOP authorization required" | `whoop-mcp setup`, or ask your AI to connect your WHOOP account |
| Redirect fails after consent | The dashboard redirect URI must be exactly `http://localhost:8765/callback` |
| `403 missing scope` | Enable all read scopes plus `offline` on the app, then re-run `whoop-mcp auth` |
| Tools missing in a client | Re-run `whoop-mcp setup`, then fully restart the client |
| No recovery shown today | WHOOP scores it after you wake and sync. The summary says so |
| Sleep stream "not available" | WHOOP doesn't expose it for every account. Nightly summaries still work |
| Anything else | `whoop-mcp doctor`, or `get_connection_status` from chat, or [open an issue](https://github.com/rajdeepmondaldotcom/whoop-mcp/issues) |

## License

MIT. See [LICENSE](LICENSE). Not affiliated with or endorsed by WHOOP. WHOOP is a trademark of WHOOP, Inc.
