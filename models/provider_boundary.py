from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class ProviderSnapshotRequest(BaseModel):
    account_name: str = Field(min_length=1)
    connector_name: str = Field(min_length=1)
    network: Optional[str] = Field(default=None, min_length=1)
    trading_pair: str = Field(min_length=1)
    refresh_portfolio: bool = True
    route_id: Optional[str] = Field(default=None, min_length=1)
    wallet_ref: Optional[str] = Field(default=None, min_length=1)


class ProviderPosition(BaseModel):
    account_name: str
    connector_name: str
    trading_pair: str
    side: Literal["LONG", "SHORT"]
    quantity: Decimal = Field(gt=0)
    entry_price: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    leverage: Optional[Decimal] = None


class ProviderSnapshotResponse(BaseModel):
    account_name: str
    connector_name: str
    trading_pair: str
    provider_available: bool
    status: Literal["available", "issues"]
    positions_status: Literal["unsupported", "available", "issues"]
    operator_issues: list[str] = Field(default_factory=list)
    limit_maker_order_supported: bool = False
    order_types: list[str] = Field(default_factory=list)
    provider_actions: list[str] = Field(default_factory=list)
    positions: list[ProviderPosition] = Field(default_factory=list)
    portfolio: Optional[Dict[str, Any]] = None
    trading_rule: Optional[Dict[str, Any]] = None


class ProviderIntentRequest(BaseModel):
    account_name: str = Field(min_length=1)
    connector_name: str = Field(min_length=1)
    market_id: str = Field(min_length=1)
    action: Literal["order", "swap"]
    mode: Literal["testnet", "mainnet"]
    side: Literal["BUY", "SELL"]
    quantity: Decimal = Field(gt=0)
    order_type: Optional[Literal["LIMIT", "LIMIT_MAKER", "MARKET"]] = None
    price: Optional[Decimal] = None
    correlation_id: Optional[str] = None
    position_effect: Literal["open", "reduce"] = Field(default="open")
    preflight_only: bool = False
    risk_metadata: Dict[str, Any] = Field(default_factory=dict)
    wallet_identity: Optional[Dict[str, Any]] = None


class ProviderIntentResponse(BaseModel):
    status: str
    correlation_id: Optional[str] = None
    external_order_id: Optional[str] = None
    submitted_quantity: Optional[Decimal] = None
    submitted_notional: Optional[Decimal] = None
    provider_status: Optional[str] = None
    provider_error: Optional[str] = None
    retry_after_seconds: Optional[int] = Field(default=None, ge=1)
    external_transaction_id: Optional[str] = None
    sent_asset: Optional[str] = None
    sent_quantity: Optional[Decimal] = Field(default=None, gt=0)
    received_asset: Optional[str] = None
    received_quantity: Optional[Decimal] = Field(default=None, gt=0)
    fee_asset: Optional[str] = None
    fee_amount: Optional[Decimal] = Field(default=None, ge=0)
    executed_at: Optional[datetime] = None
