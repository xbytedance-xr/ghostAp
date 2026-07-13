from __future__ import annotations

import inspect

import lark_oapi as lark


def test_lark_oapi_exposes_one_click_app_configuration_contract() -> None:
    assert lark.VERSION == "1.7.1"
    required = {"app_preset", "addons", "create_only", "app_id"}
    for name in ("register_app", "aregister_app"):
        assert required <= set(inspect.signature(getattr(lark, name)).parameters)
