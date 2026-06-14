import os

from fastapi import HTTPException


LIVE_ORDER_SUBMISSION_ENV = "TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED"
LIVE_GATEWAY_MUTATIONS_ENV = "TRADING_SAFETY_LIVE_GATEWAY_MUTATIONS_ENABLED"
PAPER_CONNECTOR_SUFFIX = "_paper_trade"
SAFE_GATEWAY_NETWORK_MARKERS = ("testnet", "devnet", "sepolia", "goerli", "amoy", "fuji", "local")


def assert_live_order_submission_allowed(
    *,
    account_name: str,
    connector_name: str,
    source: str,
) -> None:
    """Fail closed before direct live connector order submission."""
    if _is_paper_connector(connector_name):
        return
    if _env_bool(LIVE_ORDER_SUBMISSION_ENV):
        return
    raise HTTPException(
        status_code=503,
        detail=(
            "live order submission disabled; "
            f"set {LIVE_ORDER_SUBMISSION_ENV}=true only behind Marlin live gates "
            f"(source={source}, account={account_name}, connector={connector_name})"
        ),
    )


def assert_live_gateway_mutation_allowed(
    *,
    action: str,
    chain: str,
    network: str,
    source: str,
) -> None:
    """Fail closed before direct live Gateway signing or broadcast."""
    if _is_safe_gateway_network(network):
        return
    action_env = _gateway_action_env(action)
    if _env_bool(action_env):
        return
    raise HTTPException(
        status_code=503,
        detail=(
            f"live Gateway mutation disabled for action {action}; "
            f"set {action_env}=true only behind Marlin live gates "
            f"(source={source}, network={chain}/{network})"
        ),
    )


def _is_paper_connector(connector_name: str) -> bool:
    return connector_name.endswith(PAPER_CONNECTOR_SUFFIX)


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
