from pathlib import Path


def test_cancel_order_uses_in_flight_order_trading_pair():
    source = (Path(__file__).resolve().parents[1] / "services" / "accounts_service.py").read_text()
    cancel_source = source[source.index("async def cancel_order") : source.index("async def set_leverage")]

    assert 'trading_pair="NA"' not in cancel_source
    assert 'order = connector.in_flight_orders[client_order_id]' in cancel_source
    assert 'getattr(order, "trading_pair", None)' in cancel_source
    assert "connector.cancel(trading_pair=trading_pair, client_order_id=client_order_id)" in cancel_source
