import logging
import os
import re
import secrets
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
    cowswap_runtime_prices,
    cowswap_supported_order_types,
)
from services.live_trading_gate import (
    assert_live_gateway_mutation_allowed,
)
from services.marlin_runtime import assert_marlin_default_wallet_identity

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Provider Boundary"], prefix="/provider")
MARLIN_PROVIDER_INTENT_TOKEN_HEADER = "x-marlin-provider-intent-token"

GATEWAY_SWAP_CONNECTOR_PORTFOLIO_KEYS = {
    "aerodrome": "ethereum-base",
    "jupiter": "solana-mainnet-beta",
    "orca": "solana-mainnet-beta",
}


@router.post("/snapshot", response_model=ProviderSnapshotResponse)
async def provider_snapshot(
    body: ProviderSnapshotRequest,
    request: Request,
    accounts_service: AccountsService = Depends(get_accounts_service),
) -> ProviderSnapshotResponse:
    connector_name = body.connector_name
    metadata_connector = _metadata_connector_name(connector_name)
    available = await _provider_available(accounts_service, metadata_connector)
    cow_runtime_blocker = (
        _cowswap_provider_runtime_blocker(accounts_service)
        if metadata_connector == COWSWAP_CONNECTOR_NAME
        else None
    )
    order_types, provider_actions = await _provider_capabilities(
        request,
        accounts_service,
        metadata_connector,
        runtime_blocker=cow_runtime_blocker,
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
    if cow_runtime_blocker:
        issues.append(_cowswap_provider_runtime_issue(cow_runtime_blocker))

    if body.refresh_portfolio:
        refresh_connector_names = [connector_name]
        action_set = {action.lower() for action in provider_actions}
        tokens_by_chain_network = None
        if "swap" in action_set:
            chain_network_key = GATEWAY_SWAP_CONNECTOR_PORTFOLIO_KEYS.get(
                connector_name,
                connector_name,
            )
            refresh_connector_names = [
                chain_network_key
            ]
            tokens_by_chain_network = {
                chain_network_key: _gateway_balance_tokens_for_pair(
                    body.trading_pair,
                    chain_network_key=chain_network_key,
                )
            }
        try:
            await accounts_service.update_account_state(
                account_names=[body.account_name],
                connector_names=refresh_connector_names,
                skip_gateway="swap" not in action_set,
                tokens_by_chain_network=tokens_by_chain_network,
            )
        except Exception as exc:
            issues.append(f"portfolio refresh unavailable: {_redact_secret_text(exc)}")
        xrpl_refresh_error = _connector_balance_refresh_error(accounts_service, connector_name)
        if connector_name == "xrpl" and _xrpl_account_not_found(xrpl_refresh_error):
            _append_issue_once(
                issues,
                "account not activated: fund derived XRPL mainnet account reserve",
            )

    try:
        portfolio_state = accounts_service.get_accounts_state()
        account_state = portfolio_state.get(body.account_name, {})
        portfolio_value = account_state.get(connector_name)
        if (
            "swap" in {action.lower() for action in provider_actions}
            and connector_name in GATEWAY_SWAP_CONNECTOR_PORTFOLIO_KEYS
        ):
            chain_network_key = GATEWAY_SWAP_CONNECTOR_PORTFOLIO_KEYS[connector_name]
            gateway_portfolio_value = _gateway_verified_portfolio_rows(
                account_state.get(chain_network_key),
                network=body.network or chain_network_key,
                route_id=body.route_id,
                wallet_ref=body.wallet_ref,
            )
            if gateway_portfolio_value is not None:
                portfolio_value = gateway_portfolio_value
        if connector_name in account_state:
            portfolio = {body.account_name: {connector_name: portfolio_value}}
        elif portfolio_value is not None:
            portfolio = {body.account_name: {connector_name: portfolio_value}}
        else:
            portfolio = {body.account_name: {}}
        if (
            connector_name == COWSWAP_CONNECTOR_NAME
            and cow_runtime_blocker is None
            and trading_rule is not None
        ):
            portfolio = await _portfolio_with_cowswap_quote_prices(
                accounts_service,
                portfolio=portfolio,
                request=body,
            )
    except Exception as exc:
        issues.append(f"portfolio unavailable: {_redact_secret_text(exc)}")
    if connector_name == "xrpl" and _xrpl_portfolio_unfunded(portfolio, body.account_name):
        _append_issue_once(
            issues,
            "account not activated: fund derived XRPL mainnet account reserve",
        )

    suppress_derived_order_issues = cow_runtime_blocker is not None
    if (
        not suppress_derived_order_issues
        and not order_types
        and "swap" not in {action.lower() for action in provider_actions}
    ):
        issues.append(f"provider actions missing: {connector_name}")
    if (
        not suppress_derived_order_issues
        and trading_rule is None
        and "swap" not in {action.lower() for action in provider_actions}
    ):
        issues.append(f"trading rules missing for {body.trading_pair}")
    if (
        not suppress_derived_order_issues
        and
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
    request: Request,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager=Depends(get_database_manager),  # noqa: ANN001 - kept for parity with swap router dependencies.
) -> ProviderIntentResponse:
    del db_manager
    if body.action == "order":
        return await _submit_order_intent(body, request, accounts_service)
    return await _submit_swap_intent(
        body,
        request,
        accounts_service,
    )


async def _submit_order_intent(
    body: ProviderIntentRequest,
    request: Request,
    accounts_service: AccountsService,
) -> ProviderIntentResponse:
    order_type = body.order_type or "MARKET"
    try:
        provider_intent_authorized = _mainnet_provider_intent_authorized(body, request)
    except HTTPException as exc:
        return ProviderIntentResponse(
            status="rejected" if exc.status_code < 500 else "failed",
            correlation_id=body.correlation_id,
            provider_error=_redact_secret_text(exc.detail),
        )
    if body.preflight_only:
        return await _preflight_order_intent(body, accounts_service, order_type=order_type)
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
            marlin_provider_intent_authorized=provider_intent_authorized,
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


async def _preflight_order_intent(
    body: ProviderIntentRequest,
    accounts_service: AccountsService,
    *,
    order_type: str,
) -> ProviderIntentResponse:
    try:
        await accounts_service.update_account_state(
            account_names=[body.account_name],
            connector_names=[body.connector_name],
            skip_gateway=True,
        )
        account_state = accounts_service.get_accounts_state().get(body.account_name, {})
    except Exception as exc:
        return ProviderIntentResponse(
            status="rejected",
            correlation_id=body.correlation_id,
            provider_error=_redact_secret_text(exc),
        )
    refresh_blocker = _preflight_order_refresh_blocker(accounts_service, body.connector_name)
    if refresh_blocker is not None:
        return ProviderIntentResponse(
            status="rejected",
            correlation_id=body.correlation_id,
            provider_error=refresh_blocker,
        )
    if body.connector_name not in account_state:
        return ProviderIntentResponse(
            status="rejected",
            correlation_id=body.correlation_id,
            provider_error=f"provider account not configured: {body.connector_name}",
        )
    balance_blocker = _preflight_order_balance_blocker(
        body,
        account_state.get(body.connector_name),
    )
    if balance_blocker is not None:
        return ProviderIntentResponse(
            status="rejected",
            correlation_id=body.correlation_id,
            provider_error=balance_blocker,
        )
    return ProviderIntentResponse(
        status="accepted",
        correlation_id=body.correlation_id,
        provider_status=f"preflight_accepted:{order_type}",
        submitted_quantity=body.quantity,
        submitted_notional=body.quantity * body.price if body.price is not None else None,
    )


def _preflight_order_refresh_blocker(
    accounts_service: AccountsService,
    connector_name: str,
) -> str | None:
    refresh_error = _connector_balance_refresh_error(accounts_service, connector_name)
    if connector_name == "xrpl" and _xrpl_account_not_found(refresh_error):
        return "account not activated: fund derived XRPL mainnet account reserve"
    if refresh_error:
        return f"balance refresh unavailable: {_redact_secret_text(refresh_error)}"
    return None


def _preflight_order_balance_blocker(
    body: ProviderIntentRequest,
    connector_rows: Any,
) -> str | None:
    if not isinstance(connector_rows, list):
        return None
    if body.connector_name == "xrpl" and not connector_rows:
        return "account not activated: fund derived XRPL mainnet account reserve"
    spend_asset, required = _preflight_order_spend_requirement(body)
    if spend_asset is None or required is None:
        return f"preflight spend balance unavailable for {body.side} {body.market_id}"
    available = _available_units_for_asset(connector_rows, spend_asset)
    if available >= required:
        return None
    return (
        f"preflight spend balance {required} {spend_asset} exceeds "
        f"available balance {available}"
    )


def _preflight_order_spend_requirement(
    body: ProviderIntentRequest,
) -> tuple[str | None, Decimal | None]:
    if "-" not in body.market_id:
        return None, None
    base, quote = body.market_id.split("-", 1)
    if body.side == "SELL":
        return base, body.quantity
    if body.price is None:
        return None, None
    return quote, body.quantity * body.price


async def _preflight_swap_balance_blocker(
    body: ProviderIntentRequest,
    accounts_service: AccountsService,
    *,
    network_id: str,
) -> str | None:
    chain_network_key = GATEWAY_SWAP_CONNECTOR_PORTFOLIO_KEYS.get(body.connector_name)
    if chain_network_key is None:
        return None
    try:
        await accounts_service.update_account_state(
            account_names=[body.account_name],
            connector_names=[chain_network_key],
            skip_gateway=False,
            tokens_by_chain_network={
                chain_network_key: _gateway_balance_tokens_for_pair(
                    body.market_id,
                    chain_network_key=chain_network_key,
                )
            },
        )
        account_state = accounts_service.get_accounts_state().get(body.account_name, {})
    except Exception as exc:
        return f"balance refresh unavailable: {_redact_secret_text(exc)}"
    rows = _gateway_verified_portfolio_rows(
        account_state.get(chain_network_key),
        network=network_id,
        route_id=str(body.risk_metadata.get("route_id") or "").strip() or None,
        wallet_ref=(
            str(body.wallet_identity.get("wallet_ref") or "").strip()
            if isinstance(body.wallet_identity, dict)
            else None
        ),
    )
    spend_asset, required = _preflight_swap_spend_requirement(body)
    if not isinstance(rows, list):
        return f"preflight spend balance unavailable for {body.side} {body.market_id}"
    available = _available_units_for_asset(rows, spend_asset)
    if available >= required:
        return None
    return (
        f"preflight spend balance {required} {spend_asset} exceeds "
        f"available balance {available}"
    )


def _preflight_swap_spend_requirement(body: ProviderIntentRequest) -> tuple[str, Decimal]:
    base, quote = body.market_id.split("-", 1)
    if body.side == "SELL":
        return base, body.quantity
    return quote, body.quantity


def _available_units_for_asset(rows: list[Any], asset: str) -> Decimal:
    for row in rows:
        if not isinstance(row, dict):
            continue
        token = str(row.get("token") or row.get("asset") or row.get("symbol") or "").upper()
        if token != asset.upper():
            continue
        for key in ("available_units", "available", "units", "balance", "total"):
            value = row.get(key)
            if value is not None:
                try:
                    return Decimal(str(value))
                except Exception:
                    return Decimal("0")
    return Decimal("0")


def _mainnet_provider_intent_authorized(
    body: ProviderIntentRequest,
    request: Request,
) -> bool:
    if body.mode != "mainnet":
        return False
    expected = _marlin_provider_intent_token()
    provided = str(getattr(request, "headers", {}).get(MARLIN_PROVIDER_INTENT_TOKEN_HEADER, ""))
    if expected and provided and secrets.compare_digest(provided, expected):
        return True
    raise HTTPException(
        status_code=403,
        detail="Marlin provider intent token required for mainnet provider intents",
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


def _marlin_gateway_swap_authorization(
    *,
    connector_id: str,
    network: str,
    wallet_address: str,
    notional: Any,
    slippage_bps: Any,
) -> dict[str, str]:
    return {
        "action": "gateway_swap",
        "connector_id": connector_id,
        "network": network,
        "notional": str(notional),
        "scope": "provider_intent",
        "slippage_bps": str(slippage_bps),
        "source": "marlin",
        "wallet_address": wallet_address,
    }


async def _submit_swap_intent(
    body: ProviderIntentRequest,
    request: Request,
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
        provider_intent_authorized = _mainnet_provider_intent_authorized(body, request)
        chain, network = accounts_service.gateway_client.parse_network_id(network_id)
        slippage_pct = Decimal(str(body.risk_metadata.get("slippage_pct", "1.0")))
        assert_live_gateway_mutation_allowed(
            action="swap_execute",
            chain=chain,
            expected_connector_id=body.connector_name,
            expected_instrument=body.market_id,
            expected_notional=body.quantity,
            expected_slippage_bps=slippage_pct * Decimal("100"),
            marlin_provider_intent_authorized=provider_intent_authorized,
            network=network,
            source="provider.intents",
        )
        wallet_address = await _ensure_marlin_wallet_default(
            accounts_service,
            body=body,
            chain=chain,
            network=network,
        )
        if body.preflight_only:
            balance_blocker = await _preflight_swap_balance_blocker(
                body,
                accounts_service,
                network_id=network_id,
            )
            if balance_blocker is not None:
                return ProviderIntentResponse(
                    status="rejected",
                    correlation_id=body.correlation_id,
                    provider_error=balance_blocker,
                )
            return ProviderIntentResponse(
                status="accepted",
                correlation_id=body.correlation_id,
                provider_status="preflight_accepted",
                submitted_quantity=body.quantity,
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
            live_action_authorization=_marlin_gateway_swap_authorization(
                connector_id=body.connector_name,
                network=network,
                wallet_address=wallet_address,
                notional=body.quantity,
                slippage_bps=slippage_pct * Decimal("100"),
            ),
            marlin_provider_intent_authorized=provider_intent_authorized,
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
        provider_error = (
            result.get("error")
            or result.get("message")
            or result.get("detail")
            or result.get("error_message")
        )
        return ProviderIntentResponse(
            status="failed",
            correlation_id=body.correlation_id,
            provider_error=_redact_secret_text(provider_error)
            if provider_error
            else "swap response missing transaction hash",
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
    if connector_name in GATEWAY_SWAP_CONNECTOR_PORTFOLIO_KEYS:
        return True
    return any(_gateway_connector_name(item) == connector_name for item in await _gateway_connector_configs(accounts_service))


async def _provider_capabilities(
    request: Request,
    accounts_service: AccountsService,
    connector_name: str,
    *,
    runtime_blocker: str | None = None,
) -> tuple[list[str], list[str]]:
    if connector_name == COWSWAP_CONNECTOR_NAME:
        blocker = runtime_blocker
        if blocker is None:
            blocker = _cowswap_provider_runtime_blocker(accounts_service)
        order_types = [] if blocker else list(cowswap_supported_order_types() or ())
        return order_types, (["order", "cancel"] if order_types else [])
    if connector_name in GATEWAY_SWAP_CONNECTOR_PORTFOLIO_KEYS:
        return [], ["swap"]
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
    order_types = [order_type.name for order_type in connector_instance.supported_order_types()]
    return order_types, (["order", "cancel"] if order_types else [])


def _cowswap_provider_runtime_blocker(accounts_service: AccountsService) -> str | None:
    return cowswap_order_submission_blocker(
        COWSWAP_CONNECTOR_NAME,
        runtime_dependencies=getattr(accounts_service, "_cowswap_runtime_dependencies", None),
    )


def _cowswap_provider_runtime_issue(blocker: str) -> str:
    lowered = blocker.lower()
    if "not installed" in lowered:
        return "provider not ready: CowSwap package is not installed"
    if "raw private" in lowered:
        return "provider not ready: CowSwap metadata includes unsafe signing fields"
    return (
        "provider runtime disabled: CowSwap live order runtime requires "
        "Marlin-scoped EIP-712 signer, CoW order store, EVM balance and "
        "allowance reader, asset map, and API lifecycle"
    )


def _connector_balance_refresh_error(accounts_service: AccountsService, connector_name: str) -> str | None:
    error_getter = getattr(accounts_service, "connector_balance_refresh_error", None)
    if not callable(error_getter):
        return None
    error = error_getter(connector_name)
    return str(error) if error else None


def _append_issue_once(issues: list[str], issue: str) -> None:
    if issue not in issues:
        issues.append(issue)


def _xrpl_account_not_found(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return "actnotfound" in lowered or "accountnotfound" in lowered or "account not found" in lowered


def _xrpl_portfolio_unfunded(portfolio: dict[str, Any] | None, account_name: str) -> bool:
    if not isinstance(portfolio, dict):
        return False
    account = portfolio.get(account_name)
    if not isinstance(account, dict) or "xrpl" not in account:
        return False
    balances = account.get("xrpl")
    return isinstance(balances, list) and len(balances) == 0


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
        chain=chain,
        network=network,
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


def _gateway_verified_portfolio_rows(
    value: Any,
    *,
    network: str | None = None,
    route_id: str | None = None,
    wallet_ref: str | None = None,
) -> Any:
    if not isinstance(value, list):
        return value
    rows: list[Any] = []
    for item in value:
        if isinstance(item, dict):
            row = {**item, "balance_source": "gateway"}
            if network:
                row["network"] = _provider_snapshot_network_scope(network)
            if route_id:
                row["route_id"] = route_id
            if wallet_ref:
                row["wallet_ref"] = wallet_ref
            rows.append(row)
        else:
            rows.append(item)
    return rows


def _provider_snapshot_network_scope(network: str) -> str:
    normalized = _network_alias(network)
    if normalized == "ethereum-base":
        return "base"
    return normalized.removeprefix("solana-")


def _gateway_balance_tokens_for_pair(
    trading_pair: str,
    *,
    chain_network_key: str,
) -> list[str]:
    tokens = [
        token.strip().upper()
        for token in trading_pair.replace("/", "-").split("-")
        if token.strip()
    ]
    if chain_network_key.startswith("ethereum-"):
        tokens.append("ETH")
    if chain_network_key.startswith("solana-"):
        tokens.append("SOL")
    return list(dict.fromkeys(tokens))


def _network_alias(network: str) -> str:
    normalized = network.strip().lower()
    if normalized in {"solana-mainnet-beta", "mainnet-beta"}:
        return "solana-mainnet-beta"
    if normalized in {"ethereum-base", "base"}:
        return "ethereum-base"
    if normalized in {"ethereum-base-sepolia", "base-sepolia"}:
        return "ethereum-base-sepolia"
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
    if connector_name in GATEWAY_SWAP_CONNECTOR_PORTFOLIO_KEYS:
        return None
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
    if not isinstance(rule, dict) or "error" in rule:
        return None
    return _normalized_provider_trading_rule(connector_name, rule)


def _normalized_provider_trading_rule(connector_name: str, rule: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(rule)
    if connector_name != "hyperliquid":
        return normalized
    min_notional = Decimal(str(normalized.get("min_notional_size") or 0))
    min_order_value = Decimal(str(normalized.get("min_order_value") or 0))
    hyperliquid_minimum = Decimal("10")
    if min_notional < hyperliquid_minimum:
        normalized["min_notional_size"] = float(hyperliquid_minimum)
    if min_order_value < hyperliquid_minimum:
        normalized["min_order_value"] = float(hyperliquid_minimum)
    return normalized


async def _portfolio_with_cowswap_quote_prices(
    accounts_service: AccountsService,
    *,
    portfolio: dict[str, Any] | None,
    request: ProviderSnapshotRequest,
) -> dict[str, Any] | None:
    runtime = getattr(accounts_service, "_cowswap_runtime", None)
    prices = await cowswap_runtime_prices(
        runtime=runtime,
        trading_pairs=[request.trading_pair],
    )
    price = prices.get(request.trading_pair) if "error" not in prices else None
    if price is None:
        return portfolio
    try:
        base_price = Decimal(str(price))
    except Exception:
        return portfolio
    if base_price <= 0:
        return portfolio

    base_asset, quote_asset = _split_pair(request.trading_pair)
    account_portfolio: dict[str, Any] = dict(portfolio or {})
    account_rows = dict(account_portfolio.get(request.account_name) or {})
    connector_rows = list(account_rows.get(COWSWAP_CONNECTOR_NAME) or [])
    connector_rows = _upsert_price_row(connector_rows, token=base_asset, price=base_price)
    if quote_asset.upper() in {"DAI", "USDC", "USDT", "USD"}:
        connector_rows = _upsert_price_row(connector_rows, token=quote_asset, price=Decimal("1"))
    account_rows[COWSWAP_CONNECTOR_NAME] = connector_rows
    account_portfolio[request.account_name] = account_rows
    return account_portfolio


def _split_pair(trading_pair: str) -> tuple[str, str]:
    parts = [part.strip().upper() for part in trading_pair.replace("/", "-").split("-") if part.strip()]
    if len(parts) != 2:
        return trading_pair.upper(), ""
    return parts[0], parts[1]


def _upsert_price_row(rows: list[Any], *, token: str, price: Decimal) -> list[Any]:
    updated: list[Any] = []
    found = False
    for row in rows:
        if not isinstance(row, dict):
            updated.append(row)
            continue
        if str(row.get("token") or row.get("asset") or "").upper() != token.upper():
            updated.append(row)
            continue
        patched = dict(row)
        patched["price"] = float(price)
        updated.append(patched)
        found = True
    if not found:
        updated.append(
            {
                "available_units": 0.0,
                "price": float(price),
                "token": token.upper(),
                "units": 0.0,
                "value": 0.0,
            }
        )
    return updated
