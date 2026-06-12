"""WHOOP OAuth 2.0 authorization-code flow.

The interactive flow (``whoop-mcp auth``) spins up a tiny localhost HTTP
server matching the registered redirect URI, opens the system browser to
WHOOP's consent page, captures the authorization code, validates the CSRF
``state``, and exchanges the code for tokens. Token refresh lives here too
and is what :class:`whoop_mcp.tokens.TokenManager` calls back into.
"""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import threading
import urllib.parse
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from whoop_mcp.config import AUTH_URL, TOKEN_URL, Settings
from whoop_mcp.errors import ApiError, AuthRequiredError, ConfigError, WhoopError
from whoop_mcp.tokens import TokenSet

logger = logging.getLogger(__name__)

AUTH_TIMEOUT_SECONDS = 300

_SUCCESS_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>WHOOP connected</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto;">
<h1>&#9989; WHOOP connected</h1>
<p>Authorization complete. You can close this tab and return to your terminal.</p>
</body></html>"""

_ERROR_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>WHOOP authorization failed</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto;">
<h1>&#10060; Authorization failed</h1>
<p>{detail}</p>
<p>Close this tab and re-run <code>whoop-mcp auth</code>.</p>
</body></html>"""


def build_authorize_url(settings: Settings, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.client_id or "",
        "redirect_uri": settings.redirect_uri,
        "scope": " ".join(settings.scopes),
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


async def _post_token_request(data: dict[str, str], *, timeout: float = 30.0) -> TokenSet:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code in (400, 401):
        try:
            error = response.json().get("error", "")
        except ValueError:
            error = ""
        if error == "invalid_grant" or response.status_code == 401:
            raise AuthRequiredError(f"WHOOP rejected the grant ({error or response.status_code})")
        raise ApiError(response.status_code, f"token request failed: {response.text[:300]}")
    if response.is_error:
        raise ApiError(response.status_code, f"token request failed: {response.text[:300]}")
    payload = response.json()
    if "access_token" not in payload:
        raise ApiError(response.status_code, "token response missing access_token")
    return TokenSet.from_token_response(payload)


async def exchange_code(settings: Settings, code: str) -> TokenSet:
    """Exchange an authorization code for an access + refresh token pair."""
    settings.require_oauth_app()
    return await _post_token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.client_id or "",
            "client_secret": settings.client_secret or "",
            "redirect_uri": settings.redirect_uri,
        },
        timeout=settings.request_timeout,
    )


async def refresh_token(settings: Settings, refresh_token_value: str) -> TokenSet:
    """Trade a refresh token for a new token pair (WHOOP rotates both)."""
    settings.require_oauth_app()
    return await _post_token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token_value,
            "client_id": settings.client_id or "",
            "client_secret": settings.client_secret or "",
            "scope": "offline",
        },
        timeout=settings.request_timeout,
    )


class _CallbackResult:
    def __init__(self) -> None:
        self.code: str | None = None
        self.state: str | None = None
        self.error: str | None = None
        self.event = threading.Event()


def _make_handler(result: _CallbackResult, expected_path: str):
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(parsed.query)
            error = query.get("error", [None])[0]
            if error:
                description = query.get("error_description", [""])[0]
                result.error = f"{error}: {description}" if description else error
                self._respond(400, _ERROR_PAGE.format(detail=html.escape(result.error)))
            else:
                result.code = query.get("code", [None])[0]
                result.state = query.get("state", [None])[0]
                if result.code:
                    self._respond(200, _SUCCESS_PAGE)
                else:
                    result.error = "redirect did not include an authorization code"
                    self._respond(400, _ERROR_PAGE.format(detail=html.escape(result.error)))
            result.event.set()

        def _respond(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            # The request line carries the one-time authorization code in its
            # query string - log only the path, never the args.
            logger.debug("callback server: handled %s", self.path.partition("?")[0])

    return CallbackHandler


def _start_callback_server(settings: Settings) -> tuple[HTTPServer, _CallbackResult, str, str]:
    """Validate the redirect URI, bind the callback server, return (server,
    result, state, authorize_url). The server is already serving on a thread."""
    settings.require_oauth_app()

    redirect = urllib.parse.urlparse(settings.redirect_uri)
    if redirect.scheme != "http" or redirect.hostname not in ("localhost", "127.0.0.1"):
        raise ConfigError(
            "Interactive auth needs a localhost redirect URI such as "
            f"http://localhost:8765/callback (got {settings.redirect_uri!r}). "
            "Register it in your WHOOP app settings and set WHOOP_REDIRECT_URI to match."
        )
    port = redirect.port or 80
    path = redirect.path or "/"

    # WHOOP requires state to be at least 8 characters; this is far longer.
    state = secrets.token_urlsafe(24)
    result = _CallbackResult()

    try:
        server = HTTPServer(("127.0.0.1", port), _make_handler(result, path))
    except OSError as exc:
        raise ConfigError(
            f"Could not listen on 127.0.0.1:{port} for the OAuth callback ({exc}). "
            "Close whatever is using that port, or register a different localhost "
            "redirect URI in the WHOOP dashboard and set WHOOP_REDIRECT_URI."
        ) from exc

    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, result, state, build_authorize_url(settings, state)


async def authorize_interactive_async(
    settings: Settings,
    *,
    open_browser: bool = True,
    timeout: float = AUTH_TIMEOUT_SECONDS,
    on_url: Callable[[str], None] | None = None,
) -> TokenSet:
    """Browser authorization flow, safe inside a running event loop (and a
    running MCP server - nothing here writes to stdout)."""
    server, result, state, url = _start_callback_server(settings)
    try:
        if on_url is not None:
            on_url(url)
        if open_browser:
            opened = await asyncio.to_thread(webbrowser.open, url)
            if not opened:
                logger.warning("Could not launch a browser; the URL must be opened manually")
        # Wait in short slices so a cancelled tool call strands a worker
        # thread for at most ~1s instead of the full timeout.
        deadline = asyncio.get_running_loop().time() + timeout
        arrived = False
        while asyncio.get_running_loop().time() < deadline:
            arrived = await asyncio.to_thread(result.event.wait, 1.0)
            if arrived:
                break
        if not arrived:
            raise WhoopError(
                f"Timed out after {int(timeout)}s waiting for the WHOOP redirect. "
                "Start the authorization again and complete the consent screen."
            )
    finally:
        # Synchronous on purpose: cleanup must run even mid-cancellation, when
        # another `await` would immediately re-raise and skip the close. The
        # block is bounded by serve_forever's 0.5s poll interval.
        server.shutdown()
        server.server_close()

    if result.error:
        raise WhoopError(f"WHOOP authorization failed: {result.error}")
    if not result.code:
        raise WhoopError("WHOOP redirect did not include an authorization code.")
    if result.state != state:
        raise WhoopError(
            "OAuth state mismatch - the redirect did not come from the request we "
            "started. Start the authorization again."
        )

    return await exchange_code(settings, result.code)


def run_interactive_auth(
    settings: Settings,
    *,
    open_browser: bool = True,
    timeout: float = AUTH_TIMEOUT_SECONDS,
) -> TokenSet:
    """Synchronous wrapper for the CLI: prints the URL and runs the flow."""

    def announce(url: str) -> None:
        print(f"Opening WHOOP authorization page:\n  {url}\n")
        if not open_browser:
            print("Open the URL above in a browser to continue.")

    return asyncio.run(
        authorize_interactive_async(
            settings, open_browser=open_browser, timeout=timeout, on_url=announce
        )
    )
