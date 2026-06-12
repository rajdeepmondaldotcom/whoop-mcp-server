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

## One-time setup still needed

PyPI publishing waits on a token:

1. Create an account-scoped API token at pypi.org (Account settings, API
   tokens). Account-scoped, because the project doesn't exist yet.
2. `gh secret set PYPI_API_TOKEN` and paste it.

The next push to main publishes to PyPI, and the MCP Registry job runs
right after it. Once the first PyPI release is live, you can swap the
account token for a project-scoped one.

## After the first PyPI release

Update the README install line from the git URL to `uvx whoop-mcp`.

## After SDK 2.0

The `mcp` dependency is pinned `>=1.27,<2`. When the SDK's 2.0 line is
stable, migrate `FastMCP` to `MCPServer` and lift the pin in one release.
