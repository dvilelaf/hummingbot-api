from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    source_amount: Optional[Decimal] = Field(default=None, gt=0, allow_inf_nan=False)
    source_asset: Optional[str] = Field(default=None, min_length=1)
    destination_amount: Optional[Decimal] = Field(default=None, gt=0, allow_inf_nan=False)
    destination_asset: Optional[str] = Field(default=None, min_length=1)
    quoted_provider_cost_usd: Optional[Decimal] = Field(default=None, ge=0, allow_inf_nan=False)
    quoted_gas_cost_usd: Optional[Decimal] = Field(default=None, ge=0, allow_inf_nan=False)
    quoted_native_gas_amount: Optional[Decimal] = Field(default=None, ge=0, allow_inf_nan=False)
    quoted_native_gas_asset: Optional[str] = Field(default=None, min_length=1)
    quoted_at: Optional[datetime] = None

    @field_validator("quoted_at")
    @classmethod
    def quoted_at_must_be_timezone_aware(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is not None and value.utcoffset() is None:
            raise ValueError("quoted_at must be timezone-aware")
        return value
