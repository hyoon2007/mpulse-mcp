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


# --- get_aggregate ---------------------------------------------------------
def _echo_latest(request: httpx.Request) -> httpx.Response:
    """Echo the requested metric/timer as the series id, latest == percentile."""
    p = request.url.params
    name = p.get("metric") or p.get("timer")
    pct = int(p.get("percentile", "50"))
    return httpx.Response(
        200, json={"values": [{"id": name, "history": [1], "latest": pct}]}
    )


@respx.mock
async def test_get_aggregate_matrix(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(side_effect=_echo_latest)
    try:
        result = await server.get_aggregate(
            metrics=["Beacons"],
            timers=["PageLoad"],
            periods=[
                {"date_comparator": "ThisMonth", "label": "thismonth"},
                {"date": "2026-06-15"},
            ],
            percentiles=[50, 75],
        )
    finally:
        await wired_client.aclose()

    # 2 targets × 2 periods × 2 percentiles = 8 cells, all successful.
    assert len(result["table"]) == 8
    assert "errors" not in result
    assert set(result["targets"]) == {"Beacons", "PageLoad"}
    # latest echoes the percentile, so each cell's value == its percentile.
    for row in result["table"]:
        assert row["value"] == row["percentile"]


@respx.mock
async def test_get_aggregate_max_combos_guard(wired_client) -> None:
    query = respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(
        side_effect=_echo_latest
    )
    try:
        result = await server.get_aggregate(
            metrics=["Beacons", "AppErrors"],
            periods=[{"date": "2026-06-15"}, {"date": "2026-07-15"}],
            percentiles=[50, 75],
            max_combos=3,  # 2×2×2 = 8 > 3
        )
    finally:
        await wired_client.aclose()
    assert result["error"] == "ValidationError"
    assert query.call_count == 0  # rejected before any upstream call


@respx.mock
async def test_get_aggregate_rejects_unknown_metric(wired_client) -> None:
    query = respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(
        side_effect=_echo_latest
    )
    try:
        result = await server.get_aggregate(
            metrics=["totally_bogus_metric_xyz"],
            periods=[{"date_comparator": "LastHour"}],
        )
    finally:
        await wired_client.aclose()
    assert result["error"] == "ValidationError"
    assert query.call_count == 0


@respx.mock
async def test_get_aggregate_partial_failure(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))

    def _one_fails(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("metric") == "AppErrors":
            return httpx.Response(403, text="Forbidden")
        return _echo_latest(request)

    respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(side_effect=_one_fails)
    try:
        result = await server.get_aggregate(
            metrics=["Beacons", "AppErrors"],
            periods=[{"date_comparator": "LastHour"}],
            percentiles=[50],
        )
    finally:
        await wired_client.aclose()

    # Beacons cell succeeds; AppErrors cell fails independently.
    assert len(result["table"]) == 1
    assert result["table"][0]["target"] == "Beacons"
    assert len(result["errors"]) == 1
    assert result["errors"][0]["target"] == "AppErrors"


@respx.mock
async def test_get_aggregate_autocorrects_metric(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(
        side_effect=_echo_latest
    )
    try:
        result = await server.get_aggregate(
            metrics=["beacons"],  # wrong casing
            periods=[{"date_comparator": "LastHour"}],
        )
    finally:
        await wired_client.aclose()
    assert "corrections" in result
    assert route.calls.last.request.url.params["metric"] == "Beacons"
