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

**Generic**: `query(query_type, app=None, params={...}, raw=False)` — any
query-type with wire-name params, for cases the explicit tools don't cover.

**Meta** (anti-hallucination): `list_apps`, `list_query_types`,
`describe_query(query_type)`.

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

**Silent-fallback `warning`.** mPulse answers an unrecognized `timer`/`metric`
with a *default* (PageLoad) series instead of erroring. When the requested name
does not match the series id/name echoed in the response, the result carries a
`warning` field so the caller knows the data is **not** for the requested name.
Casing/separator-only differences (`largest_contentful_paint` vs
`LargestContentfulPaint`) are treated as a match and never warn. The `warning`
is additive metadata and appears in `raw` mode too (data stays untouched).

**Payload instrumentation.** Every successful query logs
`payload app=… query=… bytes=… points=…` to **stderr**, for measuring response
size / token cost (e.g. before/after future history-reduction work).

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
