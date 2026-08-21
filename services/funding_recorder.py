import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Dict, Optional

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.event.event_forwarder import SourceInfoEventForwarder
from hummingbot.core.event.events import MarketEvent, FundingPaymentCompletedEvent
from sqlalchemy.exc import IntegrityError

from database import AsyncDatabaseManager, FundingRepository


class FundingRecorder:
    """
    Records funding payment events and associates them with position data.
    Follows the same pattern as OrdersRecorder for consistency.
    """

    def __init__(self, db_manager: AsyncDatabaseManager, account_name: str, connector_name: str):
        self.db_manager = db_manager
        self.account_name = account_name
        self.connector_name = connector_name
        self._connector: Optional[ConnectorBase] = None
        self.logger = logging.getLogger(__name__)
        
        # Create event forwarder for funding payments
        self._funding_payment_forwarder = SourceInfoEventForwarder(self._did_funding_payment)
        
        # Event pairs mapping events to forwarders
        self._event_pairs = [
            (MarketEvent.FundingPaymentCompleted, self._funding_payment_forwarder),
        ]
    
    def start(self, connector: ConnectorBase):
        """Start recording funding payments for the given connector"""
        # Idempotency guard: prevent double-registration of listeners
        if self._connector is not None:
            self.logger.warning(f"FundingRecorder already started for {self.account_name}/{self.connector_name}, ignoring duplicate start")
            return

        self._connector = connector

        # Subscribe to funding payment events
        for event, forwarder in self._event_pairs:
            connector.add_listener(event, forwarder)
            
        self.logger.info(f"FundingRecorder started for {self.account_name}/{self.connector_name}")
    
    async def stop(self):
        """Stop recording funding payments"""
        if self._connector:
            for event, forwarder in self._event_pairs:
                self._connector.remove_listener(event, forwarder)
            self.logger.info(f"FundingRecorder stopped for {self.account_name}/{self.connector_name}")
    
    def _did_funding_payment(self, event_tag: int, market: ConnectorBase, event: FundingPaymentCompletedEvent):
        """Handle funding payment events - called by SourceInfoEventForwarder"""
        try:
            asyncio.create_task(self._handle_funding_payment(event))
        except Exception as e:
            self.logger.error(f"Error in _did_funding_payment: {e}")
    
    async def _handle_funding_payment(self, event: FundingPaymentCompletedEvent):
        """Handle funding payment events"""
        # Get current position data if available
        position_data = None
        if self._connector and hasattr(self._connector, 'account_positions'):
            try:
                positions = self._connector.account_positions
                if positions:
                    for position in positions.values():
                        if position.trading_pair == event.trading_pair:
                            position_data = {
                                "size": float(position.amount),
                                "side": position.position_side.name if hasattr(position.position_side, 'name') else str(position.position_side),
                            }
                            break
            except Exception as e:
                self.logger.warning(f"Could not get position data for funding payment: {e}")
        
        # Record the funding payment
        await self.record_funding_payment(event, self.account_name, self.connector_name, position_data)

    async def record_funding_payment(self, event: FundingPaymentCompletedEvent, 
                                   account_name: str, connector_name: str, 
                                   position_data: Optional[Dict] = None):
        """
        Record a funding payment event with optional position association.
        
        Args:
            event: FundingPaymentCompletedEvent from Hummingbot
            account_name: Account name
            connector_name: Connector name
            position_data: Optional position data at time of payment
        """
        fee_currency = getattr(event, "fee_currency", None)
        if not isinstance(fee_currency, str) or not fee_currency.strip():
            collateral_token = getattr(self._connector, "get_sell_collateral_token", None)
            fee_currency = (
                collateral_token(event.trading_pair) if callable(collateral_token) else None
            )
        if not isinstance(fee_currency, str) or not fee_currency.strip():
            raise ValueError("Funding payment currency unavailable")
        fee_currency = fee_currency.strip()

        funding_rate = Decimal(str(event.funding_rate))
        funding_payment_amount = Decimal(str(event.amount))
        if not funding_rate.is_finite() or not funding_payment_amount.is_finite():
            raise ValueError("Funding payment rate and amount must be finite")
        if isinstance(event.timestamp, datetime):
            event_timestamp = event.timestamp
        else:
            timestamp = Decimal(str(event.timestamp))
            if not timestamp.is_finite():
                raise ValueError("Funding payment timestamp must be finite")
            event_timestamp = datetime.fromtimestamp(float(timestamp), tz=UTC)
        exchange_funding_id = getattr(event, "exchange_funding_id", None)
        exchange_funding_id = str(exchange_funding_id).strip() if exchange_funding_id is not None else None
        exchange_funding_id = exchange_funding_id or None

        if exchange_funding_id:
            funding_payment_id = f"{account_name}:{connector_name}:exchange:{exchange_funding_id}"
        else:
            identity = "\x1f".join(
                (
                    str(account_name),
                    str(connector_name),
                    str(event.trading_pair),
                    event_timestamp.isoformat(),
                    str(funding_rate),
                    str(funding_payment_amount),
                    fee_currency,
                )
            )
            funding_payment_id = f"funding:{hashlib.sha256(identity.encode()).hexdigest()}"

        funding_data = {
            "funding_payment_id": funding_payment_id,
            "timestamp": event_timestamp,
            "account_name": account_name,
            "connector_name": connector_name,
            "trading_pair": event.trading_pair,
            "funding_rate": funding_rate,
            "funding_payment": funding_payment_amount,
            "fee_currency": fee_currency,
            "exchange_funding_id": exchange_funding_id,
        }

        if position_data:
            funding_data.update({
                "position_size": float(position_data.get("size", 0)),
                "position_side": position_data.get("side"),
            })

        async with self.db_manager.get_session() as session:
            funding_repo = FundingRepository(session)
            if await funding_repo.funding_payment_exists(funding_data["funding_payment_id"]):
                self.logger.info(f"Funding payment {funding_data['funding_payment_id']} already exists, skipping")
                return

            try:
                funding_payment = await funding_repo.create_funding_payment(funding_data)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await funding_repo.funding_payment_exists(funding_data["funding_payment_id"]):
                    self.logger.info(
                        f"Funding payment {funding_data['funding_payment_id']} already exists, skipping"
                    )
                    return
                raise

            self.logger.info(
                f"Recorded funding payment for {account_name}/{connector_name}: "
                f"{event.trading_pair} - Rate: {funding_rate}, Payment: {funding_payment} "
                f"{funding_data['fee_currency']}"
            )

            return funding_payment
