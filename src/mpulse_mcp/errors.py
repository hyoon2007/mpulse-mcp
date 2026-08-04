"""Exception types and HTTP-status → error mapping for the mPulse MCP server.

All messages are written so they never contain secrets (tokens, api tokens).
Use :func:`mask` when a value derived from a credential must appear in text.
"""

from __future__ import annotations


def mask(secret: str | None) -> str:
    """Return a non-reversible, log-safe rendering of a secret.

    Shows only the length and the last two characters so two different secrets
    are distinguishable in logs without disclosing the value.
    """
    if not secret:
        return "<none>"
    if len(secret) <= 4:
        return "*" * len(secret)
    return f"…{secret[-2:]} (len={len(secret)})"


class MpulseError(Exception):
    """Base class for all mPulse MCP errors.

    ``hint`` carries an optional user-facing remediation suggestion.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def user_message(self) -> str:
        return f"{self.message}\nHint: {self.hint}" if self.hint else self.message


class ConfigError(MpulseError):
    """Bad or missing configuration (registry file, env vars, app selection)."""


class AuthError(MpulseError):
    """Token generation failed, or a query kept returning 401 after re-issue."""


class LiteAccountError(MpulseError):
    """HTTP 403 — the Query API is not available on mPulse Lite accounts."""


class RateLimitError(MpulseError):
    """HTTP 429 / throttling that survived the retry budget."""


class ValidationError(MpulseError):
    """Invalid tool input (bad date, mutually exclusive args, unknown query-type)."""


class QueryError(MpulseError):
    """Non-retryable error response from the Query API (4xx other than the above)."""


class UpstreamError(MpulseError):
    """Network failure, timeout, or 5xx that survived the retry budget."""


def map_query_status(status: int, body_text: str) -> MpulseError:
    """Map a non-2xx Query API response to a typed error.

    ``body_text`` is the raw upstream body; it is assumed not to contain the
    caller's credentials (mPulse echoes query params, never the token).
    """
    snippet = (body_text or "").strip()
    if len(snippet) > 500:
        snippet = snippet[:500] + "…"

    if status == 401:
        return AuthError(
            "mPulse rejected the security token (401) even after re-issuing it.",
            hint="Verify the API token and tenant for this app are correct.",
        )
    if status == 403:
        return LiteAccountError(
            "mPulse returned 403 Forbidden for this Query API call.",
            hint=(
                "The Query API is not available on mPulse Lite accounts, and the "
                "app's API token must have query permission. Confirm the account "
                "tier and token scope."
            ),
        )
    if status == 429:
        return RateLimitError(
            "mPulse rate limit hit (429) and retries were exhausted.",
            hint="Reduce request rate or wait before retrying.",
        )
    if 500 <= status < 600:
        return UpstreamError(f"mPulse server error {status}: {snippet}")
    return QueryError(f"mPulse Query API error {status}: {snippet}")
