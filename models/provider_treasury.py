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
    amount: Decimal = Field(gt=0)
    route_id: str = Field(min_length=1)
    max_cost_bps: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1)


class ProviderTreasuryRebalanceExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderTreasuryRebalanceResponse(BaseModel):
    id: str
    status: str
    transaction_hash: Optional[str] = None
    error: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
