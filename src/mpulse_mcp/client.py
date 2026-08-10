"""mPulse Query API HTTP client with rate limiting, retries, and auth.

Rate limits (mPulse-documented defaults — the exact numbers were not
re-confirmable from the doc pages fetched during implementation, so they are
adopted from the task brief and kept configurable here):

    * concurrent: 3
    * per-minute: 100
    * per-hour:   10_000   (advisory; not separately throttled here)
    * per-day:    50_000   (advisory)

Concurrency is bounded by an ``asyncio.Semaphore(3)``. The per-minute cap is a
sliding-window soft throttle. 429/5xx/network errors are retried with
exponential backoff + jitter. A query 401 triggers exactly one token re-issue
and a single replay.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from urllib.parse import quote

import httpx

from . import log
from .auth import TokenManager
from .config import AppConfig, Registry
from .errors import (
    AuthError,
    UpstreamError,
    map_query_status,
)

QUERY_BASE_URL = "https://mpulse.soasta.com"
QUERY_PATH_TEMPLATE = "/concerto/mpulse/api/v2/{api_key}/{query_type}"

AUTH_HEADER = "Authentication"  # NOT "Authorization"

# --- Rate limit configuration (adjust as needed) ---------------------------
MAX_CONCURRENCY = 3
PER_MINUTE_LIMIT = 100
PER_MINUTE_WINDOW = 60.0

# --- Retry configuration ---------------------------------------------------
MAX_RETRIES = 5  # attempts after the first try, for retryable failures
BACKOFF_BASE = 0.5  # seconds
BACKOFF_CAP = 20.0  # seconds
REQUEST_TIMEOUT = 30.0  # seconds


class _SlidingWindowLimiter:
    """Allows at most ``limit`` acquisitions per rolling ``window`` seconds."""

    def __init__(self, limit: int, window: float) -> None:
        self._limit = limit
        self._window = window
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._window
                while self._events and self._events[0] < cutoff:
                    self._events.popleft()
                if len(self._events) < self._limit:
                    self._events.append(now)
                    return
                # Sleep until the oldest event ages out of the window.
                wait = self._events[0] + self._window - now
            log.info("Per-minute rate cap reached; throttling %.2fs", wait)
            await asyncio.sleep(max(wait, 0.01))


class MpulseClient:
    """High-level client: resolve app → ensure token → call query-type."""

    def __init__(
        self,
        registry: Registry,
        *,
        http: httpx.AsyncClient | None = None,
        token_http: httpx.AsyncClient | None = None,
    ) -> None:
        self._registry = registry
        # Separate base URLs: both APIs live on mpulse.soasta.com, so one client
        # suffices, but we keep the option to inject mocks per surface.
        self._http = http or httpx.AsyncClient(
            base_url=QUERY_BASE_URL, timeout=REQUEST_TIMEOUT
        )
        token_client = token_http or self._http
        self._tokens = TokenManager(token_client)
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        self._minute_limiter = _SlidingWindowLimiter(PER_MINUTE_LIMIT, PER_MINUTE_WINDOW)

    @property
    def registry(self) -> Registry:
        return self._registry

    async def aclose(self) -> None:
        await self._http.aclose()

    async def query(
        self,
        *,
        app: str | None,
        query_type: str,
        params: dict[str, object],
    ) -> dict:
        """Execute a query-type call for an app and return parsed JSON.

        ``params`` values are stringified; ``None`` values are dropped. The
        ``format=json`` param is enforced. Raises a typed ``MpulseError`` on
        failure.
        """
        app_cfg = self._registry.get(app)
        api_token = self._registry.api_token(app_cfg)

        # Flatten to (key, value) pairs so list values become REPEATED keys.
        query_params = [(k, v) for (k, v) in _clean_params(params) if k != "format"]
        query_params.append(("format", "json"))

        path = QUERY_PATH_TEMPLATE.format(
            api_key=quote(app_cfg.api_key, safe=""),
            query_type=quote(query_type, safe=""),
        )

        # First attempt uses a cached token; a 401 forces one re-issue + replay.
        for auth_attempt in range(2):
            token = await self._tokens.get_token(
                tenant=app_cfg.tenant,
                api_token=api_token,
                force_refresh=(auth_attempt == 1),
            )
            resp = await self._send_with_retries(
                path=path, params=query_params, token=token, app_cfg=app_cfg
            )
            if resp.status_code == 401 and auth_attempt == 0:
                log.info("Query returned 401; re-issuing token and replaying once.")
                self._tokens.invalidate(
                    tenant=app_cfg.tenant, api_token=api_token
                )
                continue
            if resp.status_code // 100 != 2:
                raise map_query_status(resp.status_code, resp.text)
            data = _parse_json(resp)
            _log_payload_stats(app_cfg.name, query_type, resp, data)
            return data

        # Both auth attempts returned 401.
        raise AuthError(
            "mPulse kept returning 401 after re-issuing the security token.",
            hint="The API token/tenant may be invalid or lack query permission.",
        )

    async def _send_with_retries(
        self,
        *,
        path: str,
        params: list[tuple[str, str]],
        token: str,
        app_cfg: AppConfig,
    ) -> httpx.Response:
        """Send one request, retrying retryable failures with backoff+jitter.

        Returns the final response (including a non-retryable 4xx or a 401,
        which the caller handles). Raises ``UpstreamError`` only when the retry
        budget is exhausted on network/5xx failures.
        """
        headers = {AUTH_HEADER: token, "Accept": "application/json"}
        last_exc: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            await self._minute_limiter.acquire()
            async with self._semaphore:
                try:
                    resp = await self._http.get(
                        path, params=params, headers=headers
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt >= MAX_RETRIES:
                        break
                    delay = _backoff(attempt)
                    log.info(
                        "Network error on '%s' (attempt %d/%d): %s; retrying in "
                        "%.2fs",
                        app_cfg.name,
                        attempt + 1,
                        MAX_RETRIES + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

            # Decide whether to retry based on status.
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt >= MAX_RETRIES:
                    return resp
                delay = _retry_after(resp) or _backoff(attempt)
                log.info(
                    "Retryable HTTP %d on '%s' (attempt %d/%d); retrying in %.2fs",
                    resp.status_code,
                    app_cfg.name,
                    attempt + 1,
                    MAX_RETRIES + 1,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            return resp

        raise UpstreamError(
            f"Request to mPulse failed after {MAX_RETRIES + 1} attempts: {last_exc}",
            hint="Check network connectivity to mpulse.soasta.com.",
        )


def _count_points(data: dict) -> int:
    """Best-effort count of numeric data points across known response shapes.

    Used only for instrumentation, so an unrecognized shape returns 0 rather
    than raising.
    """
    # timers-metrics: values:[{history:[...]}]
    values = data.get("values")
    if isinstance(values, list):
        return sum(
            len(v.get("history", []))
            for v in values
            if isinstance(v, dict) and isinstance(v.get("history"), list)
        )
    # series-based (histogram / by-minute): series:{series:[{aPoints|buckets}]}
    series = data.get("series")
    if isinstance(series, dict) and isinstance(series.get("series"), list):
        total = 0
        for s in series["series"]:
            if not isinstance(s, dict):
                continue
            for key in ("aPoints", "buckets"):
                pts = s.get(key)
                if isinstance(pts, list):
                    total += len(pts)
        return total
    return 0


def _log_payload_stats(
    app: str, query_type: str, resp: httpx.Response, data: dict
) -> None:
    """Log response size + point count to stderr for token-cost visibility."""
    try:
        n_bytes = len(resp.content)
    except Exception:
        n_bytes = -1
    n_points = _count_points(data)
    log.info(
        "payload app=%s query=%s bytes=%d points=%d",
        app,
        query_type,
        n_bytes,
        n_points,
    )


def _scalarize(value: object) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _clean_params(params: dict[str, object]) -> list[tuple[str, str]]:
    """Flatten params to ``(key, value)`` pairs for httpx.

    ``None`` is dropped. A **list/tuple** value expands to REPEATED keys — this
    is how mPulse takes multiple values for repeat-style params, e.g.
    ``custom-dimension-branch=uk&custom-dimension-branch=us`` and repeated
    ``metric=``/``timer=``/filters. (A comma-separated param like
    metrics-by-dimension's ``metrics`` is passed as a single pre-joined string,
    not a list.) ``bool`` -> ``'true'``/``'false'``.
    """
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                if item is None:
                    continue
                out.append((key, _scalarize(item)))
        else:
            out.append((key, _scalarize(value)))
    return out


def _parse_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError as exc:
        raise UpstreamError(
            "mPulse returned a non-JSON body for a successful query."
        ) from exc
    if not isinstance(data, dict):
        # Some list-shaped responses are wrapped under a key by callers; keep a
        # predictable dict envelope.
        return {"data": data}
    return data


def _backoff(attempt: int) -> float:
    """Exponential backoff with full jitter."""
    ceiling = min(BACKOFF_CAP, BACKOFF_BASE * (2**attempt))
    return random.uniform(0, ceiling)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
