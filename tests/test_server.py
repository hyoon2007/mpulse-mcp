"""Server-level integration for name validation in the shared _run path."""

from __future__ import annotations

import httpx
import pytest
import respx

import mpulse_mcp.server as server
from mpulse_mcp.auth import TOKENS_PATH
from mpulse_mcp.client import MpulseClient
from mpulse_mcp.config import Registry

BASE = "https://mpulse.soasta.com"
TOKENS_URL = BASE + TOKENS_PATH


def _q_url(api_key: str, qt: str) -> str:
    return f"{BASE}/concerto/mpulse/api/v2/{api_key}/{qt}"


@pytest.fixture
def wired_client(registry: Registry, monkeypatch: pytest.MonkeyPatch):
    """Install a respx-mockable client as the server's global singleton."""
    client = MpulseClient(registry, http=httpx.AsyncClient(base_url=BASE))
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_registry", registry)
    return client


@respx.mock
async def test_run_autocorrects_timer_on_the_wire(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "summary")).mock(
        return_value=httpx.Response(200, json={"median": "1", "n": "5"})
    )
    try:
        result = await server._run(
            app=None,
            query_type="summary",
            value_params={"timer": "pageload"},  # wrong casing
            date="2026-08-03",
            date_comparator=None,
            timezone=None,
            drilldowns={},
            aggregation="single-value",
            raw=False,
        )
    finally:
        await wired_client.aclose()

    # The corrected name reached mPulse (no silent PageLoad fallback risk).
    assert route.calls.last.request.url.params["timer"] == "PageLoad"
    assert "corrections" in result
    assert "PageLoad" in result["corrections"][0]


@respx.mock
async def test_run_rejects_unknown_metric_without_calling_api(wired_client) -> None:
    query_route = respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(
        return_value=httpx.Response(200, json={"values": []})
    )
    try:
        result = await server._run(
            app=None,
            query_type="timers-metrics",
            value_params={"metric": "totally_bogus_metric_xyz"},
            date=None,
            date_comparator="LastHour",
            timezone=None,
            drilldowns={},
            aggregation="per-minute",
            raw=False,
        )
    finally:
        await wired_client.aclose()

    assert result["error"] == "ValidationError"
    assert "totally_bogus_metric_xyz" in result["message"]
    assert query_route.call_count == 0  # rejected before any upstream call


@respx.mock
async def test_run_valid_metric_passes_through(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(
        return_value=httpx.Response(
            200, json={"values": [{"id": "Beacons", "history": [1, 2], "latest": 2}]}
        )
    )
    try:
        result = await server._run(
            app=None,
            query_type="timers-metrics",
            value_params={"metric": "Beacons"},
            date=None,
            date_comparator="LastHour",
            timezone=None,
            drilldowns={},
            aggregation="per-minute",
            raw=False,
        )
    finally:
        await wired_client.aclose()

    assert route.calls.last.request.url.params["metric"] == "Beacons"
    assert "corrections" not in result  # exact match -> no correction note
    assert "error" not in result
