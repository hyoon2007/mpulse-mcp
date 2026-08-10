"""Lossless normalization, raw passthrough, empty detection, input validation."""

from __future__ import annotations

import pytest

from mpulse_mcp.errors import ValidationError
from mpulse_mcp.formatting import _detect_silent_fallback, normalize
from mpulse_mcp.server import _check_single_day, _validate_and_build_time


# --- silent-fallback detection ---------------------------------------------
def test_fallback_warns_when_returned_id_differs() -> None:
    # Requested a (typo) timer; mPulse silently answered with PageLoad.
    data = {"series": {"series": [{"name": "PageLoad", "aPoints": [{"x": 0, "y": 1}]}]}}
    out = normalize(
        app="alpha",
        query_type="by-minute",
        request_params={"timer": "LargestContentofulPaint", "date": "2026-08-03"},
        aggregation="per-minute",
        data=data,
        raw=False,
    )
    assert "warning" in out
    assert "SILENT FALLBACK" in out["warning"]


def test_no_fallback_warning_on_casing_or_separator_difference() -> None:
    # snake_case request vs CamelCase returned id = SAME metric, must not warn.
    data = {"values": [{"id": "LargestContentfulPaint", "history": [1], "latest": 1}]}
    assert (
        _detect_silent_fallback(
            {"metric": "largest_contentful_paint"}, data
        )
        is None
    )


def test_no_fallback_warning_when_id_matches() -> None:
    data = {"values": [{"id": "PageLoad", "history": [1], "latest": 1}]}
    assert _detect_silent_fallback({"timer": "PageLoad"}, data) is None


def test_no_fallback_warning_for_summary_shape() -> None:
    # Flat summary carries no series id -> cannot (and must not) warn.
    data = {"median": "1", "n": "5"}
    assert _detect_silent_fallback({"timer": "PageLoad"}, data) is None


def test_fallback_warning_present_in_raw_mode() -> None:
    data = {"values": [{"id": "PageLoad", "history": [1], "latest": 1}]}
    out = normalize(
        app="alpha",
        query_type="timers-metrics",
        request_params={"metric": "not_a_real_metric"},
        aggregation="per-minute",
        data=data,
        raw=True,
    )
    assert out["raw"] is True
    assert out["data"] == data  # data still untouched
    assert "warning" in out


# --- normalization ---------------------------------------------------------
def test_summary_values_preserved_exactly() -> None:
    data = {"median": "1234", "moe": "12.5", "n": "98765", "p95": "4096", "p98": "5120"}
    out = normalize(
        app="alpha",
        query_type="summary",
        request_params={"date": "2026-08-03", "timezone": "UTC"},
        aggregation="single-value",
        data=data,
        raw=False,
    )
    assert out["app"] == "alpha"
    assert out["query_type"] == "summary"
    assert out["period"] == {"date": "2026-08-03", "timezone": "UTC"}
    assert out["empty"] is False
    # Values are byte-for-byte the originals — no rounding/summarizing.
    assert out["body"] == data


def test_timers_metrics_history_preserved() -> None:
    history = list(range(1440))  # a full day, per-minute
    data = {
        "dataTimeZone": "UTC",
        "values": [{"id": "PageLoad", "history": history, "latest": 1439}],
    }
    out = normalize(
        app="alpha",
        query_type="timers-metrics",
        request_params={"date-comparator": "Last24Hours"},
        aggregation="per-minute",
        data=data,
        raw=False,
    )
    assert out["body"]["series"][0]["history"] == history
    assert out["empty"] is False


def test_series_envelope_stripped_but_points_kept() -> None:
    data = {
        "chartTitle": "PageLoad",
        "datasetName": "ds",
        "series": {"series": [{"name": "PageLoad", "aPoints": [{"x": 0, "y": 100}]}]},
    }
    out = normalize(
        app="alpha",
        query_type="by-minute",
        request_params={"date": "2026-08-03"},
        aggregation="per-minute",
        data=data,
        raw=False,
    )
    assert out["body"]["meta"]["chartTitle"] == "PageLoad"
    assert out["body"]["series"][0]["aPoints"] == [{"x": 0, "y": 100}]


def test_raw_passthrough_is_untouched() -> None:
    data = {"anything": {"nested": [1, 2, 3]}, "median": "1"}
    out = normalize(
        app="alpha",
        query_type="summary",
        request_params={"date": "2026-08-03"},
        aggregation="single-value",
        data=data,
        raw=True,
    )
    assert out == {"app": "alpha", "query_type": "summary", "raw": True, "data": data}


def test_empty_drilldown_gets_note() -> None:
    data = {"values": []}
    out = normalize(
        app="alpha",
        query_type="timers-metrics",
        request_params={"date-comparator": "LastHour", "ab-test": "X", "browser": "Y"},
        aggregation="per-minute",
        data=data,
        raw=False,
    )
    assert out["empty"] is True
    assert "drilldown" in out["note"].lower()
    assert out["drilldowns"] == {"browser": "Y", "ab-test": "X"}


def test_unknown_shape_passthrough() -> None:
    data = {"weird": [1, 2, 3]}
    out = normalize(
        app="alpha",
        query_type="geography",
        request_params={"date": "2026-08-03"},
        aggregation="unspecified",
        data=data,
        raw=False,
    )
    assert out["body"] == data  # never dropped


# --- input validation ------------------------------------------------------
def test_date_and_comparator_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        _validate_and_build_time("2026-08-03", "Last24Hours", None)


def test_one_time_selection_required() -> None:
    with pytest.raises(ValidationError):
        _validate_and_build_time(None, None, None)


def test_valid_date_builds_params() -> None:
    params = _validate_and_build_time("2026-08-03", None, "America/New_York")
    assert params == {"date": "2026-08-03", "timezone": "America/New_York"}


def test_comparator_builds_wire_name() -> None:
    params = _validate_and_build_time(None, "Last24Hours", None)
    assert params == {"date-comparator": "Last24Hours"}


@pytest.mark.parametrize("bad", ["2026-08-01..2026-08-03", "2026/08/03", "not-a-date"])
def test_range_or_bad_date_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        _check_single_day(bad)


def test_single_day_ok() -> None:
    _check_single_day("2026-08-03")  # no raise
