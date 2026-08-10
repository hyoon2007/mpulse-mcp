"""App registry + credential loading.

Secrets (the pre-issued mPulse *API token*) live only in environment variables.
Non-secret registry data (api keys, tenant, default app) lives in a JSON file,
``mpulse_apps.json``, whose path is overridable via ``MPULSE_APPS_CONFIG``.

Resolution rules
----------------
* Each app must define ``api_key``.
* ``tenant`` and ``api_token_env`` are inherited from the top level unless the
  app overrides them.
* The actual API-token *value* is read from the environment variable named by
  ``api_token_env``. It is never stored in the JSON file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import log
from .errors import ConfigError, mask

DEFAULT_CONFIG_FILENAMES = ("mpulse_apps.json",)
ENV_CONFIG_PATH = "MPULSE_APPS_CONFIG"


def load_env_files() -> None:
    """Load a local ``.env`` into the process environment, if present.

    Looked up in the current working directory and next to the apps-config file
    (``MPULSE_APPS_CONFIG``). Existing environment variables are **never**
    overridden, so Claude Desktop's ``env`` block always wins over a stray
    ``.env``. No-op if python-dotenv is unavailable. Writes only to stderr.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a declared dependency
        return

    candidates: list[Path] = [Path.cwd() / ".env"]
    cfg = os.environ.get(ENV_CONFIG_PATH)
    if cfg:
        candidates.append(Path(cfg).expanduser().parent / ".env")

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        load_dotenv(dotenv_path=path, override=False)
        log.info("Loaded environment from %s", path)


@dataclass(frozen=True)
class AppConfig:
    """A single registered mPulse app (domain)."""

    name: str
    api_key: str
    tenant: str | None
    api_token_env: str
    # Custom dimensions defined for this app, keyed by the mPulse *wire label*
    # (lowercased, spaces -> underscores). mPulse has no API to list these, so
    # they are declared here purely as hints (never used to hard-reject).
    custom_dimensions: dict[str, dict] = field(default_factory=dict)

    def credential_key(self) -> tuple[str | None, str]:
        """Identity of the credential used to mint tokens.

        Apps that share (tenant, api_token_env) share a cached security token.
        """
        return (self.tenant, self.api_token_env)


@dataclass
class Registry:
    """The full resolved registry: apps + default selection."""

    default_app: str
    apps: dict[str, AppConfig] = field(default_factory=dict)

    def get(self, app: str | None) -> AppConfig:
        """Resolve an app name (or None → default) to its config."""
        name = app or self.default_app
        try:
            return self.apps[name]
        except KeyError:
            available = ", ".join(sorted(self.apps)) or "<none>"
            raise ConfigError(
                f"Unknown app '{name}'.",
                hint=f"Registered apps: {available}. Default: '{self.default_app}'.",
            ) from None

    def api_token(self, app: AppConfig) -> str:
        """Read the secret API-token value for an app from the environment."""
        value = os.environ.get(app.api_token_env)
        if not value:
            raise ConfigError(
                f"Environment variable '{app.api_token_env}' is not set for app "
                f"'{app.name}'.",
                hint="Set the pre-issued mPulse API token in that env var (see "
                ".env.example).",
            )
        return value


def _locate_config_path() -> Path:
    override = os.environ.get(ENV_CONFIG_PATH)
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            raise ConfigError(
                f"{ENV_CONFIG_PATH} points to '{p}', which does not exist."
            )
        return p
    for name in DEFAULT_CONFIG_FILENAMES:
        p = Path.cwd() / name
        if p.is_file():
            return p
    raise ConfigError(
        "No mPulse apps registry found.",
        hint=(
            f"Set {ENV_CONFIG_PATH} to your mpulse_apps.json, or place "
            "mpulse_apps.json in the working directory. See "
            "mpulse_apps.example.json."
        ),
    )


def custom_dimension_wire_label(name: str) -> str:
    """mPulse's label rule: lowercase, spaces -> underscores. 'mobile speed' -> 'mobile_speed'."""
    return name.strip().lower().replace(" ", "_")


def _normalize_custom_dimensions(raw: object, where: str) -> dict[str, dict]:
    """Validate a ``custom_dimensions`` block into ``{wire_label: {meta}}``.

    Accepts either a list of names or an object keyed by name; each value may be
    null or an object (e.g. ``{"display": ..., "description": ...}``). Keys are
    normalized to the mPulse wire label. A missing block yields ``{}``.
    """
    if raw is None:
        return {}
    out: dict[str, dict] = {}
    if isinstance(raw, list):
        items: list[tuple[str, dict]] = [(str(n), {}) for n in raw]
    elif isinstance(raw, dict):
        items = []
        for n, meta in raw.items():
            if meta is None:
                meta = {}
            if not isinstance(meta, dict):
                raise ConfigError(
                    f"custom_dimensions['{n}'] in {where} must be an object or null."
                )
            items = items + [(str(n), meta)]
    else:
        raise ConfigError(
            f"'custom_dimensions' in {where} must be a list or object."
        )
    for name, meta in items:
        label = custom_dimension_wire_label(name)
        entry = dict(meta)
        entry.setdefault("display", name)
        out[label] = entry
    return out


def load_registry(path: str | os.PathLike[str] | None = None) -> Registry:
    """Load and validate the apps registry.

    Raises :class:`ConfigError` with an actionable message on any problem.
    """
    cfg_path = Path(path).expanduser() if path is not None else _locate_config_path()
    try:
        raw = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Registry file not found: {cfg_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Registry file {cfg_path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Registry root must be a JSON object.")

    apps_raw = raw.get("apps")
    if not isinstance(apps_raw, dict) or not apps_raw:
        raise ConfigError("Registry must contain a non-empty 'apps' object.")

    top_tenant = raw.get("tenant")
    top_token_env = raw.get("api_token_env", "MPULSE_API_TOKEN")
    top_custom_dims = _normalize_custom_dimensions(raw.get("custom_dimensions"), "<top-level>")

    apps: dict[str, AppConfig] = {}
    for name, entry in apps_raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"App '{name}' entry must be a JSON object.")
        api_key = entry.get("api_key")
        if not api_key or not isinstance(api_key, str):
            raise ConfigError(f"App '{name}' is missing a string 'api_key'.")
        # Top-level custom dimensions are shared defaults; app-level ones extend
        # or override them (merge, app wins).
        app_custom_dims = _normalize_custom_dimensions(
            entry.get("custom_dimensions"), name
        )
        apps[name] = AppConfig(
            name=name,
            api_key=api_key,
            tenant=entry.get("tenant", top_tenant),
            api_token_env=entry.get("api_token_env", top_token_env),
            custom_dimensions={**top_custom_dims, **app_custom_dims},
        )

    default_app = raw.get("default_app")
    if not default_app:
        # Fall back to the sole app if unambiguous, else require explicit choice.
        if len(apps) == 1:
            default_app = next(iter(apps))
        else:
            raise ConfigError(
                "Registry must specify 'default_app' when more than one app is "
                "registered."
            )
    if default_app not in apps:
        raise ConfigError(
            f"default_app '{default_app}' is not present in 'apps'.",
            hint=f"Registered apps: {', '.join(sorted(apps))}.",
        )

    log.info(
        "Loaded registry from %s: %d app(s), default='%s'",
        cfg_path,
        len(apps),
        default_app,
    )
    for a in apps.values():
        log.info(
            "  app '%s' api_key=%s tenant=%s token_env=%s custom_dims=%d",
            a.name,
            mask(a.api_key),
            a.tenant or "<top-level/none>",
            a.api_token_env,
            len(a.custom_dimensions),
        )
    return Registry(default_app=default_app, apps=apps)
