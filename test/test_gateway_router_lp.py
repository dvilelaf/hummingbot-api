import asyncio
import importlib.util
from pathlib import Path
from decimal import Decimal

import pytest
from fastapi import HTTPException

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "gateway_client.py"
spec = importlib.util.spec_from_file_location("gateway_client_under_test", MODULE_PATH)
gateway_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway_client)
GatewayClient = gateway_client.GatewayClient

ROUTER_MODULE_PATH = Path(__file__).resolve().parents[1] / "routers" / "gateway_lp.py"
router_spec = importlib.util.spec_from_file_location("gateway_lp_under_test", ROUTER_MODULE_PATH)
gateway_lp = importlib.util.module_from_spec(router_spec)
router_spec.loader.exec_module(gateway_lp)


def test_router_add_liquidity_posts_gateway_payload():
    calls = []
    client = GatewayClient(base_url="http://gateway.local")

    async def fake_request(method, path, params=None, json=None):
        calls.append((method, path, params, json))
        return {"signature": "0xadd"}

    client._request = fake_request

    result = asyncio.run(
        client.router_add_liquidity(
            connector="aerodrome",
            network="base",
            wallet_address="0x1111111111111111111111111111111111111111",
            token_a="WETH",
            token_b="USDC",
            amount_a=0.01,
            amount_b=30,
            pool_type="volatile",
            slippage_pct=0.5,
        ),
    )

    assert result == {"signature": "0xadd"}
    assert calls == [
        (
            "POST",
            "connectors/aerodrome/router/add-liquidity",
            None,
            {
                "network": "base",
                "walletAddress": "0x1111111111111111111111111111111111111111",
                "tokenA": "WETH",
                "tokenB": "USDC",
                "amountA": "0.01",
                "amountB": "30",
                "poolType": "volatile",
                "slippagePct": 0.5,
            },
        ),
    ]


def test_router_add_liquidity_preserves_small_decimal_payload():
    calls = []
    client = GatewayClient(base_url="http://gateway.local")

    async def fake_request(method, path, params=None, json=None):
        calls.append((method, path, params, json))
        return {"signature": "0xadd"}

    client._request = fake_request

    asyncio.run(
        client.router_add_liquidity(
            connector="aerodrome",
            network="base",
            wallet_address="0x1111111111111111111111111111111111111111",
            token_a="WETH",
            token_b="USDC",
            amount_a=Decimal("0.000001"),
            amount_b=Decimal("0.000001"),
            pool_type="volatile",
            slippage_pct=0.5,
        ),
    )

    assert calls[0][3]["amountA"] == "0.000001"
    assert calls[0][3]["amountB"] == "0.000001"


def test_router_remove_liquidity_posts_gateway_payload():
    calls = []
    client = GatewayClient(base_url="http://gateway.local")

    async def fake_request(method, path, params=None, json=None):
        calls.append((method, path, params, json))
        return {"signature": "0xremove"}

    client._request = fake_request

    result = asyncio.run(
        client.router_remove_liquidity(
            connector="aerodrome",
            network="base",
            wallet_address="0x1111111111111111111111111111111111111111",
            token_a="WETH",
            token_b="USDC",
            liquidity=0.01,
            pool_type="volatile",
            slippage_pct=0.5,
        ),
    )

    assert result == {"signature": "0xremove"}
    assert calls == [
        (
            "POST",
            "connectors/aerodrome/router/remove-liquidity",
            None,
            {
                "network": "base",
                "walletAddress": "0x1111111111111111111111111111111111111111",
                "tokenA": "WETH",
                "tokenB": "USDC",
                "liquidity": "0.01",
                "poolType": "volatile",
                "slippagePct": 0.5,
            },
        ),
    ]


def test_execute_quote_posts_wallet_address_payload():
    calls = []
    client = GatewayClient(base_url="http://gateway.local")

    async def fake_request(method, path, params=None, json=None):
        calls.append((method, path, params, json))
        return {"signature": "0xquote"}

    client._request = fake_request

    result = asyncio.run(
        client.execute_quote(
            connector="jupiter",
            network="mainnet-beta",
            wallet_address="Wallet111111111111111111111111111111111",
            quote_id="quote-123",
        ),
    )

    assert result == {"signature": "0xquote"}
    assert calls == [
        (
            "POST",
            "connectors/jupiter/router/execute-quote",
            None,
            {
                "network": "mainnet-beta",
                "walletAddress": "Wallet111111111111111111111111111111111",
                "quoteId": "quote-123",
            },
        ),
    ]


def test_gateway_client_sign_typed_data_posts_wallet_payload():
    calls = []
    client = GatewayClient(base_url="http://gateway.local")

    async def fake_request(method, path, params=None, json=None):
        calls.append((method, path, params, json))
        return {"signature": "0xsig"}

    client._request = fake_request

    result = asyncio.run(
        client.sign_typed_data(
            chain="ethereum",
            network="base",
            address="0x1111111111111111111111111111111111111111",
            domain={"chainId": 8453, "verifyingContract": "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"},
            types={"Order": [{"name": "sellToken", "type": "address"}]},
            value={"sellToken": "0x2222222222222222222222222222222222222222"},
        ),
    )

    assert result == {"signature": "0xsig"}
    assert calls == [
        (
            "POST",
            "wallet/sign-typed-data",
            None,
            {
                "chain": "ethereum",
                "network": "base",
                "address": "0x1111111111111111111111111111111111111111",
                "domain": {
                    "chainId": 8453,
                    "verifyingContract": "0x9008D19f58AAbD9eD0D60971565AA8510560ab41",
                },
                "types": {"Order": [{"name": "sellToken", "type": "address"}]},
                "value": {"sellToken": "0x2222222222222222222222222222222222222222"},
            },
        ),
    ]


def test_gateway_client_get_allowances_posts_chain_payload():
    calls = []
    client = GatewayClient(base_url="http://gateway.local")

    async def fake_request(method, path, params=None, json=None):
        calls.append((method, path, params, json))
        return {"approvals": {"USDC": "1"}}

    client._request = fake_request

    result = asyncio.run(
        client.get_allowances(
            chain="ethereum",
            network="base",
            address="0x1111111111111111111111111111111111111111",
            spender="0xC92E8bdf79f0507f65a392b0ab4667716BFE0110",
            tokens=["USDC"],
        ),
    )

    assert result == {"approvals": {"USDC": "1"}}
    assert calls == [
        (
            "POST",
            "chains/ethereum/allowances",
            None,
            {
                "network": "base",
                "address": "0x1111111111111111111111111111111111111111",
                "spender": "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110",
                "tokens": ["USDC"],
            },
        ),
    ]


def test_gateway_lp_status_preserves_confirmed_and_submitted():
    assert gateway_lp._gateway_status({"status": "confirmed", "transaction_hash": "0x1"}) == "confirmed"
    assert gateway_lp._gateway_status({"transaction_hash": "0x1"}) == "submitted"


def test_gateway_lp_failed_status_raises_even_with_transaction_hash():
    with pytest.raises(HTTPException) as exc_info:
        gateway_lp._raise_if_gateway_failed(
            {
                "code": -1,
                "message": "execution reverted",
                "status": "FAILED",
                "transaction_hash": "0xreverted",
            },
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "execution reverted"


def test_gateway_lp_router_is_registered_in_main():
    main_source = (Path(__file__).resolve().parents[1] / "main.py").read_text()

    assert "gateway_lp" in main_source
    assert "app.include_router(gateway_lp.router" in main_source
