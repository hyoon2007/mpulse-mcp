"""mPulse MCP server package.

Wraps the Akamai mPulse Query API v2 and exposes it as MCP tools over stdio.

IMPORTANT (stdio safety): never write to stdout anywhere in this package.
stdout carries the MCP JSON-RPC stream; any stray print corrupts the protocol.
All diagnostics go to stderr via ``mpulse_mcp.log``.
"""

from __future__ import annotations

import logging
import sys

__version__ = "0.9.0"

# A single package-level logger wired to *stderr*. Importing this module has the
# side effect of configuring logging so that nothing ever leaks onto stdout.
log = logging.getLogger("mpulse_mcp")

if not log.handlers:
    _handler = logging.StreamHandler(stream=sys.stderr)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [mpulse-mcp] %(message)s")
    )
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    # Do not propagate to the root logger, whose default handler targets stderr
    # too but could be reconfigured by a host to stdout.
    log.propagate = False

__all__ = ["__version__", "log"]
