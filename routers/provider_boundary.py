import logging
import re
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from hummingbot.client.settings import AllConnectorSettings
from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType

from deps import get_accounts_service, get_database_manager
from models.provider_boundary import (
    ProviderIntentRequest,
    ProviderIntentResponse,
    ProviderSnapshotRequest,
    ProviderSnapshotResponse,
)
from routers.connectors import _gateway_connector_configs, _gateway_connector_name, _gateway_swap_connector
from routers.gateway_swap import get_transaction_status_from_response
from services.accounts_service import AccountsService
from services.cowswap_runtime import (
    COWSWAP_CONNECTOR_NAME,
    cowswap_order_submission_blocker,
    cowswap_supported_order_types,
)
from services.live_trading_gate import assert_live_gateway_mutation_allowed
from services.marlin_runtime import assert_marlin_default_wallet_identity

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Provider Boundary"], prefix="/provider")


@router.post("/snapshot", response_model=ProviderSnapshotResponse)
async def provider_snapshot(
    body: ProviderSnapshotRequest,
    request: Request,
    accounts_service: AccountsService = Depends(get_accounts_service),
) -> ProviderSnapshotResponse:
    connector_name = body.connector_name
    metadata_connector = _metadata_connector_name(connector_name)
    available = await _provider_available(accounts_service, metadata_connector)
    order_types, provider_actions = await _provider_capabilities(
        request,
        accounts_service,
        metadata_connector,
    )
    trading_rule = await _provider_trading_rule(
        request,
        accounts_service,
        metadata_connector,
        body.trading_pair,
    )
    portfolio: dict[str, Any] | None = None
    issues: list[str] = []

    if not available:
        issues.append(f"provider not available: {connector_name}")

    if body.refresh_portfolio:
        try:
            await accounts_service.update_account_state(
                account_names=[body.account_name],
                connector_names=[connector_name],
                skip_gateway="swap" not in {action.lower() for action in provider_actions},
            )
        except Exception as exc:
            issues.append(f"portfolio refresh unavailable: {_redact_secret_text(exc)}")

    try:
        portfolio_state = accounts_service.get_accounts_state()
        account_state = portfolio_state.get(body.account_name, {})
        if connector_name in account_state:
            portfolio = {body.account_name: {connector_name: account_state.get(connector_name, [])}}
        else:
            portfolio = {body.account_name: {}}
    except Exception as exc:
        issues.append(f"portfolio unavailable: {_redact_secret_text(exc)}")

    if not order_types and "swap" not in {action.lower() for action in provider_actions}:
        issues.append(f"provider actions missing: {connector_name}")
    if trading_rule is None and "swap" not in {action.lower() for action in provider_actions}:
        issues.append(f"trading rules missing for {body.trading_pair}")
    if (
        portfolio is not None
        and connector_name not in portfolio.get(body.account_name, {})
        and "swap" not in {action.lower() for action in provider_actions}
    ):
        issues.append(f"provider account not configured: {connector_name}")

    return ProviderSnapshotResponse(
        account_name=body.account_name,
        connector_name=connector_name,
        trading_pair=body.trading_pair,
        provider_available=available,
        status="available" if not issues else "issues",
        operator_issues=issues,
        limit_maker_order_supported="LIMIT_MAKER" in {item.upper() for item in order_types},
        order_types=order_types,
        provider_actions=provider_actions,
        portfolio=portfolio,
        trading_rule=trading_rule,
    )


@router.post("/intents", response_model=ProviderIntentResponse)
async def submit_provider_intent(
    body: ProviderIntentRequest,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager=Depends(get_database_manager),  # noqa: ANN001 - kept for parity with swap router dependencies.
) -> ProviderIntentResponse:
    del db_manager
    if body.action == "order":
        return await _submit_order_intent(body, accounts_service)
    return await _submit_swap_intent(body, accounts_service)


async def _submit_order_intent(
    body: ProviderIntentRequest,
    accounts_service: AccountsService,
) -> ProviderIntentResponse:
    order_type = body.order_type or "MARKET"
    try:
        order_id = await accounts_service.place_trade(
            account_name=body.account_name,
            connector_name=body.connector_name,
            trading_pair=body.market_id,
            trade_type=TradeType[body.side],
            amount=body.quantity,
            order_type=OrderType[order_type],
            price=body.price,
            position_action=PositionAction.OPEN,
            safe_testnet=body.mode == "testnet",
            live_action_authorization=body.live_action_authorization,
        )
    except HTTPException as exc:
        return ProviderIntentResponse(
            status="rejected" if exc.status_code < 500 else "failed",
            correlation_id=body.correlation_id,
            provider_error=_redact_secret_text(exc.detail),
        )
    except Exception as exc:
        logger.error("Provider order intent failed: %s", _redact_secret_text(exc))
        return ProviderIntentResponse(
            status="failed",
            correlation_id=body.correlation_id,
            provider_error=_redact_secret_text(exc),
        )
    return ProviderIntentResponse(
        status="submitted",
        correlation_id=body.correlation_id,
        external_order_id=str(order_id),
        submitted_quantity=body.quantity,
        submitted_notional=body.quantity * body.price if body.price is not None else None,
        provider_status="submitted",
    )


async def _submit_swap_intent(
    body: ProviderIntentRequest,
    accounts_service: AccountsService,
) -> ProviderIntentResponse:
    network_id = str(body.risk_metadata.get("network", "")).strip()
    if not network_id:
        return ProviderIntentResponse(
            status="rejected",
            correlation_id=body.correlation_id,
            provider_error="network required for swap intent",
        )
    if "-" not in body.market_id:
        return ProviderIntentResponse(
            status="rejected",
            correlation_id=body.correlation_id,
            provider_error=f"invalid trading pair: {body.market_id}",
        )
    try:
        if not await accounts_service.gateway_client.ping():
            return ProviderIntentResponse(
                status="failed",
                correlation_id=body.correlation_id,
                provider_error="Gateway service is not available",
            )
        chain, network = accounts_service.gateway_client.parse_network_id(network_id)
        slippage_pct = Decimal(str(body.risk_metadata.get("slippage_pct", "1.0")))
        assert_live_gateway_mutation_allowed(
            action="swap_execute",
            chain=chain,
            expected_connector_id=body.connector_name,
            expected_instrument=body.market_id,
            expected_notional=body.quantity,
            expected_slippage_bps=slippage_pct * Decimal("100"),
            live_action_authorization=body.live_action_authorization,
            network=network,
            source="provider.intents",
        )
        wallet_address = await _ensure_marlin_wallet_default(
            accounts_service,
            body=body,
            chain=chain,
            network=network,
        )
        base, quote = body.market_id.split("-", 1)
        result = await accounts_service.gateway_client.execute_swap(
            connector=body.connector_name,
            network=network,
            wallet_address=wallet_address,
            base_asset=base,
            quote_asset=quote,
            amount=body.quantity,
            side=body.side,
            slippage_pct=float(slippage_pct),
            pool_address=body.risk_metadata.get("pool_address"),
            live_action_authorization=body.live_action_authorization,
        )
    except HTTPException as exc:
        return ProviderIntentResponse(
            status="rejected" if exc.status_code < 500 else "failed",
            correlation_id=body.correlation_id,
            provider_error=_redact_secret_text(exc.detail),
        )
    except Exception as exc:
        logger.error("Provider swap intent failed: %s", _redact_secret_text(exc))
        return ProviderIntentResponse(
            status="failed",
            correlation_id=body.correlation_id,
            provider_error=_redact_secret_text(exc),
        )

    tx_hash = result.get("signature") or result.get("txHash") or result.get("hash")
    if not tx_hash:
        return ProviderIntentResponse(
            status="failed",
            correlation_id=body.correlation_id,
            provider_error="swap response missing transaction hash",
            provider_status=str(result.get("status", "")),
        )
    return ProviderIntentResponse(
        status=get_transaction_status_from_response(result).lower(),
        correlation_id=body.correlation_id,
        external_order_id=str(tx_hash),
        submitted_quantity=body.quantity,
        provider_status=str(result.get("status", "")),
    )


async def _provider_available(accounts_service: AccountsService, connector_name: str) -> bool:
    connectors = set(AllConnectorSettings.get_connector_settings().keys())
    if connector_name in connectors or connector_name == COWSWAP_CONNECTOR_NAME:
        return True
    return any(_gateway_connector_name(item) == connector_name for item in await _gateway_connector_configs(accounts_service))


async def _provider_capabilities(
    request: Request,
    accounts_service: AccountsService,
    connector_name: str,
) -> tuple[list[str], list[str]]:
    if connector_name == COWSWAP_CONNECTOR_NAME:
        blocker = cowswap_order_submission_blocker(connector_name)
        return ([] if blocker else list(cowswap_supported_order_types() or ())), []
    if await _gateway_swap_connector(accounts_service, connector_name) is True:
        return [], ["swap"]
    try:
        connector_instance = request.app.state.market_data_service.connector_service.get_data_connector(
            connector_name,
        )
    except Exception:
        return [], []
    if not hasattr(connector_instance, "supported_order_types"):
        return [], []
    return [order_type.name for order_type in connector_instance.supported_order_types()], []


async def _ensure_marlin_wallet_default(
    accounts_service: AccountsService,
    *,
    body: ProviderIntentRequest,
    chain: str,
    network: str,
) -> str:
    wallet_identity = body.wallet_identity
    if not isinstance(wallet_identity, dict):
        raise HTTPException(status_code=400, detail="wallet_identity required for swap intent")
    address = str(wallet_identity.get("address", "")).strip()
    wallet_ref = str(wallet_identity.get("wallet_ref", "")).strip()
    identity_chain = str(wallet_identity.get("chain", chain)).strip()
    identity_network = str(wallet_identity.get("network", network)).strip()
    if not address or not wallet_ref:
        raise HTTPException(status_code=400, detail="wallet_identity address and wallet_ref are required")
    if identity_chain != chain or _network_alias(identity_network) != _network_alias(network):
        raise HTTPException(status_code=400, detail="wallet_identity network does not match swap network")
    assert_marlin_default_wallet_identity(
        chain=identity_chain,
        network=identity_network,
        address=address,
        wallet_ref=wallet_ref,
    )
    result = await accounts_service.gateway_client.set_marlin_default_wallet(
        chain=identity_chain,
        network=identity_network,
        address=address,
        wallet_ref=wallet_ref,
    )
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=400, detail=f"Failed to set default wallet: {result.get('error')}")
    return address


def _metadata_connector_name(connector_name: str) -> str:
    for suffix in ("_paper_trade",):
        if connector_name.endswith(suffix):
            return connector_name[: -len(suffix)]
    return connector_name


def _network_alias(network: str) -> str:
    normalized = network.strip().lower()
    if normalized in {"solana-mainnet-beta", "mainnet-beta"}:
        return "solana-mainnet-beta"
    return normalized


def _redact_secret_text(value: object) -> str:
    text = str(value)
    text = re.sub(
        r"(?i)(secret|password|mnemonic|private[_-]?key|api[_-]?key|token)"
        r"([=:'\"\s-]+)([^\s,;)}\]]+)",
        r"\1\2<redacted>",
        text,
    )
    text = re.sub(r"(?i)[A-Za-z0-9_-]*secret[A-Za-z0-9_-]*", "<redacted>", text)
    text = re.sub(r"(?i)[A-Za-z0-9_-]*token[A-Za-z0-9_-]*", "<redacted>", text)
    return text


async def _provider_trading_rule(
    request: Request,
    accounts_service: AccountsService,
    connector_name: str,
    trading_pair: str,
) -> dict[str, Any] | None:
    if await _gateway_swap_connector(accounts_service, connector_name) is True:
        return None
    if connector_name == COWSWAP_CONNECTOR_NAME:
        runtime = getattr(accounts_service, "_cowswap_runtime", None)
        rules = getattr(runtime, "trading_rules", {}) if runtime is not None else {}
        rule = rules.get(trading_pair)
        return None if rule is None else {
            "min_order_size": float(getattr(rule, "min_order_size", 0)),
            "min_notional_size": 0.0,
            "min_price_increment": float(getattr(rule, "min_price_increment", 0)),
            "min_base_amount_increment": float(getattr(rule, "min_base_amount_increment", 0)),
            "supports_limit_orders": False,
            "supports_market_orders": True,
        }
    try:
        rules = await request.app.state.market_data_service.get_trading_rules(connector_name, [trading_pair])
    except Exception:
        return None
    rule = rules.get(trading_pair) if isinstance(rules, dict) else None
    return rule if isinstance(rule, dict) and "error" not in rule else None
