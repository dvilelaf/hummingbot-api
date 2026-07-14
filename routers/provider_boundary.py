import logging
import os
import re
import secrets
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from hummingbot.client.settings import AllConnectorSettings
from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType

from deps import get_accounts_service, get_database_manager
from models.provider_boundary import (
    ProviderIntentRequest,
    ProviderIntentResponse,
    ProviderPosition,
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
from services.hyperliquid_market import (
    connector_trading_pair,
    logical_balance_rows,
    logical_trading_rule,
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
COWSWAP_DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS = 1800
COWSWAP_ADAPTER_ORDER_TYPES = {"LIMIT", "MARKET"}


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
    positions: list[ProviderPosition] = []
    positions_status = "unsupported"
    issues: list[str] = []
    action_set = {action.lower() for action in provider_actions}

    if not available:
        issues.append(f"provider not available: {connector_name}")
    if cow_runtime_blocker:
        issues.append(_cowswap_provider_runtime_issue(cow_runtime_blocker))

    if body.refresh_portfolio:
        refresh_connector_names = [connector_name]
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

    if _provider_snapshot_positions_enabled(metadata_connector, action_set):
        try:
            raw_positions = await accounts_service.get_account_positions(
                body.account_name,
                connector_name,
            )
            positions = [
                _normalize_provider_position(
                    position,
                    account_name=body.account_name,
                    connector_name=connector_name,
                )
                for position in raw_positions
            ]
            positions_status = "available"
        except Exception as exc:
            positions_status = "issues"
            issues.append(f"positions refresh unavailable: {_redact_secret_text(exc)}")

    try:
        portfolio_state = accounts_service.get_accounts_state()
        account_state = portfolio_state.get(body.account_name, {})
        portfolio_value = logical_balance_rows(
            connector_name,
            account_state.get(connector_name),
        )
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
            portfolio = await _portfolio_with_cowswap_reference_prices(
                accounts_service,
                portfolio=portfolio,
                http_request=request,
                snapshot_request=body,
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
        positions_status=positions_status,
        operator_issues=issues,
        limit_maker_order_supported="LIMIT_MAKER" in {item.upper() for item in order_types},
        order_types=order_types,
        provider_actions=provider_actions,
        positions=positions,
        portfolio=portfolio,
        trading_rule=trading_rule,
    )


def _provider_snapshot_positions_enabled(
    metadata_connector: str,
    provider_actions: list[str],
) -> bool:
    return (
        "_perpetual" in metadata_connector.lower()
        and "order" in {action.lower() for action in provider_actions}
    )


def _normalize_provider_position(
    position: Any,
    *,
    account_name: str,
    connector_name: str,
) -> ProviderPosition:
    if not isinstance(position, dict):
        raise ValueError("position row is not an object")

    trading_pair = str(position.get("trading_pair", "")).strip()
    if not trading_pair:
        raise ValueError("position row is missing trading_pair")

    side = str(position.get("side", "")).upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError(f"position row has unsupported side: {side or '<missing>'}")

    try:
        quantity = abs(Decimal(str(position.get("amount"))))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError("position amount is not a valid decimal") from exc
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("position amount must be finite and nonzero")

    return ProviderPosition(
        account_name=account_name,
        connector_name=connector_name,
        trading_pair=trading_pair,
        side=side,
        quantity=quantity,
        entry_price=_optional_position_decimal(position.get("entry_price"), "entry_price"),
        unrealized_pnl=_optional_position_decimal(position.get("unrealized_pnl"), "unrealized_pnl"),
        leverage=_optional_position_decimal(position.get("leverage"), "leverage"),
    )


def _optional_position_decimal(value: Any, field_name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError(f"position {field_name} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"position {field_name} must be finite")
    return parsed


@router.post("/intents", response_model=ProviderIntentResponse)
async def submit_provider_intent(
    body: ProviderIntentRequest,
    request: Request,
    accounts_service: AccountsService = Depends(get_accounts_service),
    db_manager=Depends(get_database_manager),  # noqa: ANN001 - kept for parity with swap router dependencies.
) -> ProviderIntentResponse:
    del db_manager
    if body.action == "order":
        if _order_intent_executes_as_gateway_swap(body):
            return await _submit_swap_intent(
                body,
                request,
                accounts_service,
            )
        return await _submit_order_intent(body, request, accounts_service)
    return await _submit_swap_intent(
        body,
        request,
        accounts_service,
    )


def _order_intent_executes_as_gateway_swap(body: ProviderIntentRequest) -> bool:
    network_id = str(body.risk_metadata.get("network", "")).strip()
    order_type = (body.order_type or "MARKET").upper()
    return (
        order_type == "MARKET"
        and body.connector_name in GATEWAY_SWAP_CONNECTOR_PORTFOLIO_KEYS
        and bool(network_id)
    )


def _reduce_side(market_side: str) -> str:
    """Return the side that reduces the given market side."""
    if market_side.upper() == "LONG":
        return "SELL"
    if market_side.upper() == "SHORT":
        return "BUY"
    return ""


async def _resolve_reduce_order(
    body: ProviderIntentRequest,
    accounts_service: AccountsService,
) -> tuple[Decimal, PositionAction]:
    """Refresh positions, validate, clip quantity, return (clipped_amount, position_action)."""
    positions = await accounts_service.get_account_positions(
        body.account_name,
        body.connector_name,
    )
    fresh_position = Decimal("0")
    position_side = ""
    connector_market = (
        connector_trading_pair(body.connector_name, body.market_id)
        if body.mode == "mainnet"
        else body.market_id
    )
    for pos in positions:
        if str(pos.get("trading_pair", "")).upper() == connector_market.upper():
            fresh_position = abs(Decimal(str(pos.get("amount", "0"))))
            position_side = str(pos.get("side", "")).upper()
            break
    if fresh_position == 0 or not position_side:
        raise HTTPException(
            status_code=400,
            detail=f"no open position for {body.market_id}; reduce order requires a nonzero position",
        )
    expected_reduce_side = _reduce_side(position_side)
    if not expected_reduce_side or body.side.upper() != expected_reduce_side:
        raise HTTPException(
            status_code=400,
            detail=(
                f"reduce side mismatch for {body.market_id}: "
                f"position is {position_side}, requested {body.side}; "
                f"must be {expected_reduce_side}"
            ),
        )
    clipped = min(body.quantity, fresh_position)
    return clipped, PositionAction.CLOSE


async def _submit_order_intent(
    body: ProviderIntentRequest,
    request: Request,
    accounts_service: AccountsService,
) -> ProviderIntentResponse:
    order_type = (body.order_type or "MARKET").upper()
    if body.connector_name == COWSWAP_CONNECTOR_NAME:
        order_error = _cowswap_order_request_error(order_type, body.price)
        if order_error is not None:
            return ProviderIntentResponse(
                status="rejected",
                correlation_id=body.correlation_id,
                provider_error=order_error,
            )
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
        if body.position_effect == "reduce":
            amount, position_action = await _resolve_reduce_order(body, accounts_service)
        else:
            amount = body.quantity
            position_action = PositionAction.OPEN
    except HTTPException as exc:
        return ProviderIntentResponse(
            status="rejected" if exc.status_code < 500 else "failed",
            correlation_id=body.correlation_id,
            provider_error=_redact_secret_text(exc.detail),
        )
    connector_market = (
        connector_trading_pair(body.connector_name, body.market_id)
        if body.mode == "mainnet"
        else body.market_id
    )
    try:
        order_id = await accounts_service.place_trade(
            account_name=body.account_name,
            connector_name=body.connector_name,
            trading_pair=connector_market,
            trade_type=TradeType[body.side],
            amount=amount,
            order_type=OrderType[order_type],
            price=body.price,
            position_action=position_action,
            safe_testnet=body.mode == "testnet",
            marlin_provider_intent_authorized=provider_intent_authorized,
        )
    except HTTPException as exc:
        redacted = _redact_secret_text(exc.detail)
        return ProviderIntentResponse(
            status="rejected" if exc.status_code < 500 else "failed",
            correlation_id=body.correlation_id,
            provider_error=redacted,
            retry_after_seconds=_intent_retry_after_seconds(body, redacted),
        )
    except Exception as exc:
        redacted = _redact_secret_text(exc)
        logger.error("Provider order intent failed: %s", _redact_secret_text(exc))
        return ProviderIntentResponse(
            status="failed",
            correlation_id=body.correlation_id,
            provider_error=redacted,
            retry_after_seconds=_intent_retry_after_seconds(body, redacted),
        )
    return ProviderIntentResponse(
        status="submitted",
        correlation_id=body.correlation_id,
        external_order_id=str(order_id),
        submitted_quantity=amount,
        submitted_notional=amount * body.price if body.price is not None else None,
        provider_status="submitted",
    )


async def _preflight_order_intent(
    body: ProviderIntentRequest,
    accounts_service: AccountsService,
    *,
    order_type: str,
) -> ProviderIntentResponse:
    if body.connector_name == COWSWAP_CONNECTOR_NAME:
        return await _preflight_cowswap_order_intent(
            body,
            accounts_service,
            order_type=order_type,
        )
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


async def _preflight_cowswap_order_intent(
    body: ProviderIntentRequest,
    accounts_service: AccountsService,
    *,
    order_type: str,
) -> ProviderIntentResponse:
    order_error = _cowswap_order_request_error(order_type, body.price)
    if order_error is not None:
        return ProviderIntentResponse(
            status="rejected",
            correlation_id=body.correlation_id,
            provider_error=order_error,
        )
    runtime = getattr(accounts_service, "_cowswap_runtime", None)
    runtime_dependencies = getattr(accounts_service, "_cowswap_runtime_dependencies", None)
    blocker = cowswap_order_submission_blocker(
        COWSWAP_CONNECTOR_NAME,
        runtime_dependencies=runtime_dependencies,
    )
    if blocker:
        return ProviderIntentResponse(
            status="rejected",
            correlation_id=body.correlation_id,
            provider_error=_cowswap_provider_runtime_issue(blocker),
        )
    if runtime is None or runtime_dependencies is None:
        return ProviderIntentResponse(
            status="rejected",
            correlation_id=body.correlation_id,
            provider_error="CowSwap runtime bridge is not initialized",
        )
    try:
        sell_token, buy_token = _cowswap_tokens_for_pair(runtime, body.market_id)
        spend_token, spend_amount_atomic = await _cowswap_spend_requirement_atomic(
            body,
            runtime=runtime,
            sell_token=sell_token,
            buy_token=buy_token,
            order_type=order_type,
        )
        evm_reader = runtime_dependencies.evm_reader
        owner = runtime_dependencies.owner_address
        if evm_reader is None or not owner:
            raise ValueError("CowSwap EVM reader or owner address missing")
        balance_atomic = int(evm_reader.balance_of(spend_token, owner))
        required_atomic = int(spend_amount_atomic)
        if balance_atomic < required_atomic:
            return ProviderIntentResponse(
                status="rejected",
                correlation_id=body.correlation_id,
                provider_error=f"insufficient {spend_token.symbol} balance",
            )
        allowance_atomic = int(
            evm_reader.allowance(
                spend_token,
                owner,
                _cowswap_vault_relayer(runtime),
            )
        )
        if allowance_atomic < required_atomic:
            return ProviderIntentResponse(
                status="rejected",
                correlation_id=body.correlation_id,
                provider_error=f"insufficient {spend_token.symbol} allowance for CoW VaultRelayer",
            )
    except Exception as exc:
        redacted = _redact_secret_text(exc)
        return ProviderIntentResponse(
            status="rejected",
            correlation_id=body.correlation_id,
            provider_error=redacted,
            retry_after_seconds=_cowswap_retry_after_seconds(redacted),
        )
    return ProviderIntentResponse(
        status="accepted",
        correlation_id=body.correlation_id,
        provider_status=f"preflight_accepted:{order_type}",
        submitted_quantity=body.quantity,
        submitted_notional=body.quantity * body.price if body.price is not None else None,
    )


def _cowswap_tokens_for_pair(runtime: Any, trading_pair: str) -> tuple[Any, Any]:
    tokens_for_pair = getattr(runtime, "_tokens_for_pair", None)
    if not callable(tokens_for_pair):
        raise ValueError("CowSwap runtime token map is unavailable")
    try:
        return tokens_for_pair(trading_pair)
    except Exception as exc:
        raise ValueError(f"unsupported CowSwap trading pair {trading_pair}: {exc}") from exc


def _cowswap_order_request_error(order_type: str, price: Decimal | None) -> str | None:
    advertised_order_types = {
        str(value).upper()
        for value in (cowswap_supported_order_types() or ())
    }
    supported_order_types = sorted(advertised_order_types & COWSWAP_ADAPTER_ORDER_TYPES)
    if order_type not in supported_order_types:
        return (
            f"CowSwap order type '{order_type}' not announced; "
            f"supported types: {supported_order_types}"
        )
    if order_type == "LIMIT" and (
        price is None
        or not price.is_finite()
        or price <= Decimal("0")
    ):
        return "CowSwap LIMIT orders require a positive price"
    return None


async def _cowswap_spend_requirement_atomic(
    body: ProviderIntentRequest,
    *,
    runtime: Any,
    sell_token: Any,
    buy_token: Any,
    order_type: str,
) -> tuple[Any, str]:
    if order_type == "LIMIT":
        if body.side == "SELL":
            return sell_token, _cowswap_amount_to_atomic(
                str(body.quantity),
                int(sell_token.decimals),
            )
        if body.side == "BUY":
            if body.price is None:
                raise ValueError("CowSwap LIMIT orders require a positive price")
            return buy_token, _cowswap_amount_to_atomic(
                str(body.quantity * body.price),
                int(buy_token.decimals),
            )
        raise ValueError("CowSwap side must be BUY or SELL")
    if body.side == "SELL":
        _cowswap_amount_to_atomic(str(body.quantity), int(sell_token.decimals))
        connector = getattr(runtime, "_connector", None)
        quote_sell = getattr(connector, "quote_sell", None)
        if not callable(quote_sell):
            raise ValueError(f"preflight spend amount unavailable for SELL {body.market_id}")
        try:
            quote, _minimum_buy_amount = await quote_sell(
                sell_token,
                buy_token,
                str(body.quantity),
            )
        except Exception as exc:
            raise ValueError(f"preflight CowSwap SELL quote unavailable: {exc}") from exc
        _validate_cowswap_preflight_quote(quote)
        return sell_token, _cowswap_order_sell_amount(runtime, quote)
    if body.side == "BUY":
        connector = getattr(runtime, "_connector", None)
        quote_buy = getattr(connector, "quote_buy", None)
        if not callable(quote_buy):
            raise ValueError(f"preflight spend amount unavailable for BUY {body.market_id}")
        try:
            _quote, maximum_sell_amount = await quote_buy(
                sell_token,
                buy_token,
                str(body.quantity),
            )
        except Exception as exc:
            raise ValueError(f"preflight CowSwap BUY quote unavailable: {exc}") from exc
        _validate_cowswap_preflight_quote(_quote)
        return sell_token, str(maximum_sell_amount)
    raise ValueError("CowSwap side must be BUY or SELL")


def _cowswap_retry_after_seconds(error: object) -> int | None:
    text = str(error)
    normalized = text.lower()
    if not (
        "rate-limited" in normalized
        or "rate limited" in normalized
        or "rate-limit" in normalized
        or "rate limit" in normalized
        or "http error 429" in normalized
        or "429 client error" in normalized
        or "429 too many requests" in normalized
        or "too many requests" in normalized
    ):
        return None
    for pattern in (
        r"\bretry_after_seconds\b\s*[:=]\s*(\d+)",
        r"\bretry[-_ ]after(?:[-_ ]seconds)?\b\s*[:= ]+(\d+)",
    ):
        explicit = re.search(pattern, text, re.IGNORECASE)
        if explicit is not None:
            return max(1, int(explicit.group(1)))
    return COWSWAP_DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS


def _intent_retry_after_seconds(body: ProviderIntentRequest, error: object) -> int | None:
    if body.connector_name != COWSWAP_CONNECTOR_NAME:
        return None
    return _cowswap_retry_after_seconds(error)


def _cowswap_amount_to_atomic(amount: str, decimals: int) -> str:
    parsed = Decimal(str(amount))
    if parsed <= 0:
        raise ValueError("amount must be positive")
    scale = Decimal(10) ** decimals
    atomic = parsed * scale
    if atomic != atomic.to_integral_value():
        raise ValueError(f"amount has more precision than token decimals: {amount}")
    return str(int(atomic))


def _atomic_amount_to_human(amount: str, decimals: int) -> Decimal:
    return Decimal(str(amount)) / (Decimal(10) ** decimals)


def _cowswap_vault_relayer(runtime: Any) -> str:
    from hummingbot_cowswap.chain_config import chain_config

    connector = getattr(runtime, "_connector", None)
    config = getattr(connector, "config", None)
    if config is None:
        raise ValueError("CowSwap connector config is unavailable")
    return chain_config(int(config.chain_id), str(config.env)).vault_relayer


def _cowswap_order_sell_amount(runtime: Any, quote: Any) -> str:
    connector = getattr(runtime, "_connector", None)
    config = getattr(connector, "config", None)
    env = str(getattr(config, "env", "")).lower()
    sell_amount = int(_cow_quote_field(quote, "sellAmount"))
    if env == "staging":
        return str(sell_amount)
    return str(sell_amount + int(_cow_quote_field(quote, "feeAmount")))


def _validate_cowswap_preflight_quote(quote: Any) -> None:
    if _object_field(quote, "verified", False) is not True:
        raise ValueError("CoW quote is not verified")
    valid_to = int(_cow_quote_field(quote, "validTo"))
    if valid_to <= int(time.time()):
        raise ValueError(f"stale CoW quote valid_to={valid_to}")


def _cow_quote_field(quote: Any, field_name: str) -> str:
    payload = _object_field(quote, "quote", None)
    if payload is None:
        raise ValueError("CowSwap quote payload missing quote")
    value = _object_field(payload, field_name, None)
    if value is None:
        raise ValueError(f"CowSwap quote payload missing {field_name}")
    return str(_object_field(value, "root", value))


def _object_field(value: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field_name, default)
    return getattr(value, field_name, default)


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
    return quote, _gateway_swap_spend_amount(body)


def _gateway_swap_spend_amount(body: ProviderIntentRequest) -> Decimal:
    if body.side == "SELL":
        return body.quantity
    notional = body.notional
    if notional is None or not notional.is_finite() or notional <= 0:
        raise ValueError("BUY swap requires a positive finite notional")
    return notional


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
        spend_amount = _gateway_swap_spend_amount(body)
    except ValueError as exc:
        return ProviderIntentResponse(
            status="rejected",
            correlation_id=body.correlation_id,
            provider_error=str(exc),
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
            expected_notional=spend_amount,
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
        base, quote, amount, side = _gateway_swap_execution_terms(body)
        result = await accounts_service.gateway_client.execute_swap(
            connector=body.connector_name,
            network=network,
            wallet_address=wallet_address,
            base_asset=base,
            quote_asset=quote,
            amount=amount,
            side=side,
            slippage_pct=float(slippage_pct),
            pool_address=body.risk_metadata.get("pool_address"),
            live_action_authorization=_marlin_gateway_swap_authorization(
                connector_id=body.connector_name,
                network=network,
                wallet_address=wallet_address,
                notional=spend_amount,
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
    provider_status = _strict_swap_provider_status(result)
    return ProviderIntentResponse(
        status=provider_status.lower(),
        correlation_id=body.correlation_id,
        external_order_id=str(tx_hash),
        submitted_quantity=body.quantity,
        provider_status=str(result.get("status", "")),
        **_confirmed_swap_economics(result, provider_status=provider_status, tx_hash=str(tx_hash)),
    )


def _strict_swap_provider_status(result: dict[str, Any]) -> str:
    provider_status = str(get_transaction_status_from_response(result))
    raw_status = result.get("status")
    if provider_status.upper() == "CONFIRMED" and (
        type(raw_status) is not int or raw_status != 1
    ):
        return "SUBMITTED"
    return provider_status


def _confirmed_swap_economics(
    result: dict[str, Any],
    *,
    provider_status: str,
    tx_hash: str,
) -> dict[str, Any]:
    raw_status = result.get("status")
    if (
        provider_status.upper() != "CONFIRMED"
        or type(raw_status) is not int
        or raw_status != 1
    ):
        return {}
    data = result.get("data")
    if not isinstance(data, dict):
        return {}
    sent_asset = _asset_or_none(data.get("tokenIn"))
    received_asset = _asset_or_none(data.get("tokenOut"))
    sent_quantity = _positive_decimal_or_none(data.get("amountIn"))
    received_quantity = _positive_decimal_or_none(data.get("amountOut"))
    if sent_asset is None or received_asset is None or sent_quantity is None or received_quantity is None:
        return {}
    economics: dict[str, Any] = {
        "external_transaction_id": tx_hash,
        "sent_asset": sent_asset,
        "sent_quantity": sent_quantity,
        "received_asset": received_asset,
        "received_quantity": received_quantity,
    }
    fee_asset = _asset_or_none(data.get("feeAsset"))
    fee_amount = _non_negative_decimal_or_none(data.get("fee"))
    if "feeAsset" in data or "fee" in data:
        if fee_asset is None or fee_amount is None:
            return {}
        economics.update(fee_asset=fee_asset, fee_amount=fee_amount)
    executed_at = result.get("executedAt")
    if executed_at is not None:
        try:
            economics["executed_at"] = datetime.fromisoformat(str(executed_at))
        except ValueError:
            return {}
    return economics


def _asset_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _positive_decimal_or_none(value: Any) -> Decimal | None:
    parsed = _non_negative_decimal_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _non_negative_decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _gateway_swap_execution_terms(
    body: ProviderIntentRequest,
) -> tuple[str, str, Decimal, str]:
    base, quote = body.market_id.split("-", 1)
    if body.side == "BUY":
        return quote, base, _gateway_swap_spend_amount(body), "SELL"
    return base, quote, body.quantity, body.side


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


def _strip_scoped_metadata(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stripped: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            stripped.append(row)
            continue
        stripped.append({
            k: v
            for k, v in row.items()
            if k not in ("balance_source", "network", "route_id", "wallet_ref")
        })
    return stripped


def _cowswap_scoped_portfolio_rows(
    accounts_service: AccountsService,
    *,
    rows: list[dict[str, Any]],
    network: str | None = None,
    route_id: str | None = None,
    wallet_ref: str | None = None,
) -> list[dict[str, Any]]:
    runtime_dependencies = getattr(accounts_service, "_cowswap_runtime_dependencies", None)
    signer_provider = getattr(runtime_dependencies, "signer_provider", None) if runtime_dependencies else None
    signer_network = getattr(signer_provider, "network", None) if signer_provider else None
    signer_wallet_ref = getattr(signer_provider, "wallet_ref", None) if signer_provider else None
    if not signer_network or not signer_wallet_ref:
        return _strip_scoped_metadata(rows)
    if not network or not wallet_ref:
        return _strip_scoped_metadata(rows)
    if _network_alias(network) != _network_alias(signer_network):
        return _strip_scoped_metadata(rows)
    if wallet_ref != signer_wallet_ref:
        return _strip_scoped_metadata(rows)
    scoped: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            scoped.append(row)
            continue
        scoped.append({
            **row,
            "balance_source": "gateway",
            "network": _provider_snapshot_network_scope(network),
            "wallet_ref": wallet_ref,
            **(dict(route_id=route_id) if route_id else {}),
        })
    return scoped


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
        supported_order_types = {
            str(value).upper()
            for value in (cowswap_supported_order_types() or ())
        }
        return None if rule is None else {
            "min_order_size": float(getattr(rule, "min_order_size", 0)),
            "min_notional_size": 0.0,
            "min_price_increment": float(getattr(rule, "min_price_increment", 0)),
            "min_base_amount_increment": float(getattr(rule, "min_base_amount_increment", 0)),
            "supports_limit_orders": "LIMIT" in supported_order_types,
            "supports_market_orders": "MARKET" in supported_order_types,
        }
    connector_market = connector_trading_pair(connector_name, trading_pair)
    try:
        rules = await request.app.state.market_data_service.get_trading_rules(connector_name, [connector_market])
    except Exception:
        return None
    rule = rules.get(connector_market) if isinstance(rules, dict) else None
    if not isinstance(rule, dict) or "error" in rule:
        return None
    return logical_trading_rule(
        connector_name,
        _normalized_provider_trading_rule(connector_name, rule),
    )


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


async def _portfolio_with_cowswap_reference_prices(
    accounts_service: AccountsService,
    *,
    portfolio: dict[str, Any] | None,
    http_request: Request,
    snapshot_request: ProviderSnapshotRequest,
) -> dict[str, Any] | None:
    runtime = getattr(accounts_service, "_cowswap_runtime", None)
    connector_rows = _cowswap_balance_rows(
        accounts_service,
        runtime=runtime,
        trading_pair=snapshot_request.trading_pair,
    )

    base_asset, quote_asset = _split_pair(snapshot_request.trading_pair)
    account_portfolio: dict[str, Any] = dict(portfolio or {})
    account_rows = dict(account_portfolio.get(snapshot_request.account_name) or {})
    from_evm_reader = bool(connector_rows)
    if not connector_rows:
        connector_rows = _strip_scoped_metadata(
            list(account_rows.get(COWSWAP_CONNECTOR_NAME) or []),
        )
    for token, price in _cowswap_reference_prices(
        http_request,
        base_asset=base_asset,
        quote_asset=quote_asset,
    ).items():
        connector_rows = _upsert_price_row(connector_rows, token=token, price=price)
    if from_evm_reader:
        connector_rows = _cowswap_scoped_portfolio_rows(
            accounts_service,
            rows=connector_rows,
            network=snapshot_request.network,
            route_id=snapshot_request.route_id,
            wallet_ref=snapshot_request.wallet_ref,
        )
    account_rows[COWSWAP_CONNECTOR_NAME] = connector_rows
    account_portfolio[snapshot_request.account_name] = account_rows
    return account_portfolio


def _cowswap_reference_prices(
    request: Request,
    *,
    base_asset: str,
    quote_asset: str,
) -> dict[str, Decimal]:
    stable_assets = {"DAI", "USDC", "USDT", "USD"}
    prices = {
        asset: Decimal("1")
        for asset in (base_asset, quote_asset)
        if asset in stable_assets
    }
    try:
        rate = Decimal(
            str(request.app.state.market_data_service.get_rate(base_asset, quote_asset)),
        )
    except Exception:
        return prices
    if not rate.is_finite() or rate <= 0:
        return prices
    if quote_asset in stable_assets:
        prices[base_asset] = rate
    elif base_asset in stable_assets:
        prices[quote_asset] = Decimal("1") / rate
    return prices


def _cowswap_balance_rows(
    accounts_service: AccountsService,
    *,
    runtime: Any,
    trading_pair: str,
) -> list[dict[str, Any]]:
    runtime_dependencies = getattr(accounts_service, "_cowswap_runtime_dependencies", None)
    evm_reader = getattr(runtime_dependencies, "evm_reader", None)
    owner = getattr(runtime_dependencies, "owner_address", "")
    if runtime is None or evm_reader is None or not owner:
        return []
    try:
        tokens = _cowswap_tokens_for_pair(runtime, trading_pair)
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in tokens:
        symbol = str(getattr(token, "symbol", "")).upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        try:
            units = _atomic_amount_to_human(
                evm_reader.balance_of(token, owner),
                int(getattr(token, "decimals")),
            )
        except Exception:
            continue
        rows.append(
            {
                "available_units": float(units),
                "token": symbol,
                "units": float(units),
                "value": 0.0,
            }
        )
    return rows


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
        units = patched.get("available_units", patched.get("units", 0))
        try:
            patched["value"] = float(Decimal(str(units)) * price)
        except Exception:
            patched["value"] = 0.0
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
