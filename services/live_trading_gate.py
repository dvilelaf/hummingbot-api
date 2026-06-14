import os
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException


LIVE_ORDER_SUBMISSION_ENV = "TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED"
LIVE_ORDER_CANCEL_ENV = "TRADING_SAFETY_LIVE_ORDER_CANCEL_ENABLED"
LIVE_GATEWAY_MUTATIONS_ENV = "TRADING_SAFETY_LIVE_GATEWAY_MUTATIONS_ENABLED"
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


def assert_live_order_submission_allowed(
    *,
    account_name: str,
    connector_name: str,
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
            expected_network=network,
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


def _is_safe_connector(connector_name: str) -> bool:
    return connector_name.endswith(SAFE_CONNECTOR_SUFFIXES)


def _is_safe_gateway_network(network: str) -> bool:
    normalized = network.strip().lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in SAFE_GATEWAY_NETWORK_MARKERS)


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


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
    expected_network: str | None = None,
) -> None:
    if authorization is None:
        _raise_authorization_error("live action authorization missing", source=source)
    if authorization.get("version") != LIVE_ACTION_AUTHORIZATION_VERSION:
        _raise_authorization_error("live action authorization version mismatch", source=source)
    if authorization.get("status") != "approved":
        _raise_authorization_error("live action authorization is not approved", source=source)
    if authorization.get("blockers") != []:
        _raise_authorization_error("live action authorization has blockers", source=source)
    if authorization.get("api_live_flag") != expected_api_live_flag:
        _raise_authorization_error("live action authorization API flag mismatch", source=source)
    if authorization.get("action") != expected_action:
        _raise_authorization_error("live action authorization action mismatch", source=source)
    if expected_connector_id is not None and authorization.get("connector_id") != expected_connector_id:
        _raise_authorization_error("live action authorization connector mismatch", source=source)
    if expected_network is not None and authorization.get("network") != expected_network:
        _raise_authorization_error("live action authorization network mismatch", source=source)
    expires_at = _parse_utc_datetime(authorization.get("expires_at_utc"))
    if expires_at <= datetime.now(UTC):
        _raise_authorization_error("live action authorization expired", source=source)


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


def _raise_authorization_error(reason: str, *, source: str) -> None:
    raise HTTPException(
        status_code=503,
        detail=f"{reason}; live execution remains disabled (source={source})",
    )
