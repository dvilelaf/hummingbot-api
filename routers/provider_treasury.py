"""Destination-only provider treasury routes."""

import logging
import os
import re
import secrets
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from database.repositories import ProviderTreasuryRebalanceRepository
from deps import get_accounts_service, get_database_manager
from models.provider_treasury import (
    ProviderTreasuryRebalanceExecuteRequest,
    ProviderTreasuryRebalanceRequest,
    ProviderTreasuryRebalanceResponse,
)
from services.accounts_service import AccountsService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Provider Treasury"], prefix="/provider/treasury")
MARLIN_PROVIDER_INTENT_TOKEN_HEADER = "x-marlin-provider-intent-token"

DESTINATION_WALLET_IDENTITY_UNAVAILABLE_BLOCKER = "destination_wallet_identity_unavailable"
DESTINATION_WALLET_REF_MISMATCH_BLOCKER = "destination_wallet_ref_mismatch"
DESTINATION_WALLET_DEFAULT_FAILED_BLOCKER = "destination_wallet_default_failed"
IDEMPOTENCY_KEY_CONFLICT_BLOCKER = "idempotency_key_conflict"


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
        identity_chain, identity_network = _destination_identity_context(
            destination_chain,
            destination_network,
        )
        destination_identity = _marlin_destination_wallet_identity(
            accounts_service,
            chain=identity_chain,
            network=identity_network,
        )
        if destination_identity["wallet_ref"] != body.destination_wallet_ref.strip():
            raise HTTPException(status_code=400, detail=DESTINATION_WALLET_REF_MISMATCH_BLOCKER)

        stored_request = {
            "account_name": body.account_name.strip(),
            "amount": _decimal_payload_value(body.amount),
            "destination_address": destination_identity["address"],
            "destination_asset": body.destination_asset.strip(),
            "destination_chain": destination_chain,
            "destination_network": destination_network,
            "destination_wallet_ref": destination_identity["wallet_ref"],
            "max_cost_bps": _decimal_payload_value(body.max_cost_bps),
            "route_id": body.route_id.strip(),
        }
        database_manager = _database_manager(request, db_manager)
        stored_record = await _create_built_rebalance(
            database_manager,
            body.idempotency_key,
            stored_request,
            {"id": body.idempotency_key, "status": "built"},
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
            chain=identity_chain,
            network=identity_network,
            address=destination_identity["address"],
            wallet_ref=destination_identity["wallet_ref"],
        )
        if isinstance(wallet_result, dict) and wallet_result.get("error"):
            error = _redact_error(wallet_result.get("error"))
            raise HTTPException(
                status_code=400,
                detail=f"{DESTINATION_WALLET_DEFAULT_FAILED_BLOCKER}: {error}",
            )

        result = await accounts_service.gateway_client.create_treasury_rebalance_target(
            idempotency_key=body.idempotency_key,
            destination_chain=destination_chain,
            destination_network=destination_network,
            destination_asset=body.destination_asset.strip(),
            destination_address=destination_identity["address"],
            amount=_decimal_payload_value(body.amount),
            max_cost_bps=_decimal_payload_value(body.max_cost_bps),
        )
        if isinstance(result, dict):
            result.setdefault("id", body.idempotency_key)
            result.setdefault("status", "built")
        response = _rebalance_response(result, rebalance_id=body.idempotency_key)
        await _update_rebalance_status(
            database_manager,
            body.idempotency_key,
            _rebalance_response_payload(response),
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
            response = await _refresh_rebalance_status(
                accounts_service,
                database_manager,
                rebalance_id,
            )
            if stored_status != "pending" or response.status.lower() != "built":
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
        result = await accounts_service.gateway_client.get_treasury_rebalance(rebalance_id)
        response = _rebalance_response(result, rebalance_id=rebalance_id)
        await _update_rebalance_status(
            _database_manager(request, db_manager),
            rebalance_id,
            _rebalance_response_payload(response),
        )
        return response
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
    return {"address": address, "wallet_ref": wallet_ref}


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
    )


def _gateway_error_status(result: dict[str, Any]) -> int:
    try:
        status = int(result.get("status") or 502)
    except (TypeError, ValueError):
        return 502
    if 400 <= status < 500:
        return status
    return 502


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


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
    result = await accounts_service.gateway_client.get_treasury_rebalance(rebalance_id)
    response = _rebalance_response(result, rebalance_id=rebalance_id)
    await _update_rebalance_status(
        database_manager,
        rebalance_id,
        _rebalance_response_payload(response),
    )
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
