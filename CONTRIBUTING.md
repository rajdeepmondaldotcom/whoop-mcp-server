# Contributing

Thanks for helping make whoop-mcp better. The bar here is simple: data
correctness over features, and setup friction is a bug.

## Dev setup

```bash
git clone https://github.com/rajdeepmondaldotcom/whoop-mcp.git && cd whoop-mcp
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest          # 102 tests, all offline (fake WHOOP API)
.venv/bin/ruff check .
```

No WHOOP account is needed for development — the test suite fakes the API at
the HTTP layer, and `whoop-mcp serve --demo` gives you a full live server
with generated data.

## Invariants (please don't break these)

- **Trend math:** series are sorted by date before regression, and direction
  labels respect metric polarity (`analytics.METRIC_POLARITY`).
- **Day bucketing:** records land on calendar days via their own
  `timezone_offset` — sleeps by wake date, cycles/workouts by start date.
- **Token lifecycle:** WHOOP rotates both tokens on refresh. Refreshes stay
  lock-serialized and persisted before use; the rejected-token check in
  `TokenManager.get_access_token` prevents refresh cascades.
- **stdio safety:** nothing in a server code path may write to stdout.
- **Honesty:** if a result is truncated, padded, or approximated, the output
  says so.

## Pull requests

- Add a test that fails without your change.
- `pytest` and `ruff check .` must pass.
- Tool descriptions are UX: if you add or change a tool, write its docstring
  for the model that has to decide when to call it.

## Reporting WHOOP API changes

WHOOP evolves their API (v2 shipped mid-2025). If you see new endpoints or
fields in the [official docs](https://developer.whoop.com/api/) that this
server doesn't cover, an issue with a link is hugely valuable even without
code.
