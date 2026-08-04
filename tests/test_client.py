"""Query client: auth header, 401 replay, 429 backoff, 403 mapping, app selection."""

from __future__ import annotations

import httpx
import pytest
import respx

from mpulse_mcp.auth import TOKENS_PATH
from mpulse_mcp.client import AUTH_HEADER, MpulseClient
from mpulse_mcp.config import Registry
from mpulse_mcp.errors import LiteAccountError, RateLimitError

BASE = "https://mpulse.soasta.com"
TOKENS_URL = BASE + TOKENS_PATH


def _q_url(api_key: str, qt: str) -> str:
    return f"{BASE}/concerto/mpulse/api/v2/{api_key}/{qt}"


def _mint(token: str = "SEC") -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": token}))


@respx.mock
async def test_query_sends_auth_header_and_format(registry: Registry) -> None:
    _mint("SEC")
    route = respx.get(_q_url("KEY-ALPHA", "summary")).mock(
        return_value=httpx.Response(200, json={"n": "10", "median": "1200"})
    )
    client = MpulseClient(registry, http=httpx.AsyncClient(base_url=BASE))
    try:
        data = await client.query(
            app=None, query_type="summary", params={"date": "2026-08-03"}
        )
    finally:
        await client.aclose()

    assert data["median"] == "1200"
    request = route.calls.last.request
    assert request.headers[AUTH_HEADER] == "SEC"
    assert "Authorization" not in request.headers
    assert request.url.params["format"] == "json"
    assert request.url.params["date"] == "2026-08-03"


@respx.mock
async def test_401_triggers_reissue_and_replay(registry: Registry) -> None:
    respx.put(TOKENS_URL).mock(
        side_effect=[
            httpx.Response(201, json={"token": "OLD"}),
            httpx.Response(201, json={"token": "NEW"}),
        ]
    )
    q = respx.get(_q_url("KEY-ALPHA", "summary")).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"n": "5"}),
        ]
    )
    client = MpulseClient(registry, http=httpx.AsyncClient(base_url=BASE))
    try:
        data = await client.query(
            app="alpha", query_type="summary", params={"date": "2026-08-03"}
        )
    finally:
        await client.aclose()

    assert data["n"] == "5"
    assert q.call_count == 2
    assert q.calls[0].request.headers[AUTH_HEADER] == "OLD"
    assert q.calls[1].request.headers[AUTH_HEADER] == "NEW"


@respx.mock
async def test_429_retries_then_succeeds(registry: Registry) -> None:
    _mint()
    q = respx.get(_q_url("KEY-ALPHA", "summary")).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(200, json={"n": "7"}),
        ]
    )
    client = MpulseClient(registry, http=httpx.AsyncClient(base_url=BASE))
    try:
        data = await client.query(
            app="alpha", query_type="summary", params={"date": "2026-08-03"}
        )
    finally:
        await client.aclose()
    assert data["n"] == "7"
    assert q.call_count == 3


@respx.mock
async def test_429_exhausts_budget_raises(registry: Registry) -> None:
    _mint()
    respx.get(_q_url("KEY-ALPHA", "summary")).mock(
        return_value=httpx.Response(429)
    )
    client = MpulseClient(registry, http=httpx.AsyncClient(base_url=BASE))
    try:
        with pytest.raises(RateLimitError):
            await client.query(
                app="alpha", query_type="summary", params={"date": "2026-08-03"}
            )
    finally:
        await client.aclose()


@respx.mock
async def test_403_maps_to_lite_error(registry: Registry) -> None:
    _mint()
    respx.get(_q_url("KEY-ALPHA", "summary")).mock(
        return_value=httpx.Response(403, text="Forbidden")
    )
    client = MpulseClient(registry, http=httpx.AsyncClient(base_url=BASE))
    try:
        with pytest.raises(LiteAccountError):
            await client.query(
                app="alpha", query_type="summary", params={"date": "2026-08-03"}
            )
    finally:
        await client.aclose()


@respx.mock
async def test_non_default_app_uses_its_key_and_credential(registry: Registry) -> None:
    tokens = respx.put(TOKENS_URL).mock(
        return_value=httpx.Response(201, json={"token": "SECB"})
    )
    route = respx.get(_q_url("KEY-BETA", "summary")).mock(
        return_value=httpx.Response(200, json={"n": "1"})
    )
    client = MpulseClient(registry, http=httpx.AsyncClient(base_url=BASE))
    try:
        await client.query(
            app="beta", query_type="summary", params={"date_comparator": "LastHour"}
        )
    finally:
        await client.aclose()

    # Token minted with beta's credential (tenant-b).
    assert tokens.calls.last.request.read().decode().find("tenant-b") != -1
    assert route.calls.last.request.headers[AUTH_HEADER] == "SECB"
