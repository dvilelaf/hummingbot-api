import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.hyperliquid_market import (
    connector_trading_pair,
    funding_interval_seconds,
    logical_observation,
    logical_trading_pair,
    logical_trading_rule,
)


@pytest.mark.parametrize(
    ("connector_name", "trading_pair", "expected"),
    [
        ("hyperliquid_perpetual", "HYPE-USDC", "HYPE-USD"),
        ("hyperliquid_perpetual", "BTC-USDC", "BTC-USD"),
        ("hyperliquid_perpetual", "ETH-USDC", "ETH-USD"),
        ("hyperliquid_perpetual", "BTC-USD", "BTC-USD"),
        ("hyperliquid_perpetual", "BTC-USDT", "BTC-USDT"),
        ("hyperliquid", "SOL-USDC", "SOL-USDC"),
        ("hyperliquid_perpetual_testnet", "HYPE-USDC", "HYPE-USDC"),
    ],
)
def test_connector_trading_pair_normalizes_hyperliquid_collateral(
    connector_name, trading_pair, expected
):
    assert connector_trading_pair(connector_name, trading_pair) == expected


@pytest.mark.parametrize(
    ("connector_name", "expected"),
    [("hyperliquid_perpetual", 3600), ("unknown", None)],
)
def test_funding_interval_seconds_is_known_only_for_hyperliquid_perpetual(
    connector_name, expected
):
    assert funding_interval_seconds(connector_name) == expected


def test_market_data_funding_info_normalizes_pair_and_preserves_zero_rate(monkeypatch):
    pytest.importorskip("hummingbot")
    import services.market_data_service as service_module
    from models.market_data import FundingInfoResponse
    from services.market_data_service import MarketDataService

    data_source = SimpleNamespace(
        get_funding_info=AsyncMock(
            return_value=SimpleNamespace(
                rate=0,
                next_funding_utc_timestamp=1700003600,
                mark_price=2000,
                index_price=1999,
            )
        )
    )
    connector = SimpleNamespace(_orderbook_ds=data_source)
    service = MarketDataService.__new__(MarketDataService)
    service._connector_service = MagicMock()
    service._connector_service.get_best_connector_for_market.return_value = connector
    monkeypatch.setattr(service_module.time, "time", lambda: 1700000000.0)

    result = asyncio.run(
        service.get_funding_info("hyperliquid_perpetual", "ETH-USDC")
    )

    data_source.get_funding_info.assert_awaited_once_with("ETH-USD")
    response = FundingInfoResponse(**result)
    assert response.trading_pair == "ETH-USDC"
    assert response.funding_rate == 0.0
    assert response.funding_interval_seconds == 3600
    assert response.observed_at == 1700000000.0


@pytest.mark.parametrize(
    ("connector_name", "trading_pair", "expected"),
    [
        ("hyperliquid_perpetual", "HYPE-USD", "HYPE-USDC"),
        ("hyperliquid_perpetual", "BTC-USD", "BTC-USDC"),
        ("hyperliquid_perpetual", "ETH-USD", "ETH-USDC"),
        ("hyperliquid_perpetual", "BTC-USDC", "BTC-USDC"),
        ("hyperliquid_perpetual", "BTC-USDT", "BTC-USDT"),
        ("hyperliquid", "SOL-USD", "SOL-USD"),
        ("hyperliquid_perpetual_testnet", "HYPE-USD", "HYPE-USD"),
    ],
)
def test_logical_trading_pair_normalizes_hyperliquid_collateral(
    connector_name, trading_pair, expected
):
    assert logical_trading_pair(connector_name, trading_pair) == expected


def test_market_data_initializes_hyperliquid_connector_market():
    pytest.importorskip("hummingbot")
    from services.market_data_service import MarketDataService

    service = MarketDataService.__new__(MarketDataService)
    service._connector_service = MagicMock()
    service._connector_service.initialize_order_book = AsyncMock(return_value=True)

    result = asyncio.run(
        service.initialize_order_book(
            "hyperliquid_perpetual",
            "HYPE-USDC",
            account_name="master_account",
        )
    )

    assert result is True
    service._connector_service.initialize_order_book.assert_awaited_once_with(
        connector_name="hyperliquid_perpetual",
        trading_pair="HYPE-USD",
        account_name="master_account",
        timeout=30.0,
    )


def test_market_data_reads_connector_book_with_logical_market():
    pytest.importorskip("hummingbot")
    from services.market_data_service import MarketDataService

    book = object()
    connector = SimpleNamespace(
        order_book_tracker=SimpleNamespace(order_books={"HYPE-USD": book}),
    )
    service = MarketDataService.__new__(MarketDataService)
    service._connector_service = MagicMock()
    service._connector_service.get_best_connector_for_market.return_value = connector
    service._last_access_times = {}
    service._feed_configs = {}

    result = service.get_order_book(
        "hyperliquid_perpetual",
        "HYPE-USDC",
        "master_account",
    )

    assert result is book


def test_candles_endpoint_normalizes_hyperliquid_perpetual_pair(monkeypatch):
    pytest.importorskip("hummingbot")
    import routers.market_data as module
    from models.market_data import CandlesConfigRequest

    candles_df = MagicMock(empty=False)
    candles_df.tail.return_value = candles_df
    candles_df.drop_duplicates.return_value = candles_df
    candles_df.sort_values.return_value = candles_df
    candles_df.to_dict.return_value = [{"timestamp": 1}]
    market_data = SimpleNamespace(
        validate_trading_pair=AsyncMock(),
        get_candles_feed=MagicMock(
            return_value=SimpleNamespace(ready=True, candles_df=candles_df)
        ),
        stop_candle_feed=MagicMock(),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(market_data_service=market_data))
    )
    monkeypatch.setattr(module.CandlesFactory, "_candles_map", {"hyperliquid_perpetual": object()})

    result = asyncio.run(
        module.get_candles(
            request,
            CandlesConfigRequest(
                connector_name="hyperliquid_perpetual",
                trading_pair="HYPE-USDC",
            ),
        )
    )

    assert result == [{"timestamp": 1}]
    market_data.validate_trading_pair.assert_awaited_once_with(
        "hyperliquid_perpetual", "HYPE-USD", "1m"
    )
    assert market_data.get_candles_feed.call_args.args[0].trading_pair == "HYPE-USD"
    candles_df.sort_values.assert_called_once_with("timestamp")


def test_logical_observation_normalizes_market_and_usd_fee_currency():
    assert logical_observation(
        {
            "connector_name": "hyperliquid_perpetual",
            "fee_currency": "USD",
            "trading_pair": "HYPE-USD",
        }
    ) == {
        "connector_name": "hyperliquid_perpetual",
        "fee_currency": "USDC",
        "trading_pair": "HYPE-USDC",
    }


def test_logical_trading_rule_response_restores_public_contract():
    assert logical_trading_pair("hyperliquid_perpetual", "HYPE-USD") == "HYPE-USDC"
    assert logical_trading_rule(
        "hyperliquid_perpetual",
        {
            "buy_order_collateral_token": "USD",
            "sell_order_collateral_token": "USD",
            "min_order_size": 0.01,
        },
    ) == {
        "buy_order_collateral_token": "USDC",
        "sell_order_collateral_token": "USDC",
        "min_order_size": 0.01,
    }


def test_trading_rules_endpoint_maps_provider_and_logical_markets(monkeypatch):
    pytest.importorskip("hummingbot")
    import routers.connectors as module

    market_data = MagicMock()
    market_data.get_trading_rules = AsyncMock(
        return_value={
            "HYPE-USD": {
                "buy_order_collateral_token": "USD",
                "sell_order_collateral_token": "USD",
                "min_order_size": 0.01,
            },
        },
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                accounts_service=MagicMock(),
                market_data_service=market_data,
            ),
        ),
    )
    monkeypatch.setattr(module, "_gateway_swap_connector", AsyncMock(return_value=False))

    result = asyncio.run(
        module.get_trading_rules(
            request,
            "hyperliquid_perpetual",
            ["HYPE-USDC"],
        ),
    )

    market_data.get_trading_rules.assert_awaited_once_with(
        "hyperliquid_perpetual",
        ["HYPE-USD"],
    )
    assert result == {
        "HYPE-USDC": {
            "buy_order_collateral_token": "USDC",
            "sell_order_collateral_token": "USDC",
            "min_order_size": 0.01,
        },
    }


def _session_context():
    @asynccontextmanager
    async def context():
        yield MagicMock()

    return context()


@pytest.fixture
def accounts_service():
    pytest.importorskip("hummingbot")
    from services.accounts_service import AccountsService

    service = AccountsService.__new__(AccountsService)
    service.db_manager = MagicMock()
    service.db_manager.get_session_context.side_effect = _session_context
    service.ensure_db_initialized = AsyncMock()
    return service


def test_hyperliquid_perpetual_order_observations_use_logical_market(
    accounts_service, monkeypatch
):
    import services.accounts_service as accounts_module

    order = SimpleNamespace()
    repository = MagicMock()
    repository.get_orders = AsyncMock(return_value=[order])
    repository.to_dict.return_value = {
        "connector_name": "hyperliquid_perpetual",
        "trading_pair": "HYPE-USD",
    }
    monkeypatch.setattr(accounts_module, "OrderRepository", lambda _session: repository)

    result = asyncio.run(
        accounts_service.get_orders(
            connector_name="hyperliquid_perpetual",
            trading_pair="HYPE-USDC",
        )
    )

    assert repository.get_orders.await_args.kwargs["trading_pair"] == "HYPE-USD"
    assert result[0]["trading_pair"] == "HYPE-USDC"


def test_hyperliquid_perpetual_trade_observations_use_logical_market(
    accounts_service, monkeypatch
):
    import services.accounts_service as accounts_module

    trade = SimpleNamespace()
    order = SimpleNamespace()
    repository = MagicMock()
    repository.get_trades_with_orders = AsyncMock(return_value=[(trade, order)])
    repository.to_dict.return_value = {
        "connector_name": "hyperliquid_perpetual",
        "trading_pair": "HYPE-USD",
    }
    monkeypatch.setattr(accounts_module, "TradeRepository", lambda _session: repository)

    result = asyncio.run(
        accounts_service.get_trades(
            connector_name="hyperliquid_perpetual",
            trading_pair="HYPE-USDC",
        )
    )

    assert repository.get_trades_with_orders.await_args.kwargs["trading_pair"] == "HYPE-USD"
    assert result[0]["trading_pair"] == "HYPE-USDC"


@pytest.mark.parametrize(
    "connector_name",
    ["hyperliquid", "hyperliquid_perpetual_testnet"],
)
def test_other_hyperliquid_connectors_leave_order_market_unchanged(
    accounts_service, monkeypatch, connector_name
):
    import services.accounts_service as accounts_module

    order = SimpleNamespace()
    repository = MagicMock()
    repository.get_orders = AsyncMock(return_value=[order])
    repository.to_dict.return_value = {
        "connector_name": connector_name,
        "trading_pair": "HYPE-USDC",
    }
    monkeypatch.setattr(accounts_module, "OrderRepository", lambda _session: repository)

    result = asyncio.run(
        accounts_service.get_orders(
            connector_name=connector_name,
            trading_pair="HYPE-USDC",
        )
    )

    assert repository.get_orders.await_args.kwargs["trading_pair"] == "HYPE-USDC"
    assert result[0]["trading_pair"] == "HYPE-USDC"
