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
