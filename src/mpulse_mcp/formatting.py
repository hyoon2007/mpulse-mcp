"""Response shaping.

Two modes:

* ``raw=True``  -> the mPulse JSON is returned unchanged under ``data``.
* ``raw=False`` -> a predictable, flat envelope with explicit metadata plus the
  data body. Normalization strips mPulse's presentation envelope and lifts the
  meaningful arrays into compact, explicitly-keyed structures.

**Loss-free is on demand.** Aggregate/period values (``latest``, percentiles,
histogram buckets) are *never* rounded, summarized, or dropped. But the
per-minute *time series* (``timers-metrics.history`` / ``by-minute.aPoints``) is
by default **downsampled** (see ``history_mode``), because the dominant workload
reads only ``latest`` and full per-minute arrays are ~1,440 points/day of mostly
unused tokens. The complete series is always recoverable via ``raw=True`` or
``history_mode='full'``. ``histogram`` buckets (needed for exact p75) and
``summary`` are treated as aggregates and kept in full regardless of mode.

Every result carries an ``empty`` flag and, for drilldowns, a hint that an
empty body may mean the dimension combination is unsupported (mPulse returns no
data rather than an error for those).
"""

from __future__ import annotations

from typing import Any

from .query_types import DRILLDOWN_PARAMS

# --- History (time-series) volume control ----------------------------------
# 'full'       -> keep every per-minute point (loss-free, largest).
# 'downsample' -> keep 'latest' + a <=N-point even-spaced series + peak.
# 'none'       -> drop the series; keep 'latest', endpoints, n_points, peak.
HISTORY_MODES = ("full", "downsample", "none")
DEFAULT_HISTORY_MODE = "downsample"
DOWNSAMPLE_MAX_POINTS = 60

# Only these query-types carry a per-minute time series to reduce. Distribution
# (histogram) and single-value (summary) shapes are always kept whole.
_TEMPORAL_QUERY_TYPES = ("timers-metrics", "by-minute")


def _period(params: dict[str, Any]) -> dict[str, Any]:
    """Extract the time selection from request params for the metadata block."""
    period: dict[str, Any] = {}
    for key in (
        "date",
        "date-comparator",
        "trailing-seconds",
        "date-start",
        "date-end",
        "timezone",
    ):
        if params.get(key) is not None:
            period[key] = params[key]
    return period


def _active_drilldowns(params: dict[str, Any]) -> dict[str, Any]:
    dd = {k: params[k] for k in DRILLDOWN_PARAMS if params.get(k) is not None}
    # custom-dimension-* are dynamic
    dd.update(
        {k: v for k, v in params.items() if k.startswith("custom-dimension-")}
    )
    return dd


def _norm_name(name: str) -> str:
    """Casing/separator-insensitive key: 'largest_contentful_paint' == 'LargestContentfulPaint'."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _returned_series_ids(data: dict[str, Any]) -> list[str]:
    """Series identifiers echoed by mPulse, across the id/name-bearing shapes."""
    ids: list[str] = []
    values = data.get("values")
    if isinstance(values, list):
        ids += [v["id"] for v in values if isinstance(v, dict) and v.get("id")]
    series = data.get("series")
    if isinstance(series, dict) and isinstance(series.get("series"), list):
        ids += [
            s["name"] for s in series["series"] if isinstance(s, dict) and s.get("name")
        ]
    return ids


def _detect_silent_fallback(
    request_params: dict[str, Any], data: dict[str, Any]
) -> str | None:
    """Warn when a requested built-in timer/metric was silently replaced.

    mPulse answers an unrecognized ``timer``/``metric`` with a PageLoad (or
    default) series instead of erroring. We compare the requested name against
    the series id/name echoed in the response; a casing/separator-only
    difference is treated as a match (same metric), so only a genuinely
    different id triggers the warning. Custom timers/dimensions are not checked
    (their echoed id may legitimately differ from the param value).
    """
    returned = _returned_series_ids(data)
    if not returned:  # summary/flat/empty shapes carry no id to compare
        return None
    returned_norm = {_norm_name(r) for r in returned}

    missing: list[str] = []
    for key in ("timer", "metric"):
        requested = request_params.get(key)
        if isinstance(requested, str) and requested:
            if _norm_name(requested) not in returned_norm:
                missing.append(requested)
    if not missing:
        return None
    return (
        f"Requested {', '.join(repr(m) for m in missing)} but mPulse returned "
        f"series {returned!r} — likely a SILENT FALLBACK to a default "
        f"(unrecognized timer/metric names are not rejected). Verify the exact "
        f"name via describe_query; the returned data is NOT for the requested "
        f"name."
    )


def _is_empty(query_type: str, data: dict[str, Any]) -> bool:
    """Best-effort emptiness detection across the known response shapes."""
    if not data:
        return True
    # timers-metrics
    if "values" in data and isinstance(data["values"], list):
        vals = data["values"]
        if not vals:
            return True
        return all(not (v.get("history") or v.get("latest")) for v in vals if isinstance(v, dict))
    # series-based (histogram, by-minute)
    series = data.get("series")
    if isinstance(series, dict):
        inner = series.get("series")
        if isinstance(inner, list):
            if not inner:
                return True
            return all(
                not (s.get("aPoints") or s.get("buckets")) for s in inner if isinstance(s, dict)
            )
    # summary: all numeric fields absent/zero-count
    if query_type == "summary":
        n = data.get("n")
        return n in (None, "", "0", 0)
    return False


def normalize(
    *,
    app: str,
    query_type: str,
    request_params: dict[str, Any],
    aggregation: str,
    data: dict[str, Any],
    raw: bool,
    history_mode: str = DEFAULT_HISTORY_MODE,
    limit: int | None = None,
) -> dict[str, Any]:
    """Produce the tool's return payload.

    ``request_params`` are the wire params actually sent (hyphenated keys).

    ``history_mode`` controls per-minute time-series volume for the temporal
    query-types (``timers-metrics``, ``by-minute``) — see :data:`HISTORY_MODES`.
    It is ignored for ``raw=True`` (untouched) and for non-temporal shapes
    (``summary``/``histogram`` distributions are always kept in full).

    ``limit`` caps the largest top-level list in the body (high-cardinality
    query-types like ``dimension-values``/``geography``), recording a
    ``truncated`` note. ``None`` means no cap; ignored for ``raw=True``.
    """
    if history_mode not in HISTORY_MODES:
        history_mode = DEFAULT_HISTORY_MODE

    # Silent-fallback detection is additive metadata (never mutates data), so it
    # applies in raw mode too.
    warning = _detect_silent_fallback(request_params, data)

    if raw:
        out: dict[str, Any] = {
            "app": app,
            "query_type": query_type,
            "raw": True,
            "data": data,
        }
        if warning:
            out["warning"] = warning
        return out

    period = _period(request_params)
    drilldowns = _active_drilldowns(request_params)
    empty = _is_empty(query_type, data)

    body = _apply_history_mode(
        query_type, _strip_envelope(query_type, data), history_mode
    )
    truncation = _apply_limit(body, limit)

    envelope: dict[str, Any] = {
        "app": app,
        "query_type": query_type,
        "period": period,
        "timezone": request_params.get("timezone", "UTC"),
        "aggregation": aggregation,
        "drilldowns": drilldowns,
        "empty": empty,
        "history_mode": history_mode,
        # 'body' holds the envelope-stripped data (loss-free when
        # history_mode='full' or raw=True).
        "body": body,
    }
    if warning:
        envelope["warning"] = warning
    if truncation:
        envelope["truncated"] = truncation
    if empty:
        reason, note = _classify_empty(drilldowns)
        envelope["empty_reason"] = reason
        envelope["note"] = note
    return envelope


def _classify_empty(drilldowns: dict[str, Any]) -> tuple[str, str]:
    """Heuristically explain an empty result (cheap, no extra network call).

    mPulse returns *no data* rather than an error for an unsupported dimension
    combination, which is indistinguishable from genuine no-traffic without a
    probe. Single-dimension filters are generally supported, so one drilldown is
    most likely no-traffic; multiple drilldowns raise the chance of an
    unsupported combination. Callers can pass ``probe=true`` for a definitive
    answer (one extra query without the drilldowns).
    """
    n = len(drilldowns)
    if n == 0:
        return "no_data", "Empty result: no data for this query/period."
    if n == 1:
        return (
            "likely_no_traffic",
            "Empty result with a single drilldown. Single-dimension filters are "
            "generally supported, so this most likely means no matching traffic "
            "— not an unsupported combination. Pass probe=true to confirm.",
        )
    return (
        "possibly_unsupported_combo",
        "Empty result with multiple drilldowns. mPulse returns no data (not an "
        "error) for unsupported dimension combinations, so this may be an "
        "unsupported combo OR simply no matching traffic. Pass probe=true to "
        "disambiguate, or test the dimensions individually.",
    )


def _apply_limit(body: dict[str, Any], limit: int | None) -> dict[str, Any] | None:
    """Truncate the largest top-level list in ``body`` to ``limit`` items.

    Returns truncation metadata ``{key, total, returned}`` when it trims
    something, else ``None``. Mutates ``body`` in place (the list value is
    replaced by its prefix). Non-list bodies and ``limit is None`` are no-ops.
    This is shape-agnostic on purpose: the high-cardinality query-types
    (dimension-values, geography, page-groups, …) have differing keys, but each
    puts its rows in one dominant list.
    """
    if limit is None or limit < 0 or not isinstance(body, dict):
        return None
    # Pick the longest top-level list value as the "rows" to cap.
    key = None
    longest = -1
    for k, v in body.items():
        if isinstance(v, list) and len(v) > longest:
            key, longest = k, len(v)
    if key is None or longest <= limit:
        return None
    total = longest
    body[key] = body[key][:limit]
    return {"key": key, "total": total, "returned": limit}


def _strip_envelope(query_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Lift the meaningful payload out of mPulse's presentation wrapper.

    Loss-free: the full original numbers are preserved; we only remove chart
    chrome (titles, dataset names) into a compact ``meta`` and expose the data
    arrays under stable keys. If a shape is unrecognized, the original ``data``
    is passed through untouched.
    """
    # summary: already flat {median, moe, n, p95, p98}
    if query_type == "summary":
        return dict(data)

    # timers-metrics: {dataTimeZone, values:[{id, history[], latest}]}
    if "values" in data and isinstance(data.get("values"), list):
        return {
            "dataTimeZone": data.get("dataTimeZone"),
            "series": data["values"],  # each: {id, history:[...], latest}
        }

    # series-based: histogram / by-minute
    series = data.get("series")
    if isinstance(series, dict) and isinstance(series.get("series"), list):
        meta = {
            k: data.get(k)
            for k in (
                "chartTitle",
                "chartTitleSuffix",
                "datasetName",
                "reportType",
                "resultName",
            )
            if data.get(k) is not None
        }
        return {"meta": meta, "series": series["series"]}

    # Unknown shape -> pass through unchanged (never drop data).
    return dict(data)


# --- history_mode application ----------------------------------------------
def _downsample(seq: list[Any], max_points: int = DOWNSAMPLE_MAX_POINTS) -> list[Any]:
    """Even-spaced downsample preserving the first and last element.

    Returns the input unchanged when it already fits within ``max_points``.
    """
    n = len(seq)
    if n <= max_points:
        return list(seq)
    step = (n - 1) / (max_points - 1)
    idx = sorted({round(i * step) for i in range(max_points)})
    return [seq[i] for i in idx]


def _argmax_numeric(values: list[Any]) -> tuple[int | None, Any]:
    best_i: int | None = None
    best_v: Any = None
    for i, v in enumerate(values):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if best_v is None or v > best_v:
            best_i, best_v = i, v
    return best_i, best_v


def _peak_point(points: list[Any]) -> dict[str, Any] | None:
    """Point with the largest y among {x, y} dicts (spike location)."""
    best: dict[str, Any] | None = None
    for p in points:
        if not isinstance(p, dict):
            continue
        y = p.get("y")
        if isinstance(y, bool) or not isinstance(y, (int, float)):
            continue
        if best is None or y > best.get("y"):
            best = p
    return dict(best) if best is not None else None


def _reduce_tm_series(s: dict[str, Any], mode: str) -> dict[str, Any]:
    """Reduce a timers-metrics series {id, history:[int], latest}."""
    if not isinstance(s, dict):
        return s
    history = s.get("history")
    if not isinstance(history, list) or not history:
        return s
    out = {k: v for k, v in s.items() if k != "history"}  # keep id, latest, …
    out["n_points"] = len(history)
    peak_i, peak_v = _argmax_numeric(history)
    if peak_i is not None:
        out["peak"] = {"index": peak_i, "value": peak_v}
    if mode == "none":
        out["first"] = history[0]
        out["last"] = history[-1]
    else:  # downsample
        out["history_downsampled"] = _downsample(history)
    return out


def _reduce_bm_series(s: dict[str, Any], mode: str) -> dict[str, Any]:
    """Reduce a by-minute series {name, aPoints:[{x,y}], statistics, ...}."""
    if not isinstance(s, dict):
        return s
    pts = s.get("aPoints")
    if not isinstance(pts, list) or not pts:
        return s
    out = {k: v for k, v in s.items() if k != "aPoints"}  # keep statistics, …
    out.setdefault("pointCount", len(pts))
    peak = _peak_point(pts)
    if peak is not None:
        out["peak"] = peak
    if mode == "none":
        out["first"] = pts[0]
        out["last"] = pts[-1]
    else:  # downsample
        out["aPoints_downsampled"] = _downsample(pts)
    return out


def _apply_history_mode(
    query_type: str, body: dict[str, Any], mode: str
) -> dict[str, Any]:
    """Apply the time-series volume policy to a stripped body.

    No-op for ``mode='full'`` and for non-temporal query-types (their aggregate
    data must never be reduced). Always preserves ``latest``/statistics.
    """
    if mode == "full" or query_type not in _TEMPORAL_QUERY_TYPES:
        return body
    series = body.get("series")
    if not isinstance(series, list):
        return body
    reducer = _reduce_tm_series if query_type == "timers-metrics" else _reduce_bm_series
    body = dict(body)
    body["series"] = [reducer(s, mode) for s in series]
    return body
