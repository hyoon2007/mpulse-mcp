# mpulse-mcp

An MCP (Model Context Protocol) server that wraps the **Akamai mPulse Query
API v2** and exposes it as tools for Claude Desktop (local **stdio**). It fetches
mPulse RUM (Real User Monitoring) aggregate data — summaries, histograms, and
per-minute time series — and returns **loss-free** numbers suitable for
downstream p75 / statistical analysis.

- **Accuracy first** — endpoints, parameters, and schemas were confirmed against
  the live mPulse docs before implementation (see
  [Verified API contract](#verified-api-contract)).
- **Loss-free data** — numeric values are never rounded, summarized, or dropped.
- **stdio-safe** — nothing is ever written to stdout (that would corrupt the
  JSON-RPC stream); all logs go to **stderr**. Secrets are masked in logs.

---

## Verified API contract

Fetched from `techdocs.akamai.com/mpulse` during implementation. The table below
records what was confirmed and any **differences from the original brief's
assumed defaults**.

| Item | Confirmed value |
|---|---|
| Query base host | `https://mpulse.soasta.com` ✅ matches |
| Query URL format | `/concerto/mpulse/api/v2/<api-key>/<query-type>?<params>` ✅ matches |
| Auth header | **`Authentication: <token>`** (not `Authorization`) ✅ matches |
| Common param | `format=json` (the default `legacy` format is deprecated) ✅ |
| Token endpoint | **`PUT /concerto/services/rest/RepositoryService/v1/Tokens`** |
| Token request body | `{ "apiToken": "<pre-issued>", "tenant": "<tenant>" }` (API-token/SSO flow; no username/password) |
| Token response | `{ "token": "<security token>" }` |
| Token lifetime | **expires after 5 hours of inactivity** |
| Data characteristics | per-minute aggregation, **one calendar day per query**, timezone-dependent |

**Confirmed query-type slugs → explicit tools**

| Tool | query-type slug | Response core |
|---|---|---|
| `get_summary` | `summary` | `median, moe, n, p95, p98` |
| `get_histogram` | `histogram` | `series.series[].buckets/aPoints, median, p95, p98` |
| `get_timers` | `by-minute` | `series.series[].aPoints[{x,y}], statistics` (single timer) |
| `get_metrics` | `timers-metrics` | `dataTimeZone, values[{id, history[], latest}]` (multiple) |

Other confirmed slugs (reachable via the generic `query` tool):
`dimension-values`, `dimension-over-time`, `metrics-by-dimension`,
`metric-per-page-load-time`, `sessions-per-page-load-time`, `app-error-summary`,
`page-groups`, `browsers`, `ab-tests`, `bandwidth`, `geography`.

**Confirmed parameters**: `timer` (default `PageLoad`) / `custom-timer`,
`metric` (default `Beacons`), `percentile` (1–99, default 50), `date`
(`YYYY-MM-DD`), `date-comparator` (`Last30Minutes`, `LastHour`, `Last24Hours`,
`ThisWeek`, `Last` + `trailing-seconds`, `Between` + `date-start`/`date-end`),
`timezone` (Java TZ id, default UTC).

**Confirmed drilldown params**: `page-group`, `browser`, `browser-family`,
`ab-test`, `country` (ISO alpha-2), `region`, `device-type`, `device-model`,
`device-manufacturer`, `os`, `os-family`, `connection-type`, `isp`,
`bandwidth-block`, `beacon-type`, `site-version`, `custom-dimension-{label}`.
**Unsupported drilldown combinations return empty data, not an error** — the
server flags this in the result `note`.

### Differences from the brief's assumed defaults

1. **`date_comparator` → wire name `date-comparator`** (hyphen). Tool arguments
   use underscores and are mapped to the hyphenated wire names automatically.
2. **Token lifetime is "5 hours of inactivity"**, not a very-short token. The
   server still refreshes proactively (soft 4h TTL) and reactively on a query
   `401`, so correctness does not depend on the exact figure.
3. **Rate-limit numbers (concurrent 3 / 100 per-min / 10k per-hour / 50k
   per-day) could not be re-confirmed** from the doc pages fetched. They are
   adopted from the brief and kept **configurable** as constants in
   `client.py` (`MAX_CONCURRENCY`, `PER_MINUTE_LIMIT`, retry settings). The
   per-hour/per-day caps are advisory (not separately throttled).

---

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
```

## Configure

Secrets go in the environment; the non-secret app registry goes in a JSON file.

1. **Registry** — copy the example and fill in your api keys / tenant:

   ```bash
   cp mpulse_apps.example.json mpulse_apps.json
   ```

   ```json
   {
     "default_app": "app-a",
     "tenant": "your-tenant",
     "api_token_env": "MPULSE_API_TOKEN",
     "apps": {
       "app-a":  { "api_key": "XXXXX-XXXXX-XXXXX-XXXXX-XXXXX" },
       "app-b": { "api_key": "YYYYY-YYYYY-YYYYY-YYYYY-YYYYY",
                          "tenant": "override-if-different",
                          "api_token_env": "MPULSE_API_TOKEN_APP_B" }
     }
   }
   ```

   Resolution: an app's `tenant` / `api_token_env` override the top-level
   defaults; `api_key` is required per app. The registry path is found via
   `MPULSE_APPS_CONFIG`, else `./mpulse_apps.json`.

   **Custom dimensions (optional).** mPulse has no API to discover an app's
   custom dimensions, so declare them here to make them usable without guessing.
   Add a `custom_dimensions` object per app (top-level entries are inherited by
   every app and merged with the app's own):

   ```json
   "custom_dimensions": { "branch": { "display": "Branch" } },
   "apps": {
     "app-a": {
       "api_key": "…",
       "custom_dimensions": {
         "mobile_speed": { "display": "mobile speed", "description": "effectiveType" },
         "campaign": {}
       }
     },
     "app-b": { "api_key": "…", "custom_dimensions": ["checkout_step"] }
   }
   ```

   Keys are the mPulse **wire label** (lowercased, spaces → `_`); a value may be
   `null`, a `{display, description, values?}` object, or you may pass a plain
   list of names. Used as a split (`metrics-by-dimension dimension=<label>`) or a
   filter (`custom-dimension-<label>=<value>`). These are **hints only** (never
   used to reject), since the declared list may be incomplete.

2. **Secrets** — set the pre-issued mPulse **API token(s)** (mPulse → your name
   → *Account* → *Generate/Revoke API Token*). See `.env.example`:

   ```bash
   MPULSE_API_TOKEN=your-pre-issued-api-token
   # MPULSE_API_TOKEN_APP_B=...   # only if an app overrides api_token_env
   ```

   The short-lived **security token** used on each request is minted at runtime
   from the API token + tenant — you never manage it yourself.

   **Where does `.env` go?** For local runs and the smoke test, place `.env` in
   the project root (the folder with `pyproject.toml`, i.e. the working
   directory). It is auto-loaded at startup — also from the directory of
   `MPULSE_APPS_CONFIG`. Existing environment variables are never overridden.
   **For Claude Desktop, do not use `.env`** — put secrets in the `env` block of
   `claude_desktop_config.json` (below); that is what Claude Desktop injects, and
   it takes precedence over any `.env`.

## Run

```bash
uv run mpulse-mcp        # or: uv run python -m mpulse_mcp
```

Transport is stdio; the process speaks MCP on stdout and logs to stderr.

## Register in Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mpulse": {
      "command": "uv",
      "args": ["--directory", "/Users/hyoon/Projects/mpulse-mcp", "run", "mpulse-mcp"],
      "env": {
        "MPULSE_API_TOKEN": "your-pre-issued-api-token",
        "MPULSE_APPS_CONFIG": "/Users/hyoon/Projects/mpulse-mcp/mpulse_apps.json"
      }
    }
  }
}
```

Restart Claude Desktop; the mPulse tools appear in the tools menu.

## Tools

**Explicit** (typed, validated): `get_summary`, `get_histogram`, `get_timers`
(single timer, per-minute), `get_metrics` (multiple timers/metrics, per-minute).
Each accepts `app` (optional → default app), **exactly one** of `date`
(`YYYY-MM-DD`) or `date_comparator`, optional `timezone`, drilldown filters
(`page_group`, `browser`, `ab_test`, `country`, `device_type`, …), and
`raw` (default `false`).

**Batch**: `get_aggregate(metrics=[...], timers=[...], periods=[...],
percentiles=[50,75], …)` — a `(metric|timer) × period × percentile` matrix in
one call. Collapses the common reporting workflow (e.g. 3 metrics × 2 months ×
p50/p75 = 12 calls) into a small table of `latest` scalars (no history). Each
cell fails independently (`errors[]`); `max_combos` (default 24) caps the
fan-out; all calls share the rate limiter. See [Batch aggregate](#batch-aggregate).

**Generic**: `query(query_type, app=None, params={...}, raw=False)` — any
query-type with wire-name params, for cases the explicit tools don't cover.

**Meta** (anti-hallucination): `list_apps`, `list_query_types`,
`describe_query(query_type, app=None)` (pass `app` to include that app's custom
dimensions), `list_custom_dimensions(app=None)`.

### Output format

- `raw=false` (default): a flat envelope — `app`, `query_type`, `period`,
  `timezone`, `aggregation`, `drilldowns`, `empty`, `history_mode`, and `body`
  (the envelope-stripped data). Empty results are flagged, with a note when an
  unsupported drilldown combination is the likely cause.
- `raw=true`: mPulse's untouched JSON under `data`.

**`history_mode` — per-minute time-series volume (loss-free on demand).** The
temporal query-types `timers-metrics` and `by-minute` return ~1,440 points/day,
of which the common workload uses only the period aggregate. `history_mode`
(on `get_timers`, `get_metrics`, and `query`) controls this:

| mode | time series | always kept | ~size |
|---|---|---|---|
| `downsample` (**default**) | ≤60 even-spaced points + `peak` | `latest`/`statistics`, `n_points` | ~1/20 |
| `none` | dropped (endpoints only) | `latest`/`statistics`, `n_points`, `peak` | ~1/100 |
| `full` | every minute (loss-free) | everything | full |

The complete series is always recoverable via `history_mode="full"` or
`raw=true`. **Aggregates are never reduced**: `latest` (period value — read this
for monthly p75, don't average the history), mPulse `statistics`, `histogram`
buckets, and `summary` are kept in full regardless of mode. A `peak`
(`{index/x, value/y}`) is added so "when did it spike?" survives downsampling.

**`timer`/`metric` name resolution (input side).** mPulse silently answers an
unrecognized `timer`/`metric` with a *default* (PageLoad) series instead of
erroring, so the explicit tools resolve these names against the catalog before
calling the API:

- a casing/separator-only mismatch (`largest_contentful_paint` →
  `LargestContentfulPaint`, endpoint-specific) is **auto-corrected** silently
  (zero extra round-trips); the fix is reported in a `corrections` field;
- a genuinely unknown name is **rejected** with close suggestions
  (`Did you mean: …?`) — the wrong name never reaches mPulse;
- custom timers/dimensions and the generic `query` tool are left permissive
  (forward-compatible); if the catalog is unavailable, resolution is skipped.

**Custom `dimension` support is endpoint-specific.** A custom dimension (e.g.
`branch`) is accepted **only** by `metrics-by-dimension` (as its `dimension`
split value). `dimension-values` and `dimension-over-time` accept **built-in
dimensions only** — a custom name there returns empty/400 (a wasted call). The
`query` tool enforces this: a custom `dimension` on those two query-types is
**rejected up front** with a redirect to `metrics-by-dimension`, while a
built-in's casing is auto-corrected. `describe_query` reports
`custom_dimension_supported` per query-type so the model knows before calling.

For an app's *declared* custom dimensions (see Configure → Custom dimensions),
the `query` tool additionally normalizes the label to its wire form
(`mobile speed` → `mobile_speed`) for both the split value and
`custom-dimension-<label>` filters, and adds a soft `dimension_notes` advisory
when a name isn't in the declared set — but always passes it through (the
declared list may be incomplete). `list_custom_dimensions(app)` and
`describe_query(query_type, app)` expose the declared names so the model uses
the exact label instead of guessing.

**Passing multiple values (mechanism differs per parameter).** Verified against
every endpoint's reference doc:

- **Comma-separated, one param** — only `metrics` on `metrics-by-dimension`
  (**plural** param name; the singular `metric` is silently ignored). Pass a
  pre-joined string: `"metrics": "beacons,largest_contentful_paint"`.
- **Repeated key** — `metric`/`timer` on `timers-metrics` and **every drilldown
  filter** (`country`, `browser`, `ab-test`, …) and `custom-dimension-<label>`.
  Pass a **JSON list** and the client expands it to repeated query keys:
  `"custom-dimension-branch": ["uk","us","sec"]` →
  `custom-dimension-branch=uk&…=us&…=sec` (verified live). A Python-list-repr no
  longer leaks onto the wire.
- **Single value** — `dimension`, `percentile`, `metric` on
  `metric-per-page-load-time` (its own enum: `BounceRate` + `CustomMetric0-9`),
  etc.

`describe_query` surfaces a `parameter_mechanics` block (which params are
comma-separated vs repeat-key) so the model sets values correctly. Note:
`metrics-by-dimension` also computes percentiles and takes native `sortby` /
`limit`, returning a `{columnNames, data:[[…]]}` table.

**Silent-fallback `warning` (output side).** As a backstop for cases input
resolution can't catch, when the series id/name echoed in the response does not
match the requested `timer`/`metric`, the result carries a `warning` so the
caller knows the data is **not** for the requested name. Casing/separator-only
differences are treated as a match and never warn. The `warning` is additive
metadata and appears in `raw` mode too (data stays untouched).

**Payload instrumentation.** Every successful query logs
`payload app=… query=… bytes=… points=…` to **stderr**, for measuring response
size / token cost (e.g. before/after future history-reduction work).

**`limit` — high-cardinality caps.** `dimension-values`, `geography`,
`page-groups`, `browsers`, … can return hundreds–thousands of rows (verified
live: ~215 countries for `geography`, ~1,400 values for `dimension-values`
browser). On the `query` tool these query-types are capped at 100 by default
(pass an explicit `limit` to change it); when trimmed, the result carries
`truncated: {key, total, returned[, sorted_by]}` so nothing silently disappears.
When the rows carry a count field (e.g. geography's `timerN`), they are **sorted
by volume descending before trimming** (`sorted_by`), so the top markets survive
rather than an alphabetical slice; rows without a count (dimension-values'
strings) keep their order. `raw=true` is never trimmed.

**`empty_reason` + `probe` — why is it empty?** An unsupported drilldown
combination and genuine no-traffic both come back empty. Every empty result now
carries an `empty_reason` heuristic (`no_data` / `likely_no_traffic` /
`possibly_unsupported_combo`) at zero cost. Pass `probe=true` (on the explicit
tools or `query`) to spend **one** extra drilldown-free query and get a
definitive `unsupported_combo` vs `no_traffic`, reported in a `probe` block.

### Batch aggregate

`get_aggregate` runs a matrix of `timers-metrics` queries and returns only each
cell's period aggregate (`latest`) — the value you'd read for a monthly p75.

```jsonc
get_aggregate(
  metrics=["TotalRequestCount", "TotalTransferSize"],
  periods=[
    {"date_comparator": "Between", "date_start": "2026-06-01",
     "date_end": "2026-07-01", "label": "June"},   // date_end exclusive
    {"date_comparator": "Between", "date_start": "2026-07-01",
     "date_end": "2026-08-01", "label": "July"}
  ],
  percentiles=[50, 75]
)
// -> { "table": [ {"target":"TotalRequestCount","period":"June",
//                  "percentile":75,"value":142}, … 8 rows … ],
//      "percentiles":[50,75], "targets":[…] }
```

Period objects are `{date: "YYYY-MM-DD"}` or `{date_comparator: …}` (`Between`
needs `date_start`+`date_end`; `Last` needs `trailing_seconds`); `label` names
the column. Names are validated/auto-corrected up front (one error, not N).
A cell that fails lands in `errors[]` with its coordinates while the rest
return; `max_combos` (default 24) guards `targets × periods × percentiles`.

### Constraints surfaced to the model

One calendar day per query (ranges are rejected with guidance to split into
per-day calls — the server does not fan out implicitly); per-minute aggregation;
timezone-dependent; unsupported drilldown combinations return empty data.

## Errors

`401` → one automatic token re-issue + replay, then an auth error. `403` →
mPulse Lite / permission guidance (the Query API is not available on Lite).
`429` → exponential backoff + jitter (up to 5 retries), then a rate-limit error.
Network/timeout/5xx → retried, then a friendly upstream error. Invalid
app/query-type/params → validation errors with hints. No error message ever
contains a secret.

## Tests

```bash
uv run pytest                 # unit tests (respx-mocked; no network)
```

Covers: token mint/cache/expiry/refresh + single-flight, `Authentication`
header, 401 replay, 429 backoff, 403→Lite mapping, loss-free normalization,
`date`/`date_comparator` mutual exclusivity, and multi-app / default fallback.

**Manual smoke test** against the real API:

```bash
export MPULSE_API_TOKEN=...   # or rely on your .env / shell env
export MPULSE_APPS_CONFIG=/Users/hyoon/Projects/mpulse-mcp/mpulse_apps.json
uv run python tests/smoke.py                 # default app, yesterday
uv run python tests/smoke.py app-b 2026-08-01
```

## Project layout

```
src/mpulse_mcp/
  server.py       FastMCP instance + tool registration (entry point)
  config.py       app registry + credential loading
  auth.py         token mint/cache/refresh (single-flight)
  client.py       HTTP client + rate limiting + retries
  query_types.py  query-type / parameter metadata
  formatting.py   loss-free normalization + raw passthrough
  errors.py       typed exceptions + status mapping
tests/            unit tests + smoke.py
```
