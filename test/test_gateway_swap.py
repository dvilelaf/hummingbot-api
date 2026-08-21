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
        connector_name="aerodrome",
        base="AERO",
        quote="USDC",
        amount=Decimal("0.00005"),
        side="BUY",
    ) == ("USDC", "AERO", Decimal("0.00005"), "SELL")


class _FakeGatewayClient:
    def __init__(self, result, estimate_result):
        self.result = result
        self.estimate_result = estimate_result
        self.quote_calls = []
        self.estimate_calls = []

    async def ping(self):
        return True

    def parse_network_id(self, network_id):
        return network_id.split("-", 1)

    async def quote_swap(self, **kwargs):
        self.quote_calls.append(kwargs)
        return self.result

    async def estimate_gas(self, chain, network):
        self.estimate_calls.append((chain, network))
        return self.estimate_result


class _FakeAccountsService:
    def __init__(self, result, estimate_result):
        self.gateway_client = _FakeGatewayClient(result, estimate_result)


_DEFAULT_GAS_ESTIMATE = object()


def _fresh_observed_at():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _default_gas_estimate(observed_at=None):
    observed_at = _fresh_observed_at() if observed_at is None else observed_at
    return {"fee": "0.007", "feeAsset": "SOL", "timestamp": int(observed_at.timestamp() * 1000)}


def _get_quote(result, estimate_result=_DEFAULT_GAS_ESTIMATE, client_out=None, side="SELL", amount=Decimal("1")):
    if estimate_result is _DEFAULT_GAS_ESTIMATE:
        estimate_result = _default_gas_estimate()
    request = gateway_swap.SwapQuoteRequest(
        connector="jupiter",
        network="solana-mainnet-beta",
        trading_pair="SOL-USDC",
        side=side,
        amount=amount,
    )
    service = _FakeAccountsService(result, estimate_result)
    if client_out is not None:
        client_out.append(service.gateway_client)
    return asyncio.run(gateway_swap.get_swap_quote(request, service))


def test_gateway_swap_jupiter_buy_quote_uses_exact_output_terms():
    clients = []
    response = _get_quote(
        {**_valid_quote_result(), "amountIn": "10.25", "amountOut": "0.075", "maxAmountIn": "10.25"},
        client_out=clients,
        side="BUY",
        amount=Decimal("0.075"),
    )

    call = clients[0].quote_calls[0]
    assert (call["base_asset"], call["quote_asset"], call["amount"], call["side"]) == (
        "SOL",
        "USDC",
        Decimal("0.075"),
        "BUY",
    )
    assert (response.amount, response.amount_in, response.amount_out, response.max_amount_in) == (
        Decimal("0.075"),
        Decimal("10.25"),
        Decimal("0.075"),
        Decimal("10.25"),
    )


def _valid_quote_result(observed_at=None):
    observed_at = _fresh_observed_at() if observed_at is None else observed_at
    return {
        "price": "123.4",
        "amountIn": "1",
        "amountOut": "12.34",
        "gasEstimate": "999",
        "observedAt": observed_at.isoformat(),
    }


def test_gateway_swap_quote_uses_authoritative_chain_gas_estimate():
    clients = []
    before = _fresh_observed_at()
    response = _get_quote(_valid_quote_result(), client_out=clients)

    assert response.gas_estimate == Decimal("0.007")
    assert response.gas_estimate_asset == "SOL"
    assert before <= response.gas_estimate_observed_at <= _fresh_observed_at()
    assert clients[0].estimate_calls == [("solana", "mainnet-beta")]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"quoteId": "quote-123", "priceImpactPct": 0, "minAmountOut": "12.22", "maxAmountIn": "1.01"},
            ("quote-123", Decimal("0"), Decimal("12.22"), Decimal("1.01")),
        ),
        (
            {"quote_id": "quote-456", "price_impact_pct": "0.25", "min_amount_out": "12.30", "max_amount_in": "1.02"},
            ("quote-456", Decimal("0.25"), Decimal("12.30"), Decimal("1.02")),
        ),
    ],
)
def test_gateway_swap_quote_maps_provider_fields(fields, expected):
    observed_at = _fresh_observed_at()
    observed_field = "observedAt" if "quoteId" in fields else "observed_at"
    observed_value = observed_at.isoformat()
    if observed_field == "observed_at":
        observed_value = observed_value.replace("+00:00", "Z")
    response = _get_quote({**_valid_quote_result(observed_at), **fields, observed_field: observed_value})
    assert (response.quote_id, response.price_impact_pct, response.min_amount_out, response.max_amount_in) == expected
    assert response.observed_at == observed_at


def test_gateway_swap_quote_rejects_missing_observed_at():
    result = _valid_quote_result()
    result.pop("observedAt")

    with pytest.raises(HTTPException) as exc_info:
        _get_quote(result)

    assert exc_info.value.status_code == 502


def _assert_quote_502(result, estimate_result=_DEFAULT_GAS_ESTIMATE):
    with pytest.raises(HTTPException) as exc_info:
        _get_quote(result, estimate_result)
    assert exc_info.value.status_code == 502


@pytest.mark.parametrize("field", ["priceImpactPct", "minAmountOut", "maxAmountIn"])
def test_gateway_swap_quote_rejects_malformed_provider_numeric_field(field):
    result = _valid_quote_result()
    result[field] = "not-a-decimal"
    _assert_quote_502(result)


@pytest.mark.parametrize("estimate_result", [None, {"status": 503, "error": "RPC unavailable"}])
def test_gateway_swap_quote_rejects_unavailable_gas_estimate(estimate_result):
    _assert_quote_502(_valid_quote_result(), estimate_result)


@pytest.mark.parametrize(
    ("field", "value"),
    [("fee", "not-a-decimal"), ("fee", "NaN"), ("fee", "-1"), ("fee", "0"), ("feeAsset", ""), ("feeAsset", "   "), ("timestamp", "NaN"), ("timestamp", -1)],
)
def test_gateway_swap_quote_rejects_malformed_gas_estimate(field, value):
    estimate_result = {**_default_gas_estimate(), field: value}
    _assert_quote_502(_valid_quote_result(), estimate_result)


@pytest.mark.parametrize(
    "field,value",
    [("observedAt", "not-a-timestamp"), ("observed_at", "2026-07-17T08:30:00")],
)
def test_gateway_swap_quote_rejects_malformed_or_naive_observed_at(field, value):
    result = _valid_quote_result()
    result[field] = value
    _assert_quote_502(result)
