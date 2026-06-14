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
        live_action_authorization=_approved_authorization(
            action="order",
            api_live_flag="TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED",
            connector_id="binance",
            network="mainnet",
        ),
        source="test",
    )


def test_live_order_gate_requires_authorization_when_env_enabled(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED", "true")
    gate = _live_gate_module()

    try:
        gate.assert_live_order_submission_allowed(
            account_name="master_account",
            connector_name="binance",
            source="test",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "live action authorization missing" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_gateway_mutation_gate_allows_test_networks_by_default(monkeypatch):
    monkeypatch.delenv("TRADING_SAFETY_LIVE_GATEWAY_MUTATIONS_ENABLED", raising=False)
    gate = _live_gate_module()

    gate.assert_live_gateway_mutation_allowed(
        action="swap_execute",
        chain="ethereum",
        network="sepolia",
        source="test",
    )
    gate.assert_live_gateway_mutation_allowed(
        action="swap_execute",
        chain="solana",
        network="devnet",
        source="test",
    )


def test_gateway_mutation_gate_blocks_mainnet_by_default(monkeypatch):
    monkeypatch.delenv("TRADING_SAFETY_LIVE_GATEWAY_MUTATIONS_ENABLED", raising=False)
    gate = _live_gate_module()

    try:
        gate.assert_live_gateway_mutation_allowed(
            action="swap_execute",
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


def test_gateway_mutation_gate_blocks_mainnet_with_global_flag_only(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_GATEWAY_MUTATIONS_ENABLED", "true")
    monkeypatch.delenv("TRADING_SAFETY_LIVE_GATEWAY_SWAP_EXECUTE_ENABLED", raising=False)
    gate = _live_gate_module()

    try:
        gate.assert_live_gateway_mutation_allowed(
            action="swap_execute",
            chain="ethereum",
            network="base",
            source="test",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "live Gateway mutation disabled for action swap_execute" in str(exc.detail)
        assert "TRADING_SAFETY_LIVE_GATEWAY_SWAP_EXECUTE_ENABLED" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_gateway_mutation_gate_allows_mainnet_when_action_enabled(monkeypatch):
    monkeypatch.delenv("TRADING_SAFETY_LIVE_GATEWAY_MUTATIONS_ENABLED", raising=False)
    monkeypatch.setenv("TRADING_SAFETY_LIVE_GATEWAY_SWAP_EXECUTE_ENABLED", "true")
    gate = _live_gate_module()

    gate.assert_live_gateway_mutation_allowed(
        action="swap_execute",
        chain="ethereum",
        live_action_authorization=_approved_authorization(
            action="gateway_swap",
            api_live_flag="TRADING_SAFETY_LIVE_GATEWAY_SWAP_EXECUTE_ENABLED",
            connector_id="gateway",
            network="base",
        ),
        network="base",
        source="test",
    )


def test_gateway_mutation_gate_requires_authorization_when_action_enabled(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_GATEWAY_SWAP_EXECUTE_ENABLED", "true")
    gate = _live_gate_module()

    try:
        gate.assert_live_gateway_mutation_allowed(
            action="swap_execute",
            chain="ethereum",
            network="base",
            source="test",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "live action authorization missing" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_gateway_mutation_gate_rejects_wrong_authorization_action(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_GATEWAY_SWAP_EXECUTE_ENABLED", "true")
    gate = _live_gate_module()

    try:
        gate.assert_live_gateway_mutation_allowed(
            action="swap_execute",
            chain="ethereum",
            live_action_authorization=_approved_authorization(
                action="wallet_send",
                api_live_flag="TRADING_SAFETY_LIVE_GATEWAY_SWAP_EXECUTE_ENABLED",
                connector_id="gateway",
                network="base",
            ),
            network="base",
            source="test",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "action mismatch" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_gateway_mutation_gate_rejects_expired_authorization(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_GATEWAY_SWAP_EXECUTE_ENABLED", "true")
    gate = _live_gate_module()

    try:
        gate.assert_live_gateway_mutation_allowed(
            action="swap_execute",
            chain="ethereum",
            live_action_authorization=_approved_authorization(
                action="gateway_swap",
                api_live_flag="TRADING_SAFETY_LIVE_GATEWAY_SWAP_EXECUTE_ENABLED",
                connector_id="gateway",
                expires_at_utc="2020-01-01T00:00:00Z",
                network="base",
            ),
            network="base",
            source="test",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "expired" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


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

    assert 'action="swap_execute"' in execute_source
    assert execute_source.index("assert_live_gateway_mutation_allowed(") < execute_source.index(
        "accounts_service.gateway_client.execute_swap(",
    )


def test_gateway_lp_checks_live_gate_before_add_and_remove():
    source = (ROOT / "routers" / "gateway_lp.py").read_text()
    add_source = source[source.index("async def add_router_liquidity") : source.index("async def remove_router_liquidity")]
    remove_source = source[source.index("async def remove_router_liquidity") :]

    assert 'action="lp_add"' in add_source
    assert 'action="lp_remove"' in remove_source
    assert add_source.index("assert_live_gateway_mutation_allowed(") < add_source.index(
        "accounts_service.gateway_client.router_add_liquidity(",
    )
    assert remove_source.index("assert_live_gateway_mutation_allowed(") < remove_source.index(
        "accounts_service.gateway_client.router_remove_liquidity(",
    )


def test_gateway_wallet_send_checks_live_gate_before_send_transaction():
    source = (ROOT / "routers" / "gateway.py").read_text()
    send_source = source[source.index("async def send_transaction") : source.index("@router.post(\"/transactions/poll\")")]

    assert 'action="wallet_send"' in send_source
    assert send_source.index("assert_live_gateway_mutation_allowed(") < send_source.index(
        "accounts_service.gateway_client.send_transaction(",
    )


def test_gateway_clmm_checks_live_gate_before_mutations():
    source = (ROOT / "routers" / "gateway_clmm.py").read_text()
    mutation_calls = (
        (
            "async def open_clmm_position",
            "async def add_liquidity_to_clmm_position",
            "accounts_service.gateway_client.clmm_open_position(",
        ),
        (
            "async def add_liquidity_to_clmm_position",
            "async def remove_liquidity_from_clmm_position",
            "accounts_service.gateway_client.clmm_add_liquidity(",
        ),
        (
            "async def remove_liquidity_from_clmm_position",
            "async def close_clmm_position",
            "accounts_service.gateway_client.clmm_remove_liquidity(",
        ),
        (
            "async def close_clmm_position",
            "async def collect_fees_from_clmm_position",
            "accounts_service.gateway_client.clmm_close_position(",
        ),
        (
            "async def collect_fees_from_clmm_position",
            None,
            "accounts_service.gateway_client.clmm_collect_fees(",
        ),
    )

    expected_actions = {
        "accounts_service.gateway_client.clmm_open_position(": 'action="clmm_open_position"',
        "accounts_service.gateway_client.clmm_add_liquidity(": 'action="clmm_add_liquidity"',
        "accounts_service.gateway_client.clmm_remove_liquidity(": 'action="clmm_remove_liquidity"',
        "accounts_service.gateway_client.clmm_close_position(": 'action="clmm_close_position"',
        "accounts_service.gateway_client.clmm_collect_fees(": 'action="clmm_collect_fees"',
    }

    for start_marker, end_marker, mutation_call in mutation_calls:
        start = source.index(start_marker)
        end = source.index(end_marker) if end_marker is not None else len(source)
        mutation_source = source[start:end]

        assert expected_actions[mutation_call] in mutation_source
        assert mutation_source.index("assert_live_gateway_mutation_allowed(") < mutation_source.index(
            mutation_call,
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


def _approved_authorization(
    *,
    action,
    api_live_flag,
    connector_id,
    network,
    expires_at_utc="2099-01-01T00:00:00Z",
):
    return {
        "action": action,
        "api_live_flag": api_live_flag,
        "blockers": [],
        "connector_id": connector_id,
        "edge_sha256": "e" * 64,
        "expires_at_utc": expires_at_utc,
        "gas": "0.001",
        "gateway_live_flags": [],
        "generated_at_utc": "2026-06-14T10:00:00Z",
        "live_gate_sha256": "g" * 64,
        "network": network,
        "notional": "1",
        "slippage_bps": "25",
        "status": "approved",
        "version": "live-action-authorization-v1",
    }
