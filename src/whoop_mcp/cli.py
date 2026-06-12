"""Command-line interface: ``whoop-mcp <command>``.

``serve`` is what MCP clients launch; everything else is setup and
diagnostics a human runs in a terminal. The server never writes to stdout
(stdio transport owns it), so all logging goes to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import platform
import subprocess
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


def _claude_desktop_running() -> bool:
    if platform.system() != "Darwin":
        return False
    result = subprocess.run(["pgrep", "-x", "Claude"], capture_output=True)
    return result.returncode == 0


def _ensure_claude_desktop_closed() -> None:
    """Claude Desktop rewrites its config file when it quits, wiping edits
    made while it was open. Setup must not write the config behind its back."""
    if not _claude_desktop_running():
        return
    print("  Claude Desktop is running. It overwrites config edits when it quits,")
    print("  so it must be closed before setup can configure it safely.")
    answer = input("  Quit Claude Desktop now? [Y/n] ")
    if answer.strip().lower() in ("n", "no"):
        print("  Skipping Claude Desktop config to avoid losing the edit.")
        return
    subprocess.run(["osascript", "-e", 'quit app "Claude"'], capture_output=True, timeout=15)
    for _ in range(20):
        if not _claude_desktop_running():
            print("  ✓ Claude Desktop closed.")
            return
        time.sleep(0.5)
    print("  Could not confirm it closed. Quit it manually, then re-run setup.")


def _verify_stdio_boot(binary: str) -> bool:
    """Launch the exact configured command and confirm it answers an MCP
    initialize request over stdio."""
    request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "whoop-mcp-setup", "version": "0"},
                },
            }
        )
        + "\n"
    )
    try:
        proc = subprocess.run(
            [binary, "serve"], input=request, capture_output=True, text=True, timeout=20
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return '"serverInfo"' in proc.stdout


def cmd_setup(args: argparse.Namespace) -> int:
    """Guided end-to-end setup: app credentials → OAuth → client config → test."""
    import webbrowser

    from whoop_mcp import clients, oauth
    from whoop_mcp.config import DEFAULT_REDIRECT_URI

    if not sys.stdin.isatty():
        print(
            "error: `whoop-mcp setup` is interactive - run it in a terminal.\n"
            "(For scripted setups use `whoop-mcp auth --client-id ... --client-secret ...`.)",
            file=sys.stderr,
        )
        return 1

    print("\n┌─ whoop-mcp setup ─────────────────────────────────────────────┐")
    print("│ Connects your WHOOP account and configures your AI clients.   │")
    print("└────────────────────────────────────────────────────────────────┘\n")

    settings = load_settings()

    # Step 1 - WHOOP app credentials.
    if settings.client_id and settings.client_secret:
        print("✓ Step 1/4 - WHOOP app credentials already configured.\n")
    else:
        print("Step 1/4 - WHOOP developer app (one time, ~2 minutes, free)\n")
        print("  1. Sign in with your normal WHOOP account and create a Team, then an App:")
        print("       https://developer-dashboard.whoop.com")
        print("  2. In the app settings:")
        print("       • Scopes: enable ALL of them (including `offline`)")
        print(f"       • Redirect URI: add exactly  {DEFAULT_REDIRECT_URI}")
        print("  3. Copy the Client ID and Client Secret.\n")
        if input("  Open the dashboard in your browser now? [Y/n] ").strip().lower() not in (
            "n",
            "no",
        ):
            webbrowser.open("https://developer-dashboard.whoop.com")
        print()
        client_id = input("  Paste Client ID: ").strip()
        client_secret = getpass.getpass("  Paste Client Secret (input hidden): ").strip()
        if not client_id or not client_secret:
            print("error: both values are required.", file=sys.stderr)
            return 1
        path = save_credentials(
            settings.data_dir, client_id=client_id, client_secret=client_secret
        )
        print(f"\n✓ Saved to {path} (0600 permissions).\n")
        settings = load_settings()

    # Step 2 - authorize with WHOOP. Prefer a silent refresh over a browser trip.
    store = TokenStore(settings.tokens_path)
    existing = store.load()
    authorized = bool(existing and not existing.expires_within(120))
    if not authorized and existing and existing.refresh_token:
        try:
            refreshed = asyncio.run(oauth.refresh_token(settings, existing.refresh_token))
            if refreshed.refresh_token is None:
                refreshed.refresh_token = existing.refresh_token
            store.save(refreshed)
            authorized = True
            print("✓ Step 2/4 - refreshed your existing WHOOP authorization.\n")
        except WhoopError:
            authorized = False
    elif authorized:
        print("✓ Step 2/4 - WHOOP account already connected.\n")
    if not authorized:
        print("Step 2/4 - Authorize with WHOOP (your browser will open)\n")
        tokens = oauth.run_interactive_auth(settings)
        store.save(tokens)
        print(f"✓ Tokens saved to {settings.tokens_path}.\n")

    # Step 3 - verify end to end.
    print("Step 3/4 - Verifying with a live API call...")

    async def _verify() -> str:
        from whoop_mcp.client import WhoopClient
        from whoop_mcp.transform import recovery_zone

        manager = TokenManager(
            TokenStore(settings.tokens_path),
            lambda token: oauth.refresh_token(settings, token),
            static_access_token=settings.static_access_token,
        )
        client = WhoopClient(manager, timeout=settings.request_timeout)
        try:
            profile = await client.profile()
            recoveries, _ = await client.recoveries(max_records=1)
        finally:
            await client.aclose()
        name = " ".join(
            part for part in (profile.get("first_name"), profile.get("last_name")) if part
        )
        line = f"Connected as {name or profile.get('user_id')}"
        if recoveries and (recoveries[0].get("score") or {}).get("recovery_score") is not None:
            score = recoveries[0]["score"]["recovery_score"]
            line += f" - latest recovery {round(score)}% ({recovery_zone(score)})"
        return line

    print(f"✓ {asyncio.run(_verify())}.\n")

    # Step 4 - configure AI clients.
    print("Step 4/4 - Connect your AI clients\n")
    binary = clients.find_binary()
    configured_any = False

    if not args.skip_clients:
        _ensure_claude_desktop_closed()
        for spec in clients.detected_clients():
            if spec.key == "claude-desktop" and _claude_desktop_running():
                print("  Skipping Claude Desktop (still running).")
                continue
            answer = input(f"  {spec.name} detected. Add whoop-mcp to it now? [Y/n] ")
            if answer.strip().lower() in ("n", "no"):
                continue
            path, action = clients.install_into(spec, binary)
            configured_any = True
            if action == "unchanged":
                print(f"  ✓ {spec.name}: already configured ({path})")
            else:
                print(f"  ✓ {spec.name}: {action} in {path} (backup written)")
                print(f"    → {spec.restart_hint}")

        if configured_any:
            print("\n  Verifying the configured server boots...")
            if _verify_stdio_boot(binary):
                print("  ✓ Verified: the server answers over stdio.")
            else:
                print("  ✗ The configured command failed to start. Run `whoop-mcp doctor`.")

        if clients.claude_code_detected():
            command = clients.claude_code_command(binary)
            answer = input("\n  Claude Code detected. Register whoop-mcp with it now? [Y/n] ")
            if answer.strip().lower() not in ("n", "no"):
                import subprocess

                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                stderr = result.stderr.strip()
                if result.returncode == 0:
                    configured_any = True
                    print("  ✓ Registered with Claude Code (user scope).")
                elif "already exists" in stderr.lower():
                    configured_any = True
                    print("  ✓ Already registered with Claude Code.")
                else:
                    print(f"  ✗ `{' '.join(command)}` failed: {stderr[:200]}")
                    print("    Run it manually if needed.")

    if not configured_any:
        print("  Manual config (any MCP client):")
        print(f'    command: "{binary}"   args: ["serve"]')
        print("  ChatGPT (remote connector):")
        print(f"    {binary} serve --transport http --port 8000   # endpoint: /mcp")
        print("    then tunnel it (e.g. ngrok http 8000) and add the URL in")
        print("    ChatGPT → Settings → Apps & Connectors → Developer mode.")

    print("\n┌────────────────────────────────────────────────────────────────┐")
    print("│ Done. Open (or fully restart) your AI app, then try:           │")
    print('│   "How did I sleep last night?"                                │')
    print('│   "Give me a full overview of my health this quarter."         │')
    print("└────────────────────────────────────────────────────────────────┘")
    return 0


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
        print(f"App credentials saved to {path} (0600) - future runs need no env vars.")

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
    print(f"Connected to WHOOP as {name}. You're all set - add the server to your MCP client.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import os

    if args.demo:
        os.environ["WHOOP_MCP_DEMO"] = "1"
    settings = load_settings()
    _configure_logging(settings.log_level)
    transport = {"http": "streamable-http"}.get(args.transport, args.transport)
    logger.info(
        "Starting whoop-mcp %s (transport=%s%s)",
        __version__,
        transport,
        ", DEMO MODE" if settings.demo_mode or args.demo else "",
    )
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
        print("  tokens:        none - run `whoop-mcp auth`")
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
        print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))

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

    setup = sub.add_parser(
        "setup",
        help="Guided setup: WHOOP app → authorize → auto-configure Claude (start here!)",
    )
    setup.add_argument(
        "--skip-clients",
        action="store_true",
        help="Skip auto-configuring Claude Desktop / Claude Code",
    )
    setup.set_defaults(func=cmd_setup)

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
    serve.add_argument(
        "--demo",
        action="store_true",
        help="Serve realistic generated data - try everything without a WHOOP account",
    )
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
        if sys.stdin.isatty():
            # A human at a terminal almost never wants a silent stdio server.
            parser.print_help()
            print("\nFirst time here? Run:  whoop-mcp setup")
            sys.exit(0)
        # Spawned by an MCP client (stdin is a pipe): serve stdio so a bare
        # `whoop-mcp` works in client configs.
        args = parser.parse_args(["serve"])
    try:
        sys.exit(args.func(args))
    except WhoopError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except EOFError:
        print("\naborted (end of input)", file=sys.stderr)
        sys.exit(130)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
