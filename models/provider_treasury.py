from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ProviderTreasuryRebalanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_name: str = Field(min_length=1)
    destination_chain: str = Field(min_length=1)
    destination_network: str = Field(min_length=1)
    destination_asset: str = Field(min_length=1)
    destination_wallet_ref: str = Field(min_length=1)
    target_notional_eur: Decimal = Field(gt=0)
    destination_amount: Optional[Decimal] = Field(default=None, gt=0)
    route_id: str = Field(min_length=1)
    max_cost_bps: Decimal = Field(ge=Decimal("0"))
    idempotency_key: str = Field(min_length=1)


class ProviderTreasuryRebalanceExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1)


class ProviderTreasuryRebalanceResponse(BaseModel):
    id: str
    status: str
    transaction_hash: Optional[str] = None
    error: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
