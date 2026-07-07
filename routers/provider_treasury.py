"""Provider-neutral treasury rebalance routes."""

import logging
import os
import secrets
import asyncio
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from deps import get_accounts_service
from models.provider_treasury import (
    ProviderTreasuryRebalanceExecuteRequest,
    ProviderTreasuryRebalanceRequest,
    ProviderTreasuryRebalanceResponse,
)
from services.accounts_service import AccountsService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Provider Treasury"], prefix="/provider/treasury")
MARLIN_PROVIDER_INTENT_TOKEN_HEADER = "x-marlin-provider-intent-token"

HYPERLIQUID_BRIDGE2_ROUTE = "hyperliquid_bridge2"
CCTP_BASE_ARBITRUM_USDC_ROUTE = "cctp_base_arbitrum_usdc"
SUPPORTED_SOURCE_NETWORK = "arbitrum-mainnet"
GATEWAY_SOURCE_NETWORK = "arbitrum"
CCTP_SOURCE_NETWORK = "base"
CCTP_DESTINATION_NETWORK = "arbitrum-mainnet"
CCTP_GATEWAY_DESTINATION_NETWORK = "arbitrum"
SUPPORTED_DESTINATION_NETWORK = "mainnet"
SUPPORTED_ASSET = "USDC"
UNSUPPORTED_TREASURY_REBALANCE_ROUTE_BLOCKER = (
    "unsupported treasury rebalance route: only Hyperliquid Bridge2 from Arbitrum "
    "USDC to Hyperliquid and CCTP Base USDC to Arbitrum USDC are supported"
)
HYPERLIQUID_BRIDGE2_IDENTITY_MISMATCH_BLOCKER = (
    "hyperliquid_bridge2 identity mismatch: Arbitrum sender must equal "
    "Hyperliquid credited account"
)
MARLIN_ARBITRUM_IDENTITY_UNAVAILABLE_BLOCKER = (
    "Marlin Arbitrum wallet identity unavailable for Hyperliquid Bridge2 rebalance"
)
CCTP_IDENTITY_MISMATCH_BLOCKER = (
    "cctp_base_arbitrum_usdc identity mismatch: destination_account must be the "
    "mnemonic-derived Arbitrum EVM address"
)
MARLIN_BASE_IDENTITY_UNAVAILABLE_BLOCKER = (
    "Marlin Base wallet identity unavailable for CCTP treasury rebalance"
)
_REBALANCE_REQUESTS: dict[str, dict[str, str]] = {}
_REBALANCE_LOCKS: dict[str, asyncio.Lock] = {}


@router.post("/rebalances", response_model=ProviderTreasuryRebalanceResponse)
async def create_provider_treasury_rebalance(
    body: ProviderTreasuryRebalanceRequest,
    request: Request,
    accounts_service: AccountsService = Depends(get_accounts_service),
) -> ProviderTreasuryRebalanceResponse:
    """Build a provider-owned treasury rebalance through Gateway."""
    try:
        route = body.route.strip().lower()
        _assert_supported_treasury_rebalance(body)
        if not await accounts_service.gateway_client.ping():
            raise HTTPException(status_code=503, detail="Gateway service is not available")
        _assert_provider_treasury_authorized(request)

        if route == CCTP_BASE_ARBITRUM_USDC_ROUTE:
            source_network = CCTP_SOURCE_NETWORK
            destination_network = CCTP_GATEWAY_DESTINATION_NETWORK
            wallet_identity = _marlin_evm_wallet_identity(
                accounts_service,
                source_network=source_network,
                blocker=MARLIN_BASE_IDENTITY_UNAVAILABLE_BLOCKER,
            )
            destination_identity = _marlin_evm_wallet_identity(
                accounts_service,
                source_network=CCTP_DESTINATION_NETWORK,
                blocker=MARLIN_ARBITRUM_IDENTITY_UNAVAILABLE_BLOCKER,
            )
            if not _addresses_equal(destination_identity["address"], body.destination_account):
                raise HTTPException(status_code=400, detail=CCTP_IDENTITY_MISMATCH_BLOCKER)
            provider = CCTP_BASE_ARBITRUM_USDC_ROUTE
        else:
            source_network = _source_network(body.source_network)
            destination_network = None
            wallet_identity = _marlin_evm_wallet_identity(
                accounts_service,
                source_network=source_network,
                blocker=MARLIN_ARBITRUM_IDENTITY_UNAVAILABLE_BLOCKER,
            )
            if not _addresses_equal(wallet_identity["address"], body.destination_account):
                raise HTTPException(status_code=400, detail=HYPERLIQUID_BRIDGE2_IDENTITY_MISMATCH_BLOCKER)
            provider = HYPERLIQUID_BRIDGE2_ROUTE

        wallet_result = await accounts_service.gateway_client.set_marlin_default_wallet(
            chain="ethereum",
            network=source_network,
            address=wallet_identity["address"],
            wallet_ref=wallet_identity["wallet_ref"],
        )
        if isinstance(wallet_result, dict) and wallet_result.get("error"):
            raise HTTPException(status_code=400, detail=f"Failed to set default wallet: {wallet_result.get('error')}")
        if route == CCTP_BASE_ARBITRUM_USDC_ROUTE:
            destination_wallet_result = await accounts_service.gateway_client.set_marlin_default_wallet(
                chain="ethereum",
                network=CCTP_DESTINATION_NETWORK,
                address=destination_identity["address"],
                wallet_ref=destination_identity["wallet_ref"],
            )
            if isinstance(destination_wallet_result, dict) and destination_wallet_result.get("error"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to set destination default wallet: {destination_wallet_result.get('error')}",
                )

        rebalance_id = body.idempotency_key or _rebalance_id(body)
        result = await accounts_service.gateway_client.build_treasury_rebalance(
            idempotency_key=rebalance_id,
            wallet_address=wallet_identity["address"],
            destination_address=body.destination_account,
            amount=_decimal_payload_value(body.amount),
            provider=provider,
            source_network=GATEWAY_SOURCE_NETWORK if provider == HYPERLIQUID_BRIDGE2_ROUTE else source_network,
            destination_network=destination_network,
        )
        _REBALANCE_REQUESTS[rebalance_id] = {
            "amount": _decimal_payload_value(body.amount),
            "destination_address": body.destination_account,
            "destination_network": destination_network or "",
            "provider": provider,
            "source_network": GATEWAY_SOURCE_NETWORK if provider == HYPERLIQUID_BRIDGE2_ROUTE else source_network,
            "wallet_address": wallet_identity["address"],
        }
        result.setdefault("id", rebalance_id)
        result.setdefault("route", provider)
        return _rebalance_response(result)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Provider treasury rebalance build failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Provider treasury rebalance build failed: {exc}")


@router.post("/rebalances/{rebalance_id}/execute", response_model=ProviderTreasuryRebalanceResponse)
async def execute_provider_treasury_rebalance(
    rebalance_id: str,
    body: ProviderTreasuryRebalanceExecuteRequest,
    request: Request,
    accounts_service: AccountsService = Depends(get_accounts_service),
) -> ProviderTreasuryRebalanceResponse:
    """Execute a previously built provider-owned treasury rebalance through Gateway."""
    try:
        if not await accounts_service.gateway_client.ping():
            raise HTTPException(status_code=503, detail="Gateway service is not available")
        _assert_provider_treasury_authorized(request)
        lock = _rebalance_lock(rebalance_id)
        async with lock:
            stored = _REBALANCE_REQUESTS.get(rebalance_id)
            if stored is None:
                raise HTTPException(status_code=404, detail="provider treasury rebalance request not found")
            if stored.get("status") in {"pending", "submitted", "confirmed"}:
                raise HTTPException(status_code=409, detail="provider treasury rebalance already submitted")
            stored["status"] = "pending"
            try:
                result = await accounts_service.gateway_client.execute_treasury_rebalance(
                    idempotency_key=rebalance_id,
                    wallet_address=stored["wallet_address"],
                    destination_address=stored["destination_address"],
                    amount=stored["amount"],
                    provider=stored["provider"],
                    source_network=stored["source_network"],
                    destination_network=stored.get("destination_network") or None,
                    live_action_authorization=_marlin_gateway_rebalance_authorization(
                        wallet_address=stored["wallet_address"],
                        amount=stored["amount"],
                        provider=stored["provider"],
                        source_network=stored["source_network"],
                    ),
                    marlin_provider_intent_authorized=True,
                )
                stored["status"] = "submitted"
            except Exception:
                stored["status"] = "failed"
                raise
        result.setdefault("id", rebalance_id)
        result.setdefault("route", stored["provider"])
        return _rebalance_response(result, rebalance_id=rebalance_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Provider treasury rebalance execute failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Provider treasury rebalance execute failed: {exc}")


@router.get("/rebalances/{rebalance_id}", response_model=ProviderTreasuryRebalanceResponse)
async def get_provider_treasury_rebalance(
    rebalance_id: str,
    accounts_service: AccountsService = Depends(get_accounts_service),
) -> ProviderTreasuryRebalanceResponse:
    """Fetch provider-owned treasury rebalance status from Gateway."""
    try:
        if not await accounts_service.gateway_client.ping():
            raise HTTPException(status_code=503, detail="Gateway service is not available")
        result = await accounts_service.gateway_client.get_treasury_rebalance(rebalance_id)
        return _rebalance_response(result, rebalance_id=rebalance_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Provider treasury rebalance status failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Provider treasury rebalance status failed: {exc}")


def _assert_supported_treasury_rebalance(body: ProviderTreasuryRebalanceRequest) -> None:
    route = body.route.strip().lower()
    if route == HYPERLIQUID_BRIDGE2_ROUTE and (
        body.source_venue.strip().lower() == "gateway"
        and _source_network(body.source_network) == SUPPORTED_SOURCE_NETWORK
        and body.source_asset.strip().upper() == SUPPORTED_ASSET
        and body.destination_venue.strip().lower() == "hyperliquid"
        and body.destination_network.strip().lower() == SUPPORTED_DESTINATION_NETWORK
        and body.destination_asset.strip().upper() == SUPPORTED_ASSET
    ):
        return
    if route == CCTP_BASE_ARBITRUM_USDC_ROUTE and (
        body.source_venue.strip().lower() == "gateway"
        and _source_network(body.source_network) == CCTP_SOURCE_NETWORK
        and body.source_asset.strip().upper() == SUPPORTED_ASSET
        and body.destination_venue.strip().lower() == "gateway"
        and _source_network(body.destination_network) == CCTP_DESTINATION_NETWORK
        and body.destination_asset.strip().upper() == SUPPORTED_ASSET
    ):
        return
    else:
        raise HTTPException(status_code=400, detail=UNSUPPORTED_TREASURY_REBALANCE_ROUTE_BLOCKER)


def _source_network(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"arbitrum", "arbitrum-one", "arbitrum-mainnet", "ethereum-arbitrum-mainnet"}:
        return SUPPORTED_SOURCE_NETWORK
    if normalized in {"base", "base-mainnet", "ethereum-base-mainnet"}:
        return CCTP_SOURCE_NETWORK
    return normalized


def _marlin_evm_wallet_identity(
    accounts_service: AccountsService,
    *,
    source_network: str,
    blocker: str,
) -> dict[str, str]:
    identity_getter = getattr(accounts_service, "_marlin_gateway_wallet_identity", None)
    identity = (
        identity_getter(chain="ethereum", network=source_network)
        if callable(identity_getter)
        else None
    )
    if not isinstance(identity, dict):
        raise HTTPException(status_code=400, detail=blocker)
    address = str(identity.get("address") or "").strip()
    wallet_ref = str(identity.get("wallet_ref") or "").strip()
    if not address or not wallet_ref:
        raise HTTPException(status_code=400, detail=blocker)
    return {"address": address, "wallet_ref": wallet_ref}


def _rebalance_response(
    result: dict[str, Any] | None,
    *,
    rebalance_id: str | None = None,
) -> ProviderTreasuryRebalanceResponse:
    if result is None:
        raise HTTPException(status_code=502, detail="Gateway treasury rebalance returned no response")
    if result.get("error"):
        raise HTTPException(status_code=_gateway_error_status(result), detail=str(result.get("error")))
    response_id = str(result.get("id") or result.get("rebalanceId") or result.get("idempotencyKey") or rebalance_id or "")
    if not response_id:
        raise HTTPException(status_code=502, detail="Gateway treasury rebalance response missing id")
    return ProviderTreasuryRebalanceResponse(
        id=response_id,
        status=str(result.get("status") or "unknown"),
        provider=_optional_text(result.get("provider")),
        route=_optional_text(result.get("route")),
        transaction_hash=_optional_text(
            result.get("transaction_hash")
            or result.get("transactionHash")
            or result.get("txHash")
            or result.get("hash")
        ),
        provider_status=_optional_text(result.get("provider_status") or result.get("providerStatus")),
        provider_error=_optional_text(result.get("provider_error") or result.get("providerError")),
        metadata=result.get("metadata") if isinstance(result.get("metadata"), dict) else {},
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
    if value is None:
        return None
    return str(value)


def _decimal_payload_value(value: Decimal) -> str:
    return format(value, "f")


def _rebalance_id(body: ProviderTreasuryRebalanceRequest) -> str:
    return f"{body.account_name}:{body.route}:{body.destination_account}:{_decimal_payload_value(body.amount)}"


def _rebalance_lock(rebalance_id: str) -> asyncio.Lock:
    lock = _REBALANCE_LOCKS.get(rebalance_id)
    if lock is None:
        lock = asyncio.Lock()
        _REBALANCE_LOCKS[rebalance_id] = lock
    return lock


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


def _marlin_gateway_rebalance_authorization(
    *,
    wallet_address: str,
    amount: str,
    provider: str,
    source_network: str,
) -> dict[str, str]:
    return {
        "action": "gateway_rebalance",
        "connector_id": "treasury" if provider == CCTP_BASE_ARBITRUM_USDC_ROUTE else "hyperliquid",
        "network": source_network,
        "notional": amount,
        "scope": "provider_treasury",
        "source": "marlin",
        "wallet_address": wallet_address,
    }


def _addresses_equal(left: str, right: str) -> bool:
    if left == right:
        return True
    return left.startswith("0x") and right.startswith("0x") and left.lower() == right.lower()
