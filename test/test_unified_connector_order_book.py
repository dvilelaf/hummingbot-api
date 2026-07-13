import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("hummingbot")


@pytest.fixture
def service():
    from services.unified_connector_service import UnifiedConnectorService

    instance = UnifiedConnectorService.__new__(UnifiedConnectorService)
    instance._data_connectors = {}
    instance._data_connectors_started = {}
    instance._is_tracker_running = MagicMock(return_value=True)
    return instance


async def _hanging_add_trading_pair(*, event, cancelled):
    try:
        await event.wait()
    except asyncio.CancelledError:
        cancelled.set()
        raise


def _make_connector():
    tracker = MagicMock()
    tracker.ready = True
    tracker.order_books = {}
    tracker._trading_pairs = []
    orderbook_ds = MagicMock()
    orderbook_ds.get_new_order_book = AsyncMock()
    connector = MagicMock()
    connector.order_book_tracker = tracker
    connector._orderbook_ds = orderbook_ds
    return connector, tracker, orderbook_ds


@pytest.mark.parametrize("as_data_connector_started", [False, True])
def test_timeout_cancels_dynamic_add_before_rest_fallback(
    service, as_data_connector_started
):
    cancelled = asyncio.Event()
    event = asyncio.Event()
    connector, tracker, orderbook_ds = _make_connector()

    async def fallback(trading_pair):
        assert cancelled.is_set()
        return MagicMock()

    orderbook_ds.get_new_order_book = AsyncMock(side_effect=fallback)
    connector.add_trading_pair = lambda _trading_pair: _hanging_add_trading_pair(
        event=event, cancelled=cancelled
    )
    service.get_best_connector_for_market = MagicMock(return_value=connector)
    service._wait_for_order_book = AsyncMock(return_value=True)

    if as_data_connector_started:
        service._data_connectors["test_exchange"] = connector
        service._data_connectors_started["test_exchange"] = True

    result = asyncio.run(
        service.initialize_order_book(
            connector_name="test_exchange",
            trading_pair="BTC-USD",
            account_name=None,
            timeout=0.01,
        )
    )

    assert cancelled.is_set()
    orderbook_ds.get_new_order_book.assert_awaited_once_with("BTC-USD")
    assert "BTC-USD" in tracker.order_books
    assert result is True


def test_successful_dynamic_add_skips_rest_fallback(service):
    connector, _tracker, orderbook_ds = _make_connector()
    connector.add_trading_pair = AsyncMock(return_value=True)
    service.get_best_connector_for_market = MagicMock(return_value=connector)
    service._wait_for_order_book = AsyncMock(return_value=True)

    result = asyncio.run(
        service.initialize_order_book(
            connector_name="test_exchange",
            trading_pair="BTC-USD",
            account_name=None,
            timeout=30.0,
        )
    )

    assert result is True
    connector.add_trading_pair.assert_awaited_once_with("BTC-USD")
    orderbook_ds.get_new_order_book.assert_not_called()
