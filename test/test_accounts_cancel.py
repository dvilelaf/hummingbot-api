from pathlib import Path


def test_cancel_order_uses_in_flight_order_trading_pair():
    source = (Path(__file__).resolve().parents[1] / "services" / "accounts_service.py").read_text()
    cancel_source = source[source.index("async def cancel_order") : source.index("async def set_leverage")]

    assert 'trading_pair="NA"' not in cancel_source
    assert 'order = connector.in_flight_orders[client_order_id]' in cancel_source
    assert 'getattr(order, "trading_pair", None)' in cancel_source
    assert "connector.cancel(trading_pair=trading_pair, client_order_id=client_order_id)" in cancel_source

def test_cowswap_poll_route_delegates_to_accounts_service():
    source = (Path(__file__).resolve().parents[1] / "routers" / "trading.py").read_text()
    poll_source = source[source.index("async def poll_order") : source.index("@router.post(\"/positions\"")]

    assert "@router.post(\"/{account_name}/{connector_name}/orders/{client_order_id}/poll\")" in source
    assert "return await accounts_service.poll_order(" in poll_source
    assert "account_name=account_name" in poll_source
    assert "connector_name=connector_name" in poll_source
    assert "client_order_id=client_order_id" in poll_source


def test_accounts_service_cowswap_poll_is_fail_closed_for_other_connectors():
    source = (Path(__file__).resolve().parents[1] / "services" / "accounts_service.py").read_text()
    poll_source = source[source.index("async def poll_order") : source.index("async def set_leverage")]

    assert "connector_name != COWSWAP_CONNECTOR_NAME" in poll_source
    assert "does not support explicit order polling" in poll_source
    assert "return await poll_cowswap_order(" in poll_source
