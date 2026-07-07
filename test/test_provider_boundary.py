import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIVE_GATE_CALLS = []
EXECUTE_SWAP_CALLS = []
SET_DEFAULT_WALLET_CALLS = []
PROVIDER_INTENT_TOKEN = "test-provider-intent-token"
COWSWAP_BLOCKER = None
STUBBED_MODULES = (
    "deps",
    "fastapi",
    "hummingbot",
    "hummingbot.client",
    "hummingbot.client.settings",
    "hummingbot.core",
    "hummingbot.core.data_type",
    "hummingbot.core.data_type.common",
    "routers.connectors",
    "routers.gateway_swap",
    "services.accounts_service",
    "services.cowswap_runtime",
    "services.live_trading_gate",
    "services.marlin_runtime",
)


@pytest.fixture(autouse=True)
def _restore_stubbed_modules():
    global COWSWAP_BLOCKER
    COWSWAP_BLOCKER = None
    previous = {name: sys.modules.get(name) for name in STUBBED_MODULES}
    yield
    COWSWAP_BLOCKER = None
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


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
    cowswap_runtime.cowswap_order_submission_blocker = lambda *args, **kwargs: COWSWAP_BLOCKER
    cowswap_runtime.cowswap_runtime_prices = _fake_cowswap_runtime_prices
    cowswap_runtime.cowswap_supported_order_types = lambda: ["MARKET"]
    sys.modules["services.cowswap_runtime"] = cowswap_runtime

    live_trading_gate = types.ModuleType("services.live_trading_gate")
    live_trading_gate.assert_live_gateway_mutation_allowed = (
        lambda *args, **kwargs: LIVE_GATE_CALLS.append(kwargs)
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


def _authorized_request():
    return SimpleNamespace(
        headers={"x-marlin-provider-intent-token": PROVIDER_INTENT_TOKEN}
    )


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


async def _fake_cowswap_runtime_prices(*, runtime, trading_pairs):  # noqa: ARG001
    return {pair: 2500.0 for pair in trading_pairs}


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
        SET_DEFAULT_WALLET_CALLS.append(_kwargs)
        return {"status": "default_set"}

    async def execute_swap(self, **_kwargs):
        EXECUTE_SWAP_CALLS.append(_kwargs)
        return {"error": "Insufficient funds for transaction.", "status": "FAILED"}


class FakeAccountsService:
    def __init__(self):
        self.gateway_client = FakeGatewayClient()
        self.update_calls = []
        self.balance_refresh_errors = {}
        self._cowswap_runtime = SimpleNamespace(
            trading_rules={
                "WETH-USDC": SimpleNamespace(
                    min_base_amount_increment=0,
                    min_order_size=0,
                    min_price_increment=0,
                )
            }
        )
        self.accounts_state = {
            "master_account": {
                "jupiter": [{"token": "SOL", "units": "0"}],
            }
        }

    async def update_account_state(self, **kwargs):
        self.update_calls.append(kwargs)
        if kwargs.get("connector_names") == ["ethereum-base"]:
            self.accounts_state["master_account"]["ethereum-base"] = [
                {"token": "AERO", "units": "2"},
                {"token": "USDC", "units": "5"},
                {"token": "ETH", "units": "0.01"},
            ]
        else:
            self.accounts_state["master_account"]["solana-mainnet-beta"] = [
                {"token": "SOL", "units": "0.1"},
                {"token": "USDC", "units": "5"},
            ]

    def get_accounts_state(self):
        return self.accounts_state

    def connector_balance_refresh_error(self, connector_name):
        return self.balance_refresh_errors.get(connector_name)


def test_swap_provider_snapshot_uses_gateway_chain_network_portfolio():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="jupiter",
                network="mainnet-beta",
                route_id="jupiter-sol-usdc-mainnet",
                trading_pair="SOL-USDC",
                wallet_ref="solana:mainnet-beta:solana_gateway",
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
            "tokens_by_chain_network": {"solana-mainnet-beta": ["SOL", "USDC"]},
        }
    ]
    assert result.provider_actions == ["swap"]
    assert result.portfolio == {
        "master_account": {
            "jupiter": [
                {
                    "balance_source": "gateway",
                    "network": "mainnet-beta",
                    "route_id": "jupiter-sol-usdc-mainnet",
                    "token": "SOL",
                    "units": "0.1",
                    "wallet_ref": "solana:mainnet-beta:solana_gateway",
                },
                {
                    "balance_source": "gateway",
                    "network": "mainnet-beta",
                    "route_id": "jupiter-sol-usdc-mainnet",
                    "token": "USDC",
                    "units": "5",
                    "wallet_ref": "solana:mainnet-beta:solana_gateway",
                },
            ]
        }
    }


def test_base_swap_provider_snapshot_scopes_gateway_balance_tokens():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="aerodrome",
                network="base",
                route_id="aerodrome-aero-usdc-mainnet",
                trading_pair="AERO-USDC",
                wallet_ref="base:mainnet:evm_gateway",
            ),
            request,
            service,
        ),
    )

    assert service.update_calls == [
        {
            "account_names": ["master_account"],
            "connector_names": ["ethereum-base"],
            "skip_gateway": False,
            "tokens_by_chain_network": {"ethereum-base": ["AERO", "USDC", "ETH"]},
        }
    ]
    assert result.provider_actions == ["swap"]
    assert result.portfolio == {
        "master_account": {
            "aerodrome": [
                {
                    "balance_source": "gateway",
                    "network": "base",
                    "route_id": "aerodrome-aero-usdc-mainnet",
                    "token": "AERO",
                    "units": "2",
                    "wallet_ref": "base:mainnet:evm_gateway",
                },
                {
                    "balance_source": "gateway",
                    "network": "base",
                    "route_id": "aerodrome-aero-usdc-mainnet",
                    "token": "USDC",
                    "units": "5",
                    "wallet_ref": "base:mainnet:evm_gateway",
                },
                {
                    "balance_source": "gateway",
                    "network": "base",
                    "route_id": "aerodrome-aero-usdc-mainnet",
                    "token": "ETH",
                    "units": "0.01",
                    "wallet_ref": "base:mainnet:evm_gateway",
                },
            ]
        }
    }


def test_cowswap_provider_snapshot_reports_runtime_blocker_without_derived_noise():
    global COWSWAP_BLOCKER
    COWSWAP_BLOCKER = "CowSwap order submission is disabled: missing runtime wiring"
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="cowswap",
                trading_pair="WETH-USDC",
            ),
            request,
            service,
        ),
    )

    assert result.order_types == []
    assert result.provider_actions == []
    assert result.operator_issues == [
        "provider runtime disabled: CowSwap live order runtime requires "
        "Marlin-scoped EIP-712 signer, CoW order store, EVM balance and "
        "allowance reader, asset map, and API lifecycle"
    ]


def test_cowswap_provider_snapshot_exposes_order_actions_when_runtime_ready():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["cowswap"] = []
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="cowswap",
                trading_pair="WETH-USDC",
            ),
            request,
            service,
        ),
    )

    assert result.order_types == ["MARKET"]
    assert result.provider_actions == ["order", "cancel"]
    assert "provider actions missing: cowswap" not in result.operator_issues
    rows = result.portfolio["master_account"]["cowswap"]
    assert {"available_units": 0.0, "price": 2500.0, "token": "WETH", "units": 0.0, "value": 0.0} in rows
    assert {"available_units": 0.0, "price": 1.0, "token": "USDC", "units": 0.0, "value": 0.0} in rows


def test_xrpl_provider_snapshot_reports_account_activation_blocker():
    provider_boundary = _provider_boundary_module()
    provider_boundary._provider_available = _async_return(True)  # noqa: SLF001
    provider_boundary._provider_capabilities = _async_return((["MARKET"], ["order", "cancel"]))  # noqa: SLF001
    provider_boundary._provider_trading_rule = _async_return({"supports_market_orders": True})  # noqa: SLF001
    service = FakeAccountsService()
    service.accounts_state["master_account"]["xrpl"] = []
    service.balance_refresh_errors["xrpl"] = "actNotFound: Account not found."
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="xrpl",
                trading_pair="XRP-USD",
            ),
            request,
            service,
        ),
    )

    assert result.operator_issues == [
        "account not activated: fund derived XRPL mainnet account reserve"
    ]


def test_xrpl_provider_snapshot_reports_empty_portfolio_as_activation_blocker():
    provider_boundary = _provider_boundary_module()
    provider_boundary._provider_available = _async_return(True)  # noqa: SLF001
    provider_boundary._provider_capabilities = _async_return((["MARKET"], ["order", "cancel"]))  # noqa: SLF001
    provider_boundary._provider_trading_rule = _async_return({"supports_market_orders": True})  # noqa: SLF001
    service = FakeAccountsService()
    service.accounts_state["master_account"]["xrpl"] = []
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="xrpl",
                trading_pair="XRP-USD",
            ),
            request,
            service,
        ),
    )

    assert result.operator_issues == [
        "account not activated: fund derived XRPL mainnet account reserve"
    ]


def test_swap_provider_intent_blocks_mainnet_without_internal_token(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    LIVE_GATE_CALLS.clear()
    EXECUTE_SWAP_CALLS.clear()
    SET_DEFAULT_WALLET_CALLS.clear()
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="swap",
        connector_name="jupiter",
        correlation_id="swap-unauthorized-001",
        market_id="SOL-USDC",
        mode="mainnet",
        quantity="0.0001",
        risk_metadata={"network": "solana-mainnet-beta"},
        side="SELL",
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
            SimpleNamespace(headers={}),
            service,
        )
    )

    assert result.status == "rejected"
    assert "mainnet provider intents" in result.provider_error
    assert LIVE_GATE_CALLS == []
    assert SET_DEFAULT_WALLET_CALLS == []
    assert EXECUTE_SWAP_CALLS == []


def test_order_provider_intent_preflight_blocks_mainnet_without_internal_token(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="jupiter",
        correlation_id="order-preflight-unauthorized-001",
        market_id="SOL-USDC",
        mode="mainnet",
        preflight_only=True,
        quantity="0.0001",
        side="BUY",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            SimpleNamespace(headers={}),
            service,
        ),
    )

    assert result.status == "rejected"
    assert "mainnet provider intents" in result.provider_error
    assert service.update_calls == []


def test_order_provider_intent_preflight_rejects_xrpl_account_not_activated(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["xrpl"] = []
    service.balance_refresh_errors["xrpl"] = "actNotFound: Account not found."

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="xrpl",
        correlation_id="order-preflight-xrpl-unfunded-001",
        market_id="XRP-USD",
        mode="mainnet",
        order_type="LIMIT",
        preflight_only=True,
        price="1",
        quantity="100",
        side="BUY",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert result.provider_error == "account not activated: fund derived XRPL mainnet account reserve"


def test_order_provider_intent_preflight_rejects_empty_xrpl_without_refresh_error(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["xrpl"] = []

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="xrpl",
        correlation_id="order-preflight-xrpl-empty-001",
        market_id="XRP-USD",
        mode="mainnet",
        order_type="LIMIT",
        preflight_only=True,
        price="1",
        quantity="100",
        side="BUY",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert result.provider_error == "account not activated: fund derived XRPL mainnet account reserve"


def test_order_provider_intent_preflight_rejects_empty_spend_balance(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["hyperliquid"] = []

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="hyperliquid",
        correlation_id="order-preflight-hyperliquid-empty-001",
        market_id="HYPE-USDC",
        mode="mainnet",
        order_type="LIMIT",
        preflight_only=True,
        price="33.33",
        quantity="3",
        side="BUY",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert result.provider_error == (
        "preflight spend balance 99.99 USDC exceeds available balance 0"
    )


def test_order_provider_intent_preflight_rejects_buy_without_price(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["hyperliquid"] = [
        {"token": "USDC", "units": 150, "available_units": 120},
    ]

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="hyperliquid",
        correlation_id="order-preflight-hyperliquid-buy-market-001",
        market_id="HYPE-USDC",
        mode="mainnet",
        order_type="MARKET",
        preflight_only=True,
        quantity="3",
        side="BUY",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert result.provider_error == "preflight spend balance unavailable for BUY HYPE-USDC"


def test_order_provider_intent_preflight_accepts_sufficient_spend_balance(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["hyperliquid"] = [
        {"token": "USDC", "units": 150, "available_units": 120},
    ]

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="hyperliquid",
        correlation_id="order-preflight-hyperliquid-funded-001",
        market_id="HYPE-USDC",
        mode="mainnet",
        order_type="LIMIT",
        preflight_only=True,
        price="33.33",
        quantity="3",
        side="BUY",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "accepted"
    assert result.provider_status == "preflight_accepted:LIMIT"
    assert result.submitted_notional == provider_boundary.Decimal("99.99")


def test_swap_provider_intent_preserves_gateway_error_without_transaction_hash(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    LIVE_GATE_CALLS.clear()
    EXECUTE_SWAP_CALLS.clear()
    SET_DEFAULT_WALLET_CALLS.clear()
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
            _authorized_request(),
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
    assert gate_call["marlin_provider_intent_authorized"] is True
    assert gate_call["network"] == "mainnet-beta"
    assert gate_call["source"] == "provider.intents"
    assert SET_DEFAULT_WALLET_CALLS == [
        {
            "address": "9AtFd6KcR9tx5Etxc9SVkYrkZb7yC5BDibao7yPT5Ce1",
            "chain": "solana",
            "network": "mainnet-beta",
            "wallet_ref": "solana:mainnet-beta:solana_gateway",
        }
    ]
    assert EXECUTE_SWAP_CALLS[0]["network"] == "mainnet-beta"
    assert EXECUTE_SWAP_CALLS[0]["marlin_provider_intent_authorized"] is True
    assert EXECUTE_SWAP_CALLS[0]["live_action_authorization"] == {
        "action": "gateway_swap",
        "connector_id": "jupiter",
        "network": "mainnet-beta",
        "notional": "0.0001",
        "scope": "provider_intent",
        "slippage_bps": "100.0",
        "source": "marlin",
        "wallet_address": "9AtFd6KcR9tx5Etxc9SVkYrkZb7yC5BDibao7yPT5Ce1",
    }


def test_swap_provider_intent_preflight_does_not_execute_swap(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    LIVE_GATE_CALLS.clear()
    EXECUTE_SWAP_CALLS.clear()
    SET_DEFAULT_WALLET_CALLS.clear()
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="swap",
        connector_name="jupiter",
        correlation_id="swap-preflight-001",
        market_id="SOL-USDC",
        mode="mainnet",
        preflight_only=True,
        quantity="0.0001",
        risk_metadata={"network": "solana-mainnet-beta"},
        side="SELL",
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
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "accepted"
    assert result.provider_status == "preflight_accepted"
    assert len(LIVE_GATE_CALLS) == 1
    assert SET_DEFAULT_WALLET_CALLS == [
        {
            "address": "9AtFd6KcR9tx5Etxc9SVkYrkZb7yC5BDibao7yPT5Ce1",
            "chain": "solana",
            "network": "mainnet-beta",
            "wallet_ref": "solana:mainnet-beta:solana_gateway",
        }
    ]
    assert EXECUTE_SWAP_CALLS == []


def test_swap_provider_intent_preflight_rejects_underfunded_spend_balance(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    LIVE_GATE_CALLS.clear()
    EXECUTE_SWAP_CALLS.clear()
    SET_DEFAULT_WALLET_CALLS.clear()
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="swap",
        connector_name="jupiter",
        correlation_id="swap-preflight-underfunded-001",
        market_id="SOL-USDC",
        mode="mainnet",
        preflight_only=True,
        quantity="1",
        risk_metadata={"network": "solana-mainnet-beta"},
        side="SELL",
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
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert result.provider_error == "preflight spend balance 1 SOL exceeds available balance 0.1"
    assert len(LIVE_GATE_CALLS) == 1
    assert SET_DEFAULT_WALLET_CALLS == [
        {
            "address": "9AtFd6KcR9tx5Etxc9SVkYrkZb7yC5BDibao7yPT5Ce1",
            "chain": "solana",
            "network": "mainnet-beta",
            "wallet_ref": "solana:mainnet-beta:solana_gateway",
        }
    ]
    assert EXECUTE_SWAP_CALLS == []


def test_base_swap_provider_intent_accepts_gateway_network_identity_alias(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    LIVE_GATE_CALLS.clear()
    EXECUTE_SWAP_CALLS.clear()
    SET_DEFAULT_WALLET_CALLS.clear()
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="swap",
        connector_name="aerodrome",
        correlation_id="swap-base-001",
        market_id="AERO-USDC",
        mode="mainnet",
        quantity="0.0001",
        risk_metadata={"network": "ethereum-base"},
        side="SELL",
        wallet_identity={
            "address": "0x1111111111111111111111111111111111111111",
            "chain": "ethereum",
            "network": "ethereum-base",
            "wallet_ref": "base:mainnet:evm_gateway",
        },
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.provider_error == "Insufficient funds for transaction."
    assert SET_DEFAULT_WALLET_CALLS == [
        {
            "address": "0x1111111111111111111111111111111111111111",
            "chain": "ethereum",
            "network": "base",
            "wallet_ref": "base:mainnet:evm_gateway",
        }
    ]
    assert LIVE_GATE_CALLS[0]["chain"] == "ethereum"
    assert LIVE_GATE_CALLS[0]["network"] == "base"
    assert EXECUTE_SWAP_CALLS[0]["network"] == "base"
    assert EXECUTE_SWAP_CALLS[0]["connector"] == "aerodrome"
