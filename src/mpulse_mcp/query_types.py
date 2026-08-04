"""Query-type and parameter metadata, confirmed against the mPulse docs.

Sources (fetched during implementation):
* /concerto/mpulse/api/v2/<api-key>/<query-type>?<params>   (URL format)
* get-summary, get-histogram, get-by-minute, get-timers-metrics reference pages.

These tables power the meta tools (``list_query_types``, ``describe_query``) and
the client's parameter name mapping. They are intentionally descriptive, not
exhaustive validators: unknown-but-well-formed params are still passed through
so new mPulse features work without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Drilldown / dimension filter parameters shared by most query-types. The values
# are the *wire* parameter names (hyphenated) exactly as mPulse expects them.
DRILLDOWN_PARAMS: tuple[str, ...] = (
    "page-group",
    "browser",
    "browser-family",
    "ab-test",
    "country",
    "region",
    "device-type",
    "device-model",
    "device-manufacturer",
    "os",
    "os-family",
    "connection-type",
    "isp",
    "bandwith-block",  # NB: mPulse API misspells 'bandwidth' (verified live)
    "beacon-type",
    "site-version",
)

# Time-selection parameters common to time-series/summary query-types.
TIME_PARAMS: tuple[str, ...] = (
    "date",  # YYYY-MM-DD, single calendar day
    "date-comparator",  # Last30Minutes | LastHour | Last24Hours | ThisWeek | ...
    "trailing-seconds",  # with date-comparator=Last
    "date-start",  # with date-comparator=Between
    "date-end",
    "timezone",  # Java-parseable TZ id, default UTC
)

# Relative date-comparator values observed in the docs (not exhaustive; mPulse
# may accept more). Surfaced to the LLM via describe_query.
KNOWN_DATE_COMPARATORS: tuple[str, ...] = (
    "Last30Minutes",
    "LastHour",
    "Last3Hours",
    "Last6Hours",
    "Last12Hours",
    "Last24Hours",
    "Today",
    "Yesterday",
    "ThisWeek",
    "ThisMonth",  # current month to date (docs-confirmed)
    "Last7Days",
    "Last",  # requires trailing-seconds
    "Between",  # requires date-start + date-end
)


@dataclass(frozen=True)
class QueryTypeMeta:
    slug: str
    summary: str
    value_params: tuple[str, ...] = ()  # timer/metric/percentile-style params
    supports_time: bool = True
    supports_drilldown: bool = True
    notes: str = ""
    response_shape: str = ""

    def all_params(self) -> list[str]:
        params: list[str] = list(self.value_params)
        if self.supports_time:
            params += list(TIME_PARAMS)
        if self.supports_drilldown:
            params += list(DRILLDOWN_PARAMS)
        # custom-dimension-{label} is dynamic; represent generically.
        params.append("custom-dimension-{label}")
        params.append("format")
        return params


QUERY_TYPES: dict[str, QueryTypeMeta] = {
    "summary": QueryTypeMeta(
        slug="summary",
        summary="Aggregate stats for a timer: median, margin of error, count, p95/p98.",
        value_params=("timer", "custom-timer", "percentile"),
        response_shape="{median, moe, n, p95, p98}",
    ),
    "histogram": QueryTypeMeta(
        slug="histogram",
        summary="Distribution (buckets) for a timer over the period.",
        value_params=("timer", "custom-timer", "percentile"),
        response_shape=(
            "{chartTitle, datasetName, series:{series:[{name, aPoints:[{s,e,c}], "
            "buckets, median, p95, p98, kValue}]}}"
        ),
    ),
    "by-minute": QueryTypeMeta(
        slug="by-minute",
        summary="A single timer's value by minute over time (time series).",
        value_params=("timer", "custom-timer", "percentile"),
        response_shape=(
            "{chartTitle, series:{series:[{name, aPoints:[{x,y,label,userdata}], "
            "pointCount, statistics:{yMin,yMax,ySum,yAvg}}]}}"
        ),
        notes="Use for one timer. For multiple timers/metrics use 'timers-metrics'.",
    ),
    "timers-metrics": QueryTypeMeta(
        slug="timers-metrics",
        summary="Values for multiple timers or metrics by minute over time.",
        value_params=("timer", "metric", "custom-timer", "percentile"),
        response_shape="{dataTimeZone, values:[{id, history:[int], latest:int}]}",
        notes="metric defaults to 'Beacons', timer to 'PageLoad'.",
    ),
    "dimension-values": QueryTypeMeta(
        slug="dimension-values",
        summary="All observed values for a given dimension.",
        value_params=("dimension",),
    ),
    "dimension-over-time": QueryTypeMeta(
        slug="dimension-over-time",
        summary="Top dimension values across time.",
        value_params=("dimension", "timer", "metric"),
    ),
    "metrics-by-dimension": QueryTypeMeta(
        slug="metrics-by-dimension",
        summary="Metric values broken down by a dimension.",
        value_params=("metric", "dimension"),
    ),
    "metric-per-page-load-time": QueryTypeMeta(
        slug="metric-per-page-load-time",
        summary="A metric segmented by page-load-time ranges.",
        value_params=("metric",),
    ),
    "sessions-per-page-load-time": QueryTypeMeta(
        slug="sessions-per-page-load-time",
        summary="Session counts segmented by page-load-time ranges.",
    ),
    "app-error-summary": QueryTypeMeta(
        slug="app-error-summary",
        summary="Aggregated application errors (messages, codes, sources, types).",
    ),
    "page-groups": QueryTypeMeta(
        slug="page-groups",
        summary="Beacon counts per page group.",
    ),
    "browsers": QueryTypeMeta(
        slug="browsers",
        summary="Beacon counts per browser.",
    ),
    "ab-tests": QueryTypeMeta(
        slug="ab-tests",
        summary="Beacon counts per A/B test.",
    ),
    "bandwidth": QueryTypeMeta(
        slug="bandwidth",
        summary="Beacon counts per bandwidth block.",
    ),
    "geography": QueryTypeMeta(
        slug="geography",
        summary="Beacon counts per country/geography.",
    ),
}


def is_known(slug: str) -> bool:
    return slug in QUERY_TYPES


def describe(slug: str) -> QueryTypeMeta | None:
    return QUERY_TYPES.get(slug)
