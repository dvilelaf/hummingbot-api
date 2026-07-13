import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.hyperliquid_market import (
    connector_trading_pair,
    logical_observation,
)


@pytest.mark.parametrize(
    ("connector_name", "trading_pair", "expected"),
    [
        ("hyperliquid_perpetual", "HYPE-USDC", "HYPE-USD"),
        ("hyperliquid_perpetual", "BTC-USDC", "BTC-USDC"),
        ("hyperliquid", "HYPE-USDC", "HYPE-USDC"),
        ("hyperliquid_perpetual_testnet", "HYPE-USDC", "HYPE-USDC"),
    ],
)
def test_connector_trading_pair_is_narrowly_scoped(connector_name, trading_pair, expected):
    assert connector_trading_pair(connector_name, trading_pair) == expected


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
