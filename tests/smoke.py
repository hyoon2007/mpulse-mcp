"""Manual end-to-end smoke test against the REAL mPulse API.

This is NOT run by pytest (no test_ prefix). It uses your live credentials to
verify the whole path: registry load -> token mint -> a real summary query.

Usage
-----
    export MPULSE_API_TOKEN=...            # your pre-issued API token
    export MPULSE_APPS_CONFIG=/path/to/mpulse_apps.json
    uv run python tests/smoke.py           # summary for default app, yesterday
    uv run python tests/smoke.py beta 2026-08-01

All output goes to stderr (import side effects keep stdout clean), and results
are printed to stderr as pretty JSON. Secrets are never printed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys

from mpulse_mcp.client import MpulseClient
from mpulse_mcp.config import load_env_files, load_registry
from mpulse_mcp.formatting import normalize


def _eprint(obj: object) -> None:
    print(obj, file=sys.stderr)


async def run(app: str | None, date: str) -> int:
    load_env_files()
    registry = load_registry()
    _eprint(f"Registry loaded. Default app: {registry.default_app}")
    _eprint("Apps: " + ", ".join(registry.apps))

    client = MpulseClient(registry)
    try:
        params = {"date": date, "timer": "PageLoad"}
        _eprint(f"Querying summary for app={app or registry.default_app} date={date} ...")
        data = await client.query(app=app, query_type="summary", params=params)
        out = normalize(
            app=app or registry.default_app,
            query_type="summary",
            request_params={**params, "format": "json"},
            aggregation="single-value",
            data=data,
            raw=False,
        )
        _eprint(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    finally:
        await client.aclose()


def main() -> None:
    app = sys.argv[1] if len(sys.argv) > 1 else None
    if len(sys.argv) > 2:
        date = sys.argv[2]
    else:
        date = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    raise SystemExit(asyncio.run(run(app, date)))


if __name__ == "__main__":
    main()
