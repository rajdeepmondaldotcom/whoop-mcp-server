"""Command-line interface: ``whoop-mcp <command>``.

``serve`` is what MCP clients launch; everything else is setup and
diagnostics a human runs in a terminal. The server never writes to stdout
(stdio transport owns it), so all logging goes to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
import time
from datetime import datetime, timezone

from whoop_mcp import __version__
from whoop_mcp.config import Settings, load_settings, save_credentials
from whoop_mcp.errors import WhoopError
from whoop_mcp.tokens import TokenManager, TokenStore

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO; that's noise unless debugging.
    if level != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    from dataclasses import replace

    updates = {}
    if getattr(args, "client_id", None):
        updates["client_id"] = args.client_id
    if getattr(args, "client_secret", None):
        updates["client_secret"] = args.client_secret
    if getattr(args, "redirect_uri", None):
        updates["redirect_uri"] = args.redirect_uri
    return replace(settings, **updates) if updates else settings


# ---------------------------------------------------------------- commands


def cmd_auth(args: argparse.Namespace) -> int:
    from whoop_mcp import oauth

    settings = _apply_overrides(load_settings(), args)

    if not settings.client_id:
        print("WHOOP app Client ID (from https://developer-dashboard.whoop.com):")
        client_id = input("  Client ID: ").strip()
        settings = _apply_overrides(settings, argparse.Namespace(client_id=client_id))
    if not settings.client_secret:
        secret = getpass.getpass("  Client Secret (input hidden): ").strip()
        settings = _apply_overrides(settings, argparse.Namespace(client_secret=secret))

    tokens = oauth.run_interactive_auth(settings, open_browser=not args.no_browser)
    TokenStore(settings.tokens_path).save(tokens)
    print(f"\nTokens saved to {settings.tokens_path}")

    if args.save:
        path = save_credentials(
            settings.data_dir,
            client_id=settings.client_id or "",
            client_secret=settings.client_secret or "",
            redirect_uri=settings.redirect_uri,
        )
        print(f"App credentials saved to {path} (0600) — future runs need no env vars.")

    # Verify end-to-end with a real API call.
    async def _verify() -> str:
        from whoop_mcp.client import WhoopClient

        manager = TokenManager(
            TokenStore(settings.tokens_path),
            lambda token: oauth.refresh_token(settings, token),
        )
        client = WhoopClient(manager, timeout=settings.request_timeout)
        try:
            profile = await client.profile()
        finally:
            await client.aclose()
        name = " ".join(
            part for part in (profile.get("first_name"), profile.get("last_name")) if part
        )
        return name or f"user {profile.get('user_id')}"

    name = asyncio.run(_verify())
    print(f"Connected to WHOOP as {name}. You're all set — add the server to your MCP client.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    settings = load_settings()
    _configure_logging(settings.log_level)
    transport = {"http": "streamable-http"}.get(args.transport, args.transport)
    logger.info("Starting whoop-mcp %s (transport=%s)", __version__, transport)
    from whoop_mcp.server import run

    run(transport=transport, host=args.host, port=args.port)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings()
    print(f"whoop-mcp {__version__}")
    print(f"  data dir:      {settings.data_dir}")
    print(f"  client id:     {'set' if settings.client_id else 'NOT SET'}")
    print(f"  client secret: {'set' if settings.client_secret else 'NOT SET'}")
    print(f"  redirect uri:  {settings.redirect_uri}")
    print(f"  timezone:      {settings.timezone or 'system default'}")

    tokens = TokenStore(settings.tokens_path).load()
    if settings.static_access_token:
        print("  tokens:        using WHOOP_ACCESS_TOKEN from environment")
    elif tokens is None:
        print("  tokens:        none — run `whoop-mcp auth`")
    else:
        remaining = tokens.expires_at - time.time()
        when = datetime.fromtimestamp(tokens.expires_at, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
        state = f"expires in {int(remaining // 60)}m ({when})" if remaining > 0 else "EXPIRED"
        refresh = "yes" if tokens.refresh_token else "NO (offline scope missing!)"
        print(f"  access token:  {state}")
        print(f"  refresh token: {refresh}")
        if tokens.scope:
            print(f"  scopes:        {tokens.scope}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run connectivity checks and report what is broken (if anything)."""
    settings = load_settings()
    problems = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal problems
        mark = "ok " if ok else "FAIL"
        if not ok:
            problems += 1
        print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))

    print("whoop-mcp doctor\n")
    check("python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    try:
        from importlib.metadata import version as pkg_version

        check("mcp sdk installed", True, pkg_version("mcp"))
    except Exception as exc:  # noqa: BLE001 - any import/metadata failure is a FAIL
        check("mcp sdk installed", False, str(exc))

    has_creds = bool(settings.client_id and settings.client_secret)
    check("app credentials configured", has_creds or bool(settings.static_access_token))

    tokens = TokenStore(settings.tokens_path).load()
    has_auth = bool(tokens or settings.static_access_token)
    check(
        "whoop account connected",
        has_auth,
        "" if has_auth else "run `whoop-mcp auth`",
    )

    if has_auth and (has_creds or settings.static_access_token):

        async def _probe() -> tuple[bool, str]:
            from whoop_mcp import oauth
            from whoop_mcp.client import WhoopClient

            manager = TokenManager(
                TokenStore(settings.tokens_path),
                lambda token: oauth.refresh_token(settings, token),
                static_access_token=settings.static_access_token,
            )
            client = WhoopClient(manager, timeout=settings.request_timeout)
            try:
                profile = await client.profile()
                name = " ".join(
                    p for p in (profile.get("first_name"), profile.get("last_name")) if p
                )
                return True, name or str(profile.get("user_id"))
            except WhoopError as exc:
                return False, str(exc)
            finally:
                await client.aclose()

        ok, detail = asyncio.run(_probe())
        check("WHOOP API reachable (profile fetch)", ok, detail)

    print()
    if problems:
        print(f"{problems} problem(s) found.")
        return 1
    print("Everything looks good.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = TokenStore(settings.tokens_path)

    if args.revoke:
        tokens = store.load()
        if tokens:

            async def _revoke() -> None:
                from whoop_mcp import oauth
                from whoop_mcp.client import WhoopClient

                manager = TokenManager(
                    store, lambda token: oauth.refresh_token(settings, token)
                )
                client = WhoopClient(manager, timeout=settings.request_timeout)
                try:
                    await client.revoke_access()
                finally:
                    await client.aclose()

            try:
                asyncio.run(_revoke())
                print("Revoked API access with WHOOP.")
            except WhoopError as exc:
                print(f"Could not revoke with WHOOP ({exc}); deleting local tokens anyway.")

    store.clear()
    print(f"Deleted local tokens at {settings.tokens_path}.")
    return 0


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whoop-mcp",
        description="MCP server for WHOOP recovery, sleep, strain, and workout data.",
    )
    parser.add_argument("--version", action="version", version=f"whoop-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")

    auth = sub.add_parser("auth", help="Connect your WHOOP account (opens a browser)")
    auth.add_argument("--client-id", help="WHOOP app client id")
    auth.add_argument("--client-secret", help="WHOOP app client secret")
    auth.add_argument("--redirect-uri", help="Registered redirect URI (default localhost:8765)")
    auth.add_argument("--no-browser", action="store_true", help="Print the URL instead")
    auth.add_argument(
        "--no-save",
        dest="save",
        action="store_false",
        help="Do not persist app credentials to the data dir",
    )
    auth.set_defaults(func=cmd_auth, save=True)

    serve = sub.add_parser("serve", help="Run the MCP server (stdio by default)")
    serve.add_argument(
        "--transport",
        choices=["stdio", "http", "streamable-http", "sse"],
        default="stdio",
        help="MCP transport (http = streamable-http)",
    )
    serve.add_argument("--host", help="Bind host for HTTP transports (default 127.0.0.1)")
    serve.add_argument("--port", type=int, help="Bind port for HTTP transports (default 8000)")
    serve.set_defaults(func=cmd_serve)

    status = sub.add_parser("status", help="Show configuration and token state")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="Diagnose setup and connectivity")
    doctor.set_defaults(func=cmd_doctor)

    logout = sub.add_parser("logout", help="Delete stored tokens")
    logout.add_argument(
        "--revoke", action="store_true", help="Also revoke access with WHOOP first"
    )
    logout.set_defaults(func=cmd_logout)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # No subcommand: default to serving stdio so bare `whoop-mcp` works in
        # MCP client configs.
        args = parser.parse_args(["serve"])
    try:
        sys.exit(args.func(args))
    except WhoopError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
