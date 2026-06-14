import os

from fastapi import HTTPException


LIVE_ORDER_SUBMISSION_ENV = "TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED"
PAPER_CONNECTOR_SUFFIX = "_paper_trade"


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


def _is_paper_connector(connector_name: str) -> bool:
    return connector_name.endswith(PAPER_CONNECTOR_SUFFIX)


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}
