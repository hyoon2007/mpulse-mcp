"""Response shaping.

Two modes:

* ``raw=True``  -> the mPulse JSON is returned unchanged under ``data``.
* ``raw=False`` -> a predictable, flat envelope with explicit metadata plus the
  data body. **Numeric values are never rounded, summarized, or dropped** — the
  point of this server is to feed exact figures into downstream p75/statistical
  analysis. Normalization only strips mPulse's presentation envelope and lifts
  the meaningful arrays into compact, explicitly-keyed structures.

Every result carries an ``empty`` flag and, for drilldowns, a hint that an
empty body may mean the dimension combination is unsupported (mPulse returns no
data rather than an error for those).
"""

from __future__ import annotations

from typing import Any

from .query_types import DRILLDOWN_PARAMS


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
) -> dict[str, Any]:
    """Produce the tool's return payload.

    ``request_params`` are the wire params actually sent (hyphenated keys).
    """
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

    envelope: dict[str, Any] = {
        "app": app,
        "query_type": query_type,
        "period": period,
        "timezone": request_params.get("timezone", "UTC"),
        "aggregation": aggregation,
        "drilldowns": drilldowns,
        "empty": empty,
        # 'body' holds the loss-free, envelope-stripped data.
        "body": _strip_envelope(query_type, data),
    }
    if warning:
        envelope["warning"] = warning
    if empty and drilldowns:
        envelope["note"] = (
            "Empty result. This drilldown combination may not be supported by "
            "mPulse (unsupported combinations return no data rather than an "
            "error). Verify the dimensions individually."
        )
    elif empty:
        envelope["note"] = "Empty result: no data for this query/period."
    return envelope


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
