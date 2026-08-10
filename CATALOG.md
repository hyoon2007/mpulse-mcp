# mPulse Query API — Reference Catalog

`src/mpulse_mcp/catalog.json` + `src/mpulse_mcp/catalog.py`.

Purpose: pin down the valid metric/timer/dimension names and behavioral rules up
front so the agent doesn't re-discover them by trial and error on every call.
Sources: Akamai TechDocs endpoint reference pages + Akamai cli-mpulse + live API
verification (app=app-a).

## Layout

- **`catalog.json`** — the authoritative enum/rule data (machine-readable).
  Bundled into the hatchling wheel automatically.
- **`catalog.py`** — a fail-safe loader. Returns `{}` if the JSON is missing or
  malformed, so it never affects server operation.
- **`describe_query` in `server.py`** — merges `catalog.enrich_describe(query_type)`
  into its response. It now includes, per query type:
  - `valid_metrics` (the name list appropriate to the endpoint)
  - `valid_timers`
  - `valid_dimensions`
  - `dimension_value_examples`, `gotchas`, `metric_name_differs_by_endpoint`
  - `endpoint_params` (per-endpoint value/special params + constraints) and
    `parameter_mechanics` (comma vs repeat-key vs single)

So a single `describe_query("timers-metrics")` returns the 98 metrics, 24 timers,
and the list of gotchas.

## The most important gotcha — same metric, different name per endpoint

| Concept | `timers-metrics` (CamelCase) | `metrics-by-dimension` (snake_case) |
|---|---|---|
| Requests per page | `TotalRequestCount` * | `asset_requests_per_page` |
| Decoded Body Size | `TotalDecodedBodySize` | `asset_decoded_body_size` |
| Transfer Size | `TotalTransferSize` | `asset_transfer_size` |

`*` `TotalRequestCount` is not in the timers-metrics doc enum (the Total* group
lists 8, matching the doc's "97") yet works live (verified: June p75=142,
July=151). Recorded separately as `verified_live_extra` in `catalog.json`.

## Enum summary (full lists in catalog.json)

- **timers-metrics `metric`** — 97 CamelCase names (+`TotalRequestCount`).
  Prefixes `Bcn/Css/Font/Html/Img/Js/Other/Page/Total/Xhr` × suffixes
  `RequestCount/TransferSize/DecodedBodySize/CompressionRatio/…`.
- **metrics-by-dimension metric param = `metrics` (PLURAL, comma-separated)** —
  82 snake_case names (`asset_*`, `css_*`, `js_*`, `image_*`, `font_*`, `html_*`,
  `xhr_*`, `other_*`, `page_*`, `beacon_*` families). Example:
  `metrics=beacons,largest_contentful_paint`. **The singular `metric` is silently
  ignored** and app-default columns are returned. Supports `percentile`
  computation and has native `sortby`/`limit`. Response is a
  `{columnNames, data:[[row]]}` table.
- **metric-per-page-load-time `metric`** — single-value only, default
  `BounceRate`, enum `BounceRate` + `CustomMetric0-9` (a completely different name
  space from the other endpoints).
- **timer** — 24 values (`PageLoad` default … `LargestContentfulPaint`,
  `TotalBlockingTime`, `CustomTimer[0-9]`).
- **dimension-values `dimension`** — 24 values (underscore). `connection_type` is
  absent → `connection-type` gives a 400 on dimension-values.
- **metrics-by-dimension split `dimension`** — 33 values (underscore).
- **beacon-type** 9 values (`page view`, `xhr`, `spa_hard`, `spa`, …).
  **device-type** `Mobile/Desktop/Tablet` (+ live `(No Value)`). **bandwith-block**
  0–6/.NONE.

## Behavioral rules (gotchas)

- An invalid `timer`/`metric` → **silently falls back to PageLoad** with no error.
  Check that the response series `id` matches the requested name.
- An invalid `custom-timer` → **400**.
- **Multi-value mechanism (differs per parameter)**: (1) only `metrics`
  (metrics-by-dimension) is a **single comma-separated param**. (2) `metric`/`timer`
  (timers-metrics) and **all drilldown filters and `custom-dimension-*`** use a
  **repeated key** (`country=US&country=KR`) — via the MCP `query` tool, pass a
  **JSON list** and the client serializes it to repeated keys
  (`custom-dimension-branch=["uk","us"]`, verified live). (3) everything else is
  single.
- metrics-by-dimension **computes percentiles** (timer p75 by dimension matches
  timers-metrics). Mixing a count metric (beacons) with a percentile can yield a
  null column → keep timer/percentile metrics together.
- **`latest` = the whole-period aggregate**. Querying a percentile metric with
  `Between` makes `latest` the period-wide percentile → read `latest` for a
  monthly p75/p50, not the mean of the daily history.
- `Between`'s `date-end` is **exclusive** (the doc only says "greater than").
  Long ranges auto-coarsen the buckets.
- Rate limits: concurrent 3 / 100 per minute / 10,000 per hour / 50,000 per day.

## Bandwidth filter parameter spelling — `bandwith-block` (confirmed live, fixed in code)

mPulse's actual wire parameter is **`bandwith-block`** (a typo missing the `d`);
the correctly spelled `bandwidth-block` **does not work** (verified live by the app
owner). The code was updated accordingly:
- `query_types.DRILLDOWN_PARAMS`: `bandwidth-block` → `bandwith-block`
- `server._DRILLDOWN_ARG_TO_WIRE`: keeps the friendly arg name `bandwidth_block`
  but maps the wire value to `bandwith-block`

## Using custom dimensions

Customer-defined custom dimensions are not in the built-in enums, and **there is no
API to list them, so the user must supply the exact name**.

- As a **filter**: `custom-dimension-<label>=<value>` — e.g. to filter to
  `branch=uk`, use `custom-dimension-branch=uk`.
- As a **split**: on metrics-by-dimension, `dimension=<custom_name>` — e.g. to break
  a metric down by branch, use `dimension=branch`.
- `<label>` is the custom dimension name lowercased, spaces replaced with `_`.
  Beacons with no value match on `.NONE`.

So the 33 names in `metrics_by_dimension__dimension_split_enum` are built-ins only;
a custom dimension name is also valid directly as the `dimension` value.

## Still missing — custom metrics/timers/custom-dimensions

The enums above are all **built-in**. An app's own custom items are not in the
public docs and appear only in:
- the Repository/Objects API:
  `getRepositoryDomain(token, appName=...)["custom_metrics"]`
- or the dashboard report-builder's `<option>` list.

Once obtained, merge them into `catalog.json` as a section like
`custom_metrics_by_app`, and append them per app in `catalog.enrich_describe`.

## Maintenance

- To update an enum, edit only `catalog.json` (no code change). The loader is
  `@lru_cache`, so changes apply on process restart.
- Verify: `python -c "from mpulse_mcp import catalog; print(len(catalog.enrich_describe('timers-metrics')['valid_metrics']))"` → 98.
