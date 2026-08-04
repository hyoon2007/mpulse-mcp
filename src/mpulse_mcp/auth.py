"""Security-token lifecycle.

Flow (API-token / SSO style — no username/password):

    PUT https://mpulse.soasta.com/concerto/services/rest/RepositoryService/v1/Tokens
    body: {"apiToken": "<pre-issued>", "tenant": "<tenant>"}
    -> 201 {"token": "<security token>"}

The security token "expires after five hours of inactivity" (per mPulse docs).
We treat it conservatively: cache it with a soft TTL, refresh when the token is
near expiry, and also refresh reactively on a query 401.

Tokens are cached per *credential* (tenant + api_token value), so several apps
that share a credential reuse one token. Refresh is single-flighted with an
``asyncio.Lock`` per credential so concurrent callers mint at most one token.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from . import log
from .errors import AuthError, mask

TOKENS_PATH = "/concerto/services/rest/RepositoryService/v1/Tokens"

# The doc states 5 hours of *inactivity*. We refresh well before that to stay
# safely inside the window and to bound the blast radius of clock skew.
DEFAULT_TTL_SECONDS = 4 * 60 * 60  # 4h soft lifetime
REFRESH_SKEW_SECONDS = 60  # refresh when <= 60s remain


@dataclass
class _CachedToken:
    value: str
    expires_at: float  # monotonic clock

    def is_fresh(self, *, now: float, skew: float = REFRESH_SKEW_SECONDS) -> bool:
        return (self.expires_at - now) > skew


class TokenManager:
    """Mints, caches, and refreshes mPulse security tokens.

    One instance is shared process-wide. It holds no app state itself; callers
    pass the credential (tenant + api_token) each time.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._cache: dict[tuple[str | None, str], _CachedToken] = {}
        self._locks: dict[tuple[str | None, str], asyncio.Lock] = {}

    def _lock_for(self, key: tuple[str | None, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def get_token(
        self,
        *,
        tenant: str | None,
        api_token: str,
        force_refresh: bool = False,
    ) -> str:
        """Return a valid security token, minting/refreshing as needed.

        ``force_refresh=True`` is used after a query 401 to discard a token the
        server has already rejected.
        """
        key = (tenant, api_token)
        now = time.monotonic()

        # Snapshot the token we're (implicitly) rejecting. On a reactive refresh
        # this is the one the server just 401'd; single-flight means only the
        # first waiter re-mints, and later waiters detect the replacement by
        # object identity.
        seen = self._cache.get(key)

        if not force_refresh:
            if seen and seen.is_fresh(now=now):
                return seen.value

        lock = self._lock_for(key)
        async with lock:
            # Re-check inside the lock: another coroutine may have refreshed
            # while we waited (single-flight).
            now = time.monotonic()
            cached = self._cache.get(key)
            if not force_refresh and cached and cached.is_fresh(now=now):
                return cached.value
            if force_refresh and cached is not None and cached is not seen:
                # Someone already replaced the rejected token while we waited;
                # reuse their fresh token instead of minting again.
                return cached.value

            token = await self._mint(tenant=tenant, api_token=api_token)
            self._cache[key] = _CachedToken(
                value=token, expires_at=time.monotonic() + self._ttl
            )
            return token

    def invalidate(self, *, tenant: str | None, api_token: str) -> None:
        self._cache.pop((tenant, api_token), None)

    async def _mint(self, *, tenant: str | None, api_token: str) -> str:
        body: dict[str, str] = {"apiToken": api_token}
        if tenant:
            body["tenant"] = tenant

        log.info(
            "Requesting security token (tenant=%s, apiToken=%s)",
            tenant or "<none>",
            mask(api_token),
        )
        try:
            resp = await self._client.put(
                TOKENS_PATH,
                json=body,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise AuthError(
                f"Network error while requesting security token: {exc}",
                hint="Check connectivity to mpulse.soasta.com.",
            ) from exc

        if resp.status_code in (200, 201):
            try:
                data = resp.json()
            except ValueError as exc:
                raise AuthError(
                    "Token endpoint returned a non-JSON response."
                ) from exc
            token = data.get("token") if isinstance(data, dict) else None
            if not token:
                raise AuthError("Token endpoint response did not contain 'token'.")
            log.info("Security token issued: %s", mask(token))
            return token

        if resp.status_code in (401, 403):
            raise AuthError(
                f"Token request rejected ({resp.status_code}).",
                hint=(
                    "The pre-issued API token or tenant is likely invalid, or the "
                    "account lacks API access (mPulse Lite is not supported)."
                ),
            )
        raise AuthError(
            f"Token request failed with HTTP {resp.status_code}.",
            hint="Unexpected response from the Tokens endpoint.",
        )
