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
        if query_type in ("metrics-by-dimension", "metric-per-page-load-time"):
            return _mbd_metrics()
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

    # metric enums differ per endpoint (CamelCase vs snake_case)
    if query_type in ("timers-metrics", "dimension-over-time", "dimensions-over-time"):
        out["valid_metrics"] = _tm_metrics()
    if query_type in (
        "metrics-by-dimension",
        "metric-per-page-load-time",
    ):
        out["valid_metrics"] = _mbd_metrics()

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

    # dimension enums (context-specific)
    if query_type == "dimension-values":
        out["valid_dimensions"] = list(
            dims.get("dimension_values__dimension_enum", {}).get("enum", [])
        )
    if query_type in (
        "metrics-by-dimension",
        "dimension-over-time",
        "dimensions-over-time",
    ):
        out["valid_dimensions"] = list(
            dims.get("metrics_by_dimension__dimension_split_enum", {}).get(
                "enum", []
            )
        )

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

    return out
