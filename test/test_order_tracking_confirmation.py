from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("hummingbot")
from fastapi import HTTPException


@pytest.fixture
def accounts_service():
    from services.accounts_service import AccountsService

    service = AccountsService.__new__(AccountsService)
    service.db_manager = MagicMock()
    return service


def _session_context():
    @asynccontextmanager
    async def context():
        yield MagicMock()

    return context()


@pytest.mark.asyncio
async def test_confirmation_preserves_persisted_terminal_rejection(
    accounts_service, monkeypatch
):
    import services.accounts_service as accounts_module

    failed_order = SimpleNamespace(
        account_name="main",
        connector_name="hyperliquid",
        trading_pair="PURR-USDC",
        status="FAILED",
        error_message="insufficientSpotBalanceRejected",
    )
    repository = MagicMock()
    repository.get_order_by_client_id = AsyncMock(return_value=failed_order)
    accounts_service.db_manager.get_session_context.side_effect = _session_context
    monkeypatch.setattr(accounts_module, "OrderRepository", lambda _session: repository)
    monkeypatch.setattr(accounts_module, "ORDER_TRACKING_CONFIRM_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(accounts_module, "ORDER_TRACKING_CONFIRM_POLL_SECONDS", 0)

    connector = SimpleNamespace(in_flight_orders={})

    with pytest.raises(HTTPException) as exc_info:
        await accounts_service._confirm_connector_order_tracked(
            connector=connector,
            account_name="main",
            connector_name="hyperliquid",
            order_id="OID-1",
            trading_pair="PURR-USDC",
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "insufficientSpotBalanceRejected"


@pytest.mark.asyncio
async def test_confirmation_keeps_ambiguous_submission_fail_closed(
    accounts_service, monkeypatch
):
    import services.accounts_service as accounts_module

    repository = MagicMock()
    repository.get_order_by_client_id = AsyncMock(return_value=None)
    accounts_service.db_manager.get_session_context.side_effect = _session_context
    monkeypatch.setattr(accounts_module, "OrderRepository", lambda _session: repository)
    monkeypatch.setattr(accounts_module, "ORDER_TRACKING_CONFIRM_TIMEOUT_SECONDS", 0)

    connector = SimpleNamespace(in_flight_orders={})

    with pytest.raises(HTTPException) as exc_info:
        await accounts_service._confirm_connector_order_tracked(
            connector=connector,
            account_name="main",
            connector_name="hyperliquid",
            order_id="OID-2",
            trading_pair="PURR-USDC",
        )

    assert exc_info.value.status_code == 502
    assert "did not confirm exchange acceptance" in exc_info.value.detail
