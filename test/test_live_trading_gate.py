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


def test_gateway_mutation_gate_allows_test_networks_by_default(monkeypatch):
    monkeypatch.delenv("TRADING_SAFETY_LIVE_GATEWAY_MUTATIONS_ENABLED", raising=False)
    gate = _live_gate_module()

    gate.assert_live_gateway_mutation_allowed(
        chain="ethereum",
        network="sepolia",
        source="test",
    )
    gate.assert_live_gateway_mutation_allowed(
        chain="solana",
        network="devnet",
        source="test",
    )


def test_gateway_mutation_gate_blocks_mainnet_by_default(monkeypatch):
    monkeypatch.delenv("TRADING_SAFETY_LIVE_GATEWAY_MUTATIONS_ENABLED", raising=False)
    gate = _live_gate_module()

    try:
        gate.assert_live_gateway_mutation_allowed(
            chain="ethereum",
            network="base",
            source="test",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "live Gateway mutation disabled" in str(exc.detail)
        assert "ethereum/base" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_gateway_mutation_gate_allows_mainnet_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_GATEWAY_MUTATIONS_ENABLED", "true")
    gate = _live_gate_module()

    gate.assert_live_gateway_mutation_allowed(
        chain="ethereum",
        network="base",
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


def test_gateway_swap_checks_live_gate_before_execute_swap():
    source = (ROOT / "routers" / "gateway_swap.py").read_text()
    execute_source = source[source.index("async def execute_swap") :]

    assert execute_source.index("assert_live_gateway_mutation_allowed(") < execute_source.index(
        "accounts_service.gateway_client.execute_swap(",
    )


def test_gateway_lp_checks_live_gate_before_add_and_remove():
    source = (ROOT / "routers" / "gateway_lp.py").read_text()
    add_source = source[source.index("async def add_router_liquidity") : source.index("async def remove_router_liquidity")]
    remove_source = source[source.index("async def remove_router_liquidity") :]

    assert add_source.index("assert_live_gateway_mutation_allowed(") < add_source.index(
        "accounts_service.gateway_client.router_add_liquidity(",
    )
    assert remove_source.index("assert_live_gateway_mutation_allowed(") < remove_source.index(
        "accounts_service.gateway_client.router_remove_liquidity(",
    )


def test_gateway_wallet_send_checks_live_gate_before_send_transaction():
    source = (ROOT / "routers" / "gateway.py").read_text()
    send_source = source[source.index("async def send_transaction") : source.index("@router.post(\"/transactions/poll\")")]

    assert send_source.index("assert_live_gateway_mutation_allowed(") < send_source.index(
        "accounts_service.gateway_client.send_transaction(",
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
