# Releasing

Technical steps for cutting a release. Maintainer only.

## PyPI (one-time setup)

1. Create the `whoop-mcp` project on PyPI. In its settings, add a trusted
   publisher: owner `rajdeepmondaldotcom`, repo `whoop-mcp`, workflow
   `release.yml`, environment `pypi`.
2. Create the `pypi` environment in the GitHub repo settings.
3. Set the repo variable `PYPI_PUBLISH` to `true`:
   `gh variable set PYPI_PUBLISH -b true`

## Cutting a release

1. Bump the version in `pyproject.toml`, `src/whoop_mcp/__init__.py`, and
   `server.json`. Add a `CHANGELOG.md` entry.
2. Commit, then tag and push:

```bash
git tag -a vX.Y.Z -m "whoop-mcp X.Y.Z"
git push origin main vX.Y.Z
```

The release workflow runs tests, builds the package, creates the GitHub
release with artifacts, and publishes to PyPI when `PYPI_PUBLISH` is set.

## MCP Registry

Requires the PyPI package to exist. `server.json` is in the repo and the
README carries the required `mcp-name` marker.

```bash
mcp-publisher login github
mcp-publisher publish
```

## After SDK 2.0

The `mcp` dependency is pinned `>=1.27,<2`. When the SDK's 2.0 line is
stable, migrate `FastMCP` to `MCPServer` and lift the pin in one release.
