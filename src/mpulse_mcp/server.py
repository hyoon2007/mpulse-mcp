"""FastMCP server exposing mPulse Query API tools over stdio.

Entry point: the ``mpulse-mcp`` console script → :func:`main`.

stdio safety: this module never prints to stdout. FastMCP owns the stdout
JSON-RPC stream; all logging goes to stderr (see ``mpulse_mcp.log``).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__, log
from .client import MpulseClient
from .config import Registry, load_registry
from .errors import MpulseError, ValidationError
from .query_types import (
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
    return result


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
    note rather than an error.

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

    client = _get_client()
    app_name = app or client.registry.default_app
    try:
        data = await client.query(app=app, query_type=query_type, params=wire)
    except MpulseError as exc:
        return {"error": type(exc).__name__, "message": exc.user_message()}

    wire.setdefault("format", "json")
    return normalize(
        app=app_name,
        query_type=query_type,
        request_params=wire,
        aggregation="unspecified",
        data=data,
        raw=raw,
        history_mode=history_mode,
    )


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
def describe_query(query_type: str) -> dict[str, Any]:
    """Describe a query-type: parameters, drilldowns, response shape, and caveats.

    Pass a slug from `list_query_types` (e.g. `summary`, `timers-metrics`,
    `geography`).
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
