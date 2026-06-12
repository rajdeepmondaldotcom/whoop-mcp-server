# Contributing

Thanks for helping make whoop-mcp-server better. The bar is simple: data correctness over features, and setup friction is a bug.

## Dev setup

```bash
git clone https://github.com/rajdeepmondaldotcom/whoop-mcp-server.git && cd whoop-mcp-server
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest          # all offline, faked WHOOP API
.venv/bin/ruff check .
```

You don't need a WHOOP account to develop. The test suite fakes the API at the HTTP layer, and `whoop-mcp-server serve --demo` gives you a live server with generated data.

## Invariants

These keep the data honest. Please don't break them.

- Trend series are sorted by date before any regression. Direction labels respect metric polarity (`analytics.METRIC_POLARITY`).
- Records land on calendar days via their own `timezone_offset`. Sleeps by wake date, cycles and workouts by start date.
- WHOOP rotates both tokens on refresh. Refreshes stay lock-serialized and persisted before use. The rejected-token check in `TokenManager.get_access_token` prevents refresh cascades.
- No server code path writes to stdout.
- If a result is truncated, padded, or approximated, the output says so.

## Pull requests

- Add a test that fails without your change.
- `pytest` and `ruff check .` must pass.
- Tool docstrings are UX. If you change a tool, write its description for the model that decides when to call it.

## Reporting WHOOP API changes

WHOOP evolves their API. If you see new endpoints or fields in the [official docs](https://developer.whoop.com/api/) that this server doesn't cover, an issue with a link is valuable even without code.
