from decimal import Decimal
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class ProviderSnapshotRequest(BaseModel):
    account_name: str = Field(min_length=1)
    connector_name: str = Field(min_length=1)
    trading_pair: str = Field(min_length=1)
    refresh_portfolio: bool = True


class ProviderSnapshotResponse(BaseModel):
    account_name: str
    connector_name: str
    trading_pair: str
    provider_available: bool
    status: Literal["available", "issues"]
    operator_issues: list[str] = Field(default_factory=list)
    limit_maker_order_supported: bool = False
    order_types: list[str] = Field(default_factory=list)
    provider_actions: list[str] = Field(default_factory=list)
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
