import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("hummingbot")


@pytest.fixture
def service():
    from services.unified_connector_service import UnifiedConnectorService

    s = UnifiedConnectorService.__new__(UnifiedConnectorService)
    s.db_manager = MagicMock()
    s._trading_connectors = {}
    return s


def _session_context():
    @asynccontextmanager
    async def context():
        yield MagicMock(spec=["execute", "flush", "rollback"])

    return context()


@pytest.mark.asyncio
async def test_load_existing_orders_preserves_existing_adds_missing(service, monkeypatch):
    import database
    from hummingbot.core.data_type.in_flight_order import InFlightOrder

    existing = MagicMock(spec=InFlightOrder)
    existing.client_order_id = "ORD-1"
    connector = MagicMock()
    connector.in_flight_orders = {"ORD-1": existing}

    db_orders = [
        SimpleNamespace(client_order_id="ORD-1", trading_pair="HYPE-USDC", order_type="LIMIT",
                        trade_type="BUY", amount=10.0, price=1.0, status="OPEN",
                        filled_amount=0.0, exchange_order_id="e1", error_message=None, created_at=None),
        SimpleNamespace(client_order_id="ORD-2", trading_pair="HYPE-USDC", order_type="LIMIT",
                        trade_type="BUY", amount=5.0, price=2.0, status="OPEN",
                        filled_amount=0.0, exchange_order_id="e2", error_message=None, created_at=None),
        SimpleNamespace(client_order_id="ORD-3", trading_pair="HYPE-USDC", order_type="LIMIT",
                        trade_type="BUY", amount=5.0, price=2.0, status="FILLED",
                        filled_amount=0.0, exchange_order_id="e3", error_message=None, created_at=None),
        SimpleNamespace(client_order_id="ORD-4", trading_pair="HYPE-USDC", order_type="LIMIT",
                        trade_type="BUY", amount=5.0, price=2.0, status="FILLED",
                        filled_amount=2.0, exchange_order_id="e4", error_message=None, created_at=None),
    ]
    repo = MagicMock()
    repo.get_active_orders = AsyncMock(return_value=db_orders)
    service.db_manager.get_session_context.side_effect = _session_context
    monkeypatch.setattr(database, "OrderRepository", lambda _session: repo)

    await service._load_existing_orders(connector, "acc", "hl")

    assert connector.in_flight_orders["ORD-1"] is existing
    assert connector.in_flight_orders["ORD-2"].client_order_id == "ORD-2"
    assert connector.in_flight_orders["ORD-3"].client_order_id == "ORD-3"
    assert connector.in_flight_orders["ORD-4"].client_order_id == "ORD-4"


@pytest.mark.asyncio
async def test_active_orders_query_recovers_filled_without_durable_trades():
    from datetime import datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from database.models import Base, Order, Trade
    from database.repositories.order_repository import OrderRepository

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        missing_trade = Order(
            client_order_id="missing", account_name="acc", connector_name="hl",
            trading_pair="ETH-USDC", trade_type="BUY", order_type="MARKET",
            amount=5, filled_amount=5, status="FILLED",
        )
        complete = Order(
            client_order_id="complete", account_name="acc", connector_name="hl",
            trading_pair="ETH-USDC", trade_type="BUY", order_type="MARKET",
            amount=5, filled_amount=5, status="FILLED",
        )
        session.add_all([missing_trade, complete])
        session.flush()
        session.add(Trade(
            order_id=complete.id, trade_id="trade-1", trading_pair="ETH-USDC",
            trade_type="BUY", amount=5, price=1, fee_paid=0,
            timestamp=datetime.now(timezone.utc),
        ))
        session.commit()

        class AsyncSession:
            async def execute(self, statement):
                return session.execute(statement)

        active = await OrderRepository(AsyncSession()).get_active_orders()

    assert [order.client_order_id for order in active] == ["missing"]


@pytest.mark.asyncio
async def test_sync_loads_before_sync_per_connector_then_reconcile_once(service, monkeypatch):
    c1 = MagicMock()
    c1.in_flight_orders = {"O1": MagicMock()}
    c2 = MagicMock()
    c2.in_flight_orders = {"O2": MagicMock()}
    service._trading_connectors = {"a1": {"hl": c1}, "a2": {"hl": c2}}

    log = []

    async def fake_load(conn, acc, cn):
        log.append(("load", acc, cn))
        conn.marker = f"{acc}/{cn}"

    async def fake_sync(conn, acc, cn):
        log.append(("sync", acc, cn))

    mock_reconcile = AsyncMock()

    async def reconcile_check():
        assert c1.marker == "a1/hl"
        assert c2.marker == "a2/hl"

    mock_reconcile.side_effect = reconcile_check

    monkeypatch.setattr(service, "_load_existing_orders", fake_load)
    monkeypatch.setattr(service, "_sync_orders_to_database", fake_sync)
    monkeypatch.setattr(service, "reconcile_active_orders", mock_reconcile)

    await service.sync_all_orders_to_database()

    assert log == [("load", "a1", "hl"), ("sync", "a1", "hl"),
                   ("load", "a2", "hl"), ("sync", "a2", "hl")]
    mock_reconcile.assert_awaited_once()


@pytest.fixture
def fill_ctx(service):
    connector = SimpleNamespace(
        client_order_id="ORD-1",
        trade_type=SimpleNamespace(name="BUY"),
        trading_pair="HYPE-USDC",
        exchange_order_id="999",
    )
    db_order = SimpleNamespace(id=42, client_order_id="ORD-1", filled_amount=2.0)
    order_repo = MagicMock()
    order_repo.get_order_by_client_id = AsyncMock(return_value=db_order)
    order_repo.get_order_by_client_id_with_lock = AsyncMock(return_value=db_order)
    order_repo.update_order_fill = AsyncMock()
    trade_repo = MagicMock()
    trade_repo.get_trade_by_id = AsyncMock(return_value=None)
    trade_repo.create_trade = AsyncMock(return_value=SimpleNamespace(id=99, trade_id="ORD-1_5001"))
    trade_repo.get_trades_by_order_id = AsyncMock(return_value=[])
    fills = [{"tid": 5001, "sz": "5.0", "px": "1.5", "fee": "0.05",
              "feeToken": "USDC", "time": "1718000000000", "oid": 999}]
    return SimpleNamespace(order_repo=order_repo, trade_repo=trade_repo,
                           fills=fills, order=connector)


@pytest.mark.asyncio
async def test_fill_creates_canonical_trade(service, fill_ctx):
    await service._persist_external_order_fills(
        order_repo=fill_ctx.order_repo, trade_repo=fill_ctx.trade_repo,
        order=fill_ctx.order, fills=fill_ctx.fills,
    )
    fill_ctx.trade_repo.create_trade.assert_awaited_once()
    created_kwargs = fill_ctx.trade_repo.create_trade.await_args[0][0]
    assert created_kwargs["trade_id"] == "ORD-1_5001"
    fill_ctx.order_repo.get_order_by_client_id_with_lock.assert_awaited_once_with("ORD-1")
    fill_ctx.order_repo.update_order_fill.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_id", ["ORD-1_5001", "5001"])
async def test_fill_skips_when_already_persisted(service, fill_ctx, existing_id):
    fill_ctx.trade_repo.get_trade_by_id = AsyncMock(side_effect=lambda tid: tid == existing_id)
    await service._persist_external_order_fills(
        order_repo=fill_ctx.order_repo, trade_repo=fill_ctx.trade_repo,
        order=fill_ctx.order, fills=fill_ctx.fills,
    )
    fill_ctx.trade_repo.create_trade.assert_not_awaited()
    fill_ctx.order_repo.update_order_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_fills_recompute_from_all_persisted_trades(service, fill_ctx):
    fill_ctx.fills[0]["sz"] = "0.50"
    fill_ctx.fills[0]["px"] = "31"
    fill_ctx.fills[0]["fee"] = "0.015"
    trades = [
        SimpleNamespace(amount=0.50, price=31, fee_paid=0.015, fee_currency="USDC"),
        SimpleNamespace(amount=0.14, price=32, fee_paid=0.004, fee_currency="USDC"),
    ]
    fill_ctx.fills.append(
        {"tid": 5002, "sz": "0.14", "px": "32", "fee": "0.004",
         "feeToken": "USDC", "time": "1718000001000", "oid": 999}
    )
    fill_ctx.trade_repo.create_trade = AsyncMock(side_effect=[SimpleNamespace(), None])
    fill_ctx.trade_repo.get_trades_by_order_id = AsyncMock(return_value=trades)
    fill_ctx.order_repo.recompute_order_aggregates = AsyncMock()

    await service._persist_external_order_fills(
        order_repo=fill_ctx.order_repo,
        trade_repo=fill_ctx.trade_repo,
        order=fill_ctx.order,
        fills=fill_ctx.fills,
    )

    fill_ctx.order_repo.recompute_order_aggregates.assert_awaited_once_with(
        "ORD-1", trades, exchange_order_id="999"
    )


@pytest.mark.asyncio
async def test_recompute_order_aggregates_uses_persisted_trade_truth():
    from database.repositories.order_repository import OrderRepository

    order = SimpleNamespace(
        amount=0.64,
        filled_amount=0.50,
        average_fill_price=31,
        fee_paid=0.015,
        fee_currency="USDC",
        exchange_order_id=None,
        status="PARTIALLY_FILLED",
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = order
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()
    trades = [
        SimpleNamespace(amount=0.50, price=31, fee_paid=0.015, fee_currency="USDC"),
        SimpleNamespace(amount=0.14, price=32, fee_paid=0.004, fee_currency="USDC"),
    ]

    await OrderRepository(session).recompute_order_aggregates("ORD-1", trades, "999")

    assert order.filled_amount == pytest.approx(0.64)
    assert order.average_fill_price == pytest.approx((0.50 * 31 + 0.14 * 32) / 0.64)
    assert order.fee_paid == pytest.approx(0.019)
    assert order.exchange_order_id == "999"
    assert order.status == "FILLED"


@pytest.mark.asyncio
async def test_order_fill_lock_uses_for_update():
    from database.repositories.order_repository import OrderRepository

    result = MagicMock()
    result.scalar_one_or_none.return_value = SimpleNamespace(id=42)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    await OrderRepository(session).get_order_by_client_id_with_lock("ORD-1")

    statement = session.execute.await_args.args[0]
    assert statement._for_update_arg is not None


@pytest.mark.asyncio
async def test_duplicate_trade_rolls_back_only_savepoint():
    from database.repositories.trade_repository import TradeRepository
    from sqlalchemy.exc import IntegrityError

    entered = False

    class Savepoint:
        async def __aenter__(self):
            nonlocal entered
            entered = True

        async def __aexit__(self, *_args):
            return False

    missing = MagicMock()
    missing.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(return_value=missing)
    session.begin_nested.return_value = Savepoint()
    session.add.side_effect = lambda _trade: entered or pytest.fail("insert outside savepoint")
    session.flush = AsyncMock(side_effect=IntegrityError("duplicate", None, None))

    result = await TradeRepository(session).create_trade({"trade_id": "T-1"})

    assert result is None
    session.rollback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_id", [None, "ORD-1_5002", "5002"])
async def test_recorder_recomputes_from_durable_trades(monkeypatch, existing_id):
    from services.orders_recorder import OrdersRecorder

    db_order = SimpleNamespace(id=42)
    trades = [
        SimpleNamespace(amount=0.50, price=31, fee_paid=0.015, fee_currency="USDC"),
        SimpleNamespace(amount=0.14, price=32, fee_paid=0.004, fee_currency="USDC"),
    ]
    order_repo = MagicMock()
    order_repo.get_order_by_client_id_with_lock = AsyncMock(return_value=db_order)
    order_repo.recompute_order_aggregates = AsyncMock()
    trade_repo = MagicMock()
    trade_repo.get_trade_by_id = AsyncMock(
        side_effect=lambda trade_id: SimpleNamespace() if trade_id == existing_id else None
    )
    trade_repo.create_trade = AsyncMock(return_value=SimpleNamespace(id=1))
    trade_repo.get_trades_by_order_id = AsyncMock(return_value=trades)
    monkeypatch.setattr("services.orders_recorder.OrderRepository", lambda _session: order_repo)
    monkeypatch.setattr("services.orders_recorder.TradeRepository", lambda _session: trade_repo)
    db_manager = MagicMock()
    db_manager.get_session_context.side_effect = _session_context
    event = SimpleNamespace(
        order_id="ORD-1", trading_pair="HYPE-USDC",
        trade_type=SimpleNamespace(name="SELL"), amount=0.14, price=32,
        timestamp=1718000001.0, trade_fee=None,
        exchange_order_id="999", exchange_trade_id="5002",
    )

    await OrdersRecorder(db_manager, "acc", "hyperliquid_perpetual")._handle_order_filled(event)

    order_repo.get_order_by_client_id_with_lock.assert_awaited_once_with("ORD-1")
    order_repo.recompute_order_aggregates.assert_awaited_once_with(
        "ORD-1", trades, exchange_order_id="999"
    )
    if existing_id is None:
        trade_repo.create_trade.assert_awaited_once()
    else:
        trade_repo.create_trade.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_fills", [True, False])
async def test_reconcile_persistence_selects_fills_or_tracked(service, monkeypatch, has_fills):
    from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
    import database
    from database.repositories import trade_repository

    connector = MagicMock()
    order = MagicMock(spec=InFlightOrder)
    order.client_order_id = "ORD-1"
    order.exchange_order_id = "e1"
    connector.in_flight_orders = {"ORD-1": order}
    service._trading_connectors = {"acc": {"binance": connector}}

    order_update = MagicMock()
    order_update.new_state = OrderState.FILLED
    connector._request_order_status = AsyncMock(return_value=order_update)
    connector._is_order_not_found_during_status_update_error = MagicMock(return_value=False)

    external_fills = [{"tid": "5001", "sz": "5.0", "px": "1.5", "time": "1718000000000"}] if has_fills else []

    monkeypatch.setattr(service, "_fetch_hyperliquid_order_fills", AsyncMock(return_value=external_fills))

    persist_tracked = AsyncMock()
    persist_external = AsyncMock()
    monkeypatch.setattr(service, "_persist_tracked_order_fill", persist_tracked)
    monkeypatch.setattr(service, "_persist_external_order_fills", persist_external)

    monkeypatch.setattr(database, "OrderRepository", lambda s: MagicMock())
    monkeypatch.setattr(trade_repository, "TradeRepository", lambda s: MagicMock())

    service.db_manager.get_session_context.side_effect = _session_context

    await service.reconcile_active_orders()

    if has_fills:
        persist_tracked.assert_not_awaited()
        persist_external.assert_awaited_once()
    else:
        persist_tracked.assert_awaited_once()
        persist_external.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_before_fill_keeps_order_reconcilable(monkeypatch):
    from services.orders_recorder import OrdersRecorder

    order = SimpleNamespace(
        status="OPEN", filled_amount=0, amount=5, exchange_order_id=None,
    )
    order_repo = MagicMock()
    order_repo.get_order_by_client_id_with_lock = AsyncMock(return_value=order)
    monkeypatch.setattr("services.orders_recorder.OrderRepository", lambda _session: order_repo)
    db_manager = MagicMock()
    db_manager.get_session_context.side_effect = _session_context
    event = SimpleNamespace(order_id="ORD-1", exchange_order_id="999")

    await OrdersRecorder(db_manager, "acc", "hyperliquid_perpetual")._handle_order_completed(event)

    assert order.status == "OPEN"
    assert order.exchange_order_id == "999"


@pytest.mark.asyncio
async def test_sync_keeps_terminal_fill_tracked_until_reconcile(service, monkeypatch):
    import database
    from hummingbot.core.data_type.in_flight_order import OrderState

    order = SimpleNamespace(client_order_id="ORD-1", current_state=OrderState.FILLED)
    connector = SimpleNamespace(in_flight_orders={"ORD-1": order})
    db_order = SimpleNamespace(status="OPEN")
    order_repo = MagicMock()
    order_repo.get_order_by_client_id = AsyncMock(return_value=db_order)
    order_repo.update_order_status = AsyncMock()
    monkeypatch.setattr(database, "OrderRepository", lambda _session: order_repo)
    service.db_manager.get_session_context.side_effect = _session_context

    await service._sync_orders_to_database(connector, "acc", "hl")

    assert connector.in_flight_orders["ORD-1"] is order
    order_repo.update_order_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_filled_zero_is_retried_without_publishing_false_fill(service, monkeypatch):
    import database
    from database.repositories import trade_repository
    from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState

    connector = MagicMock()
    order = MagicMock(spec=InFlightOrder)
    order.client_order_id = "ORD-1"
    order.exchange_order_id = "999"
    connector.in_flight_orders = {"ORD-1": order}
    connector._request_order_status = AsyncMock(return_value=SimpleNamespace(new_state=OrderState.FILLED))
    connector._is_order_not_found_during_status_update_error = MagicMock(return_value=False)
    service._trading_connectors = {"acc": {"hyperliquid_perpetual": connector}}
    db_order = SimpleNamespace(id=1, status="FILLED", filled_amount=0, amount=5)
    order_repo = MagicMock()
    order_repo.get_order_by_client_id = AsyncMock(return_value=db_order)
    order_repo.update_order_status = AsyncMock()
    monkeypatch.setattr(database, "OrderRepository", lambda _session: order_repo)
    trade_repo = MagicMock()
    trade_repo.get_trades_by_order_id = AsyncMock(return_value=[])
    monkeypatch.setattr(trade_repository, "TradeRepository", lambda _session: trade_repo)
    monkeypatch.setattr(service, "_fetch_hyperliquid_order_fills", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_persist_tracked_order_fill", AsyncMock())
    service.db_manager.get_session_context.side_effect = _session_context

    await service.reconcile_active_orders()

    assert connector.in_flight_orders["ORD-1"] is order
    order_repo.update_order_status.assert_awaited_once_with(
        client_order_id="ORD-1", status="SUBMITTED", error_message=None,
    )
    service._persist_tracked_order_fill.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_terminal_fill_stays_partial(service, monkeypatch):
    import database
    from database.repositories import trade_repository
    from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState

    connector = MagicMock()
    order = MagicMock(spec=InFlightOrder)
    order.client_order_id = "ORD-1"
    order.exchange_order_id = "999"
    connector.in_flight_orders = {"ORD-1": order}
    connector._request_order_status = AsyncMock(return_value=SimpleNamespace(new_state=OrderState.FILLED))
    connector._is_order_not_found_during_status_update_error = MagicMock(return_value=False)
    service._trading_connectors = {"acc": {"hyperliquid_perpetual": connector}}
    db_order = SimpleNamespace(id=1, status="FILLED", filled_amount=2, amount=5)
    order_repo = MagicMock()
    order_repo.get_order_by_client_id = AsyncMock(return_value=db_order)
    order_repo.update_order_status = AsyncMock()
    monkeypatch.setattr(database, "OrderRepository", lambda _session: order_repo)
    trade_repo = MagicMock()
    trade_repo.get_trades_by_order_id = AsyncMock(
        return_value=[SimpleNamespace(amount=2)]
    )
    monkeypatch.setattr(trade_repository, "TradeRepository", lambda _session: trade_repo)
    monkeypatch.setattr(service, "_fetch_hyperliquid_order_fills", AsyncMock(return_value=[]))
    service.db_manager.get_session_context.side_effect = _session_context

    await service.reconcile_active_orders()

    order_repo.update_order_status.assert_awaited_once_with(
        client_order_id="ORD-1", status="PARTIALLY_FILLED", error_message=None,
    )
    assert "ORD-1" in connector.in_flight_orders


@pytest.mark.asyncio
async def test_reconcile_calls_are_serialized(service, monkeypatch):
    entered = 0
    max_entered = 0

    async def reconcile_once():
        nonlocal entered, max_entered
        entered += 1
        max_entered = max(max_entered, entered)
        await asyncio.sleep(0)
        entered -= 1
        return {}

    monkeypatch.setattr(service, "_reconcile_active_orders_once", reconcile_once)

    await asyncio.gather(service.reconcile_active_orders(), service.reconcile_active_orders())

    assert max_entered == 1


@pytest.mark.asyncio
async def test_order_not_found_remains_tracked_and_unverified(service):
    from hummingbot.core.data_type.in_flight_order import InFlightOrder

    connector = MagicMock()
    order = MagicMock(spec=InFlightOrder)
    order.client_order_id = "ORD-1"
    connector.in_flight_orders = {"ORD-1": order}
    connector._request_order_status = AsyncMock(side_effect=RuntimeError("not found"))
    connector._is_order_not_found_during_status_update_error = MagicMock(return_value=True)
    service._trading_connectors = {"acc": {"hyperliquid_perpetual": connector}}

    summary = await service.reconcile_active_orders()

    assert "ORD-1" in connector.in_flight_orders
    assert summary["unverified"] == 1


@pytest.mark.asyncio
async def test_user_fills_make_filled_consistent_and_remove_once(service, monkeypatch):
    import database
    from database.repositories import trade_repository
    from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState

    connector = MagicMock()
    order = MagicMock(spec=InFlightOrder)
    order.client_order_id = "ORD-1"
    order.exchange_order_id = "999"
    connector.in_flight_orders = {"ORD-1": order}
    connector._request_order_status = AsyncMock(return_value=SimpleNamespace(new_state=OrderState.FILLED))
    connector._is_order_not_found_during_status_update_error = MagicMock(return_value=False)
    service._trading_connectors = {"acc": {"hl": connector}}
    db_order = SimpleNamespace(id=1, status="OPEN", filled_amount=0, amount=5)
    order_repo = MagicMock()
    order_repo.get_order_by_client_id = AsyncMock(return_value=db_order)
    order_repo.update_order_status = AsyncMock()
    monkeypatch.setattr(database, "OrderRepository", lambda _session: order_repo)
    trade_repo = MagicMock()
    trade_repo.get_trades_by_order_id = AsyncMock(
        return_value=[SimpleNamespace(amount=5)]
    )
    monkeypatch.setattr(trade_repository, "TradeRepository", lambda _session: trade_repo)
    monkeypatch.setattr(service, "_fetch_hyperliquid_order_fills", AsyncMock(return_value=[{"tid": "1"}]))

    async def persist_external(**_kwargs):
        db_order.filled_amount = 5
        db_order.status = "FILLED"

    monkeypatch.setattr(service, "_persist_external_order_fills", persist_external)
    service.db_manager.get_session_context.side_effect = _session_context

    await service.reconcile_active_orders()
    await service.reconcile_active_orders()

    assert "ORD-1" not in connector.in_flight_orders
    order_repo.update_order_status.assert_awaited_once_with(
        client_order_id="ORD-1", status="FILLED", error_message=None,
    )
