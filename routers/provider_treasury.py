"""Destination-only provider treasury routes."""

import logging
import os
import re
import secrets
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from database.repositories import ProviderTreasuryRebalanceRepository
from deps import get_accounts_service, get_database_manager
from models.provider_treasury import (
    ProviderTreasuryRebalanceExecuteRequest,
    ProviderTreasuryRebalanceRequest,
    ProviderTreasuryRebalanceResponse,
    ProviderTreasuryStage,
)
from services.accounts_service import AccountsService
from services.marlin_runtime import _derive_marlin_credential_values

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Provider Treasury"], prefix="/provider/treasury")
MARLIN_PROVIDER_INTENT_TOKEN_HEADER = "x-marlin-provider-intent-token"

DESTINATION_WALLET_IDENTITY_UNAVAILABLE_BLOCKER = "destination_wallet_identity_unavailable"
DESTINATION_WALLET_REF_MISMATCH_BLOCKER = "destination_wallet_ref_mismatch"
DESTINATION_WALLET_DEFAULT_FAILED_BLOCKER = "destination_wallet_default_failed"
IDEMPOTENCY_KEY_CONFLICT_BLOCKER = "idempotency_key_conflict"
HYPERLIQUID_MAINNET_WALLET_REF = "hyperliquid:mainnet:hyperliquid_trader"
HL_BASELINE_FIELD = "_hl_baseline_usdc"
GATEWAY_TREASURY_STAGE_FIELDS = frozenset(
    {
        "index",
        "kind",
        "status",
        "sourceAmount",
        "sourceAsset",
        "destinationAmount",
        "destinationAsset",
        "transactionHash",
        "error",
    }
)
GATEWAY_TREASURY_STAGE_REQUIRED_FIELDS = frozenset({"index", "kind", "status"})
_GATEWAY_TREASURY_STAGE_FIELD_MISSING = object()

GATEWAY_RECOVERABLE_EXECUTION_STATUSES = frozenset(
    {
        "approval_submission_ambiguous",
        "approval_submission_pending",
        "approval_confirmed",
        "approval_submitted",
        "built",
        "submission_ambiguous",
        "submission_pending",
        "submitted",
        "wrap_confirmed",
        "wrap_submission_ambiguous",
        "wrap_submission_pending",
        "wrap_submitted",
    }
)


@router.post("/rebalances", response_model=ProviderTreasuryRebalanceResponse)
async def create_provider_treasury_rebalance(
    body: ProviderTreasuryRebalanceRequest,
    request: Request,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager=Depends(get_database_manager),
) -> ProviderTreasuryRebalanceResponse:
    """Create a Gateway-owned funding target from a neutral destination intent."""
    try:
        if not await accounts_service.gateway_client.ping():
            raise HTTPException(status_code=503, detail="Gateway service is not available")
        _assert_provider_treasury_authorized(request)

        destination_chain = body.destination_chain.strip().lower()
        destination_network = body.destination_network.strip().lower()
        destination_identity, gateway_identity = _destination_wallet_identities(
            accounts_service,
            destination_chain=destination_chain,
            destination_network=destination_network,
            destination_wallet_ref=body.destination_wallet_ref.strip(),
        )

        stored_request = {
            "account_name": body.account_name.strip(),
            "target_notional_eur": _decimal_payload_value(body.target_notional_eur),
            "destination_address": destination_identity["address"],
            "destination_asset": body.destination_asset.strip(),
            "destination_chain": destination_chain,
            "destination_network": destination_network,
            "destination_wallet_ref": destination_identity["wallet_ref"],
            "max_cost_bps": _decimal_payload_value(body.max_cost_bps),
            "route_id": body.route_id.strip(),
        }
        if body.destination_amount is not None:
            stored_request["destination_amount"] = _decimal_payload_value(body.destination_amount)
        database_manager = _database_manager(request, db_manager)
        initial_response_payload = {"id": body.idempotency_key, "status": "built"}
        if destination_chain == "hyperliquid" and destination_network == "mainnet":
            initial_response_payload[HL_BASELINE_FIELD] = str(
                await _hl_usdc_balance(accounts_service, body.account_name.strip())
            )
        stored_record = await _create_built_rebalance(
            database_manager,
            body.idempotency_key,
            stored_request,
            initial_response_payload,
        )
        if stored_record.request_payload != stored_request:
            raise HTTPException(status_code=409, detail=IDEMPOTENCY_KEY_CONFLICT_BLOCKER)
        if stored_record.status != "built":
            return await _refresh_rebalance_status(
                accounts_service,
                database_manager,
                body.idempotency_key,
            )

        wallet_result = await accounts_service.gateway_client.set_marlin_default_wallet(
            chain=gateway_identity["chain"],
            network=gateway_identity["network"],
            address=gateway_identity["address"],
            wallet_ref=gateway_identity["wallet_ref"],
        )
        if isinstance(wallet_result, dict) and wallet_result.get("error"):
            error = _redact_error(wallet_result.get("error"))
            raise HTTPException(
                status_code=400,
                detail=f"{DESTINATION_WALLET_DEFAULT_FAILED_BLOCKER}: {error}",
            )

        gateway_kwargs = dict(
            idempotency_key=body.idempotency_key,
            destination_chain=destination_chain,
            destination_network=destination_network,
            destination_asset=body.destination_asset.strip(),
            destination_address=destination_identity["address"],
            target_notional_eur=_decimal_payload_value(body.target_notional_eur),
            max_cost_bps=_decimal_payload_value(body.max_cost_bps),
        )
        if body.destination_amount is not None:
            gateway_kwargs["destination_amount"] = _decimal_payload_value(body.destination_amount)
        result = await accounts_service.gateway_client.create_treasury_rebalance_target(**gateway_kwargs)
        if isinstance(result, dict):
            result.setdefault("id", body.idempotency_key)
            result.setdefault("status", "built")
        response = _rebalance_response(result, rebalance_id=body.idempotency_key)
        response_payload = _rebalance_response_payload(response)
        await _update_rebalance_status(
            database_manager,
            body.idempotency_key,
            response_payload,
        )
        return response
    except HTTPException:
        raise
    except Exception as exc:
        redacted = _redact_error(exc)
        logger.error("Treasury rebalance target create failed: %s", redacted)
        raise HTTPException(status_code=500, detail=f"Treasury rebalance target create failed: {redacted}")


@router.post("/rebalances/{rebalance_id}/execute", response_model=ProviderTreasuryRebalanceResponse)
async def execute_provider_treasury_rebalance(
    rebalance_id: str,
    body: ProviderTreasuryRebalanceExecuteRequest,
    request: Request,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager=Depends(get_database_manager),
) -> ProviderTreasuryRebalanceResponse:
    """Execute Gateway's durable selection for a previously created target."""
    if body.idempotency_key != rebalance_id:
        raise HTTPException(status_code=409, detail=IDEMPOTENCY_KEY_CONFLICT_BLOCKER)
    try:
        if not await accounts_service.gateway_client.ping():
            raise HTTPException(status_code=503, detail="Gateway service is not available")
        _assert_provider_treasury_authorized(request)
        database_manager = _database_manager(request, db_manager)
        stored_record = await _get_rebalance(database_manager, rebalance_id)
        if stored_record is None:
            result = await accounts_service.gateway_client.get_treasury_rebalance(rebalance_id)
            return _rebalance_response(result, rebalance_id=rebalance_id)

        stored_status = str(stored_record.status).lower()
        claimed_record = await _claim_rebalance_for_execution(database_manager, rebalance_id)
        if claimed_record is None:
            stored_status_is_recoverable = stored_status == "pending" or (
                stored_status != "built" and stored_status in GATEWAY_RECOVERABLE_EXECUTION_STATUSES
            )
            response = await _refresh_rebalance_status(
                accounts_service,
                database_manager,
                rebalance_id,
            )
            if (
                not stored_status_is_recoverable
                or response.status.lower() not in GATEWAY_RECOVERABLE_EXECUTION_STATUSES
            ):
                return response

            result = await accounts_service.gateway_client.execute_treasury_rebalance_target(rebalance_id)
            _rebalance_response(result, rebalance_id=rebalance_id)
            return await _refresh_rebalance_status(
                accounts_service,
                database_manager,
                rebalance_id,
            )

        result = await accounts_service.gateway_client.execute_treasury_rebalance_target(rebalance_id)
        _rebalance_response(result, rebalance_id=rebalance_id)
        return await _refresh_rebalance_status(
            accounts_service,
            database_manager,
            rebalance_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        redacted = _redact_error(exc)
        logger.error("Treasury rebalance target execute failed: %s", redacted)
        raise HTTPException(status_code=500, detail=f"Treasury rebalance target execute failed: {redacted}")


@router.get("/rebalances/{rebalance_id}", response_model=ProviderTreasuryRebalanceResponse)
async def get_provider_treasury_rebalance(
    rebalance_id: str,
    request: Request,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager=Depends(get_database_manager),
) -> ProviderTreasuryRebalanceResponse:
    """Fetch neutral status for a Gateway-owned treasury target."""
    try:
        if not await accounts_service.gateway_client.ping():
            raise HTTPException(status_code=503, detail="Gateway service is not available")
        return await _refresh_rebalance_status(
            accounts_service,
            _database_manager(request, db_manager),
            rebalance_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        redacted = _redact_error(exc)
        logger.error("Treasury rebalance target status failed: %s", redacted)
        raise HTTPException(status_code=500, detail=f"Treasury rebalance target status failed: {redacted}")


def _destination_identity_context(destination_chain: str, destination_network: str) -> tuple[str, str]:
    if (destination_chain, destination_network) == ("hyperliquid", "mainnet"):
        return "ethereum", "arbitrum-mainnet"
    return destination_chain, destination_network


def _destination_wallet_identities(
    accounts_service: AccountsService,
    *,
    destination_chain: str,
    destination_network: str,
    destination_wallet_ref: str,
) -> tuple[dict[str, str], dict[str, str]]:
    identity_chain, identity_network = _destination_identity_context(
        destination_chain,
        destination_network,
    )
    if (destination_chain, destination_network) == ("hyperliquid", "mainnet"):
        if destination_wallet_ref != HYPERLIQUID_MAINNET_WALLET_REF:
            raise HTTPException(status_code=400, detail=DESTINATION_WALLET_REF_MISMATCH_BLOCKER)
        destination_identity = _marlin_hyperliquid_wallet_identity()
        gateway_identity = _marlin_destination_wallet_identity(
            accounts_service,
            chain=identity_chain,
            network=identity_network,
        )
        if destination_identity["address"].lower() != gateway_identity["address"].lower():
            raise HTTPException(status_code=400, detail=DESTINATION_WALLET_IDENTITY_UNAVAILABLE_BLOCKER)
        return destination_identity, gateway_identity

    destination_identity = _marlin_destination_wallet_identity(
        accounts_service,
        chain=identity_chain,
        network=identity_network,
    )
    if destination_identity["wallet_ref"] != destination_wallet_ref:
        raise HTTPException(status_code=400, detail=DESTINATION_WALLET_REF_MISMATCH_BLOCKER)
    return destination_identity, destination_identity


def _marlin_hyperliquid_wallet_identity() -> dict[str, str]:
    credentials = _derive_marlin_credential_values("hyperliquid")
    address = str(credentials.get("hyperliquid_address") if isinstance(credentials, dict) else "").strip()
    if not address:
        raise HTTPException(status_code=400, detail=DESTINATION_WALLET_IDENTITY_UNAVAILABLE_BLOCKER)
    return {"address": address, "wallet_ref": HYPERLIQUID_MAINNET_WALLET_REF}


def _marlin_destination_wallet_identity(
    accounts_service: AccountsService,
    *,
    chain: str,
    network: str,
) -> dict[str, str]:
    identity_getter = getattr(accounts_service, "_marlin_gateway_wallet_identity", None)
    identity = identity_getter(chain=chain, network=network) if callable(identity_getter) else None
    if not isinstance(identity, dict):
        raise HTTPException(status_code=400, detail=DESTINATION_WALLET_IDENTITY_UNAVAILABLE_BLOCKER)
    address = str(identity.get("address") or "").strip()
    wallet_ref = str(identity.get("wallet_ref") or "").strip()
    if not address or not wallet_ref:
        raise HTTPException(status_code=400, detail=DESTINATION_WALLET_IDENTITY_UNAVAILABLE_BLOCKER)
    return {
        "chain": chain,
        "network": network,
        "address": address,
        "wallet_ref": wallet_ref,
    }


async def _hl_usdc_balance(accounts_service: AccountsService, account_name: str) -> Decimal:
    """Return a fresh hyperliquid_perpetual USDC balance, fail-closed on any error."""
    try:
        val = await accounts_service.get_fresh_available_balance(
            account_name,
            "hyperliquid_perpetual",
            "USDC",
        )
    except Exception:
        raise HTTPException(status_code=502, detail="Hyperliquid balance refresh failed")
    if not val.is_finite() or val < 0:
        raise HTTPException(status_code=502, detail="Hyperliquid USDC balance non-finite or negative")
    return val


def _rebalance_response(
    result: dict[str, Any] | None,
    *,
    rebalance_id: str | None = None,
) -> ProviderTreasuryRebalanceResponse:
    if result is None:
        raise HTTPException(status_code=502, detail="Gateway treasury rebalance returned no response")
    if result.get("error"):
        raise HTTPException(status_code=_gateway_error_status(result), detail=_redact_error(result.get("error")))
    response_id = str(result.get("id") or result.get("rebalanceId") or result.get("idempotencyKey") or rebalance_id or "")
    if not response_id:
        raise HTTPException(status_code=502, detail="Gateway treasury rebalance response missing id")
    return ProviderTreasuryRebalanceResponse(
        id=response_id,
        status=str(result.get("status") or "unknown"),
        transaction_hash=_optional_text(
            result.get("transaction_hash")
            or result.get("transactionHash")
            or result.get("txHash")
            or result.get("hash")
            or result.get("signature")
        ),
        error=_optional_error(result.get("provider_error") or result.get("providerError")),
        metadata=_safe_metadata(result.get("metadata")),
        source_amount=_parse_gateway_amount(result.get("sourceAmount"), "sourceAmount"),
        source_asset=_parse_gateway_asset(result.get("sourceAsset"), "sourceAsset"),
        destination_amount=_parse_gateway_amount(result.get("destinationAmount"), "destinationAmount"),
        destination_asset=_parse_gateway_asset(result.get("destinationAsset"), "destinationAsset"),
        quoted_provider_cost_usd=_parse_gateway_decimal(result.get("quotedProviderCostUsd")),
        quoted_gas_cost_usd=_parse_gateway_decimal(result.get("quotedGasCostUsd")),
        quoted_native_gas_amount=_parse_gateway_decimal(result.get("quotedNativeGasAmount")),
        quoted_native_gas_asset=_parse_gateway_asset(result.get("quotedNativeGasAsset"), "quotedNativeGasAsset"),
        quoted_at=_parse_gateway_quoted_at(result.get("quotedAt")),
        stage_index=_parse_gateway_stage_index(result.get("stageIndex")),
        stage_count=_parse_gateway_stage_count(result.get("stageCount")),
        stage_status=_parse_gateway_stage_status(result.get("stageStatus")),
        stages=_parse_gateway_stages(result["stages"]) if "stages" in result else None,
    )


def _stored_rebalance_response(
    payload: Any,
    *,
    rebalance_id: str | None = None,
) -> ProviderTreasuryRebalanceResponse:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Stored treasury rebalance response is malformed")
    values = dict(payload)
    if not values.get("id") and rebalance_id:
        values["id"] = rebalance_id
    try:
        return ProviderTreasuryRebalanceResponse.model_validate(values)
    except (TypeError, ValueError):
        raise HTTPException(status_code=502, detail="Stored treasury rebalance response is malformed")


def _gateway_error_status(result: dict[str, Any]) -> int:
    try:
        status = int(result.get("status") or 502)
    except (TypeError, ValueError):
        return 502
    if 400 <= status < 500:
        return status
    return 502


def _parse_gateway_stage_index(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise HTTPException(status_code=502, detail=f"Gateway returned non-integer stageIndex: {_redact_error(value)}")
    v = value
    if v < 0:
        raise HTTPException(status_code=502, detail=f"Gateway returned negative stageIndex: {_redact_error(value)}")
    return v


def _parse_gateway_stage_count(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise HTTPException(status_code=502, detail=f"Gateway returned non-integer stageCount: {_redact_error(value)}")
    v = value
    if v < 1:
        raise HTTPException(status_code=502, detail=f"Gateway returned stageCount < 1: {_redact_error(value)}")
    return v


def _parse_gateway_stage_status(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=502, detail=f"Gateway returned non-string stageStatus: {_redact_error(value)}")
    raw = value.strip()
    if not raw:
        raise HTTPException(status_code=502, detail=f"Gateway returned empty stageStatus: {_redact_error(value)}")
    return raw


def _parse_gateway_stages(value: Any) -> list[ProviderTreasuryStage]:
    if type(value) is not list:
        raise HTTPException(status_code=502, detail="Gateway returned non-list treasury stages")

    stages: list[ProviderTreasuryStage] = []
    for position, item in enumerate(value):
        if type(item) is not dict:
            _raise_malformed_gateway_stage(position, "item must be an object")
        fields = set(item)
        if not fields.issubset(GATEWAY_TREASURY_STAGE_FIELDS):
            _raise_malformed_gateway_stage(position, "contains unsupported fields")
        if not GATEWAY_TREASURY_STAGE_REQUIRED_FIELDS.issubset(fields):
            _raise_malformed_gateway_stage(position, "is missing required fields")
        stages.append(
            ProviderTreasuryStage(
                index=_parse_gateway_stage_item_index(item["index"], position),
                kind=_parse_gateway_stage_kind(item["kind"], position),
                status=_parse_gateway_stage_item_status(item["status"], position),
                source_amount=_parse_gateway_stage_amount(
                    item.get("sourceAmount", _GATEWAY_TREASURY_STAGE_FIELD_MISSING),
                    "sourceAmount",
                    position,
                ),
                source_asset=_parse_gateway_stage_text(
                    item.get("sourceAsset", _GATEWAY_TREASURY_STAGE_FIELD_MISSING),
                    "sourceAsset",
                    position,
                ),
                destination_amount=_parse_gateway_stage_amount(
                    item.get("destinationAmount", _GATEWAY_TREASURY_STAGE_FIELD_MISSING),
                    "destinationAmount",
                    position,
                ),
                destination_asset=_parse_gateway_stage_text(
                    item.get("destinationAsset", _GATEWAY_TREASURY_STAGE_FIELD_MISSING),
                    "destinationAsset",
                    position,
                ),
                transaction_hash=_parse_gateway_stage_text(
                    item.get("transactionHash", _GATEWAY_TREASURY_STAGE_FIELD_MISSING),
                    "transactionHash",
                    position,
                ),
                error=_parse_gateway_stage_text(
                    item.get("error", _GATEWAY_TREASURY_STAGE_FIELD_MISSING),
                    "error",
                    position,
                    redact=True,
                ),
            )
        )
    return stages


def _raise_malformed_gateway_stage(position: int, reason: str) -> None:
    raise HTTPException(status_code=502, detail=f"Gateway returned malformed treasury stage {position}: {reason}")


def _parse_gateway_stage_item_index(value: Any, position: int) -> int:
    if type(value) is not int:
        _raise_malformed_gateway_stage(position, "index must be an integer")
    if value < 0:
        _raise_malformed_gateway_stage(position, "index must be non-negative")
    if value != position:
        _raise_malformed_gateway_stage(position, "index must match list position")
    return value


def _parse_gateway_stage_kind(value: Any, position: int) -> str:
    if type(value) is not str or value not in {"conversion", "funding"}:
        _raise_malformed_gateway_stage(position, "kind must be conversion or funding")
    return value


def _parse_gateway_stage_item_status(value: Any, position: int) -> str:
    if not isinstance(value, str):
        _raise_malformed_gateway_stage(position, "status must be a string")
    raw = value.strip()
    if not raw:
        _raise_malformed_gateway_stage(position, "status must not be blank")
    return raw


def _parse_gateway_stage_amount(value: Any, field: str, position: int) -> Decimal | None:
    if value is _GATEWAY_TREASURY_STAGE_FIELD_MISSING:
        return None
    if value is None:
        _raise_malformed_gateway_stage(position, f"{field} must not be null")
    if not isinstance(value, str):
        _raise_malformed_gateway_stage(position, f"{field} must be a decimal string")
    try:
        amount = Decimal(value.strip())
    except (TypeError, ValueError, ArithmeticError):
        _raise_malformed_gateway_stage(position, f"{field} must be a valid decimal")
    if not amount.is_finite() or amount <= 0:
        _raise_malformed_gateway_stage(position, f"{field} must be positive and finite")
    return amount


def _parse_gateway_stage_text(
    value: Any,
    field: str,
    position: int,
    *,
    redact: bool = False,
) -> str | None:
    if value is _GATEWAY_TREASURY_STAGE_FIELD_MISSING:
        return None
    if value is None:
        _raise_malformed_gateway_stage(position, f"{field} must not be null")
    if not isinstance(value, str):
        _raise_malformed_gateway_stage(position, f"{field} must be a string")
    raw = value.strip()
    if not raw:
        _raise_malformed_gateway_stage(position, f"{field} must not be blank")
    return _redact_error(raw) if redact else raw


def _parse_gateway_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=502, detail=f"Gateway returned non-string decimal: {_redact_error(value)}")
    try:
        d = Decimal(value.strip())
    except (TypeError, ValueError, ArithmeticError):
        raise HTTPException(status_code=502, detail=f"Gateway returned malformed decimal: {_redact_error(value)}")
    if not d.is_finite():
        raise HTTPException(status_code=502, detail=f"Gateway returned non-finite decimal: {_redact_error(value)}")
    return d


def _parse_gateway_amount(value: Any, field: str) -> Decimal | None:
    d = _parse_gateway_decimal(value)
    if d is None:
        return None
    if d <= 0:
        raise HTTPException(status_code=502, detail=f"Gateway returned non-positive {field}: {_redact_error(value)}")
    return d


def _parse_gateway_asset(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=502, detail=f"Gateway returned non-string {field}: {_redact_error(value)}")
    raw = value.strip()
    if not raw:
        raise HTTPException(status_code=502, detail=f"Gateway returned empty {field}: {_redact_error(value)}")
    return raw


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _parse_gateway_quoted_at(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.utcoffset() is None:
            raise HTTPException(
                status_code=502,
                detail=f"Gateway returned naive quotedAt timestamp: {_redact_error(value)}",
            )
        return dt
    except (TypeError, ValueError):
        raise HTTPException(status_code=502, detail=f"Gateway returned malformed quotedAt: {_redact_error(value)}")


def _optional_error(value: Any) -> str | None:
    return None if value is None else _redact_error(value)


_SENSITIVE_METADATA_KEYS = {
    "apikey",
    "attestation",
    "attestationbytes",
    "bearer",
    "calldata",
    "data",
    "mnemonic",
    "privatekey",
    "provider",
    "rawcalldata",
    "route",
    "routepayload",
    "routes",
    "secret",
    "secretkey",
    "signature",
    "source",
    "sourceasset",
    "sourcechain",
    "sourcenetwork",
    "target",
    "token",
    "to",
    "transactionrequest",
    "txcalldata",
    "value",
    "walletfile",
}


def _safe_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = str(key).replace("_", "").replace("-", "").lower()
        if normalized_key in _SENSITIVE_METADATA_KEYS:
            continue
        if isinstance(item, dict):
            safe[str(key)] = _safe_metadata(item)
        elif isinstance(item, list):
            safe[str(key)] = [_safe_metadata(entry) if isinstance(entry, dict) else entry for entry in item]
        else:
            safe[str(key)] = item
    return safe


def _decimal_payload_value(value: Decimal) -> str:
    return format(value, "f")


def _redact_error(error: Any) -> str:
    raw = str(error)
    raw = re.sub(r"0x[a-fA-F0-9]{80,}", "[redacted-hex]", raw)
    raw = re.sub(
        r"([?&](?:api_?key|token|signature|attestation)=)[^&\s]+",
        r"\1[redacted]",
        raw,
        flags=re.IGNORECASE,
    )
    raw = re.sub(
        r"\b(token|api[-_]?key|signature|attestation|secret|mnemonic|private_?key|wallet_?file|bearer)\b[:=\s]+[^\s&]+",
        r"\1 [redacted]",
        raw,
        flags=re.IGNORECASE,
    )
    return raw[:300]


def _database_manager(request: Request, manager):
    if manager is None:
        manager = getattr(getattr(getattr(request, "app", None), "state", None), "db_manager", None)
    if manager is None:
        raise RuntimeError("HBA database manager is unavailable")
    return manager


async def _get_rebalance(db_manager, rebalance_id: str):
    async with db_manager.get_session_context() as session:
        return await ProviderTreasuryRebalanceRepository(session).get_rebalance(rebalance_id)


async def _create_built_rebalance(
    db_manager,
    rebalance_id: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
):
    async with db_manager.get_session_context() as session:
        return await ProviderTreasuryRebalanceRepository(session).create_built(
            rebalance_id,
            request_payload,
            response_payload,
        )


async def _claim_rebalance_for_execution(db_manager, rebalance_id: str):
    async with db_manager.get_session_context() as session:
        return await ProviderTreasuryRebalanceRepository(session).claim_for_execution(rebalance_id)


async def _update_rebalance_status(
    db_manager,
    rebalance_id: str,
    response_payload: dict[str, Any],
):
    status = str(response_payload.get("status") or "unknown").lower()
    async with db_manager.get_session_context() as session:
        return await ProviderTreasuryRebalanceRepository(session).update_status(
            rebalance_id,
            status,
            response_payload,
        )


async def _refresh_rebalance_status(
    accounts_service: AccountsService,
    database_manager,
    rebalance_id: str,
) -> ProviderTreasuryRebalanceResponse:
    record = await _get_rebalance(database_manager, rebalance_id)
    if (
        record is not None
        and str(record.status).lower() == "confirmed"
        and isinstance(record.response_payload, dict)
        and str(record.response_payload.get("status", "")).lower() == "confirmed"
    ):
        return _stored_rebalance_response(record.response_payload, rebalance_id=rebalance_id)
    result = await accounts_service.gateway_client.get_treasury_rebalance(rebalance_id)
    response = _rebalance_response(result, rebalance_id=rebalance_id)
    payload = _rebalance_response_payload(response)

    if response.status.lower() == "destination_pending":
        if record is not None and isinstance(record.request_payload, dict):
            rp = record.request_payload
            staged_hyperliquid_credit = (
                rp.get("destination_chain") == "hyperliquid"
                and rp.get("destination_network") == "mainnet"
                and str(rp.get("destination_asset", "")).upper() == "USDC"
                and response.stage_index == 1
                and response.stage_count == 2
                and (response.stage_status or "").lower() == "source_confirmed"
            )
            if staged_hyperliquid_credit:
                if not isinstance(record.response_payload, dict):
                    raise HTTPException(status_code=502, detail="Hyperliquid treasury baseline unavailable")
                baseline_raw = record.response_payload.get(HL_BASELINE_FIELD)
                try:
                    baseline = Decimal(str(baseline_raw))
                except (TypeError, ValueError, ArithmeticError):
                    raise HTTPException(status_code=502, detail="Hyperliquid treasury baseline malformed")
                if not baseline.is_finite() or baseline < 0:
                    raise HTTPException(status_code=502, detail="Hyperliquid treasury baseline malformed")
                if response.destination_amount is None or response.destination_amount <= 0:
                    raise HTTPException(status_code=502, detail="Hyperliquid treasury destination amount unavailable")
                account_name = str(rp.get("account_name", "")).strip()
                if not account_name:
                    raise HTTPException(status_code=502, detail="Hyperliquid treasury account unavailable")
                fresh = await _hl_usdc_balance(accounts_service, account_name)
                if fresh - baseline >= response.destination_amount:
                    response = response.model_copy(update={"status": "confirmed"})
                    payload = _rebalance_response_payload(response)

    updated = await _update_rebalance_status(
        database_manager,
        rebalance_id,
        payload,
    )
    if (
        updated is not None
        and str(updated.status).lower() == "confirmed"
        and response.status.lower() != "confirmed"
    ):
        return _stored_rebalance_response(updated.response_payload, rebalance_id=rebalance_id)
    return response


def _rebalance_response_payload(response: ProviderTreasuryRebalanceResponse) -> dict[str, Any]:
    return response.model_dump(mode="json", exclude_none=True)


def _assert_provider_treasury_authorized(request: Request) -> None:
    expected = _marlin_provider_intent_token()
    provided = str(getattr(request, "headers", {}).get(MARLIN_PROVIDER_INTENT_TOKEN_HEADER, ""))
    if expected and provided and secrets.compare_digest(provided, expected):
        return
    raise HTTPException(
        status_code=403,
        detail="Marlin provider intent token required for mainnet provider treasury rebalances",
    )


def _marlin_provider_intent_token() -> str:
    value = os.getenv("MARLIN_PROVIDER_INTENT_TOKEN", "").strip()
    if value:
        return value
    file_path = os.getenv("MARLIN_PROVIDER_INTENT_TOKEN_FILE", "").strip()
    if not file_path:
        return ""
    try:
        with open(file_path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""
