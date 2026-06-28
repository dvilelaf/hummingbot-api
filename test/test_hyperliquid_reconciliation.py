from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_terminal_reconciliation_refreshes_fills_before_status_update():
    source = (ROOT / "services" / "unified_connector_service.py").read_text()
    reconcile = source[
        source.index("async def reconcile_active_orders")
        : source.index("async def _refresh_tracked_order_fills")
    ]

    assert "await self._refresh_tracked_order_fills(connector, order)" in reconcile
    assert "await self._persist_tracked_order_fill(order_repo, order)" in reconcile
    assert reconcile.index("await self._persist_tracked_order_fill(order_repo, order)") < reconcile.index(
        "await order_repo.update_order_status("
    )


def test_fill_refresh_uses_standard_hummingbot_hooks():
    source = (ROOT / "services" / "unified_connector_service.py").read_text()
    refresh = source[
        source.index("async def _refresh_tracked_order_fills")
        : source.index("@staticmethod", source.index("async def _refresh_tracked_order_fills"))
    ]

    assert "_all_trade_updates_for_order" in refresh
    assert "process_trade_update" in refresh
    assert "_update_trade_history" in refresh


def test_external_hyperliquid_fills_are_idempotent_before_order_fill_update():
    source = (ROOT / "services" / "unified_connector_service.py").read_text()
    persist = source[
        source.index("async def _persist_external_order_fills")
        : source.index("async def sync_all_orders_to_database")
    ]

    assert 'await trade_repo.get_trade_by_id(trade_id)' in persist
    assert 'created_trade = await trade_repo.create_trade(' in persist
    assert 'if created_trade is None:' in persist
    assert persist.index('await trade_repo.get_trade_by_id(trade_id)') < persist.index(
        'await order_repo.update_order_fill('
    )
    assert persist.index('created_trade = await trade_repo.create_trade(') < persist.index(
        'await order_repo.update_order_fill('
    )
