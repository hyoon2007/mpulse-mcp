"""Authoritative mPulse enum catalog loader (anti-hallucination helper).

Loads ``catalog.json`` (metric/timer/dimension enums + behavioural gotchas,
compiled from the Akamai endpoint reference pages and verified against the live
API) and exposes catalog-derived *valid value* hints for ``describe_query``.

Design goals:
* **Additive & fail-safe** — any load/parse error yields empty data, so the
  server behaves exactly as before if the JSON is missing or malformed.
* **No import-time side effects** — the file is read lazily and cached.

The catalog covers BUILT-IN names only. App-specific *custom* metrics/timers
must come from the Repository/Objects API (``domain['custom_metrics']``) or the
dashboard report-builder; see ``_meta.custom_metric_source`` in the JSON.
"""

from __future__ import annotations

import difflib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_CATALOG_PATH = Path(__file__).with_name("catalog.json")

# Timer enum carries a placeholder pattern entry, e.g. "CustomTimer[0-9]".
_PLACEHOLDER_SUFFIX = "[0-9]"
_CUSTOM_TIMER_RE = re.compile(r"^CustomTimer[0-9]$", re.IGNORECASE)


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    """Return the parsed catalog, or ``{}`` if it can't be read/parsed."""
    try:
        return json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:  # never break the server over reference data
        return {}


def _tm_metrics() -> list[str]:
    c = load().get("metrics_for_timers_metrics__metric_param", {})
    return list(c.get("enum", [])) + list(c.get("verified_live_extra", []))


def _mbd_metrics() -> list[str]:
    return list(
        load().get("metrics_for_metrics_by_dimension__metric_param", {}).get(
            "enum", []
        )
    )


def _mplt_metrics() -> list[str]:
    return list(
        load().get("metrics_for_metric_per_page_load_time__metric_param", {}).get(
            "enum", []
        )
    )


def _timers() -> list[str]:
    return list(load().get("timers_for_timer_param", {}).get("enum", []))


def _dimensions() -> dict[str, Any]:
    return load().get("dimensions", {})


def date_comparators() -> list[str]:
    """Docs-confirmed date-comparator values (may be empty)."""
    return list(load().get("common_params", {}).get("date_comparator", {}).get(
        "docs_enum", []
    ))


# --- Name resolution (auto-correct + suggest) ------------------------------
def _norm(s: str) -> str:
    """Casing/separator-insensitive key: 'largest_contentful_paint' == 'LargestContentfulPaint'."""
    return "".join(c for c in s.lower() if c.isalnum())


def _enum_for(kind: str, query_type: str) -> list[str]:
    """The catalog enum a timer/metric value should be checked against.

    The metric name space is endpoint-specific (CamelCase for timers-metrics,
    snake_case for metrics-by-dimension), so the query-type selects the enum.
    """
    if kind == "timer":
        return _timers()
    if kind == "metric":
        if query_type == "metrics-by-dimension":
            return _mbd_metrics()
        if query_type == "metric-per-page-load-time":
            return _mplt_metrics()  # distinct: BounceRate + CustomMetric0-9
        return _tm_metrics()  # timers-metrics, dimension-over-time, default
    return []


def resolve_value(kind: str, name: Any, query_type: str) -> tuple[str, Any]:
    """Resolve a ``timer``/``metric`` value against the catalog.

    Returns ``(status, payload)``:

    * ``("ok", canonical)`` — matched; ``canonical`` may differ from ``name``
      only in casing/separators (auto-correctable).
    * ``("unknown", [suggestions])`` — catalog present but no confident match;
      payload is up to 3 close candidates (possibly empty).
    * ``("skip", None)`` — nothing to check: empty/non-str value, catalog
      unavailable, unknown endpoint, or a ``CustomTimer[0-9]`` value.

    This is intentionally scoped to the two names that mPulse silently falls
    back on; custom timers/dimensions are never validated.
    """
    if not name or not isinstance(name, str):
        return ("skip", None)
    if kind == "timer" and _CUSTOM_TIMER_RE.match(name):
        return ("ok", name)

    enum = [e for e in _enum_for(kind, query_type) if not e.endswith(_PLACEHOLDER_SUFFIX)]
    if not enum:  # catalog missing or endpoint has no enum -> stay permissive
        return ("skip", None)

    norm_map = {_norm(e): e for e in enum}
    hit = norm_map.get(_norm(name))
    if hit is not None:
        return ("ok", hit)

    suggestions = difflib.get_close_matches(name, enum, n=3, cutoff=0.5)
    if not suggestions:
        suggestions = [
            norm_map[m]
            for m in difflib.get_close_matches(_norm(name), list(norm_map), n=3, cutoff=0.5)
        ]
    return ("unknown", suggestions)


def custom_dimension_supported(query_type: str) -> bool | None:
    """Does ``query_type`` accept a CUSTOM dimension for its ``dimension`` param?

    True (metrics-by-dimension), False (dimension-values, dimension-over-time),
    or None when the catalog is unavailable / the query-type isn't a dimension
    endpoint.
    """
    support = load().get("custom_dimension_support", {}).get("by_query_type", {})
    if query_type == "dimensions-over-time":  # tolerate the plural alias
        query_type = "dimension-over-time"
    return support.get(query_type)


def _dimension_enum_for(query_type: str) -> list[str]:
    dims = _dimensions()
    dv = list(dims.get("dimension_values__dimension_enum", {}).get("enum", []))
    mbd = list(dims.get("metrics_by_dimension__dimension_split_enum", {}).get("enum", []))
    dot = list(dims.get("dimension_over_time__dimension_enum", {}).get("enum", []))
    if query_type == "dimension-values":
        return dv
    if query_type in ("dimension-over-time", "dimensions-over-time"):
        # Prefer the dedicated dot enum; fall back to the union to avoid false
        # rejections if it's ever absent.
        return dot or list(dict.fromkeys(dv + mbd))
    if query_type == "metrics-by-dimension":
        return mbd
    return []


def resolve_dimension(name: Any, query_type: str) -> tuple[str, Any]:
    """Resolve a ``dimension`` value, honoring per-endpoint custom-dimension rules.

    Returns ``(status, payload)``:

    * ``("ok", canonical)`` — matched a built-in (casing/separator corrected).
    * ``("ok_custom", name)`` — not a built-in, but this endpoint accepts custom
      dimensions (metrics-by-dimension) → pass through unchanged.
    * ``("unknown_no_custom", [suggestions])`` — not a built-in and this endpoint
      does NOT accept custom dimensions (dimension-values / dimension-over-time)
      → the caller should reject before spending an upstream call.
    * ``("skip", None)`` — empty value, catalog unavailable, or non-dimension
      query-type.
    """
    if not name or not isinstance(name, str):
        return ("skip", None)
    supports_custom = custom_dimension_supported(query_type)
    enum = _dimension_enum_for(query_type)
    if supports_custom is None or not enum:
        return ("skip", None)

    norm_map = {_norm(e): e for e in enum}
    hit = norm_map.get(_norm(name))
    if hit is not None:
        return ("ok", hit)
    if supports_custom:
        return ("ok_custom", name)
    suggestions = difflib.get_close_matches(name, enum, n=3, cutoff=0.5)
    if not suggestions:
        suggestions = [
            norm_map[m]
            for m in difflib.get_close_matches(_norm(name), list(norm_map), n=3, cutoff=0.5)
        ]
    return ("unknown_no_custom", suggestions)


def enrich_describe(query_type: str) -> dict[str, Any]:
    """Catalog-derived valid-value hints for ``describe_query`` output.

    Returns an empty dict when the catalog is unavailable or the query-type has
    no relevant enums, so callers can safely ``result.update(enrich_describe(...))``.
    """
    catalog = load()
    if not catalog:
        return {}

    out: dict[str, Any] = {}
    dims = _dimensions()

    # metric enums differ per endpoint (CamelCase vs snake_case vs its own set)
    if query_type in ("timers-metrics", "dimension-over-time", "dimensions-over-time"):
        out["valid_metrics"] = _tm_metrics()
    if query_type == "metrics-by-dimension":
        out["valid_metrics"] = _mbd_metrics()
        mbd = catalog.get("metrics_for_metrics_by_dimension__metric_param", {})
        out["metric_param_name"] = mbd.get("param_name", "metrics")
        out["metric_note"] = mbd.get("metric_singular_ignored", "")
    if query_type == "metric-per-page-load-time":
        out["valid_metrics"] = _mplt_metrics()  # distinct: BounceRate + CustomMetric0-9

    # timer enum (timer-family query-types)
    if query_type in (
        "summary",
        "histogram",
        "by-minute",
        "timers-metrics",
        "dimension-over-time",
        "dimensions-over-time",
    ):
        out["valid_timers"] = _timers()

    # dimension enums (context-specific; each endpoint has its OWN valid set)
    if query_type == "dimension-values":
        out["valid_dimensions"] = list(
            dims.get("dimension_values__dimension_enum", {}).get("enum", [])
        )
    if query_type == "metrics-by-dimension":
        out["valid_dimensions"] = list(
            dims.get("metrics_by_dimension__dimension_split_enum", {}).get("enum", [])
        )
    if query_type in ("dimension-over-time", "dimensions-over-time"):
        out["valid_dimensions"] = _dimension_enum_for(query_type)

    # Whether a CUSTOM dimension (e.g. 'branch') is accepted for the `dimension`
    # param — true only for metrics-by-dimension. Surfacing this stops the model
    # from wasting a call on dimension-values/dimension-over-time with a custom
    # name (which return empty/400).
    supports_custom = custom_dimension_supported(query_type)
    if supports_custom is not None:
        cds = catalog.get("custom_dimension_support", {})
        out["custom_dimension_supported"] = supports_custom
        out["custom_dimension_note"] = cds.get("note", "")
        if not supports_custom:
            out["custom_dimension_redirect"] = cds.get("redirect", "")

    # broadly-useful reference bits
    ev = dims.get("enumerated_values", {})
    if ev:
        out["dimension_value_examples"] = {
            k: v.get("values") for k, v in ev.items() if isinstance(v, dict)
        }
    gotchas = catalog.get("behavioral_gotchas", {}).get("items", [])
    if gotchas:
        out["gotchas"] = gotchas
    naming = catalog.get(
        "CRITICAL_same_metric_different_name_per_endpoint", {}
    )
    if naming.get("mapping"):
        out["metric_name_differs_by_endpoint"] = naming["mapping"]

    custom_dims = catalog.get("custom_dimensions")
    if custom_dims:
        out["custom_dimensions"] = custom_dims

    # Per-endpoint parameter contract: value params, special params (limit,
    # sortby, interval, series-format), whether drilldown filters apply, and
    # constraints. Compiled from each get-*.md reference page.
    ep = catalog.get("endpoint_params", {}).get(query_type)
    if isinstance(ep, dict):
        out["endpoint_params"] = {
            k: v for k, v in ep.items() if not k.startswith("_")
        }

    # How to pass multiple values (comma vs repeated key vs single) — the whole
    # point of this: the model must know the per-param mechanism to set values.
    mech = catalog.get("parameter_mechanics")
    if mech:
        entry: dict[str, Any] = {
            "how_to_pass": mech.get("how_to_pass", ""),
            "multiple_via_repeated_key": mech.get("multiple_via_repeated_key", []),
        }
        comma = mech.get("comma_separated_in_one_param", {}).get(query_type)
        if comma:
            entry["comma_separated_params"] = comma
        out["parameter_mechanics"] = entry

    return out
