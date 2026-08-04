"""Token issuance, caching, refresh, and single-flight behavior."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from mpulse_mcp.auth import TOKENS_PATH, TokenManager
from mpulse_mcp.errors import AuthError

BASE = "https://mpulse.soasta.com"
TOKENS_URL = BASE + TOKENS_PATH


def _token_response(token: str) -> httpx.Response:
    return httpx.Response(201, json={"token": token})


@respx.mock
async def test_mint_and_cache() -> None:
    route = respx.put(TOKENS_URL).mock(return_value=_token_response("T1"))
    async with httpx.AsyncClient(base_url=BASE) as http:
        mgr = TokenManager(http)
        t1 = await mgr.get_token(tenant="tenant-a", api_token="api-a")
        t2 = await mgr.get_token(tenant="tenant-a", api_token="api-a")
    assert t1 == t2 == "T1"
    assert route.call_count == 1  # cached: minted once


@respx.mock
async def test_single_flight_concurrent() -> None:
    route = respx.put(TOKENS_URL).mock(return_value=_token_response("T1"))
    async with httpx.AsyncClient(base_url=BASE) as http:
        mgr = TokenManager(http)
        results = await asyncio.gather(
            *(mgr.get_token(tenant="t", api_token="a") for _ in range(10))
        )
    assert set(results) == {"T1"}
    assert route.call_count == 1  # single-flight collapses the stampede


@respx.mock
async def test_force_refresh_reissues() -> None:
    route = respx.put(TOKENS_URL).mock(
        side_effect=[_token_response("T1"), _token_response("T2")]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        mgr = TokenManager(http)
        first = await mgr.get_token(tenant="t", api_token="a")
        second = await mgr.get_token(tenant="t", api_token="a", force_refresh=True)
    assert first == "T1"
    assert second == "T2"
    assert route.call_count == 2


@respx.mock
async def test_expiry_triggers_refresh() -> None:
    route = respx.put(TOKENS_URL).mock(
        side_effect=[_token_response("T1"), _token_response("T2")]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        mgr = TokenManager(http, ttl_seconds=0)  # everything is immediately stale
        first = await mgr.get_token(tenant="t", api_token="a")
        second = await mgr.get_token(tenant="t", api_token="a")
    assert first == "T1"
    assert second == "T2"
    assert route.call_count == 2


@respx.mock
async def test_distinct_credentials_do_not_share() -> None:
    respx.put(TOKENS_URL).mock(
        side_effect=[_token_response("Ta"), _token_response("Tb")]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        mgr = TokenManager(http)
        ta = await mgr.get_token(tenant="tenant-a", api_token="api-a")
        tb = await mgr.get_token(tenant="tenant-b", api_token="api-b")
    assert ta == "Ta"
    assert tb == "Tb"


@respx.mock
async def test_invalid_credentials_raise_auth_error() -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(401))
    async with httpx.AsyncClient(base_url=BASE) as http:
        mgr = TokenManager(http)
        with pytest.raises(AuthError):
            await mgr.get_token(tenant="t", api_token="bad")


@respx.mock
async def test_token_never_appears_in_error(caplog: pytest.LogCaptureFixture) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(403))
    async with httpx.AsyncClient(base_url=BASE) as http:
        mgr = TokenManager(http)
        with pytest.raises(AuthError) as exc:
            await mgr.get_token(tenant="t", api_token="super-secret-token")
    assert "super-secret-token" not in str(exc.value)
