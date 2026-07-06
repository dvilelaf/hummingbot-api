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


@pytest.mark.asyncio
async def test_cowswap_order_book_initialization_uses_runtime_price(monkeypatch):
    from services import market_data_service as module
    from services.market_data_service import MarketDataService

    class ConnectorService:
        def initialize_order_book(self, *_args, **_kwargs):
            raise AssertionError("CowSwap order-book init must not use generic connectors")

    class AccountsService:
        _cowswap_runtime = object()

    async def fake_cowswap_prices(*, runtime, trading_pairs):
        assert runtime is AccountsService._cowswap_runtime
        assert trading_pairs == ["WETH-USDC"]
        return {"WETH-USDC": 2500.0}

    monkeypatch.setattr(module, "cowswap_runtime_prices", fake_cowswap_prices)
    service = MarketDataService(connector_service=ConnectorService(), rate_oracle=object())
    service.configure_accounts_service(AccountsService())

    initialized = await service.initialize_order_book("cowswap", "WETH-USDC")

    assert initialized is True


@pytest.mark.asyncio
async def test_cowswap_order_book_initialization_fails_without_runtime_price(monkeypatch):
    from services import market_data_service as module
    from services.market_data_service import MarketDataService

    class ConnectorService:
        def initialize_order_book(self, *_args, **_kwargs):
            raise AssertionError("CowSwap order-book init must not use generic connectors")

    async def fake_cowswap_prices(*, runtime, trading_pairs):  # noqa: ARG001
        return {"error": "unsupported pair"}

    monkeypatch.setattr(module, "cowswap_runtime_prices", fake_cowswap_prices)
    service = MarketDataService(connector_service=ConnectorService(), rate_oracle=object())

    initialized = await service.initialize_order_book("cowswap", "UNI-USDC")

    assert initialized is False


@pytest.mark.asyncio
async def test_cowswap_order_book_data_uses_runtime_price(monkeypatch):
    from services import market_data_service as module
    from services.market_data_service import MarketDataService

    class ConnectorService:
        def get_best_connector_for_market(self, *_args, **_kwargs):
            raise AssertionError("CowSwap order-book data must not use generic connectors")

    async def fake_cowswap_prices(*, runtime, trading_pairs):  # noqa: ARG001
        assert trading_pairs == ["WETH-USDC"]
        return {"WETH-USDC": 2500.0}

    monkeypatch.setattr(module, "cowswap_runtime_prices", fake_cowswap_prices)
    service = MarketDataService(connector_service=ConnectorService(), rate_oracle=object())

    data = await service.get_order_book_data("cowswap", "WETH-USDC")

    assert data["trading_pair"] == "WETH-USDC"
    assert data["bids"][0][0] < 2500.0
    assert data["asks"][0][0] > 2500.0


@pytest.mark.asyncio
async def test_cowswap_order_book_data_fails_without_runtime_price(monkeypatch):
    from services import market_data_service as module
    from services.market_data_service import MarketDataService

    class ConnectorService:
        def get_best_connector_for_market(self, *_args, **_kwargs):
            raise AssertionError("CowSwap order-book data must not use generic connectors")

    async def fake_cowswap_prices(*, runtime, trading_pairs):  # noqa: ARG001
        return {"error": "unsupported pair"}

    monkeypatch.setattr(module, "cowswap_runtime_prices", fake_cowswap_prices)
    service = MarketDataService(connector_service=ConnectorService(), rate_oracle=object())

    data = await service.get_order_book_data("cowswap", "UNI-USDC")

    assert data == {"error": "unsupported pair"}
