import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
LIVE_GATE_CALLS = []
EXECUTE_SWAP_CALLS = []


def _install_provider_boundary_stubs():
    fastapi = types.ModuleType("fastapi")

    class FakeAPIRouter:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, *args, **kwargs):
            return lambda func: func

        def get(self, *args, **kwargs):
            return lambda func: func

    class FakeHTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.APIRouter = FakeAPIRouter
    fastapi.Depends = lambda value=None: None
    fastapi.HTTPException = FakeHTTPException
    fastapi.Query = lambda default=None, **kwargs: default
    fastapi.Request = object
    sys.modules["fastapi"] = fastapi

    deps = types.ModuleType("deps")
    deps.get_accounts_service = lambda: None
    deps.get_database_manager = lambda: None
    sys.modules["deps"] = deps

    settings = types.ModuleType("hummingbot.client.settings")
    settings.AllConnectorSettings = SimpleNamespace(get_connector_settings=lambda: {})
    sys.modules["hummingbot"] = types.ModuleType("hummingbot")
    sys.modules["hummingbot.client"] = types.ModuleType("hummingbot.client")
    sys.modules["hummingbot.client.settings"] = settings

    common = types.ModuleType("hummingbot.core.data_type.common")
    common.OrderType = {}
    common.PositionAction = {}
    common.TradeType = {}
    sys.modules["hummingbot.core"] = types.ModuleType("hummingbot.core")
    sys.modules["hummingbot.core.data_type"] = types.ModuleType("hummingbot.core.data_type")
    sys.modules["hummingbot.core.data_type.common"] = common

    connectors = types.ModuleType("routers.connectors")
    connectors._gateway_connector_configs = _fake_gateway_connector_configs
    connectors._gateway_connector_name = lambda item: item.get("name")
    connectors._gateway_swap_connector = _fake_gateway_swap_connector
    sys.modules["routers.connectors"] = connectors

    gateway_swap = types.ModuleType("routers.gateway_swap")
    gateway_swap.get_transaction_status_from_response = lambda value: value.get("status", "submitted")
    sys.modules["routers.gateway_swap"] = gateway_swap

    cowswap_runtime = types.ModuleType("services.cowswap_runtime")
    cowswap_runtime.COWSWAP_CONNECTOR_NAME = "cowswap"
    cowswap_runtime.cowswap_order_submission_blocker = lambda *args, **kwargs: None
    cowswap_runtime.cowswap_supported_order_types = lambda: ()
    sys.modules["services.cowswap_runtime"] = cowswap_runtime

    live_trading_gate = types.ModuleType("services.live_trading_gate")
    live_trading_gate.assert_live_gateway_mutation_allowed = (
        lambda *args, **kwargs: LIVE_GATE_CALLS.append(kwargs)
    )
    live_trading_gate.consume_marlin_provider_intent_swap_authorization = (
        lambda *args, **kwargs: True
    )
    sys.modules["services.live_trading_gate"] = live_trading_gate

    marlin_runtime = types.ModuleType("services.marlin_runtime")
    marlin_runtime.assert_marlin_default_wallet_identity = lambda *args, **kwargs: None
    sys.modules["services.marlin_runtime"] = marlin_runtime

    accounts_service = types.ModuleType("services.accounts_service")
    accounts_service.AccountsService = object
    sys.modules["services.accounts_service"] = accounts_service


async def _fake_gateway_swap_connector(service, connector_name):
    for item in await _fake_gateway_connector_configs(service):
        if item.get("name") == connector_name:
            return "router" in {str(value).lower() for value in item.get("tradingTypes", [])}
    return None


async def _fake_gateway_connector_configs(service):
    configs = await service.gateway_client._request("GET", "config/connectors")
    return configs["connectors"]


def _provider_boundary_module():
    _install_provider_boundary_stubs()
    spec = importlib.util.spec_from_file_location(
        "provider_boundary_under_test",
        ROOT / "routers" / "provider_boundary.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGatewayClient:
    async def ping(self):
        return True

    @staticmethod
    def parse_network_id(network_id):
        chain, network = network_id.split("-", 1)
        return chain, network

    async def _request(self, method, path):
        assert method == "GET"
        assert path == "config/connectors"
        return {
            "connectors": [
                {"name": "jupiter", "tradingTypes": ["router"]},
                {"name": "aerodrome", "tradingTypes": ["router"]},
            ]
        }

    async def set_marlin_default_wallet(self, **_kwargs):
        return {"status": "default_set"}

    async def execute_swap(self, **_kwargs):
        EXECUTE_SWAP_CALLS.append(_kwargs)
        return {"error": "Insufficient funds for transaction.", "status": "FAILED"}


class FakeAccountsService:
    def __init__(self):
        self.gateway_client = FakeGatewayClient()
        self.update_calls = []
        self.accounts_state = {
            "master_account": {
                "jupiter": [{"token": "SOL", "units": "0"}],
            }
        }

    async def update_account_state(self, **kwargs):
        self.update_calls.append(kwargs)
        self.accounts_state["master_account"]["solana-mainnet-beta"] = [
            {"token": "SOL", "units": "0.1"},
            {"token": "USDC", "units": "5"},
        ]

    def get_accounts_state(self):
        return self.accounts_state


def test_swap_provider_snapshot_uses_gateway_chain_network_portfolio():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="jupiter",
                trading_pair="SOL-USDC",
            ),
            request,
            service,
        ),
    )

    assert service.update_calls == [
        {
            "account_names": ["master_account"],
            "connector_names": ["solana-mainnet-beta"],
            "skip_gateway": False,
        }
    ]
    assert result.provider_actions == ["swap"]
    assert result.portfolio == {
        "master_account": {
            "jupiter": [
                {"balance_source": "gateway", "token": "SOL", "units": "0.1"},
                {"balance_source": "gateway", "token": "USDC", "units": "5"},
            ]
        }
    }


def test_swap_provider_intent_preserves_gateway_error_without_transaction_hash():
    LIVE_GATE_CALLS.clear()
    EXECUTE_SWAP_CALLS.clear()
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="swap",
        connector_name="jupiter",
        correlation_id="swap-001",
        market_id="SOL-USDC",
        mode="mainnet",
        quantity="0.0001",
        risk_metadata={"network": "solana-mainnet-beta"},
        side="SELL",
        live_action_authorization={
            "action": "gateway_swap",
            "scope": "provider_intent",
            "source": "marlin",
        },
        wallet_identity={
            "address": "9AtFd6KcR9tx5Etxc9SVkYrkZb7yC5BDibao7yPT5Ce1",
            "chain": "solana",
            "network": "mainnet-beta",
            "wallet_ref": "solana:mainnet-beta:solana_gateway",
        },
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            SimpleNamespace(headers={"x-marlin-provider-intent-signature": "test-signature"}),
            service,
        ),
    )

    assert result.status == "failed"
    assert result.provider_error == "Insufficient funds for transaction."
    assert result.provider_status == "FAILED"
    assert len(LIVE_GATE_CALLS) == 1
    gate_call = LIVE_GATE_CALLS[0]
    assert gate_call["action"] == "swap_execute"
    assert gate_call["chain"] == "solana"
    assert gate_call["expected_connector_id"] == "jupiter"
    assert gate_call["expected_instrument"] == "SOL-USDC"
    assert gate_call["expected_notional"] == provider_boundary.Decimal("0.0001")
    assert gate_call["expected_slippage_bps"] == provider_boundary.Decimal("100.0")
    assert gate_call["live_action_authorization"] == {
        "action": "gateway_swap",
        "scope": "provider_intent",
        "source": "marlin",
    }
    assert gate_call["marlin_provider_intent_authorized"] is True
    assert gate_call["network"] == "mainnet-beta"
    assert gate_call["provider_intent_signature"] == "test-signature"
    assert gate_call["provider_intent_payload"]["connector_name"] == "jupiter"
    assert gate_call["provider_intent_payload"]["quantity"] == "0.0001"
    assert gate_call["source"] == "provider.intents"
    assert EXECUTE_SWAP_CALLS[0]["network"] == "mainnet-beta"
    assert EXECUTE_SWAP_CALLS[0]["marlin_provider_intent_authorized"] is True
    assert EXECUTE_SWAP_CALLS[0]["live_action_authorization"] == {
        "action": "gateway_swap",
        "scope": "provider_intent",
        "source": "marlin",
    }
