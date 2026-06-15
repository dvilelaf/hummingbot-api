import hashlib
import hmac
import importlib.util
import json
from pathlib import Path

import pytest
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


def test_live_order_gate_allows_testnet_and_sandbox_connectors_by_default(monkeypatch):
    monkeypatch.delenv("TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED", raising=False)
    gate = _live_gate_module()

    for connector_name in ("binance_perpetual_testnet", "coinbase_sandbox"):
        gate.assert_live_order_submission_allowed(
            account_name="master_account",
            connector_name=connector_name,
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
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")
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
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")
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


def test_live_order_gate_rejects_unsigned_authorization_when_enabled(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED", "true")
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")
    gate = _live_gate_module()
    authorization = _approved_authorization(
        action="order",
        api_live_flag="TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED",
        connector_id="binance",
        network="mainnet",
    )
    authorization.pop("signature")

    try:
        gate.assert_live_order_submission_allowed(
            account_name="master_account",
            connector_name="binance",
            live_action_authorization=authorization,
            source="test",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "signature missing" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_live_order_gate_rejects_tampered_authorization(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED", "true")
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")
    gate = _live_gate_module()
    authorization = _approved_authorization(
        action="order",
        api_live_flag="TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED",
        connector_id="binance",
        network="mainnet",
    )
    authorization["notional"] = "1000"

    try:
        gate.assert_live_order_submission_allowed(
            account_name="master_account",
            connector_name="binance",
            live_action_authorization=authorization,
            source="test",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "signature mismatch" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_live_order_gate_rejects_authorization_notional_mismatch(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED", "true")
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")
    gate = _live_gate_module()

    try:
        gate.assert_live_order_submission_allowed(
            account_name="master_account",
            connector_name="binance",
            expected_instrument="BTC-USDT",
            expected_notional="2",
            live_action_authorization=_approved_authorization(
                action="order",
                api_live_flag="TRADING_SAFETY_LIVE_ORDER_SUBMISSION_ENABLED",
                connector_id="binance",
                instrument="BTC-USDT",
                network="mainnet",
                notional="1",
            ),
            source="test",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "notional mismatch" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_live_order_cancel_gate_uses_separate_action_and_flag(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_ORDER_CANCEL_ENABLED", "true")
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")
    gate = _live_gate_module()

    gate.assert_live_order_cancel_allowed(
        account_name="master_account",
        connector_name="binance",
        live_action_authorization=_approved_authorization(
            action="order_cancel",
            api_live_flag="TRADING_SAFETY_LIVE_ORDER_CANCEL_ENABLED",
            connector_id="binance",
            network="mainnet",
        ),
        source="test",
    )


def test_live_order_cancel_gate_rejects_submit_authorization(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_ORDER_CANCEL_ENABLED", "true")
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")
    gate = _live_gate_module()

    try:
        gate.assert_live_order_cancel_allowed(
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
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "API flag mismatch" in str(exc.detail)
    else:
        raise AssertionError("expected HTTPException")


def test_live_order_cancel_gate_blocks_non_paper_connector_by_default(monkeypatch):
    monkeypatch.delenv("TRADING_SAFETY_LIVE_ORDER_CANCEL_ENABLED", raising=False)
    gate = _live_gate_module()

    try:
        gate.assert_live_order_cancel_allowed(
            account_name="master_account",
            connector_name="binance",
            source="test",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "live order cancellation disabled" in str(exc.detail)
        assert "TRADING_SAFETY_LIVE_ORDER_CANCEL_ENABLED" in str(exc.detail)
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
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")
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
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")
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
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")
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
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")
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


def test_gateway_mutation_gate_rejects_connector_and_slippage_mismatch(monkeypatch):
    monkeypatch.setenv("TRADING_SAFETY_LIVE_GATEWAY_SWAP_EXECUTE_ENABLED", "true")
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")
    gate = _live_gate_module()

    try:
        gate.assert_live_gateway_mutation_allowed(
            action="swap_execute",
            chain="ethereum",
            expected_connector_id="aerodrome",
            expected_instrument="WETH-USDC",
            expected_slippage_bps="50",
            live_action_authorization=_approved_authorization(
                action="gateway_swap",
                api_live_flag="TRADING_SAFETY_LIVE_GATEWAY_SWAP_EXECUTE_ENABLED",
                connector_id="jupiter",
                instrument="WETH-USDC",
                network="base",
                slippage_bps="25",
            ),
            network="base",
            source="test",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "connector mismatch" in str(exc.detail)
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
    assert "live_action_authorization=live_action_authorization" in place_trade[:buy_index]
    assert "expected_instrument=trading_pair" in place_trade[:buy_index]
    assert "expected_notional=notional_size" in place_trade[:buy_index]


def test_trading_route_passes_authorization_to_place_trade():
    source = (ROOT / "routers" / "trading.py").read_text()
    place_route = source[source.index("async def place_trade") : source.index("@router.post(\"/{account_name}")]

    assert "live_action_authorization=trade_request.live_action_authorization" in place_route


def test_trading_route_passes_authorization_to_cancel_order():
    source = (ROOT / "routers" / "trading.py").read_text()
    cancel_route = source[source.index("async def cancel_order") : source.index("@router.post(\"/{account_name}/{connector_name}/orders/{client_order_id}/poll\")")]

    assert "cancel_request: CancelOrderRequest" in cancel_route
    assert "live_action_authorization = (" in cancel_route
    assert "cancel_request.live_action_authorization" in cancel_route
    assert "live_action_authorization=live_action_authorization" in cancel_route


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

    assert cancel_source.index("assert_live_order_cancel_allowed(") < cancel_source.index(
        "connector.cancel(",
    )
    assert "live_action_authorization=live_action_authorization" in cancel_source


def test_gateway_swap_checks_live_gate_before_execute_swap():
    source = (ROOT / "routers" / "gateway_swap.py").read_text()
    execute_source = source[source.index("async def execute_swap") :]

    assert 'action="swap_execute"' in execute_source
    assert "expected_connector_id=request.connector" in execute_source
    assert "expected_instrument=request.trading_pair" in execute_source
    assert "expected_notional=request.amount" in execute_source
    assert "expected_slippage_bps=slippage_bps" in execute_source
    assert "live_action_authorization=request.live_action_authorization" in execute_source
    assert execute_source.index("assert_live_gateway_mutation_allowed(") < execute_source.index(
        "accounts_service.gateway_client.execute_swap(",
    )
    assert execute_source.index("live_action_authorization=request.live_action_authorization") < execute_source.index(
        "accounts_service.gateway_client.execute_swap(",
    )


def test_gateway_lp_checks_live_gate_before_add_and_remove():
    source = (ROOT / "routers" / "gateway_lp.py").read_text()
    add_source = source[source.index("async def add_router_liquidity") : source.index("async def remove_router_liquidity")]
    remove_source = source[source.index("async def remove_router_liquidity") :]

    assert 'action="lp_add"' in add_source
    assert 'action="lp_remove"' in remove_source
    assert "expected_connector_id=request.connector" in add_source
    assert "expected_connector_id=request.connector" in remove_source
    assert "expected_instrument=f\"{request.token_a}-{request.token_b}\"" in add_source
    assert "expected_instrument=f\"{request.token_a}-{request.token_b}\"" in remove_source
    assert "expected_slippage_bps=slippage_bps" in add_source
    assert "expected_slippage_bps=slippage_bps" in remove_source
    assert "live_action_authorization=request.live_action_authorization" in add_source
    assert "live_action_authorization=request.live_action_authorization" in remove_source
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
    assert "expected_notional=request.amount" in send_source
    assert "live_action_authorization=request.live_action_authorization" in send_source
    assert send_source.index("assert_live_gateway_mutation_allowed(") < send_source.index(
        "accounts_service.gateway_client.send_transaction(",
    )


def test_bridge_execution_gate_rejects_provider_route_mismatch(monkeypatch):
    gate = _live_gate_module()
    monkeypatch.setenv("TRADING_SAFETY_LIVE_GATEWAY_BRIDGE_EXECUTE_ENABLED", "true")
    monkeypatch.setenv("TRADING_SAFETY_BRIDGE_PROVIDER_ALLOWLIST", "lifi,across")
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")

    with pytest.raises(HTTPException) as exc:
        gate.assert_live_bridge_execution_allowed(
            expected_authorization_nonce="bridge-auth-001",
            expected_calldata_hash="sha256:calldata",
            expected_provider="lifi",
            expected_provider_route_id="route-tampered",
            expected_quote_id="quote-123",
            expected_route_payload_hash="sha256:route",
            expected_source_chain_id="1",
            expected_target="0x1111111111111111111111111111111111111111",
            expected_value="0",
            live_action_authorization=_approved_bridge_authorization(),
            source="gateway_bridge.execute_bridge",
        )

    assert "bridge provider_route_id mismatch" in str(exc.value.detail)


def test_bridge_execution_gate_rejects_nonce_replay(monkeypatch):
    gate = _live_gate_module()
    monkeypatch.setenv("TRADING_SAFETY_LIVE_GATEWAY_BRIDGE_EXECUTE_ENABLED", "true")
    monkeypatch.setenv("TRADING_SAFETY_BRIDGE_PROVIDER_ALLOWLIST", "lifi")
    monkeypatch.setenv("MARLIN_LIVE_ACTION_AUTH_SECRET", "test-secret")

    kwargs = {
        "expected_authorization_nonce": "bridge-auth-001",
        "expected_calldata_hash": "sha256:calldata",
        "expected_provider": "lifi",
        "expected_provider_route_id": "route-123",
        "expected_quote_id": "quote-123",
        "expected_route_payload_hash": "sha256:route",
        "expected_source_chain_id": "1",
        "expected_target": "0x1111111111111111111111111111111111111111",
        "expected_value": "0",
        "live_action_authorization": _approved_bridge_authorization(),
        "source": "gateway_bridge.execute_bridge",
    }

    gate.assert_live_bridge_execution_allowed(**kwargs)
    with pytest.raises(HTTPException) as exc:
        gate.assert_live_bridge_execution_allowed(**kwargs)

    assert "bridge authorization nonce replay" in str(exc.value.detail)


def test_gateway_bridge_router_checks_live_gate_before_gateway_execution():
    source = (ROOT / "routers" / "gateway_bridge.py").read_text()
    execute_source = source[source.index("async def execute_bridge") :]

    assert "assert_live_bridge_execution_allowed(" in execute_source
    assert "expected_provider=request.provider" in execute_source
    assert "expected_provider_route_id=request.provider_route_id" in execute_source
    assert "expected_quote_id=request.quote_id" in execute_source
    assert "expected_target=request.tx_target" in execute_source
    assert "expected_value=request.tx_value" in execute_source
    assert "expected_calldata_hash=request.tx_calldata_hash" in execute_source
    assert execute_source.index("assert_live_bridge_execution_allowed(") < execute_source.index(
        "accounts_service.gateway_client.execute_bridge(",
    )


def test_gateway_bridge_router_is_registered_in_main():
    main_source = (ROOT / "main.py").read_text()

    assert "gateway_bridge" in main_source
    assert "app.include_router(gateway_bridge.router" in main_source


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
        assert "live_action_authorization=request.live_action_authorization" in mutation_source
        assert mutation_source.index("assert_live_gateway_mutation_allowed(") < mutation_source.index(
            mutation_call,
        )


def test_gateway_mutation_models_accept_live_action_authorization():
    models_source = (ROOT / "models" / "gateway_trading.py").read_text()
    gateway_source = (ROOT / "models" / "gateway.py").read_text()

    for class_name in (
        "SwapExecuteRequest",
        "RouterAddLiquidityRequest",
        "RouterRemoveLiquidityRequest",
        "CLMMOpenPositionRequest",
        "CLMMAddLiquidityRequest",
        "CLMMRemoveLiquidityRequest",
        "CLMMClosePositionRequest",
        "CLMMCollectFeesRequest",
    ):
        start = models_source.index(f"class {class_name}")
        end = models_source.find("\n\nclass ", start + 1)
        class_source = models_source[start : end if end != -1 else len(models_source)]
        assert "live_action_authorization: Optional[Dict[str, Any]]" in class_source

    start = gateway_source.index("class SendTransactionRequest")
    end = gateway_source.find("\n\nclass ", start + 1)
    send_model = gateway_source[start : end if end != -1 else len(gateway_source)]
    assert "live_action_authorization: Optional[Dict[str, Any]]" in send_model


def test_gateway_client_forwards_live_action_authorization_to_gateway():
    source = (ROOT / "services" / "gateway_client.py").read_text()

    for method_name in (
        "execute_swap",
        "router_add_liquidity",
        "router_remove_liquidity",
        "execute_bridge",
        "clmm_open_position",
        "clmm_add_liquidity",
        "clmm_remove_liquidity",
        "clmm_close_position",
        "clmm_collect_fees",
        "send_transaction",
    ):
        start = source.index(f"async def {method_name}")
        end = source.find("\n    async def ", start + 1)
        method_source = source[start : end if end != -1 else len(source)]
        assert "live_action_authorization: Optional[Dict[str, Any]] = None" in method_source
        assert 'payload["liveActionAuthorization"] = live_action_authorization' in method_source


def test_accounts_trading_interface_checks_live_gate_before_cancel_submission():
    source = (ROOT / "services" / "accounts_service.py").read_text()
    interface_source = source[source.index("class AccountTradingInterface") : source.index("class AccountsService")]
    cancel_source = interface_source[interface_source.index("    def cancel(") : interface_source.index("    def get_active_orders(")]

    assert cancel_source.index("assert_live_order_cancel_allowed(") < cancel_source.index(
        "connector.cancel(",
    )


def test_trading_service_checks_live_gate_before_executor_cancel_submission():
    source = (ROOT / "services" / "trading_service.py").read_text()
    cancel_source = source[source.index("    def cancel(") : source.index("    def get_active_orders(")]

    assert cancel_source.index("assert_live_order_cancel_allowed(") < cancel_source.index(
        "connector.cancel(",
    )


def _approved_authorization(
    *,
    action,
    api_live_flag,
    connector_id,
    gas="0.001",
    instrument=None,
    network,
    notional="1",
    slippage_bps="25",
    expires_at_utc="2099-01-01T00:00:00Z",
):
    authorization = {
        "action": action,
        "api_live_flag": api_live_flag,
        "blockers": [],
        "connector_id": connector_id,
        "edge_sha256": "e" * 64,
        "expires_at_utc": expires_at_utc,
        "gas": gas,
        "gateway_live_flags": [],
        "generated_at_utc": "2026-06-14T10:00:00Z",
        "instrument": instrument,
        "live_gate_sha256": "g" * 64,
        "network": network,
        "notional": notional,
        "slippage_bps": slippage_bps,
        "status": "approved",
        "version": "live-action-authorization-v1",
    }
    authorization["signature"] = _signature(authorization)
    return authorization


def _approved_bridge_authorization():
    authorization = {
        "action": "bridge",
        "api_live_flag": "TRADING_SAFETY_LIVE_GATEWAY_BRIDGE_EXECUTE_ENABLED",
        "blockers": [],
        "bridge_authorization_nonce": "bridge-auth-001",
        "bridge_provider": "lifi",
        "bridge_provider_route_id": "route-123",
        "bridge_quote_id": "quote-123",
        "bridge_route_payload_hash": "sha256:route",
        "bridge_source_chain_id": "1",
        "bridge_tx_calldata_hash": "sha256:calldata",
        "bridge_tx_target": "0x1111111111111111111111111111111111111111",
        "bridge_tx_value": "0",
        "connector_id": "gateway-bridge",
        "edge_sha256": "e" * 64,
        "expires_at_utc": "2099-01-01T00:00:00Z",
        "gas": "0.001",
        "gateway_live_flags": [
            "GATEWAY_LIVE_BRIDGE_EXECUTE_ENABLED",
            "GATEWAY_LIVE_ETHEREUM_TRANSACTION_ENABLED",
            "GATEWAY_LIVE_SOLANA_RAW_TRANSACTION_ENABLED",
            "GATEWAY_LIVE_SOLANA_TRANSACTION_ENABLED",
        ],
        "generated_at_utc": "2026-06-14T10:00:00Z",
        "instrument": "ethereum:USDC->base:USDC",
        "live_gate_sha256": "g" * 64,
        "network": "ethereum-mainnet",
        "notional": "100",
        "slippage_bps": "25",
        "status": "approved",
        "version": "live-action-authorization-v1",
    }
    authorization["signature"] = _signature(authorization)
    return authorization


def _signature(authorization):
    payload = dict(authorization)
    payload.pop("signature", None)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
