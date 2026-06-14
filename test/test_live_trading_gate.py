import importlib.util
from pathlib import Path

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
LIVE_GATE_PATH = ROOT / "services" / "live_trading_gate.py"


def _live_gate_module():
    spec = importlib.util.spec_from_file_location(
        "live_trading_gate_under_test",
        LIVE_GATE_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("expected live trading gate module spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_order_gate_allows_paper_connector_by_default(monkeypatch):
    monkeypatch.delenv("TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED", raising=False)
    gate = _live_gate_module()

    gate.assert_live_order_submission_allowed(
        account_name="master_account",
        connector_name="binance_paper_trade",
        source="test",
    )


def test_live_order_gate_blocks_non_paper_connector_by_default(monkeypatch):
    monkeypatch.delenv("TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED", raising=False)
    gate = _live_gate_module()

    try:
        gate.assert_live_order_submission_allowed(
            account_name="master_account",
            connector_name="binance",
            source="test",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "live order submission disabled" in str(exc.detail)
        assert "binance" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_live_order_gate_allows_non_paper_connector_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED", "true")
    gate = _live_gate_module()

    gate.assert_live_order_submission_allowed(
        account_name="master_account",
        connector_name="binance",
        source="test",
    )


def test_accounts_service_checks_live_gate_before_connector_order_submission():
    source = (ROOT / "services" / "accounts_service.py").read_text()
    place_trade = source[source.index("async def place_trade") :]
    gate_index = place_trade.index("assert_live_order_submission_allowed(")
    buy_index = place_trade.index("connector.buy(")
    sell_index = place_trade.index("connector.sell(")

    assert gate_index < buy_index
    assert gate_index < sell_index


def test_trading_service_checks_live_gate_before_executor_order_submission():
    source = (ROOT / "services" / "trading_service.py").read_text()
    buy_source = source[source.index("    def buy(") : source.index("    def sell(")]
    sell_source = source[source.index("    def sell(") : source.index("    def cancel(")]

    assert buy_source.index("assert_live_order_submission_allowed(") < buy_source.index(
        "connector.buy(",
    )
    assert sell_source.index("assert_live_order_submission_allowed(") < sell_source.index(
        "connector.sell(",
    )


def test_accounts_service_checks_live_gate_before_cancel_order_submission():
    source = (ROOT / "services" / "accounts_service.py").read_text()
    cancel_source = source[source.index("async def cancel_order") : source.index("async def set_leverage")]

    assert cancel_source.index("assert_live_order_submission_allowed(") < cancel_source.index(
        "connector.cancel(",
    )


def test_accounts_trading_interface_checks_live_gate_before_cancel_submission():
    source = (ROOT / "services" / "accounts_service.py").read_text()
    interface_source = source[source.index("class AccountTradingInterface") : source.index("class AccountsService")]
    cancel_source = interface_source[interface_source.index("    def cancel(") : interface_source.index("    def get_active_orders(")]

    assert cancel_source.index("assert_live_order_submission_allowed(") < cancel_source.index(
        "connector.cancel(",
    )


def test_trading_service_checks_live_gate_before_executor_cancel_submission():
    source = (ROOT / "services" / "trading_service.py").read_text()
    cancel_source = source[source.index("    def cancel(") : source.index("    def get_active_orders(")]

    assert cancel_source.index("assert_live_order_submission_allowed(") < cancel_source.index(
        "connector.cancel(",
    )
