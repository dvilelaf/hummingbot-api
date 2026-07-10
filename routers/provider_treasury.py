"""Provider-neutral treasury rebalance routes."""

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

HYPERLIQUID_BRIDGE2_ROUTE = "hyperliquid_bridge2"
CCTP_USDC_ROUTE = "cctp_usdc"
CCTP_BASE_ARBITRUM_USDC_ROUTE = "cctp_base_arbitrum_usdc"
SQUID_ROUTER_ROUTE = "squid_router"
PROVIDER_TREASURY_SAME_CHAIN_SWAP_ROUTE = "provider_treasury_same_chain_swap"
CCTP_ROUTE_ALIASES: set[str] = set()
SUPPORTED_SOURCE_NETWORK = "arbitrum-mainnet"
GATEWAY_SOURCE_NETWORK = "arbitrum"
SUPPORTED_DESTINATION_NETWORK = "mainnet"
SUPPORTED_ASSET = "USDC"
UNSUPPORTED_TREASURY_REBALANCE_ROUTE_BLOCKER = (
    "unsupported treasury rebalance route: only Hyperliquid Bridge2 from Arbitrum "
    "USDC to Hyperliquid, Squid gateway-to-gateway, and Arbitrum same-chain "
    "provider treasury swaps "
    "treasury rebalances are supported"
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
SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER = (
    "Squid treasury rebalance requires mnemonic-derived Gateway source and destination "
    "wallet identity; use provider-owned or external treasury rebalance"
)
SAME_CHAIN_SWAP_BLOCKER = (
    "provider_treasury_same_chain_swap requires mnemonic-derived Arbitrum source and "
    "destination wallet identity for gateway Arbitrum ETH/WETH to USDC"
)
SQUID_NATIVE_TOKEN_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
SQUID_EVM_USDC_ASSETS = {
    ("base", "usdc"): "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    ("arbitrum", "usdc"): "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    ("mainnet", "usdc"): "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    ("optimism", "usdc"): "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    ("polygon", "usdc"): "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    ("avalanche", "usdc"): "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
}
SQUID_EVM_SOURCE_ASSETS = SQUID_EVM_USDC_ASSETS
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
SQUID_EVM_GATEWAY_NETWORK_ALIASES = {
    **EVM_GATEWAY_NETWORK_ALIASES,
    "bnb": "bsc",
    "bnb-mainnet": "bsc",
    "bsc": "bsc",
    "bsc-mainnet": "bsc",
    "ethereum-bsc-mainnet": "bsc",
}
GATEWAY_NETWORK_TO_WALLET_NETWORK = {
    "arbitrum": "arbitrum-mainnet",
    "avalanche": "avalanche",
    "base": "base",
    "bsc": "bsc",
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
SQUID_NON_EVM_DESTINATION_ALIASES = {
    "solana": ("solana", "mainnet-beta"),
    "solana-mainnet": ("solana", "mainnet-beta"),
    "solana-mainnet-beta": ("solana", "mainnet-beta"),
    "xrpl": ("xrpl", "mainnet"),
    "xrpl-mainnet": ("xrpl", "mainnet"),
    "xrp-ledger": ("xrpl", "mainnet"),
    "xrp-ledger-mainnet": ("xrpl", "mainnet"),
}
GATEWAY_NETWORK_TO_WALLET_CONTEXT = {
    "solana": ("solana", "mainnet-beta"),
}
@router.post("/rebalances", response_model=ProviderTreasuryRebalanceResponse)
async def create_provider_treasury_rebalance(
    body: ProviderTreasuryRebalanceRequest,
    request: Request,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager=Depends(get_database_manager),
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
            source_chain = "ethereum"
            source_asset = SUPPORTED_ASSET
            destination_chain = None
            destination_asset = SUPPORTED_ASSET
            destination_venue = None
        elif route == SQUID_ROUTER_ROUTE:
            source_network = _squid_evm_gateway_network(body.source_network)
            destination_chain, destination_network, destination_wallet_chain, destination_wallet_network = (
                _squid_destination_context(body.destination_network)
            )
            source_wallet_chain, source_wallet_network = _wallet_identity_context(source_network)
            wallet_identity = _marlin_wallet_identity(
                accounts_service,
                chain=source_wallet_chain,
                source_network=source_wallet_network,
                blocker=SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER,
            )
            destination_identity = _marlin_wallet_identity(
                accounts_service,
                chain=destination_wallet_chain,
                source_network=destination_wallet_network,
                blocker=SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER,
            )
            if not _addresses_equal(destination_identity["address"], body.destination_account):
                raise HTTPException(status_code=400, detail=SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER)
            provider = SQUID_ROUTER_ROUTE
            source_chain = source_wallet_chain
            source_asset = _squid_source_asset(body.source_asset, source_network)
            source_asset_decimals = body.source_asset_decimals
            destination_asset = _squid_destination_asset(
                body.destination_asset,
                destination_chain=destination_chain,
                destination_network=destination_network,
            )
            destination_venue = body.destination_venue.strip().lower()
        elif route == PROVIDER_TREASURY_SAME_CHAIN_SWAP_ROUTE:
            source_network = _same_chain_swap_network(body.source_network)
            destination_network = _same_chain_swap_network(body.destination_network)
            source_wallet_chain, source_wallet_network = _wallet_identity_context(source_network)
            destination_wallet_chain, destination_wallet_network = _wallet_identity_context(destination_network)
            wallet_identity = _marlin_wallet_identity(
                accounts_service,
                chain=source_wallet_chain,
                source_network=source_wallet_network,
                blocker=SAME_CHAIN_SWAP_BLOCKER,
            )
            destination_identity = _marlin_wallet_identity(
                accounts_service,
                chain=destination_wallet_chain,
                source_network=destination_wallet_network,
                blocker=SAME_CHAIN_SWAP_BLOCKER,
            )
            if not _addresses_equal(wallet_identity["address"], body.destination_account) or not _addresses_equal(
                destination_identity["address"],
                body.destination_account,
            ):
                raise HTTPException(status_code=400, detail=SAME_CHAIN_SWAP_BLOCKER)
            provider = PROVIDER_TREASURY_SAME_CHAIN_SWAP_ROUTE
            source_chain = source_wallet_chain
            source_asset = body.source_asset.strip().upper()
            source_asset_decimals = body.source_asset_decimals
            destination_chain = destination_wallet_chain
            destination_asset = body.destination_asset.strip().upper()
            destination_venue = body.destination_venue.strip().lower()
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
            source_chain = "ethereum"
            source_asset = SUPPORTED_ASSET
            source_asset_decimals = None
            destination_chain = None
            destination_asset = SUPPORTED_ASSET
            destination_venue = "hyperliquid"

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
            **_provider_semantic_gateway_fields(
                provider=provider,
                source_chain=source_chain,
                source_asset=source_asset,
                destination_chain=destination_chain,
                destination_asset=destination_asset,
                destination_venue=destination_venue,
                source_asset_decimals=source_asset_decimals,
            ),
        )
        built_destination_network = str(result.get("destinationNetwork") or destination_network or "").strip()
        stored_request = {
            "amount": _decimal_payload_value(body.amount),
            "destination_address": body.destination_account,
            "destination_asset": destination_asset,
            "destination_chain": destination_chain or "",
            "destination_network": built_destination_network,
            "destination_venue": destination_venue or "",
            "provider": provider,
            "source_asset": source_asset,
            "source_asset_decimals": str(source_asset_decimals or ""),
            "source_chain": source_chain,
            "source_network": GATEWAY_SOURCE_NETWORK if provider == HYPERLIQUID_BRIDGE2_ROUTE else source_network,
            "wallet_address": wallet_identity["address"],
        }
        result.setdefault("id", rebalance_id)
        result.setdefault("route", provider)
        result.setdefault("status", "built")
        response = _rebalance_response(result)
        await _create_built_rebalance(
            _database_manager(request, db_manager),
            rebalance_id,
            stored_request,
            _rebalance_response_payload(response),
        )
        return response
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
    db_manager=Depends(get_database_manager),
) -> ProviderTreasuryRebalanceResponse:
    """Execute a previously built provider-owned treasury rebalance through Gateway."""
    try:
        if not await accounts_service.gateway_client.ping():
            raise HTTPException(status_code=503, detail="Gateway service is not available")
        _assert_provider_treasury_authorized(request)
        database_manager = _database_manager(request, db_manager)
        stored_record = await _get_rebalance(database_manager, rebalance_id)
        if stored_record is None:
            result = await accounts_service.gateway_client.get_treasury_rebalance(rebalance_id)
            return _rebalance_response(result, rebalance_id=rebalance_id)
        claimed_record = await _claim_rebalance_for_execution(database_manager, rebalance_id)
        if claimed_record is None:
            result = await accounts_service.gateway_client.get_treasury_rebalance(rebalance_id)
            response = _rebalance_response(result, rebalance_id=rebalance_id)
            await _update_rebalance_status(
                database_manager,
                rebalance_id,
                _rebalance_response_payload(response),
            )
            return response

        stored = claimed_record.request_payload
        result = await accounts_service.gateway_client.execute_treasury_rebalance(
            idempotency_key=rebalance_id,
            wallet_address=stored["wallet_address"],
            destination_address=stored["destination_address"],
            amount=stored["amount"],
            provider=stored["provider"],
            source_network=stored["source_network"],
            destination_network=stored.get("destination_network") or None,
            **_stored_provider_semantic_gateway_fields(stored),
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
        _rebalance_response(result, rebalance_id=rebalance_id)
        status_result = await accounts_service.gateway_client.get_treasury_rebalance(rebalance_id)
        status_result.setdefault("id", rebalance_id)
        status_result.setdefault("route", stored["provider"])
        result = status_result
        result.setdefault("id", rebalance_id)
        result.setdefault("route", stored["provider"])
        response = _rebalance_response(result, rebalance_id=rebalance_id)
        await _update_rebalance_status(
            database_manager,
            rebalance_id,
            _rebalance_response_payload(response),
        )
        return response
    except HTTPException:
        raise
    except Exception as exc:
        redacted = _redact_provider_error(exc)
        logger.error("Provider treasury rebalance execute failed: %s", redacted)
        raise HTTPException(status_code=500, detail=f"Provider treasury rebalance execute failed: {redacted}")


@router.get("/rebalances/{rebalance_id}", response_model=ProviderTreasuryRebalanceResponse)
async def get_provider_treasury_rebalance(
    rebalance_id: str,
    request: Request,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager=Depends(get_database_manager),
) -> ProviderTreasuryRebalanceResponse:
    """Fetch provider-owned treasury rebalance status from Gateway."""
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
    if route == SQUID_ROUTER_ROUTE:
        if body.source_venue.strip().lower() != "gateway" or body.destination_venue.strip().lower() != "gateway":
            raise HTTPException(status_code=400, detail=SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER)
        source_network = _squid_evm_gateway_network(body.source_network)
        destination_chain, destination_network, _, _ = _squid_destination_context(body.destination_network)
        _squid_source_asset(body.source_asset, source_network)
        _squid_destination_asset(
            body.destination_asset,
            destination_chain=destination_chain,
            destination_network=destination_network,
        )
        return
    if route == PROVIDER_TREASURY_SAME_CHAIN_SWAP_ROUTE:
        if body.source_venue.strip().lower() != "gateway" or body.destination_venue.strip().lower() != "gateway":
            raise HTTPException(status_code=400, detail=SAME_CHAIN_SWAP_BLOCKER)
        source_network = _same_chain_swap_network(body.source_network)
        destination_network = _same_chain_swap_network(body.destination_network)
        if source_network != destination_network:
            raise HTTPException(status_code=400, detail=SAME_CHAIN_SWAP_BLOCKER)
        if body.source_asset.strip().upper() not in {"ETH", "WETH"}:
            raise HTTPException(status_code=400, detail=SAME_CHAIN_SWAP_BLOCKER)
        if body.destination_asset.strip().upper() != SUPPORTED_ASSET:
            raise HTTPException(status_code=400, detail=SAME_CHAIN_SWAP_BLOCKER)
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


def _squid_evm_gateway_network(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    network = SQUID_EVM_GATEWAY_NETWORK_ALIASES.get(normalized)
    if network is None:
        raise HTTPException(status_code=400, detail=SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER)
    return "arbitrum" if network == "arbitrum-mainnet" else network


def _same_chain_swap_network(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    network = SQUID_EVM_GATEWAY_NETWORK_ALIASES.get(normalized)
    if network in {"arbitrum-mainnet", "arbitrum"}:
        return "arbitrum"
    raise HTTPException(status_code=400, detail=SAME_CHAIN_SWAP_BLOCKER)


def _squid_destination_context(value: str) -> tuple[str, str, str, str]:
    normalized = value.strip().lower().replace("_", "-")
    network = SQUID_EVM_GATEWAY_NETWORK_ALIASES.get(normalized)
    if network is not None:
        gateway_network = "arbitrum" if network == "arbitrum-mainnet" else network
        wallet_chain, wallet_network = _wallet_identity_context(gateway_network)
        return wallet_chain, gateway_network, wallet_chain, wallet_network
    non_evm_context = SQUID_NON_EVM_DESTINATION_ALIASES.get(normalized)
    if non_evm_context is None:
        raise HTTPException(status_code=400, detail=SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER)
    chain, gateway_network = non_evm_context
    return chain, gateway_network, chain, gateway_network


def _squid_source_asset(value: str, source_network: str) -> str:
    if _is_squid_native_asset_for_context(value, chain="ethereum", network=source_network):
        return SQUID_NATIVE_TOKEN_ADDRESS
    normalized = value.strip().lower()
    if re.fullmatch(r"0x[a-fA-F0-9]{40}", value.strip()):
        return value.strip()
    token_address = SQUID_EVM_SOURCE_ASSETS.get((source_network, normalized))
    if token_address:
        return token_address
    raise HTTPException(status_code=400, detail=SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER)


def _squid_destination_asset(value: str, *, destination_chain: str, destination_network: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        raise HTTPException(status_code=400, detail=SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER)
    if _is_squid_native_asset_for_context(value, chain=destination_chain, network=destination_network):
        return SQUID_NATIVE_TOKEN_ADDRESS
    if destination_chain.strip().lower() == "ethereum":
        token_address = SQUID_EVM_USDC_ASSETS.get((destination_network, normalized))
        if token_address:
            return token_address
    return value.strip()


def _is_squid_native_asset_for_context(value: str, *, chain: str, network: str) -> bool:
    normalized = value.strip().lower()
    if normalized == SQUID_NATIVE_TOKEN_ADDRESS.lower():
        return True
    normalized_chain = chain.strip().lower()
    normalized_network = network.strip().lower()
    if normalized_chain == "solana":
        return normalized == "sol"
    if normalized_chain == "xrpl":
        return normalized in {"xrp", "xrpl"}
    if normalized_network == "bsc":
        return normalized == "bnb"
    if normalized_network == "polygon":
        return normalized in {"matic", "pol"}
    if normalized_network == "avalanche":
        return normalized == "avax"
    return normalized == "eth"


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
    "data",
    "mnemonic",
    "privatekey",
    "rawcalldata",
    "routepayload",
    "routes",
    "secret",
    "secretkey",
    "signature",
    "target",
    "token",
    "to",
    "transactionrequest",
    "transaction_request",
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
        if normalized_key in _SENSITIVE_METADATA_KEYS or (
            normalized_key == "route" and isinstance(item, (dict, list))
        ):
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
        "connector_id": "hyperliquid" if provider == HYPERLIQUID_BRIDGE2_ROUTE else "treasury",
        "destination_address": destination_address,
        "destination_network": destination_network,
        "network": source_network,
        "notional": amount,
        "scope": "provider_treasury",
        "source": "marlin",
        "wallet_address": wallet_address,
    }


def _provider_semantic_gateway_fields(
    *,
    provider: str,
    source_chain: str,
    source_asset: str,
    destination_chain: str | None,
    destination_asset: str,
    destination_venue: str | None,
    source_asset_decimals: int | str | None = None,
) -> dict[str, str]:
    if provider not in {SQUID_ROUTER_ROUTE, PROVIDER_TREASURY_SAME_CHAIN_SWAP_ROUTE}:
        return {}
    fields = {
        "source_chain": source_chain,
        "source_asset": source_asset,
        "destination_asset": destination_asset,
    }
    if destination_chain:
        fields["destination_chain"] = destination_chain
    if destination_venue:
        fields["destination_venue"] = destination_venue
    if source_asset_decimals not in (None, ""):
        fields["source_asset_decimals"] = str(source_asset_decimals)
    return fields


def _stored_provider_semantic_gateway_fields(stored: dict[str, str]) -> dict[str, str]:
    return _provider_semantic_gateway_fields(
        provider=stored["provider"],
        source_chain=stored.get("source_chain") or "ethereum",
        source_asset=stored.get("source_asset") or SUPPORTED_ASSET,
        destination_chain=stored.get("destination_chain") or None,
        destination_asset=stored.get("destination_asset") or SUPPORTED_ASSET,
        destination_venue=stored.get("destination_venue") or None,
        source_asset_decimals=stored.get("source_asset_decimals") or None,
    )


def _addresses_equal(left: str, right: str) -> bool:
    if left == right:
        return True
    return left.startswith("0x") and right.startswith("0x") and left.lower() == right.lower()
