"""FastMCP server exposing mPulse Query API tools over stdio.

Entry point: the ``mpulse-mcp`` console script → :func:`main`.

stdio safety: this module never prints to stdout. FastMCP owns the stdout
JSON-RPC stream; all logging goes to stderr (see ``mpulse_mcp.log``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__, log
from .client import MpulseClient
from .config import AppConfig, Registry, load_registry
from .errors import MpulseError, ValidationError
from .query_types import (
    DRILLDOWN_PARAMS,
    KNOWN_DATE_COMPARATORS,
    QUERY_TYPES,
    describe as describe_qt,
)

mcp = FastMCP("mpulse")

# --- Lazily-initialized singletons -----------------------------------------
_registry: Registry | None = None
_client: MpulseClient | None = None


def _get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = load_registry()
    return _registry


def _get_client() -> MpulseClient:
    global _client
    if _client is None:
        _client = MpulseClient(_get_registry())
    return _client


# --- Shared argument handling ----------------------------------------------
# Map friendly (underscore) tool argument names to mPulse wire (hyphen) params.
_DRILLDOWN_ARG_TO_WIRE = {
    "page_group": "page-group",
    "browser": "browser",
    "browser_family": "browser-family",
    "ab_test": "ab-test",
    "country": "country",
    "region": "region",
    "device_type": "device-type",
    "device_model": "device-model",
    "device_manufacturer": "device-manufacturer",
    "os": "os",
    "os_family": "os-family",
    "connection_type": "connection-type",
    "isp": "isp",
    "bandwidth_block": "bandwith-block",  # API misspells 'bandwidth' (verified live)
    "beacon_type": "beacon-type",
    "site_version": "site-version",
}


def _validate_and_build_time(
    date: str | None, date_comparator: str | None, timezone: str | None
) -> dict[str, Any]:
    """Validate the mutually-exclusive time selection and build wire params."""
    if date and date_comparator:
        raise ValidationError(
            "Provide either 'date' or 'date_comparator', not both.",
            hint="mPulse queries cover a single time selection.",
        )
    if not date and not date_comparator:
        raise ValidationError(
            "One of 'date' or 'date_comparator' is required.",
            hint="e.g. date='2026-08-03' or date_comparator='Last24Hours'.",
        )

    params: dict[str, Any] = {}
    if date:
        _check_single_day(date)
        params["date"] = date
    if date_comparator:
        params["date-comparator"] = date_comparator
    if timezone:
        params["timezone"] = timezone
    return params


def _check_single_day(date: str) -> None:
    """Enforce a single calendar day in YYYY-MM-DD form.

    Range syntax (``start..end`` or two dates) is rejected with guidance: mPulse
    aggregates one calendar day per query, so the caller (LLM) should split a
    range into per-day calls itself — this server does not fan out implicitly.
    """
    if ".." in date or "," in date or "/" in date:
        raise ValidationError(
            f"'{date}' looks like a range. mPulse returns one calendar day per "
            "query.",
            hint=(
                "Pass a single YYYY-MM-DD date. To cover a range, call the tool "
                "once per day and combine the results yourself."
            ),
        )
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise ValidationError(
            f"'{date}' is not a valid date. Use YYYY-MM-DD (a single calendar "
            "day).",
        ) from None


def _collect_drilldowns(kwargs: dict[str, Any]) -> dict[str, Any]:
    wire: dict[str, Any] = {}
    for arg, value in kwargs.items():
        if value is None:
            continue
        wire_name = _DRILLDOWN_ARG_TO_WIRE.get(arg)
        if wire_name:
            wire[wire_name] = value
    return wire


async def _run(
    *,
    app: str | None,
    query_type: str,
    value_params: dict[str, Any],
    date: str | None,
    date_comparator: str | None,
    timezone: str | None,
    drilldowns: dict[str, Any],
    aggregation: str,
    raw: bool,
    history_mode: str = "downsample",
    probe: bool = False,
) -> dict[str, Any]:
    """Shared execution path for the explicit tools.

    All :class:`MpulseError` cases — input validation *and* upstream failures —
    are returned as a structured ``{"error", "message"}`` dict (message includes
    any hint) rather than raised, so the model sees consistent, actionable text.
    """
    from .formatting import normalize  # local import keeps import graph light

    client = _get_client()
    app_name = app or client.registry.default_app
    corrections: list[str] = []
    try:
        time_params = _validate_and_build_time(date, date_comparator, timezone)
        params: dict[str, Any] = {}
        params.update({k: v for k, v in value_params.items() if v is not None})
        params.update(time_params)
        params.update(drilldowns)
        corrections = _resolve_value_names(query_type, params)
        data = await client.query(app=app, query_type=query_type, params=params)
    except MpulseError as exc:
        return {"error": type(exc).__name__, "message": exc.user_message()}

    # Re-add format for metadata parity (client injects it on the wire).
    params.setdefault("format", "json")
    result = normalize(
        app=app_name,
        query_type=query_type,
        request_params=params,
        aggregation=aggregation,
        data=data,
        raw=raw,
        history_mode=history_mode,
    )
    if corrections:
        result["corrections"] = corrections
    if probe:
        await _probe_empty_reason(client, app, query_type, params, result)
    return result


# High-cardinality query-types are capped by default so a value dump can't blow
# the token budget; callers can override with an explicit `limit`.
_HIGH_CARDINALITY_QUERY_TYPES = frozenset(
    {
        "dimension-values",
        "dimension-over-time",
        "dimensions-over-time",
        "metrics-by-dimension",
        "geography",
        "page-groups",
        "browsers",
        "ab-tests",
        "bandwidth",
    }
)
_DEFAULT_HIGH_CARDINALITY_LIMIT = 100


async def _probe_empty_reason(
    client: MpulseClient,
    app: str | None,
    query_type: str,
    params: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Disambiguate an empty drilldown result with one extra drilldown-free query.

    Opt-in (a network call). Only runs when ``result`` is a non-raw empty
    envelope that had drilldowns. Sets ``empty_reason`` to ``unsupported_combo``
    (the same query without drilldowns has data) or ``no_traffic`` (still empty),
    and records a ``probe`` block. Failures are reported, never raised.
    """
    from .formatting import _is_empty

    if not isinstance(result, dict) or not result.get("empty"):
        return
    if not result.get("drilldowns"):
        return

    base = {
        k: v
        for k, v in params.items()
        if k not in DRILLDOWN_PARAMS and not k.startswith("custom-dimension-")
    }
    try:
        base_data = await client.query(app=app, query_type=query_type, params=base)
    except MpulseError as exc:
        result["probe"] = {"ran": False, "error": exc.user_message()}
        return

    base_empty = _is_empty(query_type, base_data)
    result["empty_reason"] = "no_traffic" if base_empty else "unsupported_combo"
    result["probe"] = {"ran": True, "base_empty": base_empty}
    result["note"] = (
        "Probe: same query without drilldowns is "
        + ("also empty → no matching traffic."
           if base_empty
           else "non-empty → the drilldown combination is unsupported.")
    )


def _resolve_value_names(query_type: str, params: dict[str, Any]) -> list[str]:
    """Auto-correct known ``timer``/``metric`` names in-place, or reject bad ones.

    Uses the catalog to (a) canonicalize a casing/separator-only mismatch
    silently — avoiding a silent PageLoad fallback at zero token cost — and
    (b) raise a :class:`ValidationError` with close suggestions when a value is
    genuinely unknown, so a wrong name never reaches mPulse. Custom timers and
    values are left untouched; if the catalog is unavailable this is a no-op.
    Returns human-readable notes for any auto-corrections made.
    """
    from .catalog import resolve_value

    notes: list[str] = []
    for kind in ("timer", "metric"):
        value = params.get(kind)
        status, payload = resolve_value(kind, value, query_type)
        if status == "ok" and payload != value:
            params[kind] = payload
            notes.append(f"{kind} {value!r} auto-corrected to {payload!r}")
        elif status == "unknown":
            hint = (
                f"Did you mean: {', '.join(payload)}?"
                if payload
                else f"Call describe_query('{query_type}') for valid {kind} values."
            )
            raise ValidationError(
                f"Unknown {kind} {value!r} for query-type '{query_type}'.",
                hint=hint,
            )
    return notes


# ===========================================================================
# Explicit tools
# ===========================================================================
@mcp.tool()
async def get_summary(
    app: str | None = None,
    timer: str | None = None,
    custom_timer: str | None = None,
    percentile: int | None = None,
    date: str | None = None,
    date_comparator: str | None = None,
    timezone: str | None = None,
    page_group: str | None = None,
    browser: str | None = None,
    browser_family: str | None = None,
    ab_test: str | None = None,
    country: str | None = None,
    region: str | None = None,
    device_type: str | None = None,
    os: str | None = None,
    connection_type: str | None = None,
    beacon_type: str | None = None,
    raw: bool = False,
    probe: bool = False,
) -> dict[str, Any]:
    """Aggregate summary stats for one timer (median, margin-of-error, count, p95, p98).

    mPulse query-type: `summary`. Data is per-minute aggregated over a single
    calendar day in the given timezone (default UTC).

    Time selection (exactly one required):
      * `date` — a single calendar day, `YYYY-MM-DD`.
      * `date_comparator` — a relative window, e.g. `Last24Hours`, `LastHour`.
    A date *range* is not supported per call; split it into per-day calls.

    Timer: `timer` (built-in, default `PageLoad`) OR `custom_timer` (a custom
    timer name). `percentile` is 1–99 (default 50).

    Drilldowns filter the data (e.g. `page_group`, `browser`, `country`,
    `device_type`, `ab_test`). Unsupported combinations return empty data with a
    note rather than an error; an `empty_reason` heuristic labels the likely
    cause, and `probe=True` runs one extra drilldown-free query to decide
    `unsupported_combo` vs `no_traffic` definitively.

    Set `raw=True` to get mPulse's untouched JSON. Numeric values are never
    rounded or summarized.
    """
    return await _run(
        app=app,
        query_type="summary",
        value_params={
            "timer": timer,
            "custom-timer": custom_timer,
            "percentile": percentile,
        },
        date=date,
        date_comparator=date_comparator,
        timezone=timezone,
        drilldowns=_collect_drilldowns(
            {
                "page_group": page_group,
                "browser": browser,
                "browser_family": browser_family,
                "ab_test": ab_test,
                "country": country,
                "region": region,
                "device_type": device_type,
                "os": os,
                "connection_type": connection_type,
                "beacon_type": beacon_type,
            }
        ),
        aggregation="single-value (period aggregate)",
        raw=raw,
        probe=probe,
    )


@mcp.tool()
async def get_histogram(
    app: str | None = None,
    timer: str | None = None,
    custom_timer: str | None = None,
    percentile: int | None = None,
    date: str | None = None,
    date_comparator: str | None = None,
    timezone: str | None = None,
    page_group: str | None = None,
    browser: str | None = None,
    ab_test: str | None = None,
    country: str | None = None,
    device_type: str | None = None,
    beacon_type: str | None = None,
    raw: bool = False,
    probe: bool = False,
) -> dict[str, Any]:
    """Distribution (histogram buckets) for one timer over the period.

    mPulse query-type: `histogram`. Returns per-bucket counts plus median/p95/
    p98. Time selection and drilldowns behave exactly as in `get_summary`
    (single calendar day OR relative `date_comparator`).

    Use `raw=True` for untouched mPulse JSON. Bucket counts are preserved
    losslessly.
    """
    return await _run(
        app=app,
        query_type="histogram",
        value_params={
            "timer": timer,
            "custom-timer": custom_timer,
            "percentile": percentile,
        },
        date=date,
        date_comparator=date_comparator,
        timezone=timezone,
        drilldowns=_collect_drilldowns(
            {
                "page_group": page_group,
                "browser": browser,
                "ab_test": ab_test,
                "country": country,
                "device_type": device_type,
                "beacon_type": beacon_type,
            }
        ),
        aggregation="per-bucket distribution",
        raw=raw,
        probe=probe,
    )


@mcp.tool()
async def get_timers(
    app: str | None = None,
    timer: str | None = None,
    custom_timer: str | None = None,
    percentile: int | None = None,
    date: str | None = None,
    date_comparator: str | None = None,
    timezone: str | None = None,
    page_group: str | None = None,
    browser: str | None = None,
    ab_test: str | None = None,
    country: str | None = None,
    device_type: str | None = None,
    beacon_type: str | None = None,
    raw: bool = False,
    history_mode: str = "downsample",
    probe: bool = False,
) -> dict[str, Any]:
    """A single timer's value **by minute over time** (time series).

    mPulse query-type: `by-minute`. Best for one timer (built-in `timer`,
    default `PageLoad`, or a `custom_timer`). For *multiple* timers/metrics at
    once, use `get_metrics` (query-type `timers-metrics`).

    Returns a per-minute series for the selected calendar day (or
    `date_comparator` window). `history_mode` controls series volume:
    `downsample` (default, ≤60 points + `peak` + `statistics`), `none` (drop the
    series, keep endpoints/`peak`/`statistics`), or `full` (every minute,
    loss-free). Use `full` or `raw=True` when you need exact per-minute values.
    """
    return await _run(
        app=app,
        query_type="by-minute",
        value_params={
            "timer": timer,
            "custom-timer": custom_timer,
            "percentile": percentile,
        },
        date=date,
        date_comparator=date_comparator,
        timezone=timezone,
        drilldowns=_collect_drilldowns(
            {
                "page_group": page_group,
                "browser": browser,
                "ab_test": ab_test,
                "country": country,
                "device_type": device_type,
                "beacon_type": beacon_type,
            }
        ),
        aggregation="per-minute",
        raw=raw,
        history_mode=history_mode,
        probe=probe,
    )


@mcp.tool()
async def get_metrics(
    app: str | None = None,
    metric: str | None = None,
    timer: str | None = None,
    custom_timer: str | None = None,
    percentile: int | None = None,
    date: str | None = None,
    date_comparator: str | None = None,
    timezone: str | None = None,
    page_group: str | None = None,
    browser: str | None = None,
    ab_test: str | None = None,
    country: str | None = None,
    device_type: str | None = None,
    beacon_type: str | None = None,
    raw: bool = False,
    history_mode: str = "downsample",
    probe: bool = False,
) -> dict[str, Any]:
    """Multiple timers/metrics **by minute over time**.

    mPulse query-type: `timers-metrics`. `metric` defaults to `Beacons`, `timer`
    to `PageLoad`. Returns `series:[{id, latest, ...}]` plus the data timezone;
    `latest` is the period-wide aggregate (e.g. monthly p75 — read this, do NOT
    average the per-minute history).

    `history_mode` controls per-minute volume: `downsample` (default, `latest` +
    ≤60-point `history_downsampled` + `peak`), `none` (just `latest`, endpoints,
    `peak`, `n_points`), or `full` (every minute, loss-free). `latest` is always
    preserved. Use `full` or `raw=True` for exact per-minute values.
    """
    return await _run(
        app=app,
        query_type="timers-metrics",
        value_params={
            "metric": metric,
            "timer": timer,
            "custom-timer": custom_timer,
            "percentile": percentile,
        },
        date=date,
        date_comparator=date_comparator,
        timezone=timezone,
        drilldowns=_collect_drilldowns(
            {
                "page_group": page_group,
                "browser": browser,
                "ab_test": ab_test,
                "country": country,
                "device_type": device_type,
                "beacon_type": beacon_type,
            }
        ),
        aggregation="per-minute",
        raw=raw,
        history_mode=history_mode,
        probe=probe,
    )


# ===========================================================================
# Generic tool
# ===========================================================================
@mcp.tool()
async def query(
    query_type: str,
    app: str | None = None,
    params: dict[str, Any] | None = None,
    raw: bool = False,
    history_mode: str = "downsample",
    limit: int | None = None,
    probe: bool = False,
) -> dict[str, Any]:
    """Call an arbitrary mPulse query-type with arbitrary parameters.

    Use this for query-types not covered by the explicit tools (see
    `list_query_types`), e.g. `dimension-values`, `geography`, `page-groups`,
    `app-error-summary`.

    `params` is a dict of mPulse *wire* parameter names (hyphenated), e.g.
    `{"date-comparator": "Last24Hours", "page-group": "Home", "timer":
    "PageLoad"}`. `format=json` is added automatically. Remember the one-
    calendar-day-per-query constraint.

    `history_mode` (`downsample`/`none`/`full`) only affects the temporal
    query-types `timers-metrics` and `by-minute`; other query-types ignore it.

    `limit` caps the returned rows for high-cardinality query-types (a
    `truncated` note reports the total). High-cardinality types
    (`dimension-values`, `geography`, `page-groups`, `browsers`, …) are capped at
    100 by default; pass an explicit `limit` (or a large one) to change this.

    `probe=True`: if the result is empty *and* had drilldowns, run one extra
    query without the drilldowns to decide `empty_reason`
    (`unsupported_combo` vs `no_traffic`).

    `raw=True` returns mPulse's untouched JSON.
    """
    from .formatting import normalize

    wire = dict(params or {})
    # Enforce the single-day rule when a literal date is supplied.
    if isinstance(wire.get("date"), str):
        _check_single_day(wire["date"])
    if "date" in wire and "date-comparator" in wire:
        return {
            "error": "ValidationError",
            "message": "Provide either 'date' or 'date-comparator', not both.",
        }

    # Reject a custom dimension on the endpoints that don't accept one
    # (dimension-values / dimension-over-time), before spending a wasted call;
    # auto-correct a built-in's casing. metrics-by-dimension accepts custom names.
    dim_note = _resolve_dimension_param(query_type, wire)
    if isinstance(dim_note, dict):  # a ValidationError payload
        return dim_note

    dim_notes: list[str] = [dim_note] if isinstance(dim_note, str) else []

    # Normalize custom-dimension labels/values against the app's declared set
    # (hints only — never rejected, since the declared list may be incomplete).
    try:
        app_cfg = _get_registry().get(app)
    except MpulseError:
        app_cfg = None
    dim_notes += _normalize_custom_dimension_params(query_type, wire, app_cfg)

    effective_limit = limit
    if effective_limit is None and query_type in _HIGH_CARDINALITY_QUERY_TYPES:
        effective_limit = _DEFAULT_HIGH_CARDINALITY_LIMIT

    client = _get_client()
    app_name = app or client.registry.default_app
    try:
        data = await client.query(app=app, query_type=query_type, params=wire)
    except MpulseError as exc:
        return {"error": type(exc).__name__, "message": exc.user_message()}

    wire.setdefault("format", "json")
    result = normalize(
        app=app_name,
        query_type=query_type,
        request_params=wire,
        aggregation="unspecified",
        data=data,
        raw=raw,
        history_mode=history_mode,
        limit=effective_limit,
    )
    if dim_notes:
        result["dimension_notes"] = dim_notes
    if probe:
        await _probe_empty_reason(client, app, query_type, wire, result)
    return result


def _resolve_dimension_param(
    query_type: str, wire: dict[str, Any]
) -> dict[str, Any] | str | None:
    """Validate/auto-correct the ``dimension`` wire param for dimension endpoints.

    Returns a ValidationError payload (dict) to short-circuit, a correction note
    (str) when a built-in's casing was fixed, or ``None`` when there is nothing
    to do (custom dimension accepted, exact match, or non-dimension query-type).
    """
    from .catalog import resolve_dimension

    value = wire.get("dimension")
    status, payload = resolve_dimension(value, query_type)
    if status == "ok" and payload != value:
        wire["dimension"] = payload
        return f"dimension {value!r} auto-corrected to {payload!r}"
    if status == "unknown_no_custom":
        hint = (
            f"'{query_type}' accepts BUILT-IN dimensions only — custom dimensions "
            "(e.g. 'branch') are not supported here. To break data down by a "
            "custom dimension, use metrics-by-dimension with dimension=<name>."
        )
        if payload:
            hint += f" Or did you mean a built-in: {', '.join(payload)}?"
        return {
            "error": "ValidationError",
            "message": (
                f"Unknown dimension {value!r} for query-type '{query_type}'.\n"
                f"Hint: {hint}"
            ),
        }
    return None


def _normalize_custom_dimension_params(
    query_type: str, wire: dict[str, Any], app_cfg: AppConfig | None
) -> list[str]:
    """Normalize custom-dimension label/value casing and hint on unknown names.

    Hints only — mPulse has no discovery API and the declared list may be
    incomplete, so a custom name is normalized and (softly) flagged, never
    rejected. Handles both the metrics-by-dimension split value and any
    ``custom-dimension-<label>`` filter key. Returns advisory notes.
    """
    from .catalog import resolve_dimension
    from .config import custom_dimension_wire_label

    known = app_cfg.custom_dimensions if app_cfg else {}
    app_label = f" for app '{app_cfg.name}'" if app_cfg else ""
    notes: list[str] = []

    def _flag_unknown(label: str, kind: str) -> None:
        if known and label not in known:
            notes.append(
                f"{kind} '{label}' is not a declared custom dimension{app_label} "
                f"(declared: {', '.join(sorted(known))}). Proceeding; verify the "
                f"exact name via list_custom_dimensions."
            )

    # (a) metrics-by-dimension split value, only when it's a custom (non-builtin) name
    if query_type == "metrics-by-dimension":
        val = wire.get("dimension")
        if isinstance(val, str) and val:
            status, _ = resolve_dimension(val, query_type)
            if status == "ok_custom":
                label = custom_dimension_wire_label(val)
                if label != val:
                    wire["dimension"] = label
                    notes.append(f"dimension {val!r} normalized to {label!r}")
                _flag_unknown(label, "dimension")

    # (b) custom-dimension-<label> filter keys (any query-type)
    for key in list(wire.keys()):
        if not key.startswith("custom-dimension-"):
            continue
        raw_label = key[len("custom-dimension-"):]
        label = custom_dimension_wire_label(raw_label)
        if label != raw_label:
            wire[f"custom-dimension-{label}"] = wire.pop(key)
            notes.append(f"filter label {raw_label!r} normalized to {label!r}")
        _flag_unknown(label, "custom-dimension filter")

    return notes


# ===========================================================================
# Batch aggregate tool
# ===========================================================================
def _norm_name(name: str) -> str:
    return "".join(c for c in str(name).lower() if c.isalnum())


def _build_period(period: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Turn one period spec into a (label, wire-params) pair, validating it.

    A period is ``{date}`` (single calendar day) OR ``{date_comparator, ...}``
    (relative; ``Between`` needs ``date_start``+``date_end``, ``Last`` needs
    ``trailing_seconds``). An optional ``label`` names the column.
    """
    if not isinstance(period, dict):
        raise ValidationError("Each period must be an object.")
    dc = period.get("date_comparator")
    date = period.get("date")
    if dc and date:
        raise ValidationError(
            "A period cannot set both 'date' and 'date_comparator'."
        )
    wire: dict[str, Any] = {}
    if dc:
        wire["date-comparator"] = dc
        if dc == "Between":
            ds, de = period.get("date_start"), period.get("date_end")
            if not ds or not de:
                raise ValidationError(
                    "date_comparator='Between' requires 'date_start' and "
                    "'date_end' (date_end is exclusive)."
                )
            wire["date-start"], wire["date-end"] = ds, de
            label = period.get("label") or f"{ds}..{de}"
        elif dc == "Last":
            ts = period.get("trailing_seconds")
            if ts is None:
                raise ValidationError(
                    "date_comparator='Last' requires 'trailing_seconds'."
                )
            wire["trailing-seconds"] = ts
            label = period.get("label") or f"Last{ts}s"
        else:
            label = period.get("label") or dc
    elif date:
        _check_single_day(date)
        wire["date"] = date
        label = period.get("label") or date
    else:
        raise ValidationError("Each period needs 'date' or 'date_comparator'.")
    return label, wire


def _extract_latest(data: dict[str, Any], name: str) -> tuple[Any, str | None]:
    """Pull the period aggregate (`latest`) for `name` from a timers-metrics body.

    Returns ``(value, warning)``. Matches the requested name against the series
    id case/separator-insensitively; a differing id means a silent fallback.
    """
    values = data.get("values")
    if not isinstance(values, list) or not values:
        return None, "empty result (no data, or unsupported drilldown combo)"
    target = _norm_name(name)
    for v in values:
        if isinstance(v, dict) and _norm_name(v.get("id", "")) == target:
            return v.get("latest"), None
    first = values[0] if isinstance(values[0], dict) else {}
    return (
        first.get("latest"),
        f"requested {name!r} but mPulse returned id {first.get('id')!r} "
        f"(silent fallback — value is NOT for the requested name)",
    )


@mcp.tool()
async def get_aggregate(
    metrics: list[str] | None = None,
    timers: list[str] | None = None,
    periods: list[dict[str, Any]] | None = None,
    percentiles: list[int] | None = None,
    app: str | None = None,
    timezone: str | None = None,
    page_group: str | None = None,
    browser: str | None = None,
    ab_test: str | None = None,
    country: str | None = None,
    device_type: str | None = None,
    beacon_type: str | None = None,
    max_combos: int = 24,
) -> dict[str, Any]:
    """Batch aggregate matrix: (metric|timer) × period × percentile → one table.

    Collapses the common reporting workflow — e.g. 3 metrics × 2 months × p50/p75
    (which is 12 separate calls) — into ONE tool call. Internally it issues one
    `timers-metrics` query per cell and returns **only the period aggregate**
    (`latest`), with NO per-minute history, so a dozen large responses become a
    small table of scalars.

    Inputs:
    - `metrics` / `timers`: name lists (at least one across the two). Metric
      names are the timers-metrics (CamelCase) space; unknown names are rejected
      up front with suggestions, casing is auto-corrected.
    - `periods`: list of period objects, each `{date: "YYYY-MM-DD"}` OR
      `{date_comparator: "Last24Hours" | "ThisMonth" | ...}`; `Between` needs
      `date_start`+`date_end` (date_end exclusive), `Last` needs
      `trailing_seconds`. Optional `label` names the column.
    - `percentiles`: e.g. `[50, 75]` (default `[50]`).
    - drilldowns (`page_group`, `browser`, `country`, `device_type`, …) and
      `timezone` apply to every cell.
    - `max_combos` (default 24) caps `len(targets) × len(periods) ×
      len(percentiles)` to prevent an accidental fan-out.

    Returns `{app, timezone, drilldowns, table: [{target, kind, period,
    percentile, value, [warning]}], [errors], [corrections]}`. Cells fail
    independently: a failed cell appears in `errors`, the rest still return.
    All calls go through the shared rate limiter (concurrency 3, 100/min).
    """
    from .catalog import resolve_value

    client = _get_client()
    app_name = app or client.registry.default_app
    pcts = percentiles or [50]

    try:
        targets: list[tuple[str, str]] = [("metric", m) for m in (metrics or [])]
        targets += [("timer", t) for t in (timers or [])]
        if not targets:
            raise ValidationError("Provide at least one of 'metrics' or 'timers'.")
        if not periods:
            raise ValidationError("Provide at least one period in 'periods'.")

        combos = len(targets) * len(periods) * len(pcts)
        if combos > max_combos:
            raise ValidationError(
                f"{combos} combinations exceed max_combos={max_combos}.",
                hint="Reduce metrics/periods/percentiles, or raise max_combos.",
            )

        corrections: list[str] = []
        resolved: list[tuple[str, str]] = []
        for kind, name in targets:
            status, payload = resolve_value(kind, name, "timers-metrics")
            if status == "unknown":
                hint = (
                    f"Did you mean: {', '.join(payload)}?"
                    if payload
                    else "Call describe_query('timers-metrics') for valid names."
                )
                raise ValidationError(
                    f"Unknown {kind} {name!r}.", hint=hint
                )
            canonical = payload if status == "ok" else name
            if status == "ok" and canonical != name:
                corrections.append(f"{kind} {name!r} auto-corrected to {canonical!r}")
            resolved.append((kind, canonical))

        built_periods = [_build_period(p) for p in periods]
    except MpulseError as exc:
        return {"error": type(exc).__name__, "message": exc.user_message()}

    drilldowns = _collect_drilldowns(
        {
            "page_group": page_group,
            "browser": browser,
            "ab_test": ab_test,
            "country": country,
            "device_type": device_type,
            "beacon_type": beacon_type,
        }
    )

    async def _cell(
        kind: str, name: str, plabel: str, pwire: dict[str, Any], pct: int
    ) -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {**drilldowns, **pwire, kind: name, "percentile": pct}
        if timezone:
            params["timezone"] = timezone
        try:
            data = await client.query(
                app=app, query_type="timers-metrics", params=params
            )
        except MpulseError as exc:
            return (
                "error",
                {
                    "target": name,
                    "kind": kind,
                    "period": plabel,
                    "percentile": pct,
                    "message": exc.user_message(),
                },
            )
        value, warn = _extract_latest(data, name)
        row: dict[str, Any] = {
            "target": name,
            "kind": kind,
            "period": plabel,
            "percentile": pct,
            "value": value,
        }
        if warn:
            row["warning"] = warn
        return ("row", row)

    cells = [
        _cell(kind, name, plabel, pwire, pct)
        for (kind, name) in resolved
        for (plabel, pwire) in built_periods
        for pct in pcts
    ]
    results = await asyncio.gather(*cells)

    table = [r for (tag, r) in results if tag == "row"]
    errors = [r for (tag, r) in results if tag == "error"]

    out: dict[str, Any] = {
        "app": app_name,
        "query_type": "aggregate(timers-metrics.latest)",
        "timezone": timezone or "UTC",
        "drilldowns": drilldowns,
        "percentiles": pcts,
        "targets": [name for (_, name) in resolved],
        "table": table,
    }
    if corrections:
        out["corrections"] = corrections
    if errors:
        out["errors"] = errors
    return out


# ===========================================================================
# Meta tools (anti-hallucination)
# ===========================================================================
@mcp.tool()
def list_apps() -> dict[str, Any]:
    """List registered mPulse apps and the default app.

    Use this before other tools so you reference real app names. The `app`
    argument on every tool is optional and falls back to the default.
    """
    try:
        reg = _get_registry()
    except MpulseError as exc:
        return {"error": type(exc).__name__, "message": exc.user_message()}
    return {
        "default_app": reg.default_app,
        "apps": [
            {"name": a.name, "tenant": a.tenant} for a in reg.apps.values()
        ],
    }


@mcp.tool()
def list_custom_dimensions(app: str | None = None) -> dict[str, Any]:
    """List the custom dimensions configured for an app (or the default app).

    mPulse has **no API to discover custom dimensions**, so they are declared in
    the app registry. Each entry's key is the exact **wire label** to use:
    - as a split: `metrics-by-dimension` with `dimension=<label>`
    - as a filter: `custom-dimension-<label>=<value>` on any query

    Use this before querying a custom dimension so you use the exact name.
    Returns `{app, custom_dimensions: [{label, display, description}]}`. An empty
    list means none are configured (not necessarily that none exist).
    """
    try:
        reg = _get_registry()
        app_cfg = reg.get(app)
    except MpulseError as exc:
        return {"error": type(exc).__name__, "message": exc.user_message()}
    return {
        "app": app_cfg.name,
        "custom_dimensions": [
            {
                "label": label,
                "display": meta.get("display", label),
                "description": meta.get("description", ""),
                **({"values": meta["values"]} if meta.get("values") else {}),
            }
            for label, meta in app_cfg.custom_dimensions.items()
        ],
    }


@mcp.tool()
def list_query_types() -> dict[str, Any]:
    """List the mPulse query-types this server knows about, with one-line summaries.

    The explicit tools cover: summary→`get_summary`, histogram→`get_histogram`,
    by-minute→`get_timers`, timers-metrics→`get_metrics`. Everything else is
    reachable via the generic `query` tool.
    """
    return {
        "query_types": [
            {"slug": m.slug, "summary": m.summary} for m in QUERY_TYPES.values()
        ],
        "date_comparators": list(KNOWN_DATE_COMPARATORS),
    }


@mcp.tool()
def describe_query(query_type: str, app: str | None = None) -> dict[str, Any]:
    """Describe a query-type: parameters, drilldowns, response shape, and caveats.

    Pass a slug from `list_query_types` (e.g. `summary`, `timers-metrics`,
    `geography`). Pass `app` to also include that app's configured **custom
    dimensions** (usable as a `metrics-by-dimension` split or a
    `custom-dimension-<label>` filter).
    """
    meta = describe_qt(query_type)
    if meta is None:
        return {
            "error": "ValidationError",
            "message": f"Unknown query-type '{query_type}'.",
            "known": list(QUERY_TYPES.keys()),
        }
    result: dict[str, Any] = {
        "slug": meta.slug,
        "summary": meta.summary,
        "parameters": meta.all_params(),
        "value_parameters": list(meta.value_params),
        "supports_time_selection": meta.supports_time,
        "supports_drilldown": meta.supports_drilldown,
        "response_shape": meta.response_shape or "(varies)",
        "date_comparators": list(KNOWN_DATE_COMPARATORS),
        "notes": meta.notes,
        "constraints": (
            "One calendar day per query; per-minute aggregation; timezone-"
            "dependent. Unsupported drilldown combinations return empty data."
        ),
    }
    # Enrich with authoritative enum hints (valid metrics/timers/dimensions,
    # gotchas) from catalog.json. Fail-safe: returns {} if catalog unavailable.
    from .catalog import enrich_describe

    result.update(enrich_describe(query_type))

    # App-specific custom dimensions (mPulse has no API to discover these).
    if app is not None:
        try:
            app_cfg = _get_registry().get(app)
            if app_cfg.custom_dimensions:
                result["app_custom_dimensions"] = [
                    {"label": label, "display": m.get("display", label)}
                    for label, m in app_cfg.custom_dimensions.items()
                ]
        except MpulseError:
            pass  # describe is best-effort; a bad app name shouldn't break it
    return result


def main() -> None:
    """Console-script entry point. Runs the MCP server over stdio."""
    log.info("Starting mpulse-mcp v%s (stdio transport)", __version__)
    # Load a local .env (if any) before reading credentials. Existing env vars
    # (e.g. Claude Desktop's env block) take precedence.
    from .config import load_env_files

    load_env_files()
    # Validate config early so misconfiguration surfaces on stderr at startup
    # rather than on the first tool call. Failure here is non-fatal: the meta/
    # tool errors will still explain the problem to the client.
    try:
        _get_registry()
    except MpulseError as exc:
        log.error("Configuration problem at startup: %s", exc.user_message())
    mcp.run()


if __name__ == "__main__":
    main()
