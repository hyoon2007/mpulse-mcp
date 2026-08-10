"""Registry loading: custom-dimension parsing, wire-label normalization, merge."""

from __future__ import annotations

import json

from mpulse_mcp.config import custom_dimension_wire_label, load_registry


def test_wire_label_rule() -> None:
    assert custom_dimension_wire_label("mobile speed") == "mobile_speed"
    assert custom_dimension_wire_label("PAGE_SPEED") == "page_speed"
    assert custom_dimension_wire_label("camelCase") == "camelcase"


def _write(tmp_path, obj) -> str:
    p = tmp_path / "mpulse_apps.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_custom_dimensions_merge_and_normalize(tmp_path) -> None:
    cfg = _write(
        tmp_path,
        {
            "default_app": "a",
            "tenant": "t",
            # shared, inherited by every app
            "custom_dimensions": {"Branch": {"description": "site code"}},
            "apps": {
                "a": {
                    "api_key": "K1",
                    # object form + a name needing normalization
                    "custom_dimensions": {"mobile speed": {}, "PAGE_SPEED": None},
                },
                "b": {
                    "api_key": "K2",
                    # list form
                    "custom_dimensions": ["checkout_step"],
                },
            },
        },
    )
    reg = load_registry(cfg)

    a = reg.apps["a"]
    # top-level 'Branch' merged + app labels normalized to wire form
    assert set(a.custom_dimensions) == {"branch", "mobile_speed", "page_speed"}
    assert a.custom_dimensions["branch"]["description"] == "site code"
    # display defaults to the original name
    assert a.custom_dimensions["mobile_speed"]["display"] == "mobile speed"

    b = reg.apps["b"]
    assert set(b.custom_dimensions) == {"branch", "checkout_step"}


def test_no_custom_dimensions_is_empty(tmp_path) -> None:
    cfg = _write(
        tmp_path,
        {"default_app": "a", "apps": {"a": {"api_key": "K1"}}},
    )
    reg = load_registry(cfg)
    assert reg.apps["a"].custom_dimensions == {}
