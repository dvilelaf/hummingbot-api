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
    ]
    repo = MagicMock()
    repo.get_active_orders = AsyncMock(return_value=db_orders)
    service.db_manager.get_session_context.side_effect = _session_context
    monkeypatch.setattr(database, "OrderRepository", lambda _session: repo)

    await service._load_existing_orders(connector, "acc", "hl")

    assert connector.in_flight_orders["ORD-1"] is existing
    assert connector.in_flight_orders["ORD-2"].client_order_id == "ORD-2"


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
    order_repo.update_order_fill = AsyncMock()
    trade_repo = MagicMock()
    trade_repo.get_trade_by_id = AsyncMock(return_value=None)
    trade_repo.create_trade = AsyncMock(return_value=SimpleNamespace(id=99, trade_id="ORD-1_5001"))
    fills = [{"tid": 5001, "sz": "5.0", "px": "1.5", "fee": "0.05",
              "feeToken": "USDC", "time": "1718000000000", "oid": 999}]
    return SimpleNamespace(order_repo=order_repo, trade_repo=trade_repo,
                           fills=fills, order=connector)


@pytest.mark.asyncio
async def test_fill_creates_canonical_updates_once(service, fill_ctx):
    await service._persist_external_order_fills(
        order_repo=fill_ctx.order_repo, trade_repo=fill_ctx.trade_repo,
        order=fill_ctx.order, fills=fill_ctx.fills,
    )
    fill_ctx.trade_repo.create_trade.assert_awaited_once()
    created_kwargs = fill_ctx.trade_repo.create_trade.await_args[0][0]
    assert created_kwargs["trade_id"] == "ORD-1_5001"
    fill_ctx.order_repo.update_order_fill.assert_awaited_once()


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
    service._trading_connectors = {"acc": {"hl": connector}}

    order_update = MagicMock()
    order_update.new_state = OrderState.FILLED
    connector._request_order_status = AsyncMock(return_value=order_update)
    connector._is_order_not_found_during_status_update_error = MagicMock(return_value=False)

    external_fills = [{"tid": "5001", "sz": "5.0", "px": "1.5", "time": "1718000000000"}] if has_fills else []

    async def fake_fetch(conn, o):
        return external_fills

    monkeypatch.setattr(service, "_refresh_tracked_order_fills", AsyncMock())
    monkeypatch.setattr(service, "_fetch_hyperliquid_order_fills", fake_fetch)

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
