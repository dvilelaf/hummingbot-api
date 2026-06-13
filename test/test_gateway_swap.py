import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException

ROUTER_MODULE_PATH = Path(__file__).resolve().parents[1] / "routers" / "gateway_swap.py"
router_spec = importlib.util.spec_from_file_location("gateway_swap_under_test", ROUTER_MODULE_PATH)
gateway_swap = importlib.util.module_from_spec(router_spec)
router_spec.loader.exec_module(gateway_swap)


def test_gateway_swap_quote_error_fails_closed():
    with pytest.raises(HTTPException) as exc_info:
        gateway_swap._raise_if_invalid_quote(
            {"status": 400, "error": "No route found for devUSDC -> devUSDT"},
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "No route found for devUSDC -> devUSDT"


def test_gateway_swap_zero_quote_fails_closed():
    with pytest.raises(HTTPException) as exc_info:
        gateway_swap._raise_if_invalid_quote(
            {
                "price": "0",
                "amountIn": None,
                "amountOut": None,
            },
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Gateway returned an invalid swap quote"


def test_gateway_swap_positive_quote_is_accepted():
    gateway_swap._raise_if_invalid_quote(
        {
            "price": "123.4",
            "amountIn": "0.1",
            "amountOut": "12.34",
        },
    )
