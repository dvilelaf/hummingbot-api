from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ProviderTreasuryRebalanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_name: str = Field(min_length=1)
    source_venue: str = Field(min_length=1)
    source_network: str = Field(min_length=1)
    source_asset: str = Field(min_length=1)
    source_asset_decimals: Optional[int] = Field(default=None, ge=0, le=36)
    destination_venue: str = Field(min_length=1)
    destination_network: str = Field(min_length=1)
    destination_asset: str = Field(min_length=1)
    destination_account: str = Field(min_length=1)
    amount: Decimal = Field(gt=0)
    route: str = Field(min_length=1)
    idempotency_key: Optional[str] = Field(default=None, min_length=1)


class ProviderTreasuryRebalanceExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_name: Optional[str] = Field(default=None, min_length=1)
    idempotency_key: Optional[str] = Field(default=None, min_length=1)


class ProviderTreasuryRebalanceResponse(BaseModel):
    id: str
    status: str
    provider: Optional[str] = None
    route: Optional[str] = None
    transaction_hash: Optional[str] = None
    approval_transaction_hash: Optional[str] = None
    burn_transaction_hash: Optional[str] = None
    finalize_transaction_hash: Optional[str] = None
    provider_status: Optional[str] = None
    provider_error: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
