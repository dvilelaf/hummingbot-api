import os
import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException


LIVE_ORDER_SUBMISSION_ENV = "TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED"
LIVE_ORDER_CANCEL_ENV = "TRADING_SAFETY_LIVE_ORDER_CANCEL_ENABLED"
LIVE_GATEWAY_MUTATIONS_ENV = "TRADING_SAFETY_LIVE_GATEWAY_MUTATIONS_ENABLED"
LIVE_GATEWAY_BRIDGE_EXECUTE_ENV = "TRADING_SAFETY_LIVE_GATEWAY_BRIDGE_EXECUTE_ENABLED"
BRIDGE_PROVIDER_ALLOWLIST_ENV = "TRADING_SAFETY_BRIDGE_PROVIDER_ALLOWLIST"
LIVE_ACTION_AUTH_SECRET_ENV = "MARLIN_LIVE_ACTION_AUTH_SECRET"
LIVE_ACTION_AUTHORIZATION_VERSION = "live-action-authorization-v1"
SAFE_CONNECTOR_SUFFIXES = ("_paper_trade", "_testnet", "_sandbox")
SAFE_GATEWAY_NETWORK_MARKERS = ("testnet", "devnet", "sepolia", "goerli", "amoy", "fuji", "local")
GATEWAY_ACTION_AUTHORIZATION_ACTIONS = {
    "swap_execute": "gateway_swap",
    "lp_add": "lp_add",
    "lp_remove": "lp_remove",
    "wallet_send": "wallet_send",
    "clmm_open_position": "lp_add",
    "clmm_add_liquidity": "lp_add",
    "clmm_remove_liquidity": "lp_remove",
    "clmm_close_position": "lp_remove",
    "clmm_collect_fees": "lp_remove",
}
BRIDGE_GATEWAY_FLAGS = (
    "GATEWAY_LIVE_BRIDGE_EXECUTE_ENABLED",
    "GATEWAY_LIVE_ETHEREUM_TRANSACTION_ENABLED",
    "GATEWAY_LIVE_SOLANA_RAW_TRANSACTION_ENABLED",
    "GATEWAY_LIVE_SOLANA_TRANSACTION_ENABLED",
)
_used_bridge_authorization_nonces: set[str] = set()


def assert_live_order_submission_allowed(
    *,
    account_name: str,
    connector_name: str,
    expected_instrument: Any | None = None,
    expected_notional: Any | None = None,
    live_action_authorization: dict[str, Any] | None = None,
    source: str,
) -> None:
    """Fail closed before direct live connector order submission."""
    if _is_safe_connector(connector_name):
        return
    if _env_bool(LIVE_ORDER_SUBMISSION_ENV):
        _assert_live_action_authorization(
            live_action_authorization,
            expected_action="order",
            expected_api_live_flag=LIVE_ORDER_SUBMISSION_ENV,
            expected_connector_id=connector_name,
            expected_instrument=expected_instrument,
            expected_notional=expected_notional,
            source=source,
        )
        return
    raise HTTPException(
        status_code=503,
        detail=(
            "live order submission disabled; "
            f"set {LIVE_ORDER_SUBMISSION_ENV}=true only behind Marlin live gates "
            f"(source={source}, account={account_name}, connector={connector_name})"
        ),
    )


def assert_live_order_cancel_allowed(
    *,
    account_name: str,
    connector_name: str,
    live_action_authorization: dict[str, Any] | None = None,
    source: str,
) -> None:
    """Fail closed before direct live connector order cancellation."""
    if _is_safe_connector(connector_name):
        return
    if _env_bool(LIVE_ORDER_CANCEL_ENV):
        _assert_live_action_authorization(
            live_action_authorization,
            expected_action="order_cancel",
            expected_api_live_flag=LIVE_ORDER_CANCEL_ENV,
            expected_connector_id=connector_name,
            source=source,
        )
        return
    raise HTTPException(
        status_code=503,
        detail=(
            "live order cancellation disabled; "
            f"set {LIVE_ORDER_CANCEL_ENV}=true only behind Marlin live gates "
            f"(source={source}, account={account_name}, connector={connector_name})"
        ),
    )


def assert_live_gateway_mutation_allowed(
    *,
    action: str,
    chain: str,
    expected_connector_id: Any | None = None,
    expected_gas: Any | None = None,
    expected_instrument: Any | None = None,
    expected_notional: Any | None = None,
    expected_slippage_bps: Any | None = None,
    live_action_authorization: dict[str, Any] | None = None,
    network: str,
    source: str,
) -> None:
    """Fail closed before direct live Gateway signing or broadcast."""
    if _is_safe_gateway_network(network):
        return
    action_env = _gateway_action_env(action)
    if _env_bool(action_env):
        _assert_live_action_authorization(
            live_action_authorization,
            expected_action=_gateway_authorization_action(action),
            expected_api_live_flag=action_env,
            expected_connector_id=expected_connector_id,
            expected_gas=expected_gas,
            expected_instrument=expected_instrument,
            expected_network=network,
            expected_notional=expected_notional,
            expected_slippage_bps=expected_slippage_bps,
            source=source,
        )
        return
    raise HTTPException(
        status_code=503,
        detail=(
            f"live Gateway mutation disabled for action {action}; "
            f"set {action_env}=true only behind Marlin live gates "
            f"(source={source}, network={chain}/{network})"
        ),
    )


def assert_live_bridge_execution_allowed(
    *,
    expected_authorization_nonce: Any,
    expected_calldata_hash: Any,
    expected_provider: Any,
    expected_provider_route_id: Any,
    expected_quote_id: Any,
    expected_route_payload_hash: Any,
    expected_source_chain_id: Any,
    expected_target: Any,
    expected_value: Any,
    live_action_authorization: dict[str, Any] | None,
    source: str,
) -> None:
    """Fail closed before forwarding a Marlin-approved bridge execution to Gateway."""
    if not _env_bool(LIVE_GATEWAY_BRIDGE_EXECUTE_ENV):
        raise HTTPException(
            status_code=503,
            detail=(
                "live Gateway bridge execution disabled; "
                f"set {LIVE_GATEWAY_BRIDGE_EXECUTE_ENV}=true only behind Marlin live gates "
                f"(source={source})"
            ),
        )
    _assert_bridge_provider_allowed(expected_provider, source=source)
    _assert_live_action_authorization(
        live_action_authorization,
        expected_action="bridge",
        expected_api_live_flag=LIVE_GATEWAY_BRIDGE_EXECUTE_ENV,
        source=source,
    )
    _assert_bridge_gateway_flags(live_action_authorization, source=source)
    _assert_bridge_field_matches(
        live_action_authorization,
        "bridge_authorization_nonce",
        expected_authorization_nonce,
        "bridge authorization_nonce",
        source=source,
    )
    _assert_bridge_field_matches(
        live_action_authorization,
        "bridge_provider",
        expected_provider,
        "bridge provider",
        source=source,
    )
    _assert_bridge_field_matches(
        live_action_authorization,
        "bridge_provider_route_id",
        expected_provider_route_id,
        "bridge provider_route_id",
        source=source,
    )
    _assert_bridge_field_matches(
        live_action_authorization,
        "bridge_quote_id",
        expected_quote_id,
        "bridge quote_id",
        source=source,
    )
    _assert_bridge_field_matches(
        live_action_authorization,
        "bridge_route_payload_hash",
        expected_route_payload_hash,
        "bridge route_payload_hash",
        source=source,
    )
    _assert_bridge_field_matches(
        live_action_authorization,
        "bridge_source_chain_id",
        expected_source_chain_id,
        "bridge source_chain_id",
        source=source,
    )
    _assert_bridge_field_matches(
        live_action_authorization,
        "bridge_tx_calldata_hash",
        expected_calldata_hash,
        "bridge tx_calldata_hash",
        source=source,
    )
    _assert_bridge_field_matches(
        live_action_authorization,
        "bridge_tx_target",
        expected_target,
        "bridge tx_target",
        source=source,
    )
    _assert_authorization_decimal_matches(
        live_action_authorization or {},
        "bridge_tx_value",
        expected_value,
        source=source,
    )
    nonce = str(expected_authorization_nonce).strip()
    if nonce in _used_bridge_authorization_nonces:
        _raise_authorization_error("bridge authorization nonce replay", source=source)
    _used_bridge_authorization_nonces.add(nonce)


def _is_safe_connector(connector_name: str) -> bool:
    return connector_name.endswith(SAFE_CONNECTOR_SUFFIXES)


def _is_safe_gateway_network(network: str) -> bool:
    normalized = network.strip().lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in SAFE_GATEWAY_NETWORK_MARKERS)


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _assert_bridge_provider_allowed(provider: Any, *, source: str) -> None:
    allowed = {
        item.strip()
        for item in os.getenv(BRIDGE_PROVIDER_ALLOWLIST_ENV, "").split(",")
        if item.strip()
    }
    if str(provider).strip() not in allowed:
        _raise_authorization_error("bridge provider not allowlisted", source=source)


def _assert_bridge_gateway_flags(authorization: dict[str, Any] | None, *, source: str) -> None:
    flags = (authorization or {}).get("gateway_live_flags")
    if not isinstance(flags, list) or set(map(str, flags)) != set(BRIDGE_GATEWAY_FLAGS):
        _raise_authorization_error("live action authorization Gateway flag mismatch", source=source)


def _assert_bridge_field_matches(
    authorization: dict[str, Any] | None,
    field: str,
    expected: Any,
    label: str,
    *,
    source: str,
) -> None:
    if str((authorization or {}).get(field, "")).strip() != str(expected).strip():
        _raise_authorization_error(f"{label} mismatch", source=source)


def _gateway_action_env(action: str) -> str:
    normalized = "".join(
        character if character.isalnum() else "_"
        for character in action.strip().upper()
    ).strip("_")
    return f"TRADING_SAFETY_LIVE_GATEWAY_{normalized}_ENABLED"


def _gateway_authorization_action(action: str) -> str:
    return GATEWAY_ACTION_AUTHORIZATION_ACTIONS.get(action, action)


def _assert_live_action_authorization(
    authorization: dict[str, Any] | None,
    *,
    expected_action: str,
    expected_api_live_flag: str,
    source: str,
    expected_connector_id: str | None = None,
    expected_gas: Any | None = None,
    expected_instrument: Any | None = None,
    expected_network: str | None = None,
    expected_notional: Any | None = None,
    expected_slippage_bps: Any | None = None,
) -> None:
    if authorization is None:
        _raise_authorization_error("live action authorization missing", source=source)
    if authorization.get("version") != LIVE_ACTION_AUTHORIZATION_VERSION:
        _raise_authorization_error("live action authorization version mismatch", source=source)
    if authorization.get("status") != "approved":
        _raise_authorization_error("live action authorization is not approved", source=source)
    if authorization.get("blockers") != []:
        _raise_authorization_error("live action authorization has blockers", source=source)
    _assert_authorization_signature(authorization, source=source)
    if authorization.get("api_live_flag") != expected_api_live_flag:
        _raise_authorization_error("live action authorization API flag mismatch", source=source)
    if authorization.get("action") != expected_action:
        _raise_authorization_error("live action authorization action mismatch", source=source)
    if expected_connector_id is not None and authorization.get("connector_id") != expected_connector_id:
        _raise_authorization_error("live action authorization connector mismatch", source=source)
    if expected_network is not None and authorization.get("network") != expected_network:
        _raise_authorization_error("live action authorization network mismatch", source=source)
    _assert_authorization_string_matches(
        authorization,
        "instrument",
        expected_instrument,
        source=source,
    )
    _assert_authorization_decimal_matches(
        authorization,
        "notional",
        expected_notional,
        source=source,
    )
    _assert_authorization_decimal_matches(
        authorization,
        "gas",
        expected_gas,
        source=source,
    )
    _assert_authorization_decimal_matches(
        authorization,
        "slippage_bps",
        expected_slippage_bps,
        source=source,
    )
    expires_at = _parse_utc_datetime(authorization.get("expires_at_utc"))
    if expires_at <= datetime.now(UTC):
        _raise_authorization_error("live action authorization expired", source=source)


def _assert_authorization_signature(authorization: dict[str, Any], *, source: str) -> None:
    secret = os.getenv(LIVE_ACTION_AUTH_SECRET_ENV, "").strip()
    if not secret:
        _raise_authorization_error("live action authorization secret missing", source=source)
    signature = authorization.get("signature")
    if not isinstance(signature, str) or not signature.strip():
        _raise_authorization_error("live action authorization signature missing", source=source)
    payload = dict(authorization)
    payload.pop("signature", None)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        _raise_authorization_error("live action authorization signature mismatch", source=source)


def _parse_utc_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _raise_authorization_error("live action authorization expiry missing", source="live_trading_gate")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        _raise_authorization_error("live action authorization expiry invalid", source="live_trading_gate")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _assert_authorization_string_matches(
    authorization: dict[str, Any],
    field: str,
    expected: Any | None,
    *,
    source: str,
) -> None:
    if expected is None:
        return
    if str(authorization.get(field, "")).strip() != str(expected).strip():
        _raise_authorization_error(f"live action authorization {field} mismatch", source=source)


def _assert_authorization_decimal_matches(
    authorization: dict[str, Any],
    field: str,
    expected: Any | None,
    *,
    source: str,
) -> None:
    if expected is None:
        return
    actual = authorization.get(field)
    try:
        actual_decimal = Decimal(str(actual))
        expected_decimal = Decimal(str(expected))
    except (InvalidOperation, ValueError):
        _raise_authorization_error(f"live action authorization {field} mismatch", source=source)
    if actual_decimal != expected_decimal:
        _raise_authorization_error(f"live action authorization {field} mismatch", source=source)


def _raise_authorization_error(reason: str, *, source: str) -> None:
    raise HTTPException(
        status_code=503,
        detail=f"{reason}; live execution remains disabled (source={source})",
    )
