"""Lossless normalization, raw passthrough, empty detection, input validation."""

from __future__ import annotations

import pytest

from mpulse_mcp.errors import ValidationError
from mpulse_mcp.formatting import (
    _apply_limit,
    _classify_empty,
    _detect_silent_fallback,
    normalize,
)
from mpulse_mcp.server import _check_single_day, _validate_and_build_time


# --- limit / truncation ----------------------------------------------------
def test_apply_limit_truncates_longest_list() -> None:
    body = {"meta": {"x": 1}, "rows": list(range(200))}
    info = _apply_limit(body, 50)
    assert info == {"key": "rows", "total": 200, "returned": 50}
    assert body["rows"] == list(range(50))
    assert body["meta"] == {"x": 1}  # untouched


def test_apply_limit_sorts_by_count_before_truncating() -> None:
    # geography-shaped rows (verified live): {country, timerN, ...}. Low-volume
    # rows come first in input; the top-by-volume must survive the cap.
    rows = [{"country": f"C{i}", "timerN": str(i)} for i in range(150)]  # ascending
    body = {"data": rows}
    info = _apply_limit(body, 10)
    assert info == {"key": "data", "total": 150, "returned": 10, "sorted_by": "timerN"}
    kept = body["data"]
    assert len(kept) == 10
    # highest timerN (149) is first; smallest kept is 140 — none of the tiny ones.
    assert kept[0]["timerN"] == "149"
    assert all(int(r["timerN"]) >= 140 for r in kept)


def test_apply_limit_no_count_keeps_order() -> None:
    # dimension-values shape: plain strings, no volume -> alphabetical prefix.
    body = {"values": [f"Chrome/{i}" for i in range(150)]}
    info = _apply_limit(body, 10)
    assert "sorted_by" not in info
    assert body["values"] == [f"Chrome/{i}" for i in range(10)]


def test_apply_limit_noop_when_under_limit_or_none() -> None:
    body = {"rows": [1, 2, 3]}
    assert _apply_limit(body, 50) is None
    assert _apply_limit({"rows": [1, 2, 3]}, None) is None


def test_normalize_reports_truncation() -> None:
    data = {"countries": [{"c": i} for i in range(150)]}
    out = normalize(
        app="alpha",
        query_type="geography",
        request_params={"date": "2026-08-03"},
        aggregation="unspecified",
        data=data,
        raw=False,
        limit=100,
    )
    assert out["truncated"] == {"key": "countries", "total": 150, "returned": 100}
    assert len(out["body"]["countries"]) == 100


def test_limit_ignored_in_raw_mode() -> None:
    data = {"countries": list(range(150))}
    out = normalize(
        app="alpha",
        query_type="geography",
        request_params={},
        aggregation="unspecified",
        data=data,
        raw=True,
        limit=10,
    )
    assert out["data"]["countries"] == list(range(150))  # raw untouched


# --- empty_reason heuristic ------------------------------------------------
def test_classify_empty_levels() -> None:
    assert _classify_empty({})[0] == "no_data"
    assert _classify_empty({"browser": "X"})[0] == "likely_no_traffic"
    assert _classify_empty({"browser": "X", "ab-test": "Y"})[0] == (
        "possibly_unsupported_combo"
    )


def test_normalize_sets_empty_reason() -> None:
    out = normalize(
        app="alpha",
        query_type="timers-metrics",
        request_params={"date-comparator": "LastHour", "browser": "X", "ab-test": "Y"},
        aggregation="per-minute",
        data={"values": []},
        raw=False,
    )
    assert out["empty"] is True
    assert out["empty_reason"] == "possibly_unsupported_combo"


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


def test_timers_metrics_history_preserved_in_full_mode() -> None:
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
        history_mode="full",
    )
    # full mode is loss-free: every point preserved, byte-for-byte.
    assert out["body"]["series"][0]["history"] == history
    assert out["history_mode"] == "full"
    assert out["empty"] is False


def test_series_envelope_stripped_but_points_kept_in_full_mode() -> None:
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
        history_mode="full",
    )
    assert out["body"]["meta"]["chartTitle"] == "PageLoad"
    assert out["body"]["series"][0]["aPoints"] == [{"x": 0, "y": 100}]


# --- history_mode ----------------------------------------------------------
def _tm_day(peak_at: int = 800, peak_val: int = 9999) -> dict:
    history = list(range(1440))
    history[peak_at] = peak_val
    return {
        "dataTimeZone": "UTC",
        "values": [{"id": "PageLoad", "history": history, "latest": 3441}],
    }


def test_downsample_is_default_and_shrinks_history() -> None:
    out = normalize(
        app="alpha",
        query_type="timers-metrics",
        request_params={"date-comparator": "Last24Hours"},
        aggregation="per-minute",
        data=_tm_day(),
        raw=False,  # no history_mode -> default
    )
    s = out["body"]["series"][0]
    assert out["history_mode"] == "downsample"
    assert s["latest"] == 3441  # aggregate always preserved
    assert s["n_points"] == 1440
    assert "history" not in s
    assert len(s["history_downsampled"]) <= 60
    assert s["peak"] == {"index": 800, "value": 9999}  # spike location kept


def test_none_mode_drops_series_keeps_aggregates() -> None:
    out = normalize(
        app="alpha",
        query_type="timers-metrics",
        request_params={"date-comparator": "Last24Hours"},
        aggregation="per-minute",
        data=_tm_day(),
        raw=False,
        history_mode="none",
    )
    s = out["body"]["series"][0]
    assert "history" not in s and "history_downsampled" not in s
    assert s["latest"] == 3441
    assert s["n_points"] == 1440
    assert s["first"] == 0 and s["last"] == 1439
    assert s["peak"]["value"] == 9999


def test_by_minute_downsample_keeps_statistics_and_peak() -> None:
    pts = [{"x": i, "y": i} for i in range(1440)]
    pts[500]["y"] = 8888
    data = {
        "series": {
            "series": [
                {"name": "PageLoad", "aPoints": pts, "pointCount": 1440,
                 "statistics": {"yMin": 0, "yMax": 8888, "ySum": 1, "yAvg": 1}}
            ]
        }
    }
    out = normalize(
        app="alpha",
        query_type="by-minute",
        request_params={"date": "2026-08-03"},
        aggregation="per-minute",
        data=data,
        raw=False,
    )
    s = out["body"]["series"][0]
    assert "aPoints" not in s
    assert len(s["aPoints_downsampled"]) <= 60
    assert s["statistics"]["yMax"] == 8888  # mPulse aggregates untouched
    assert s["peak"]["y"] == 8888


def test_histogram_never_reduced_even_at_none() -> None:
    # histogram buckets are a distribution (needed for exact p75), not a time
    # series -> must survive any history_mode intact.
    buckets = [{"s": i, "e": i + 1, "c": i} for i in range(200)]
    data = {"series": {"series": [{"name": "PageLoad", "aPoints": buckets}]}}
    out = normalize(
        app="alpha",
        query_type="histogram",
        request_params={"date": "2026-08-03"},
        aggregation="per-bucket",
        data=data,
        raw=False,
        history_mode="none",
    )
    assert out["body"]["series"][0]["aPoints"] == buckets


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
