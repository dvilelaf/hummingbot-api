"""Provider-neutral treasury rebalance routes."""

import logging
import os
import re
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
CCTP_USDC_ROUTE = "cctp_usdc"
CCTP_BASE_ARBITRUM_USDC_ROUTE = "cctp_base_arbitrum_usdc"
CCTP_ROUTE_ALIASES = {CCTP_USDC_ROUTE, CCTP_BASE_ARBITRUM_USDC_ROUTE}
SUPPORTED_SOURCE_NETWORK = "arbitrum-mainnet"
GATEWAY_SOURCE_NETWORK = "arbitrum"
SUPPORTED_DESTINATION_NETWORK = "mainnet"
SUPPORTED_ASSET = "USDC"
UNSUPPORTED_TREASURY_REBALANCE_ROUTE_BLOCKER = (
    "unsupported treasury rebalance route: only Hyperliquid Bridge2 from Arbitrum "
    "USDC to Hyperliquid and CCTP gateway-to-gateway USDC are supported"
)
HYPERLIQUID_BRIDGE2_IDENTITY_MISMATCH_BLOCKER = (
    "hyperliquid_bridge2 identity mismatch: Arbitrum sender must equal "
    "Hyperliquid credited account"
)
MARLIN_ARBITRUM_IDENTITY_UNAVAILABLE_BLOCKER = (
    "Marlin Arbitrum wallet identity unavailable for Hyperliquid Bridge2 rebalance"
)
CCTP_IDENTITY_MISMATCH_BLOCKER = (
    "CCTP identity mismatch: destination_account must be the mnemonic-derived "
    "destination wallet address"
)
MARLIN_CCTP_IDENTITY_UNAVAILABLE_BLOCKER = (
    "Marlin destination wallet identity unavailable for CCTP treasury rebalance"
)
MARLIN_CCTP_SOURCE_IDENTITY_UNAVAILABLE_BLOCKER = (
    "Marlin EVM source wallet identity unavailable for CCTP treasury rebalance"
)
EVM_GATEWAY_NETWORK_ALIASES = {
    "arbitrum": "arbitrum-mainnet",
    "arbitrum-mainnet": "arbitrum-mainnet",
    "arbitrum-one": "arbitrum-mainnet",
    "ethereum-arbitrum-mainnet": "arbitrum-mainnet",
    "avalanche": "avalanche",
    "avalanche-mainnet": "avalanche",
    "ethereum-avalanche-mainnet": "avalanche",
    "base": "base",
    "base-mainnet": "base",
    "ethereum-base-mainnet": "base",
    "codex": "codex",
    "codex-mainnet": "codex",
    "ethereum-codex-mainnet": "codex",
    "cronos": "cronos",
    "cronos-mainnet": "cronos",
    "ethereum-cronos-mainnet": "cronos",
    "edge": "edge",
    "edge-mainnet": "edge",
    "ethereum-edge-mainnet": "edge",
    "ethereum": "mainnet",
    "ethereum-mainnet": "mainnet",
    "mainnet": "mainnet",
    "hyperevm": "hyperevm",
    "hyperevm-mainnet": "hyperevm",
    "ethereum-hyperevm-mainnet": "hyperevm",
    "ink": "ink",
    "ink-mainnet": "ink",
    "ethereum-ink-mainnet": "ink",
    "injective": "injective",
    "injective-mainnet": "injective",
    "ethereum-injective-mainnet": "injective",
    "linea": "linea",
    "linea-mainnet": "linea",
    "ethereum-linea-mainnet": "linea",
    "monad": "monad",
    "monad-mainnet": "monad",
    "ethereum-monad-mainnet": "monad",
    "morph": "morph",
    "morph-mainnet": "morph",
    "ethereum-morph-mainnet": "morph",
    "optimism": "optimism",
    "optimism-mainnet": "optimism",
    "op-mainnet": "optimism",
    "ethereum-optimism-mainnet": "optimism",
    "pharos": "pharos",
    "pharos-mainnet": "pharos",
    "ethereum-pharos-mainnet": "pharos",
    "plume": "plume",
    "plume-mainnet": "plume",
    "ethereum-plume-mainnet": "plume",
    "polygon": "polygon",
    "polygon-mainnet": "polygon",
    "polygon-pos": "polygon",
    "ethereum-polygon-mainnet": "polygon",
    "sei": "sei",
    "sei-mainnet": "sei",
    "ethereum-sei-mainnet": "sei",
    "sonic": "sonic",
    "sonic-mainnet": "sonic",
    "ethereum-sonic-mainnet": "sonic",
    "unichain": "unichain",
    "unichain-mainnet": "unichain",
    "ethereum-unichain-mainnet": "unichain",
    "world-chain": "world-chain",
    "world-chain-mainnet": "world-chain",
    "worldchain": "world-chain",
    "worldchain-mainnet": "world-chain",
    "ethereum-world-chain-mainnet": "world-chain",
    "xdc": "xdc",
    "xdc-mainnet": "xdc",
    "ethereum-xdc-mainnet": "xdc",
}
GATEWAY_NETWORK_TO_WALLET_NETWORK = {
    "arbitrum": "arbitrum-mainnet",
    "avalanche": "avalanche",
    "base": "base",
    "codex": "codex",
    "cronos": "cronos",
    "edge": "edge",
    "hyperevm": "hyperevm",
    "ink": "ink",
    "injective": "injective",
    "linea": "linea",
    "mainnet": "mainnet",
    "monad": "monad",
    "morph": "morph",
    "optimism": "optimism",
    "pharos": "pharos",
    "plume": "plume",
    "polygon": "polygon",
    "sei": "sei",
    "sonic": "sonic",
    "unichain": "unichain",
    "world-chain": "world-chain",
    "xdc": "xdc",
}
NON_EVM_CCTP_DESTINATION_ALIASES = {
    "solana": "solana",
    "solana-mainnet": "solana",
    "solana-mainnet-beta": "solana",
}
GATEWAY_NETWORK_TO_WALLET_CONTEXT = {
    "solana": ("solana", "mainnet-beta"),
}
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

        if route in CCTP_ROUTE_ALIASES:
            source_network = _cctp_evm_gateway_network(body.source_network)
            destination_network = _cctp_gateway_network(body.destination_network)
            if source_network == destination_network:
                raise HTTPException(status_code=400, detail="CCTP source and destination networks must differ")
            source_wallet_chain, source_wallet_network = _wallet_identity_context(source_network)
            destination_wallet_chain, destination_wallet_network = _wallet_identity_context(destination_network)
            wallet_identity = _marlin_wallet_identity(
                accounts_service,
                chain=source_wallet_chain,
                source_network=source_wallet_network,
                blocker=MARLIN_CCTP_SOURCE_IDENTITY_UNAVAILABLE_BLOCKER,
            )
            destination_identity = _marlin_wallet_identity(
                accounts_service,
                chain=destination_wallet_chain,
                source_network=destination_wallet_network,
                blocker=MARLIN_CCTP_IDENTITY_UNAVAILABLE_BLOCKER,
            )
            if not _addresses_equal(destination_identity["address"], body.destination_account):
                raise HTTPException(status_code=400, detail=CCTP_IDENTITY_MISMATCH_BLOCKER)
            provider = CCTP_USDC_ROUTE
        else:
            source_network = _source_network(body.source_network)
            destination_network = None
            source_wallet_network = source_network
            destination_wallet_network = None
            source_wallet_chain = "ethereum"
            destination_wallet_chain = None
            wallet_identity = _marlin_wallet_identity(
                accounts_service,
                chain=source_wallet_chain,
                source_network=source_wallet_network,
                blocker=MARLIN_ARBITRUM_IDENTITY_UNAVAILABLE_BLOCKER,
            )
            if not _addresses_equal(wallet_identity["address"], body.destination_account):
                raise HTTPException(status_code=400, detail=HYPERLIQUID_BRIDGE2_IDENTITY_MISMATCH_BLOCKER)
            provider = HYPERLIQUID_BRIDGE2_ROUTE

        wallet_result = await accounts_service.gateway_client.set_marlin_default_wallet(
            chain=source_wallet_chain,
            network=source_wallet_network,
            address=wallet_identity["address"],
            wallet_ref=wallet_identity["wallet_ref"],
        )
        if isinstance(wallet_result, dict) and wallet_result.get("error"):
            raise HTTPException(status_code=400, detail=f"Failed to set default wallet: {wallet_result.get('error')}")
        if destination_wallet_network is not None:
            destination_wallet_result = await accounts_service.gateway_client.set_marlin_default_wallet(
                chain=destination_wallet_chain,
                network=destination_wallet_network,
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
        result.setdefault("status", "built")
        return _rebalance_response(result)
    except HTTPException:
        raise
    except Exception as exc:
        redacted = _redact_provider_error(exc)
        logger.error("Provider treasury rebalance build failed: %s", redacted)
        raise HTTPException(status_code=500, detail=f"Provider treasury rebalance build failed: {redacted}")


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
                result = await accounts_service.gateway_client.get_treasury_rebalance(rebalance_id)
                return _rebalance_response(result, rebalance_id=rebalance_id)
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
                        destination_address=stored["destination_address"],
                        destination_network=stored.get("destination_network") or "",
                        wallet_address=stored["wallet_address"],
                        amount=stored["amount"],
                        provider=stored["provider"],
                        source_network=stored["source_network"],
                    ),
                    marlin_provider_intent_authorized=True,
                )
                status_result = await accounts_service.gateway_client.get_treasury_rebalance(rebalance_id)
                status_result.setdefault("id", rebalance_id)
                status_result.setdefault("route", stored["provider"])
                result = status_result
                stored["status"] = str(status_result.get("status") or "submitted")
            except Exception:
                stored["status"] = "failed"
                raise
        result.setdefault("id", rebalance_id)
        result.setdefault("route", stored["provider"])
        return _rebalance_response(result, rebalance_id=rebalance_id)
    except HTTPException:
        raise
    except Exception as exc:
        redacted = _redact_provider_error(exc)
        logger.error("Provider treasury rebalance execute failed: %s", redacted)
        raise HTTPException(status_code=500, detail=f"Provider treasury rebalance execute failed: {redacted}")


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
        redacted = _redact_provider_error(exc)
        logger.error("Provider treasury rebalance status failed: %s", redacted)
        raise HTTPException(status_code=500, detail=f"Provider treasury rebalance status failed: {redacted}")


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
    if route in CCTP_ROUTE_ALIASES and (
        body.source_venue.strip().lower() == "gateway"
        and body.source_asset.strip().upper() == SUPPORTED_ASSET
        and body.destination_venue.strip().lower() == "gateway"
        and body.destination_asset.strip().upper() == SUPPORTED_ASSET
    ):
        _cctp_evm_gateway_network(body.source_network)
        _cctp_gateway_network(body.destination_network)
        return
    else:
        raise HTTPException(status_code=400, detail=UNSUPPORTED_TREASURY_REBALANCE_ROUTE_BLOCKER)


def _source_network(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"arbitrum", "arbitrum-one", "arbitrum-mainnet", "ethereum-arbitrum-mainnet"}:
        return SUPPORTED_SOURCE_NETWORK
    return normalized


def _cctp_gateway_network(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    network = EVM_GATEWAY_NETWORK_ALIASES.get(normalized)
    if network is None:
        network = NON_EVM_CCTP_DESTINATION_ALIASES.get(normalized)
    if network is None:
        raise HTTPException(status_code=400, detail=UNSUPPORTED_TREASURY_REBALANCE_ROUTE_BLOCKER)
    return "arbitrum" if network == "arbitrum-mainnet" else network


def _cctp_evm_gateway_network(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    network = EVM_GATEWAY_NETWORK_ALIASES.get(normalized)
    if network is None:
        raise HTTPException(status_code=400, detail=UNSUPPORTED_TREASURY_REBALANCE_ROUTE_BLOCKER)
    return "arbitrum" if network == "arbitrum-mainnet" else network


def _wallet_identity_network(gateway_network: str) -> str:
    return GATEWAY_NETWORK_TO_WALLET_NETWORK.get(gateway_network, gateway_network)


def _wallet_identity_context(gateway_network: str) -> tuple[str, str]:
    return GATEWAY_NETWORK_TO_WALLET_CONTEXT.get(
        gateway_network,
        ("ethereum", _wallet_identity_network(gateway_network)),
    )


def _marlin_wallet_identity(
    accounts_service: AccountsService,
    *,
    chain: str,
    source_network: str,
    blocker: str,
) -> dict[str, str]:
    identity_getter = getattr(accounts_service, "_marlin_gateway_wallet_identity", None)
    identity = (
        identity_getter(chain=chain, network=source_network)
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
        raise HTTPException(status_code=_gateway_error_status(result), detail=_redact_provider_error(result.get("error")))
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
        approval_transaction_hash=_optional_text(
            result.get("approval_transaction_hash") or result.get("approvalTransactionHash")
        ),
        burn_transaction_hash=_optional_text(
            result.get("burn_transaction_hash") or result.get("burnTransactionHash")
        ),
        finalize_transaction_hash=_optional_text(
            result.get("finalize_transaction_hash") or result.get("finalizeTransactionHash")
        ),
        provider_status=_optional_text(result.get("provider_status") or result.get("providerStatus")),
        provider_error=_optional_provider_error(result.get("provider_error") or result.get("providerError")),
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
    if value is None:
        return None
    return str(value)


def _optional_provider_error(value: Any) -> str | None:
    if value is None:
        return None
    return _redact_provider_error(value)


_SENSITIVE_METADATA_KEYS = {
    "apikey",
    "attestation",
    "attestationbytes",
    "bearer",
    "calldata",
    "mnemonic",
    "privatekey",
    "rawcalldata",
    "secret",
    "secretkey",
    "signature",
    "token",
    "txcalldata",
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
        safe[str(key)] = _safe_metadata(item) if isinstance(item, dict) else item
    return safe


def _decimal_payload_value(value: Decimal) -> str:
    return format(value, "f")


def _redact_provider_error(error: Any) -> str:
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
    destination_address: str,
    destination_network: str,
    wallet_address: str,
    amount: str,
    provider: str,
    source_network: str,
) -> dict[str, str]:
    return {
        "action": "gateway_rebalance",
        "connector_id": "treasury" if provider in CCTP_ROUTE_ALIASES else "hyperliquid",
        "destination_address": destination_address,
        "destination_network": destination_network,
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
