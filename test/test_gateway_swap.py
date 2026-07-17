import asyncio
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal

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


def test_gateway_swap_buy_quote_uses_inverted_sell_terms():
    assert gateway_swap._gateway_swap_terms(
        base="AERO",
        quote="USDC",
        amount=Decimal("0.00005"),
        side="BUY",
    ) == ("USDC", "AERO", Decimal("0.00005"), "SELL")


class _FakeGatewayClient:
    def __init__(self, result):
        self.result = result

    async def ping(self):
        return True

    def parse_network_id(self, network_id):
        return network_id.split("-", 1)

    async def quote_swap(self, **kwargs):
        return self.result


class _FakeAccountsService:
    def __init__(self, result):
        self.gateway_client = _FakeGatewayClient(result)


def _get_quote(result):
    request = gateway_swap.SwapQuoteRequest(
        connector="jupiter",
        network="solana-mainnet-beta",
        trading_pair="SOL-USDC",
        side="SELL",
        amount=Decimal("1"),
    )
    return asyncio.run(gateway_swap.get_swap_quote(request, _FakeAccountsService(result)))


def _valid_quote_result():
    return {"price": "123.4", "amountIn": "1", "amountOut": "12.34", "gasEstimate": "42"}


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"quoteId": "quote-123", "priceImpactPct": 0, "minAmountOut": "12.22", "maxAmountIn": "1.01", "observedAt": "2026-07-17T10:30:00+02:00"},
            ("quote-123", Decimal("0"), Decimal("12.22"), Decimal("1.01"), datetime(2026, 7, 17, 8, 30, tzinfo=timezone.utc)),
        ),
        (
            {"quote_id": "quote-456", "price_impact_pct": "0.25", "min_amount_out": "12.30", "max_amount_in": "1.02", "observed_at": "2026-07-17T08:30:00Z"},
            ("quote-456", Decimal("0.25"), Decimal("12.30"), Decimal("1.02"), datetime(2026, 7, 17, 8, 30, tzinfo=timezone.utc)),
        ),
    ],
)
def test_gateway_swap_quote_maps_provider_fields(fields, expected):
    response = _get_quote({**_valid_quote_result(), **fields})
    assert (response.quote_id, response.price_impact_pct, response.min_amount_out, response.max_amount_in, response.observed_at) == expected


def test_gateway_swap_quote_uses_receipt_time_for_missing_observed_at():
    before = datetime.now(timezone.utc)
    response = _get_quote(_valid_quote_result())
    after = datetime.now(timezone.utc)

    assert response.quote_id is None
    assert (response.price_impact_pct, response.min_amount_out, response.max_amount_in) == (None, None, None)
    assert before <= response.observed_at <= after
    assert response.observed_at.tzinfo == timezone.utc


def _assert_quote_502(result):
    with pytest.raises(HTTPException) as exc_info:
        _get_quote(result)
    assert exc_info.value.status_code == 502


@pytest.mark.parametrize("field", ["gasEstimate", "priceImpactPct", "minAmountOut", "maxAmountIn"])
def test_gateway_swap_quote_rejects_malformed_provider_numeric_field(field):
    result = _valid_quote_result()
    result[field] = "not-a-decimal"
    _assert_quote_502(result)


@pytest.mark.parametrize(
    "field,value",
    [("observedAt", "not-a-timestamp"), ("observed_at", "2026-07-17T08:30:00")],
)
def test_gateway_swap_quote_rejects_malformed_or_naive_observed_at(field, value):
    result = _valid_quote_result()
    result[field] = value
    _assert_quote_502(result)
