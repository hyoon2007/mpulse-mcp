"""Shared pytest fixtures for the mPulse MCP test suite."""

from __future__ import annotations

import pytest

from mpulse_mcp.config import AppConfig, Registry


@pytest.fixture
def registry() -> Registry:
    """A two-app registry: 'alpha' (default) and 'beta' with a distinct credential."""
    apps = {
        "alpha": AppConfig(
            name="alpha",
            api_key="KEY-ALPHA",
            tenant="tenant-a",
            api_token_env="MPULSE_API_TOKEN",
        ),
        "beta": AppConfig(
            name="beta",
            api_key="KEY-BETA",
            tenant="tenant-b",
            api_token_env="MPULSE_API_TOKEN_BETA",
        ),
    }
    return Registry(default_app="alpha", apps=apps)


@pytest.fixture(autouse=True)
def _tokens_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MPULSE_API_TOKEN", "apitoken-alpha")
    monkeypatch.setenv("MPULSE_API_TOKEN_BETA", "apitoken-beta")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make backoff/throttle sleeps instant so retry tests run fast."""
    import asyncio

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)
