import pytest

pytest.importorskip("hummingbot")


@pytest.mark.asyncio
async def test_market_data_prices_use_cowswap_runtime(monkeypatch):
    from services import market_data_service as module
    from services.market_data_service import MarketDataService

    class ConnectorService:
        def get_best_connector_for_market(self, *_args, **_kwargs):
            raise AssertionError("CowSwap prices must not use generic connector market data")

    class AccountsService:
        _cowswap_runtime = object()

    async def fake_cowswap_prices(*, runtime, trading_pairs):
        assert runtime is AccountsService._cowswap_runtime
        assert trading_pairs == ["WETH-USDC"]
        return {"WETH-USDC": 2500.0}

    monkeypatch.setattr(module, "cowswap_runtime_prices", fake_cowswap_prices)
    service = MarketDataService(connector_service=ConnectorService(), rate_oracle=object())
    service.configure_accounts_service(AccountsService())

    prices = await service.get_prices("cowswap", ["WETH-USDC"])

    assert prices == {"WETH-USDC": 2500.0}
