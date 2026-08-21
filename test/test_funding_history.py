import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError


def _event(**overrides):
    values = {
        "timestamp": datetime(2026, 8, 21, tzinfo=timezone.utc).timestamp(),
        "trading_pair": "HYPE-USDC",
        "funding_rate": "0.001",
        "amount": "1.25",
        "fee_currency": "USDC",
        "exchange_funding_id": "exchange-funding-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def recorder(monkeypatch):
    pytest.importorskip("hummingbot")
    from services import funding_recorder as module

    repository = MagicMock()
    repository.funding_payment_exists = AsyncMock(return_value=False)
    repository.create_funding_payment = AsyncMock(return_value=SimpleNamespace())
    session = MagicMock()
    session.commit = AsyncMock()

    @asynccontextmanager
    async def session_context():
        yield session

    recorder = module.FundingRecorder(MagicMock(), "account", "hyperliquid_perpetual")
    recorder.db_manager.get_session = session_context
    recorder._connector = SimpleNamespace(get_sell_collateral_token=lambda _pair: "USDC")
    monkeypatch.setattr(module, "FundingRepository", lambda _session: repository)
    return recorder, repository


def test_funding_recorder_rejects_missing_fee_currency(recorder):
    funding_recorder, repository = recorder
    event = _event()
    del event.fee_currency
    funding_recorder._connector = None

    with pytest.raises(ValueError, match="currency"):
        asyncio.run(funding_recorder.record_funding_payment(event, "account", "connector"))

    repository.funding_payment_exists.assert_not_awaited()


def test_funding_payment_exchange_id_deduplicates_and_preserves_provider_pair(recorder):
    funding_recorder, repository = recorder
    checked_ids = []

    async def exists(payment_id):
        checked_ids.append(payment_id)
        return len(checked_ids) > 1

    repository.funding_payment_exists.side_effect = exists
    first = _event()
    second = _event(amount="9.99", funding_rate="0.9")

    asyncio.run(funding_recorder.record_funding_payment(first, "account", "connector"))
    asyncio.run(funding_recorder.record_funding_payment(second, "account", "connector"))

    assert checked_ids[0] == checked_ids[1]
    assert "exchange-funding-1" in checked_ids[0]
    assert repository.create_funding_payment.await_count == 1
    assert repository.create_funding_payment.await_args.args[0]["trading_pair"] == "HYPE-USDC"
    assert repository.create_funding_payment.await_args.args[0]["exchange_funding_id"] == "exchange-funding-1"


def test_funding_payment_uses_provider_collateral_currency(recorder):
    funding_recorder, repository = recorder
    from hummingbot.core.event.events import FundingPaymentCompletedEvent

    event = FundingPaymentCompletedEvent(
        timestamp=datetime(2026, 8, 21, tzinfo=timezone.utc).timestamp(),
        market="hyperliquid_perpetual",
        trading_pair="HYPE-USDC",
        amount=Decimal("1.25"),
        funding_rate=Decimal("0.001"),
    )

    asyncio.run(funding_recorder.record_funding_payment(event, "account", "connector"))

    recorded = repository.create_funding_payment.await_args.args[0]
    assert recorded["fee_currency"] == "USDC"
    assert recorded["timestamp"] == datetime(2026, 8, 21, tzinfo=timezone.utc)
    assert recorded["funding_rate"] == Decimal("0.001")
    assert recorded["funding_payment"] == Decimal("1.25")
    assert recorded["exchange_funding_id"] is None
    assert recorded["funding_payment_id"].startswith("funding:")


def test_funding_payment_unique_race_is_deduplicated(recorder):
    funding_recorder, repository = recorder
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    @asynccontextmanager
    async def session_context():
        yield session

    funding_recorder.db_manager.get_session = session_context
    repository.funding_payment_exists.side_effect = [False, True]
    repository.create_funding_payment.side_effect = IntegrityError("insert", {}, Exception())

    result = asyncio.run(
        funding_recorder.record_funding_payment(_event(), "account", "connector")
    )

    assert result is None
    session.rollback.assert_awaited_once()


def test_funding_history_db_failure_is_http_500():
    pytest.importorskip("hummingbot")
    from fastapi import HTTPException
    from services.accounts_service import AccountsService

    service = AccountsService.__new__(AccountsService)
    service.ensure_db_initialized = AsyncMock()
    service.db_manager = MagicMock()

    @asynccontextmanager
    async def broken_session():
        raise RuntimeError("database unavailable")
        yield

    service.db_manager.get_session_context = broken_session

    with pytest.raises(HTTPException) as raised:
        asyncio.run(service.get_funding_payments("account", "connector"))

    assert raised.value.status_code == 500


def test_funding_history_serializes_exact_signed_decimals():
    pytest.importorskip("hummingbot")
    from database.repositories.funding_repository import FundingRepository

    payment = SimpleNamespace(
        id=1,
        funding_payment_id="funding:exact",
        timestamp=datetime(2026, 8, 21, tzinfo=timezone.utc),
        account_name="account",
        connector_name="hyperliquid_perpetual",
        trading_pair="HYPE-USDC",
        funding_rate=Decimal("0.000012345678901234"),
        funding_payment=Decimal("-0.123456789012345678"),
        fee_currency="USDC",
        position_size=None,
        position_side=None,
        exchange_funding_id=None,
    )

    row = FundingRepository(MagicMock()).to_dict(payment)

    assert row["funding_rate"] == "0.000012345678901234"
    assert row["funding_payment"] == "-0.123456789012345678"


def test_funding_history_empty_success_remains_empty():
    pytest.importorskip("hummingbot")
    from services import accounts_service as module

    service = module.AccountsService.__new__(module.AccountsService)
    service.ensure_db_initialized = AsyncMock()
    service.db_manager = MagicMock()
    repository = MagicMock()
    repository.get_funding_payments = AsyncMock(return_value=[])

    @asynccontextmanager
    async def session_context():
        yield MagicMock()

    service.db_manager.get_session_context = session_context
    original_repository = module.FundingRepository
    module.FundingRepository = lambda _session: repository
    try:
        result = asyncio.run(service.get_funding_payments("account", "connector"))
    finally:
        module.FundingRepository = original_repository

    assert result == []


def test_funding_history_malformed_row_is_http_500():
    pytest.importorskip("hummingbot")
    from fastapi import HTTPException
    from services import accounts_service as module

    service = module.AccountsService.__new__(module.AccountsService)
    service.ensure_db_initialized = AsyncMock()
    service.db_manager = MagicMock()
    repository = MagicMock()
    repository.get_funding_payments = AsyncMock(return_value=[object()])
    repository.to_dict.return_value = {
        "funding_payment_id": " ",
        "timestamp": "not-a-timestamp",
        "trading_pair": "HYPE-USDC",
        "funding_rate": float("nan"),
        "funding_payment": 1,
        "fee_currency": "USDC",
    }

    @asynccontextmanager
    async def session_context():
        yield MagicMock()

    service.db_manager.get_session_context = session_context
    original_repository = module.FundingRepository
    module.FundingRepository = lambda _session: repository
    try:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(service.get_funding_payments("account", "connector"))
    finally:
        module.FundingRepository = original_repository

    assert raised.value.status_code == 500


def test_funding_history_connector_failure_does_not_return_partial():
    pytest.importorskip("hummingbot")
    from fastapi import HTTPException
    from models import FundingPaymentFilterRequest
    from routers import trading

    accounts_service = MagicMock()
    accounts_service.get_funding_payments = AsyncMock(
        side_effect=[[{"funding_payment_id": "one", "timestamp": "2026-01-01"}], RuntimeError("provider down")]
    )
    connector_service = MagicMock()
    connector_service.get_all_trading_connectors.return_value = {
        "account": {"first_perpetual": object(), "second_perpetual": object()}
    }

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            trading.get_funding_payments(
                FundingPaymentFilterRequest(limit=10), accounts_service, connector_service
            )
        )

    assert raised.value.status_code == 500
