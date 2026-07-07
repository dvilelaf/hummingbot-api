import os
from typing import Any

from fastapi import HTTPException


LIVE_ORDER_SUBMISSION_ENV = "TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED"
LIVE_ORDER_CANCEL_ENV = "TRADING_SAFETY_LIVE_ORDER_CANCEL_ENABLED"
LIVE_GATEWAY_MUTATIONS_ENV = "TRADING_SAFETY_LIVE_GATEWAY_MUTATIONS_ENABLED"
LIVE_GATEWAY_BRIDGE_EXECUTE_ENV = "TRADING_SAFETY_LIVE_GATEWAY_BRIDGE_EXECUTE_ENABLED"
MARLIN_RUNTIME_PROFILE_ENV = "MARLIN_RUNTIME_PROFILE"
BRIDGE_PROVIDER_ALLOWLIST_ENV = "TRADING_SAFETY_BRIDGE_PROVIDER_ALLOWLIST"
SAFE_CONNECTOR_SUFFIXES = ("_paper_trade", "_testnet", "_sandbox")
SAFE_GATEWAY_NETWORK_MARKERS = ("testnet", "devnet", "sepolia", "goerli", "amoy", "fuji", "local")
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
    if _is_marlin_runtime_profile():
        raise HTTPException(
            status_code=503,
            detail=(
                "direct live order submission disabled in Marlin runtime; "
                "submit mainnet Marlin orders through /provider/intents with "
                "MARLIN_PROVIDER_INTENT_TOKEN "
                f"(source={source}, account={account_name}, connector={connector_name})"
            ),
        )
    if _env_bool(LIVE_ORDER_SUBMISSION_ENV):
        return
    raise HTTPException(
        status_code=503,
        detail=(
            "live order submission disabled; "
            f"set {LIVE_ORDER_SUBMISSION_ENV}=true only for the Marlin runtime "
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
    if _is_marlin_runtime_profile():
        raise HTTPException(
            status_code=503,
            detail=(
                "direct live order cancellation disabled in Marlin runtime; "
                "submit mainnet Marlin cancellations through /provider/intents with "
                "MARLIN_PROVIDER_INTENT_TOKEN "
                f"(source={source}, account={account_name}, connector={connector_name})"
            ),
        )
    if _env_bool(LIVE_ORDER_CANCEL_ENV):
        return
    raise HTTPException(
        status_code=503,
        detail=(
            "live order cancellation disabled; "
            f"set {LIVE_ORDER_CANCEL_ENV}=true only for the Marlin runtime "
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
    marlin_provider_intent_authorized: bool = False,
    network: str,
    provider_intent_payload: dict[str, Any] | None = None,
    provider_intent_signature: str | None = None,
    source: str,
) -> None:
    """Fail closed before direct live Gateway signing or broadcast."""
    if _is_safe_gateway_network(network):
        return
    if marlin_provider_intent_authorized:
        return
    if _is_marlin_runtime_profile():
        raise HTTPException(
            status_code=503,
            detail=(
                f"direct live Gateway mutation disabled for action {action} in Marlin runtime; "
                "submit mainnet Marlin Gateway mutations through /provider/intents with "
                "MARLIN_PROVIDER_INTENT_TOKEN "
                f"(source={source}, network={chain}/{network})"
            ),
        )
    action_env = _gateway_action_env(action)
    if _env_bool(action_env):
        return
    raise HTTPException(
        status_code=503,
        detail=(
            f"live Gateway mutation disabled for action {action}; "
            f"set {action_env}=true only for the Marlin runtime "
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
                f"set {LIVE_GATEWAY_BRIDGE_EXECUTE_ENV}=true only for the Marlin runtime "
                f"(source={source})"
            ),
        )
    _assert_bridge_provider_allowed(expected_provider, source=source)
    _assert_bridge_authorization_matches(
        live_action_authorization,
        {
            "bridge_authorization_nonce": expected_authorization_nonce,
            "bridge_provider": expected_provider,
            "bridge_provider_route_id": expected_provider_route_id,
            "bridge_quote_id": expected_quote_id,
            "bridge_route_payload_hash": expected_route_payload_hash,
            "bridge_source_chain_id": expected_source_chain_id,
            "bridge_tx_calldata_hash": expected_calldata_hash,
            "bridge_tx_target": expected_target,
            "bridge_tx_value": expected_value,
        },
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


def _is_marlin_runtime_profile() -> bool:
    return os.getenv(MARLIN_RUNTIME_PROFILE_ENV, "").strip().lower() == "marlin"


def _assert_bridge_provider_allowed(provider: Any, *, source: str) -> None:
    allowed = {
        item.strip()
        for item in os.getenv(BRIDGE_PROVIDER_ALLOWLIST_ENV, "").split(",")
        if item.strip()
    }
    if str(provider).strip() not in allowed:
        _raise_authorization_error("bridge provider not allowlisted", source=source)


def _assert_bridge_authorization_matches(
    authorization: dict[str, Any] | None,
    expectations: dict[str, Any],
    *,
    source: str,
) -> None:
    if not isinstance(authorization, dict):
        _raise_authorization_error("bridge authorization missing", source=source)
    for key, expected in expectations.items():
        if not _authorization_value_matches(expected, authorization.get(key)):
            _raise_authorization_error(f"bridge authorization {key} mismatch", source=source)


def _authorization_value_matches(expected: Any, provided: Any) -> bool:
    if expected is None or str(expected).strip() == "":
        return True
    if provided is None or str(provided).strip() == "":
        return False
    return str(provided).strip() == str(expected).strip()


def _gateway_action_env(action: str) -> str:
    normalized = "".join(
        character if character.isalnum() else "_"
        for character in action.strip().upper()
    ).strip("_")
    return f"TRADING_SAFETY_LIVE_GATEWAY_{normalized}_ENABLED"


def _raise_authorization_error(reason: str, *, source: str) -> None:
    raise HTTPException(
        status_code=503,
        detail=f"{reason}; live execution remains disabled (source={source})",
    )
