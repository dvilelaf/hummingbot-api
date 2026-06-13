import asyncio
import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "gateway_client.py"
spec = importlib.util.spec_from_file_location("gateway_client_under_test", MODULE_PATH)
gateway_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway_client)
GatewayClient = gateway_client.GatewayClient


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


def test_gateway_lp_router_is_registered_in_main():
    main_source = (Path(__file__).resolve().parents[1] / "main.py").read_text()

    assert "gateway_lp" in main_source
    assert "app.include_router(gateway_lp.router" in main_source
