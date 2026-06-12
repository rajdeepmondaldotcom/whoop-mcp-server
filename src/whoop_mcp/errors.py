"""Exception hierarchy for whoop-mcp.

Tool handlers let these propagate; the MCP layer converts them into
``isError`` results whose message is shown to the model/user, so every
message here should say what happened *and* what to do about it.
"""

from __future__ import annotations


class WhoopError(Exception):
    """Base class for all whoop-mcp errors."""


class ConfigError(WhoopError):
    """Missing or invalid configuration (client id/secret, redirect URI, ...)."""


class AuthRequiredError(WhoopError):
    """No usable WHOOP credentials - the user must (re)authorize.

    Raised when there are no stored tokens, or the refresh token has been
    revoked/expired and a browser re-authorization is needed.
    """

    def __init__(self, detail: str = "") -> None:
        message = (
            "WHOOP authorization required. Run `whoop-mcp auth` in a terminal to "
            "connect your WHOOP account, then try again."
        )
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


class ApiError(WhoopError):
    """The WHOOP API returned a non-success response that we will not retry."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"WHOOP API error (HTTP {status_code}): {detail}")


class RateLimitError(ApiError):
    """Rate limited by the WHOOP API even after retries."""

    def __init__(self, detail: str = "rate limit exceeded; try again shortly") -> None:
        super().__init__(429, detail)
