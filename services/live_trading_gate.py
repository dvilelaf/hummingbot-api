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
    chain: str,
    network: str,
    source: str,
) -> None:
    """Fail closed before direct live Gateway signing or broadcast."""
    if _is_safe_gateway_network(network):
        return
    if _env_bool(LIVE_GATEWAY_MUTATIONS_ENV):
        return
    raise HTTPException(
        status_code=503,
        detail=(
            "live Gateway mutation disabled; "
            f"set {LIVE_GATEWAY_MUTATIONS_ENV}=true only behind Marlin live gates "
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
