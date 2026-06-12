# Releasing

Releases are automatic. Every push to `main` runs tests, tags a version,
creates a GitHub release with built artifacts, publishes to PyPI, and lists
the new version in the MCP Registry.

## How versioning works

The version lives in `pyproject.toml` (mirrored in
`src/whoop_mcp/__init__.py` and `server.json`).

- If the current version has no tag yet, the workflow tags and releases it
  as-is. That's how 0.0.1 went out.
- If it's already tagged, the workflow bumps the patch number (0.0.1 to
  0.0.2), commits `release: vX.Y.Z`, tags, and releases.
- For a minor or major bump, edit the version in those three files yourself
  and push. The workflow releases exactly what you set.

## PyPI

The package publishes as `whoop-mcp-server` (the name `whoop-mcp` was
already taken on PyPI). The CLI command stays `whoop-mcp`, with a
`whoop-mcp-server` alias so `uvx whoop-mcp-server` works.

The `PYPI_API_TOKEN` repo secret powers the publish step. Once the project
exists on PyPI, you can swap the account-scoped token for one scoped to
`whoop-mcp-server`.

## After SDK 2.0

The `mcp` dependency is pinned `>=1.27,<2`. When the SDK's 2.0 line is
stable, migrate `FastMCP` to `MCPServer` and lift the pin in one release.
