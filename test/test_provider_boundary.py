import asyncio
import importlib.util
import sys
import types
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIVE_GATE_CALLS = []
EXECUTE_SWAP_CALLS = []
SET_DEFAULT_WALLET_CALLS = []
PROVIDER_INTENT_TOKEN = "test-provider-intent-token"
COWSWAP_BLOCKER = None
COWSWAP_ORDER_TYPES = ["MARKET"]
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
    "hummingbot_cowswap",
    "hummingbot_cowswap.chain_config",
)


@pytest.fixture(autouse=True)
def _restore_stubbed_modules():
    global COWSWAP_BLOCKER, COWSWAP_ORDER_TYPES
    COWSWAP_BLOCKER = None
    COWSWAP_ORDER_TYPES = ["MARKET"]
    previous = {name: sys.modules.get(name) for name in STUBBED_MODULES}
    yield
    COWSWAP_BLOCKER = None
    COWSWAP_ORDER_TYPES = ["MARKET"]
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
    cowswap_runtime.cowswap_runtime_prices = _unexpected_cowswap_runtime_prices
    cowswap_runtime.cowswap_supported_order_types = lambda: list(COWSWAP_ORDER_TYPES)
    sys.modules["services.cowswap_runtime"] = cowswap_runtime

    hummingbot_cowswap = types.ModuleType("hummingbot_cowswap")
    sys.modules["hummingbot_cowswap"] = hummingbot_cowswap
    chain_config = types.ModuleType("hummingbot_cowswap.chain_config")
    chain_config.chain_config = lambda chain_id, env: SimpleNamespace(
        chain_id=chain_id,
        env=env,
        vault_relayer="0xvaultrelayer",
    )
    sys.modules["hummingbot_cowswap.chain_config"] = chain_config

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


async def _unexpected_cowswap_runtime_prices(*, runtime, trading_pairs):  # noqa: ARG001
    raise AssertionError("CowSwap provider snapshots must not call quote/prices")


class FakeCowSwapEvmReader:
    def __init__(self):
        self.balances = {
            "WETH": "0",
            "USDC": "5000000",
        }
        self.allowances = {
            "WETH": "0",
            "USDC": "5000000",
        }
        self.balance_calls = []
        self.allowance_calls = []

    def balance_of(self, token, owner):
        self.balance_calls.append((token.symbol, owner))
        return self.balances.get(token.symbol, "0")

    def allowance(self, token, owner, spender):
        self.allowance_calls.append((token.symbol, owner, spender))
        return self.allowances.get(token.symbol, "0")


class FakeCowSwapConnector:
    def __init__(self):
        self.config = SimpleNamespace(chain_id=8453, env="prod")
        self.quote_sell_calls = []
        self.quote_buy_calls = []
        self.maximum_sell_amount = "2500"
        self.sell_fee_amount = "10"
        self.verified = True
        self.valid_to = 9999999999
        self.sell_error = None

    async def quote_sell(self, sell_token, buy_token, amount):
        self.quote_sell_calls.append((sell_token.symbol, buy_token.symbol, amount))
        if self.sell_error is not None:
            raise self.sell_error
        sell_amount = str(int(Decimal(str(amount)) * (Decimal(10) ** sell_token.decimals)))
        return SimpleNamespace(
            verified=self.verified,
            quote=SimpleNamespace(
                sellAmount=SimpleNamespace(root=sell_amount),
                feeAmount=SimpleNamespace(root=self.sell_fee_amount),
                validTo=SimpleNamespace(root=str(self.valid_to)),
            ),
        ), "0"

    async def quote_buy(self, sell_token, buy_token, amount):
        self.quote_buy_calls.append((sell_token.symbol, buy_token.symbol, amount))
        return SimpleNamespace(
            verified=self.verified,
            quote=SimpleNamespace(validTo=SimpleNamespace(root=str(self.valid_to))),
        ), self.maximum_sell_amount


class FakeCowSwapRuntime:
    def __init__(self):
        self._connector = FakeCowSwapConnector()
        self.trading_rules = {
            "WETH-USDC": SimpleNamespace(
                min_base_amount_increment=0,
                min_order_size=0,
                min_price_increment=0,
            ),
            "USDC-WETH": SimpleNamespace(
                min_base_amount_increment=0,
                min_order_size=0,
                min_price_increment=0,
            ),
        }
        self.tokens = {
            "WETH-USDC": (
                SimpleNamespace(symbol="WETH", decimals=18),
                SimpleNamespace(symbol="USDC", decimals=6),
            ),
            "USDC-WETH": (
                SimpleNamespace(symbol="USDC", decimals=6),
                SimpleNamespace(symbol="WETH", decimals=18),
            ),
        }

    def _tokens_for_pair(self, trading_pair):
        if trading_pair not in self.tokens:
            raise ValueError(f"unsupported trading_pair for CoW shim: {trading_pair}")
        return self.tokens[trading_pair]


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


class FakeMarketDataService:
    def __init__(self):
        self.rate_calls = []
        self.trading_rule_calls = []
        self.rates = {
            "USDC-WETH": Decimal("0.0004"),
            "WETH-USDC": Decimal("2500"),
        }

    def get_rate(self, base, quote):
        self.rate_calls.append((base, quote))
        return self.rates.get(f"{base}-{quote}")

    async def get_trading_rules(self, connector_name, trading_pairs):
        self.trading_rule_calls.append((connector_name, trading_pairs))
        return {
            "HYPE-USD": {
                "buy_order_collateral_token": "USD",
                "min_notional_size": 10.0,
                "sell_order_collateral_token": "USD",
            }
        }


class FakeAccountsService:
    def __init__(self):
        self.gateway_client = FakeGatewayClient()
        self.update_calls = []
        self.balance_refresh_errors = {}
        self.cowswap_evm_reader = FakeCowSwapEvmReader()
        self._cowswap_runtime = FakeCowSwapRuntime()
        self._cowswap_runtime_dependencies = SimpleNamespace(
            evm_reader=self.cowswap_evm_reader,
            owner_address="0xowner",
            token_map=self._cowswap_runtime.tokens,
            order_store=object(),
            signer_provider=SimpleNamespace(
                network="base",
                wallet_ref="base:mainnet:evm_gateway",
            ),
        )
        self.accounts_state = {
            "master_account": {
                "jupiter": [{"token": "SOL", "units": "0"}],
            }
        }
        self.update_error = None
        self.update_success = True
        self.place_trade_error = None
        self.place_trade_calls = []
        self.account_positions = []
        self.position_calls = []
        self.position_refresh_error = None

    async def get_account_positions(self, account_name, connector_name):
        self.position_calls.append((account_name, connector_name))
        if self.position_refresh_error is not None:
            raise self.position_refresh_error
        return list(self.account_positions)

    async def update_account_state(self, **kwargs):
        self.update_calls.append(kwargs)
        if self.update_error is not None:
            raise self.update_error
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
        return self.update_success

    def get_accounts_state(self):
        return self.accounts_state

    def connector_balance_refresh_error(self, connector_name):
        return self.balance_refresh_errors.get(connector_name)

    async def place_trade(self, **_kwargs):
        self.place_trade_calls.append(_kwargs)
        if self.place_trade_error is not None:
            raise self.place_trade_error
        return "order-1"


def _request_with_market_data():
    market_data_service = FakeMarketDataService()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(market_data_service=market_data_service)),
    )
    return request, market_data_service


def test_hyperliquid_perpetual_snapshot_uses_native_market_and_exposes_logical_collateral():
    provider_boundary = _provider_boundary_module()
    provider_boundary._provider_available = _async_return(True)  # noqa: SLF001
    provider_boundary._provider_capabilities = _async_return((['MARKET'], ['order', 'cancel']))  # noqa: SLF001
    service = FakeAccountsService()
    service.accounts_state["master_account"]["hyperliquid_perpetual"] = [
        {"available_units": 15.0, "token": "USD", "units": 15.0, "value": 15.0},
    ]
    request, market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="hyperliquid_perpetual",
                refresh_portfolio=False,
                trading_pair="HYPE-USDC",
            ),
            request,
            service,
        ),
    )

    assert market_data_service.trading_rule_calls == [
        ("hyperliquid_perpetual", ["HYPE-USD"]),
    ]
    assert result.trading_pair == "HYPE-USDC"
    assert result.positions_status == "available"
    assert result.positions == []
    assert service.position_calls == [("master_account", "hyperliquid_perpetual")]
    assert result.trading_rule == {
        "buy_order_collateral_token": "USDC",
        "min_notional_size": 10.0,
        "sell_order_collateral_token": "USDC",
    }
    assert result.portfolio == {
        "master_account": {
            "hyperliquid_perpetual": [
                {"available_units": 15.0, "token": "USDC", "units": 15.0, "value": 15.0},
            ],
        },
    }
    assert result.portfolio_observed_at_utc is None


def test_swap_provider_snapshot_omits_observation_time_when_gateway_refresh_fails():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.update_success = False
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

    assert result.portfolio_observed_at_utc is None


def test_perpetual_provider_snapshot_refreshes_and_normalizes_positions():
    provider_boundary = _provider_boundary_module()
    provider_boundary._provider_available = _async_return(True)  # noqa: SLF001
    provider_boundary._provider_capabilities = _async_return((['MARKET'], ['order', 'cancel']))  # noqa: SLF001
    service = FakeAccountsService()
    service.account_positions = [
        {
            "account_name": "master_account",
            "connector_name": "hyperliquid_perpetual",
            "trading_pair": "HYPE-USD",
            "side": "LONG",
            "amount": "-0.64",
            "entry_price": "25.5",
            "unrealized_pnl": "1.25",
            "leverage": "3",
        },
    ]
    request, _market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="hyperliquid_perpetual",
                refresh_portfolio=False,
                trading_pair="HYPE-USDC",
            ),
            request,
            service,
        ),
    )

    assert service.position_calls == [("master_account", "hyperliquid_perpetual")]
    assert len(result.positions) == 1
    position = result.positions[0]
    assert position.account_name == "master_account"
    assert position.connector_name == "hyperliquid_perpetual"
    assert position.trading_pair == "HYPE-USD"
    assert position.side == "LONG"
    assert position.quantity == Decimal("0.64")
    assert position.entry_price == Decimal("25.5")
    assert position.unrealized_pnl == Decimal("1.25")
    assert position.leverage == Decimal("3")


@pytest.mark.parametrize("connector_name", ["binance", "aerodrome"])
def test_non_perpetual_provider_snapshot_returns_no_positions(connector_name):
    provider_boundary = _provider_boundary_module()
    provider_boundary._provider_available = _async_return(True)  # noqa: SLF001
    provider_boundary._provider_capabilities = _async_return((['MARKET'], ['order']))  # noqa: SLF001
    service = FakeAccountsService()
    service.account_positions = [
        {"trading_pair": "HYPE-USD", "side": "LONG", "amount": "0.64"},
    ]
    request, _market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name=connector_name,
                refresh_portfolio=False,
                trading_pair="HYPE-USDC",
            ),
            request,
            service,
        ),
    )

    assert result.positions == []
    assert result.positions_status == "unsupported"
    assert service.position_calls == []


def test_perpetual_provider_snapshot_reports_position_refresh_failure_without_flat_state():
    provider_boundary = _provider_boundary_module()
    provider_boundary._provider_available = _async_return(True)  # noqa: SLF001
    provider_boundary._provider_capabilities = _async_return((['MARKET'], ['order', 'cancel']))  # noqa: SLF001
    service = FakeAccountsService()
    service.position_refresh_error = RuntimeError("exchange position endpoint unavailable")
    request, _market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="hyperliquid_perpetual",
                refresh_portfolio=False,
                trading_pair="HYPE-USDC",
            ),
            request,
            service,
        ),
    )

    assert service.position_calls == [("master_account", "hyperliquid_perpetual")]
    assert result.positions == []
    assert result.positions_status == "issues"
    assert result.status == "issues"
    assert "positions refresh unavailable: exchange position endpoint unavailable" in result.operator_issues


@pytest.mark.parametrize(
    ("connector_name", "mode", "logical_pair", "submitted_pair"),
    [
        ("hyperliquid_perpetual", "mainnet", "HYPE-USDC", "HYPE-USD"),
        ("hyperliquid", "mainnet", "HYPE-USDC", "HYPE-USDC"),
        ("hyperliquid_perpetual_testnet", "testnet", "HYPE-USDC", "HYPE-USDC"),
    ],
)
def test_provider_order_intent_maps_only_hyperliquid_perpetual_mainnet_market(
    monkeypatch,
    connector_name,
    mode,
    logical_pair,
    submitted_pair,
):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    monkeypatch.setattr(provider_boundary, "TradeType", {"BUY": "BUY"})
    monkeypatch.setattr(provider_boundary, "OrderType", {"LIMIT": "LIMIT"})
    monkeypatch.setattr(provider_boundary, "PositionAction", SimpleNamespace(OPEN="OPEN"))
    service = FakeAccountsService()

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            provider_boundary.ProviderIntentRequest(
                account_name="master_account",
                action="order",
                connector_name=connector_name,
                market_id=logical_pair,
                mode=mode,
                order_type="LIMIT",
                price="25",
                quantity="1",
                side="BUY",
            ),
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "submitted"
    assert service.place_trade_calls[0]["trading_pair"] == submitted_pair


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
    assert result.portfolio_observed_at_utc is not None
    assert result.portfolio_observed_at_utc.tzinfo == timezone.utc


def test_provider_snapshot_exposes_aware_utc_observation_time_after_successful_refresh():
    provider_boundary = _provider_boundary_module()
    provider_boundary._provider_available = _async_return(True)  # noqa: SLF001
    provider_boundary._provider_capabilities = _async_return((["MARKET"], ["order"]))  # noqa: SLF001
    provider_boundary._provider_trading_rule = _async_return(None)  # noqa: SLF001
    service = FakeAccountsService()
    service.accounts_state["master_account"]["binance"] = [
        {"token": "USDT", "units": "5"},
    ]
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="binance",
                trading_pair="BTC-USDT",
            ),
            request,
            service,
        ),
    )

    assert result.portfolio_observed_at_utc is not None
    assert result.portfolio_observed_at_utc.tzinfo == timezone.utc


def test_provider_snapshot_reports_unavailable_refresh_with_cached_portfolio():
    provider_boundary = _provider_boundary_module()
    provider_boundary._provider_available = _async_return(True)  # noqa: SLF001
    provider_boundary._provider_capabilities = _async_return((["MARKET"], ["order"]))  # noqa: SLF001
    provider_boundary._provider_trading_rule = _async_return(None)  # noqa: SLF001
    service = FakeAccountsService()
    service.update_success = False
    service.accounts_state["master_account"]["binance"] = [
        {"token": "USDT", "units": "5"},
    ]
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="binance",
                refresh_portfolio=True,
                trading_pair="BTC-USDT",
            ),
            request,
            service,
        ),
    )

    assert result.status == "issues"
    assert result.portfolio_observed_at_utc is None
    assert "portfolio refresh unavailable" in result.operator_issues
    assert result.portfolio == {
        "master_account": {
            "binance": [{"token": "USDT", "units": "5"}],
        },
    }


@pytest.mark.parametrize("failure", ["raised", "connector_error"])
def test_provider_snapshot_omits_observation_time_when_refresh_is_not_proven(failure):
    provider_boundary = _provider_boundary_module()
    provider_boundary._provider_available = _async_return(True)  # noqa: SLF001
    provider_boundary._provider_capabilities = _async_return((["MARKET"], ["order"]))  # noqa: SLF001
    provider_boundary._provider_trading_rule = _async_return(None)  # noqa: SLF001
    service = FakeAccountsService()
    if failure == "raised":
        service.update_error = RuntimeError("connector refresh failed")
    else:
        service.balance_refresh_errors["binance"] = "connector balance refresh failed"
        service.update_success = False
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="binance",
                trading_pair="BTC-USDT",
            ),
            request,
            service,
        ),
    )

    assert result.portfolio_observed_at_utc is None



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
    service.update_success = False
    service.accounts_state["master_account"]["cowswap"] = []
    request, market_data_service = _request_with_market_data()

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
    assert result.status == "available"
    assert result.portfolio_observed_at_utc is not None
    assert "portfolio refresh unavailable" not in result.operator_issues
    assert service.update_calls == []
    assert service.cowswap_evm_reader.balance_calls == [
        ("WETH", "0xowner"),
        ("USDC", "0xowner"),
    ]
    assert "provider actions missing: cowswap" not in result.operator_issues
    assert service._cowswap_runtime._connector.quote_sell_calls == []
    assert service._cowswap_runtime._connector.quote_buy_calls == []
    assert market_data_service.rate_calls == [("WETH", "USDC")]
    rows = result.portfolio["master_account"]["cowswap"]
    assert {
        "available_units": 0.0,
        "price": 2500.0,
        "token": "WETH",
        "units": 0.0,
        "value": 0.0,
    } in rows
    assert {"available_units": 5.0, "price": 1.0, "token": "USDC", "units": 5.0, "value": 5.0} in rows


def test_cowswap_provider_snapshot_exposes_reverse_pair_gateway_balances():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["cowswap"] = []
    request, market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="cowswap",
                trading_pair="USDC-WETH",
            ),
            request,
            service,
        ),
    )

    assert result.status == "available"
    assert result.trading_rule is not None
    assert service.update_calls == []
    rows = result.portfolio["master_account"]["cowswap"]
    assert service._cowswap_runtime._connector.quote_sell_calls == []
    assert service._cowswap_runtime._connector.quote_buy_calls == []
    assert market_data_service.rate_calls == [("USDC", "WETH")]
    assert {"available_units": 5.0, "price": 1.0, "token": "USDC", "units": 5.0, "value": 5.0} in rows
    assert {
        "available_units": 0.0,
        "price": 2500.0,
        "token": "WETH",
        "units": 0.0,
        "value": 0.0,
    } in rows


def test_cowswap_provider_snapshot_missing_reference_price_fails_closed():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["cowswap"] = []
    request, market_data_service = _request_with_market_data()
    market_data_service.rates.clear()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="cowswap",
                trading_pair="USDC-WETH",
            ),
            request,
            service,
        ),
    )

    assert result.status == "available"
    assert service._cowswap_runtime._connector.quote_sell_calls == []
    assert service._cowswap_runtime._connector.quote_buy_calls == []
    assert market_data_service.rate_calls == [("USDC", "WETH")]
    assert result.portfolio == {
        "master_account": {
            "cowswap": [
                {"available_units": 5.0, "price": 1.0, "token": "USDC", "units": 5.0, "value": 5.0},
                {"available_units": 0.0, "token": "WETH", "units": 0.0, "value": 0.0},
            ],
        },
    }


def test_cowswap_provider_snapshot_scopes_rows_when_request_matches_signer_identity():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["cowswap"] = []
    request, market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="cowswap",
                network="base",
                route_id="cowswap-usdc-weth-base",
                trading_pair="USDC-WETH",
                wallet_ref="base:mainnet:evm_gateway",
            ),
            request,
            service,
        ),
    )

    assert result.status == "available"
    rows = result.portfolio["master_account"]["cowswap"]
    for row in rows:
        assert row.get("balance_source") == "gateway"
        assert row.get("network") == "base"
        assert row.get("wallet_ref") == "base:mainnet:evm_gateway"
        assert row.get("route_id") == "cowswap-usdc-weth-base"


def test_cowswap_provider_snapshot_omits_scope_on_network_mismatch():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["cowswap"] = []
    request, market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="cowswap",
                network="ethereum-mainnet",
                route_id="cowswap-usdc-weth-eth",
                trading_pair="USDC-WETH",
                wallet_ref="ethereum:mainnet:evm_gateway",
            ),
            request,
            service,
        ),
    )

    assert result.status == "available"
    rows = result.portfolio["master_account"]["cowswap"]
    for row in rows:
        assert "balance_source" not in row
        assert "network" not in row
        assert "wallet_ref" not in row


def test_cowswap_provider_snapshot_omits_scope_on_wallet_ref_mismatch():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["cowswap"] = []
    request, market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="cowswap",
                network="base",
                route_id="cowswap-usdc-weth-base",
                trading_pair="USDC-WETH",
                wallet_ref="base:mainnet:evm_gateway_wrong",
            ),
            request,
            service,
        ),
    )

    assert result.status == "available"
    rows = result.portfolio["master_account"]["cowswap"]
    for row in rows:
        assert "balance_source" not in row
        assert "network" not in row
        assert "wallet_ref" not in row


def test_cowswap_provider_snapshot_omits_scope_when_request_missing_network_or_wallet_ref():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["cowswap"] = []
    request, market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="cowswap",
                trading_pair="USDC-WETH",
            ),
            request,
            service,
        ),
    )

    assert result.status == "available"
    rows = result.portfolio["master_account"]["cowswap"]
    for row in rows:
        assert "balance_source" not in row
        assert "network" not in row
        assert "wallet_ref" not in row


def test_cowswap_order_provider_preflight_accepts_runtime_gateway_balance_and_allowance(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-sell-funded",
        market_id="USDC-WETH",
        mode="mainnet",
        order_type="MARKET",
        preflight_only=True,
        quantity="0.00005",
        side="SELL",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "accepted"
    assert result.provider_status == "preflight_accepted:MARKET"
    assert service.update_calls == []
    assert service._cowswap_runtime._connector.quote_sell_calls == [("USDC", "WETH", "0.00005")]
    assert service.cowswap_evm_reader.balance_calls == [("USDC", "0xowner")]
    assert service.cowswap_evm_reader.allowance_calls == [("USDC", "0xowner", "0xvaultrelayer")]


@pytest.mark.parametrize(
    ("market_id", "side", "quantity", "price", "spend_token"),
    [
        ("WETH-USDC", "BUY", "0.001", "1000", "USDC"),
        ("USDC-WETH", "SELL", "2", "0.0004", "USDC"),
    ],
)
def test_cowswap_limit_preflight_uses_base_or_quote_spend_without_market_quote(
    monkeypatch,
    market_id,
    side,
    quantity,
    price,
    spend_token,
):
    global COWSWAP_ORDER_TYPES
    COWSWAP_ORDER_TYPES = ["MARKET", "LIMIT"]
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-limit-spend-funded",
        market_id=market_id,
        mode="mainnet",
        order_type="LIMIT",
        preflight_only=True,
        price=price,
        quantity=quantity,
        side=side,
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
    assert result.submitted_notional == body.quantity * body.price
    assert service._cowswap_runtime._connector.quote_buy_calls == []
    assert service._cowswap_runtime._connector.quote_sell_calls == []
    assert service.cowswap_evm_reader.balance_calls == [(spend_token, "0xowner")]
    assert service.cowswap_evm_reader.allowance_calls == [(spend_token, "0xowner", "0xvaultrelayer")]


@pytest.mark.parametrize("price", [None, "0", "-1"])
def test_cowswap_limit_preflight_requires_positive_price(monkeypatch, price):
    global COWSWAP_ORDER_TYPES
    COWSWAP_ORDER_TYPES = ["MARKET", "LIMIT"]
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-limit-invalid-price",
        market_id="WETH-USDC",
        mode="mainnet",
        order_type="LIMIT",
        preflight_only=True,
        price=price,
        quantity="0.001",
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
    assert result.provider_error == "CowSwap LIMIT orders require a positive price"
    assert service.cowswap_evm_reader.balance_calls == []
    assert service.cowswap_evm_reader.allowance_calls == []


def test_cowswap_sell_preflight_checks_quoted_sell_amount_with_fee(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.cowswap_evm_reader.balances["USDC"] = "50"
    service.cowswap_evm_reader.allowances["USDC"] = "60"

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-sell-fee-blocked",
        market_id="USDC-WETH",
        mode="mainnet",
        order_type="MARKET",
        preflight_only=True,
        quantity="0.00005",
        side="SELL",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert result.provider_error == "insufficient USDC balance"
    assert service._cowswap_runtime._connector.quote_sell_calls == [("USDC", "WETH", "0.00005")]


def test_cowswap_preflight_rejects_unverified_quote_before_balance_checks(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service._cowswap_runtime._connector.verified = False

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-sell-unverified-quote",
        market_id="USDC-WETH",
        mode="mainnet",
        order_type="MARKET",
        preflight_only=True,
        quantity="0.00005",
        side="SELL",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert result.provider_error == "CoW quote is not verified"
    assert service.cowswap_evm_reader.balance_calls == []
    assert service.cowswap_evm_reader.allowance_calls == []


def test_cowswap_preflight_rate_limit_rejection_defaults_retry_after(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service._cowswap_runtime._connector.sell_error = RuntimeError(
        "rate-limited by CoW Order Book API: HTTP error 429; retry after provider cooldown"
    )

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-sell-rate-limited-default",
        market_id="USDC-WETH",
        mode="mainnet",
        order_type="MARKET",
        preflight_only=True,
        quantity="0.00005",
        side="SELL",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert "rate-limited by CoW Order Book API" in result.provider_error
    assert result.retry_after_seconds == 1800
    assert service.cowswap_evm_reader.balance_calls == []
    assert service.cowswap_evm_reader.allowance_calls == []


def test_cowswap_preflight_rate_limit_rejection_preserves_explicit_retry_after(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service._cowswap_runtime._connector.sell_error = RuntimeError(
        "rate-limited by CoW Order Book API: HTTP error 429; retry-after: 47"
    )

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-sell-rate-limited-explicit",
        market_id="USDC-WETH",
        mode="mainnet",
        order_type="MARKET",
        preflight_only=True,
        quantity="0.00005",
        side="SELL",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert "HTTP error 429" in result.provider_error
    assert result.retry_after_seconds == 47
    assert service.cowswap_evm_reader.balance_calls == []
    assert service.cowswap_evm_reader.allowance_calls == []


def test_cowswap_preflight_quote_failure_without_rate_limit_has_no_retry_after(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service._cowswap_runtime._connector.sell_error = RuntimeError("CoW quote temporarily unavailable")

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-sell-quote-error",
        market_id="USDC-WETH",
        mode="mainnet",
        order_type="MARKET",
        preflight_only=True,
        quantity="0.00005",
        side="SELL",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert "CoW quote temporarily unavailable" in result.provider_error
    assert result.retry_after_seconds is None
    assert service.cowswap_evm_reader.balance_calls == []
    assert service.cowswap_evm_reader.allowance_calls == []


def test_cowswap_submit_rate_limit_rejection_returns_retry_after(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    monkeypatch.setattr(provider_boundary, "TradeType", {"SELL": "SELL"})
    monkeypatch.setattr(provider_boundary, "OrderType", {"MARKET": "MARKET"})
    monkeypatch.setattr(provider_boundary, "PositionAction", SimpleNamespace(OPEN="OPEN"))
    service = FakeAccountsService()
    service.place_trade_error = RuntimeError(
        "429 Too Many Requests; retry-after: 47"
    )

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-submit-rate-limited",
        market_id="USDC-WETH",
        mode="mainnet",
        order_type="MARKET",
        preflight_only=False,
        quantity="0.00005",
        side="SELL",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "failed"
    assert "429 Too Many Requests" in result.provider_error
    assert result.retry_after_seconds == 47


def test_cowswap_limit_submit_forwards_order_type_and_price(monkeypatch):
    global COWSWAP_ORDER_TYPES
    COWSWAP_ORDER_TYPES = ["MARKET", "LIMIT"]
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    monkeypatch.setattr(provider_boundary, "TradeType", {"BUY": "BUY"})
    monkeypatch.setattr(provider_boundary, "OrderType", {"LIMIT": "LIMIT"})
    monkeypatch.setattr(provider_boundary, "PositionAction", SimpleNamespace(OPEN="OPEN"))
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-limit-submit",
        market_id="WETH-USDC",
        mode="mainnet",
        order_type="LIMIT",
        preflight_only=False,
        price="2500",
        quantity="0.01",
        side="BUY",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "submitted"
    assert service.place_trade_calls[0]["order_type"] == "LIMIT"
    assert service.place_trade_calls[0]["price"] == Decimal("2500")


def test_cowswap_order_provider_preflight_rejects_insufficient_allowance(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.cowswap_evm_reader.allowances["USDC"] = "0"

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-sell-no-allowance",
        market_id="USDC-WETH",
        mode="mainnet",
        order_type="MARKET",
        preflight_only=True,
        quantity="0.00005",
        side="SELL",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert result.provider_error == "insufficient USDC allowance for CoW VaultRelayer"


def test_cowswap_buy_preflight_uses_quote_buy_for_spend_amount(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-buy-funded",
        market_id="USDC-WETH",
        mode="mainnet",
        order_type="MARKET",
        preflight_only=True,
        quantity="0.000000001",
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
    assert service._cowswap_runtime._connector.quote_buy_calls == [("USDC", "WETH", "1E-9")]
    assert service.cowswap_evm_reader.balance_calls == [("USDC", "0xowner")]
    assert service.cowswap_evm_reader.allowance_calls == [("USDC", "0xowner", "0xvaultrelayer")]


def test_cowswap_preflight_rejects_unsupported_pair_before_balance_checks(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="cowswap",
        correlation_id="cow-unsupported",
        market_id="UNI-USDC",
        mode="mainnet",
        order_type="MARKET",
        preflight_only=True,
        quantity="1",
        side="SELL",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert "unsupported CowSwap trading pair UNI-USDC" in result.provider_error
    assert service.cowswap_evm_reader.balance_calls == []
    assert service.cowswap_evm_reader.allowance_calls == []


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
    assert EXECUTE_SWAP_CALLS[0]["base_asset"] == "SOL"
    assert EXECUTE_SWAP_CALLS[0]["quote_asset"] == "USDC"
    assert EXECUTE_SWAP_CALLS[0]["amount"] == provider_boundary.Decimal("0.0001")
    assert EXECUTE_SWAP_CALLS[0]["side"] == "SELL"
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


@pytest.mark.parametrize(("raw_status", "expected_status"), [(1, "confirmed"), (True, "submitted")])
def test_confirmed_swap_intent_preserves_provider_economics(
    monkeypatch,
    raw_status,
    expected_status,
):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    monkeypatch.setattr(
        provider_boundary,
        "get_transaction_status_from_response",
        lambda result: "CONFIRMED" if result.get("status") == 1 else "SUBMITTED",
    )
    service = FakeAccountsService()
    monkeypatch.setattr(
        service.gateway_client,
        "execute_swap",
        _async_return(
            {
                "signature": "tx-001",
                "status": raw_status,
                "data": {
                    "tokenIn": "SOL",
                    "tokenOut": "USDC",
                    "amountIn": "0.001",
                    "amountOut": "0.147",
                    "feeAsset": "SOL",
                    "fee": "0.000005",
                },
                "executedAt": "2026-07-13T12:30:00+00:00",
            },
        ),
    )
    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="swap",
        connector_name="jupiter",
        correlation_id="swap-confirmed-001",
        market_id="SOL-USDC",
        mode="mainnet",
        quantity="0.001",
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
        provider_boundary.submit_provider_intent(body, _authorized_request(), service),
    )

    common = {
        "status": expected_status,
        "correlation_id": "swap-confirmed-001",
        "external_order_id": "tx-001",
        "submitted_quantity": provider_boundary.Decimal("0.001"),
        "submitted_notional": provider_boundary.Decimal("0.001"),
        "provider_status": str(raw_status),
    }
    if raw_status is True:
        assert result.model_dump(exclude_none=True) == common
        return
    assert result.model_dump(exclude_none=True) == {
        **common,
        "external_transaction_id": "tx-001",
        "sent_asset": "SOL",
        "sent_quantity": provider_boundary.Decimal("0.001"),
        "received_asset": "USDC",
        "received_quantity": provider_boundary.Decimal("0.147"),
        "fee_asset": "SOL",
        "fee_amount": provider_boundary.Decimal("0.000005"),
        "executed_at": provider_boundary.datetime.fromisoformat("2026-07-13T12:30:00+00:00"),
    }


@pytest.mark.parametrize(
    ("status", "data"),
    [
        ("SUBMITTED", {"tokenIn": "SOL", "tokenOut": "USDC", "amountIn": 1, "amountOut": 2}),
        ("CONFIRMED", None),
        ("CONFIRMED", {"tokenIn": "SOL", "tokenOut": "USDC", "amountIn": 1}),
        ("CONFIRMED", {"tokenIn": "SOL", "tokenOut": "USDC", "amountIn": "nan", "amountOut": 2}),
        ("CONFIRMED", {"tokenIn": {"symbol": "SOL"}, "tokenOut": "USDC", "amountIn": 1, "amountOut": 2}),
        ("CONFIRMED", {"tokenIn": "   ", "tokenOut": "USDC", "amountIn": 1, "amountOut": 2}),
        ("CONFIRMED", {"tokenIn": "SOL", "tokenOut": "USDC", "amountIn": 1, "amountOut": 2, "fee": "bad", "feeAsset": "SOL"}),
        ("CONFIRMED", {"tokenIn": "SOL", "tokenOut": "USDC", "amountIn": 1, "amountOut": 2, "fee": 1}),
    ],
)
def test_swap_economics_omits_non_terminal_or_incomplete_truth(status, data):
    provider_boundary = _provider_boundary_module()

    assert provider_boundary._confirmed_swap_economics(
        {"status": 1, "data": data},
        provider_status=status,
        tx_hash="tx-001",
    ) == {}


def test_swap_economics_rejects_boolean_confirmation():
    provider_boundary = _provider_boundary_module()

    assert provider_boundary._confirmed_swap_economics(
        {
            "status": True,
            "data": {"tokenIn": "SOL", "tokenOut": "USDC", "amountIn": 1, "amountOut": 2},
        },
        provider_status="CONFIRMED",
        tx_hash="tx-001",
    ) == {}


def test_gateway_market_order_provider_intent_executes_as_authorized_swap(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    LIVE_GATE_CALLS.clear()
    EXECUTE_SWAP_CALLS.clear()
    SET_DEFAULT_WALLET_CALLS.clear()
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="jupiter",
        correlation_id="order-jupiter-swap-001",
        market_id="SOL-USDC",
        mode="mainnet",
        order_type="MARKET",
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
    assert len(LIVE_GATE_CALLS) == 1
    assert EXECUTE_SWAP_CALLS[0]["connector"] == "jupiter"
    assert EXECUTE_SWAP_CALLS[0]["network"] == "mainnet-beta"
    assert EXECUTE_SWAP_CALLS[0]["marlin_provider_intent_authorized"] is True
    assert EXECUTE_SWAP_CALLS[0]["live_action_authorization"]["scope"] == "provider_intent"
    assert EXECUTE_SWAP_CALLS[0]["live_action_authorization"]["action"] == "gateway_swap"


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
    assert result.submitted_quantity == provider_boundary.Decimal("0.0001")
    assert result.submitted_notional == provider_boundary.Decimal("0.0001")
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


def test_base_swap_provider_intent_buy_executes_as_gateway_sell(monkeypatch):
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
        correlation_id="swap-base-buy-001",
        market_id="AERO-USDC",
        mode="mainnet",
        quantity="0.1",
        notional="10",
        risk_metadata={"network": "ethereum-base"},
        side="BUY",
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
    assert LIVE_GATE_CALLS[0]["expected_instrument"] == "AERO-USDC"
    assert LIVE_GATE_CALLS[0]["expected_notional"] == provider_boundary.Decimal("10")
    assert EXECUTE_SWAP_CALLS[0]["base_asset"] == "USDC"
    assert EXECUTE_SWAP_CALLS[0]["quote_asset"] == "AERO"
    assert EXECUTE_SWAP_CALLS[0]["amount"] == provider_boundary.Decimal("10")
    assert EXECUTE_SWAP_CALLS[0]["side"] == "SELL"


def test_buy_swap_provider_intent_reports_quote_spend_notional(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    LIVE_GATE_CALLS.clear()
    EXECUTE_SWAP_CALLS.clear()
    SET_DEFAULT_WALLET_CALLS.clear()
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    monkeypatch.setattr(
        service.gateway_client,
        "execute_swap",
        _async_return({"signature": "tx-buy-001", "status": "SUBMITTED"}),
    )

    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="swap",
        connector_name="aerodrome",
        correlation_id="swap-base-buy-submitted-001",
        market_id="AERO-USDC",
        mode="mainnet",
        quantity="0.1",
        notional="10",
        risk_metadata={"network": "ethereum-base"},
        side="BUY",
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

    assert result.status == "submitted"
    assert result.submitted_quantity == provider_boundary.Decimal("0.1")
    assert result.submitted_notional == provider_boundary.Decimal("10")


@pytest.mark.parametrize("notional", [None, "0", "-1"])
def test_buy_swap_rejects_missing_or_invalid_notional_before_provider_mutation(
    monkeypatch,
    notional,
):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    LIVE_GATE_CALLS.clear()
    EXECUTE_SWAP_CALLS.clear()
    SET_DEFAULT_WALLET_CALLS.clear()
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    body_kwargs = {
        "account_name": "master_account",
        "action": "swap",
        "connector_name": "aerodrome",
        "correlation_id": "swap-base-buy-invalid-notional-001",
        "market_id": "AERO-USDC",
        "mode": "mainnet",
        "quantity": "0.1",
        "risk_metadata": {"network": "ethereum-base"},
        "side": "BUY",
        "wallet_identity": {
            "address": "0x1111111111111111111111111111111111111111",
            "chain": "ethereum",
            "network": "ethereum-base",
            "wallet_ref": "base:mainnet:evm_gateway",
        },
    }
    if notional is not None:
        body_kwargs["notional"] = notional
    body = provider_boundary.ProviderIntentRequest(**body_kwargs)

    result = asyncio.run(
        provider_boundary.submit_provider_intent(
            body,
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "rejected"
    assert result.provider_error == "BUY swap requires a positive finite notional"
    assert LIVE_GATE_CALLS == []
    assert SET_DEFAULT_WALLET_CALLS == []
    assert EXECUTE_SWAP_CALLS == []


def test_reduce_order_refreshes_position_clips_and_submits_close(monkeypatch):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    monkeypatch.setattr(provider_boundary, "TradeType", {"SELL": "SELL"})
    monkeypatch.setattr(provider_boundary, "OrderType", {"MARKET": "MARKET"})
    monkeypatch.setattr(
        provider_boundary,
        "PositionAction",
        SimpleNamespace(OPEN="OPEN", CLOSE="CLOSE"),
    )
    service = FakeAccountsService()
    service.account_positions = [
        {"trading_pair": "HYPE-USD", "side": "LONG", "amount": "0.64"},
    ]
    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="hyperliquid_perpetual",
        market_id="HYPE-USDC",
        mode="mainnet",
        order_type="MARKET",
        quantity="0.80",
        side="SELL",
        position_effect="reduce",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(body, _authorized_request(), service),
    )

    assert result.status == "submitted"
    assert result.submitted_quantity == provider_boundary.Decimal("0.64")
    assert service.place_trade_calls[0]["amount"] == provider_boundary.Decimal("0.64")
    assert service.place_trade_calls[0]["position_action"] == "CLOSE"


@pytest.mark.parametrize(
    ("positions", "side", "error"),
    [
        ([], "SELL", "no open position"),
        ([{"trading_pair": "HYPE-USD", "side": "LONG", "amount": "0.64"}], "BUY", "reduce side mismatch"),
    ],
)
def test_reduce_order_rejects_non_reducing_position(monkeypatch, positions, side, error):
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    provider_boundary = _provider_boundary_module()
    monkeypatch.setattr(provider_boundary, "TradeType", {side: side})
    monkeypatch.setattr(provider_boundary, "OrderType", {"MARKET": "MARKET"})
    monkeypatch.setattr(
        provider_boundary,
        "PositionAction",
        SimpleNamespace(OPEN="OPEN", CLOSE="CLOSE"),
    )
    service = FakeAccountsService()
    service.account_positions = positions
    body = provider_boundary.ProviderIntentRequest(
        account_name="master_account",
        action="order",
        connector_name="hyperliquid_perpetual",
        market_id="HYPE-USDC",
        mode="mainnet",
        order_type="MARKET",
        quantity="0.64",
        side=side,
        position_effect="reduce",
    )

    result = asyncio.run(
        provider_boundary.submit_provider_intent(body, _authorized_request(), service),
    )

    assert result.status == "rejected"
    assert error in result.provider_error
    assert service.place_trade_calls == []


def test_cowswap_fallback_account_rows_are_not_scoped_when_evm_reader_fails():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["cowswap"] = [
        {"available_units": 100.0, "token": "WETH", "units": 100.0, "value": 200000.0},
        {"available_units": 50000.0, "token": "USDC", "units": 50000.0, "value": 50000.0},
    ]
    service.cowswap_evm_reader = None
    service._cowswap_runtime_dependencies.evm_reader = None
    request, market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="cowswap",
                network="base",
                route_id="cowswap-usdc-weth-base",
                trading_pair="USDC-WETH",
                wallet_ref="base:mainnet:evm_gateway",
            ),
            request,
            service,
        ),
    )

    assert result.status == "issues"
    assert result.portfolio_observed_at_utc is None
    assert "portfolio refresh unavailable" in result.operator_issues
    rows = result.portfolio["master_account"]["cowswap"]
    for row in rows:
        assert "balance_source" not in row, f"unexpected balance_source in {row}"
        assert "network" not in row, f"unexpected network in {row}"
        assert "wallet_ref" not in row, f"unexpected wallet_ref in {row}"


def test_cowswap_provider_snapshot_keeps_cached_rows_stale_when_second_balance_read_fails():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["cowswap"] = [
        {"available_units": 100.0, "token": "WETH", "units": 100.0, "value": 200000.0},
        {"available_units": 50000.0, "token": "USDC", "units": 50000.0, "value": 50000.0},
    ]

    def balance_of(token, owner):
        service.cowswap_evm_reader.balance_calls.append((token.symbol, owner))
        if token.symbol == "USDC":
            raise RuntimeError("second token balance unavailable")
        return service.cowswap_evm_reader.balances[token.symbol]

    service.cowswap_evm_reader.balance_of = balance_of
    request, _market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="cowswap",
                network="base",
                route_id="cowswap-weth-usdc-base",
                trading_pair="WETH-USDC",
                wallet_ref="base:mainnet:evm_gateway",
            ),
            request,
            service,
        ),
    )

    assert result.status == "issues"
    assert result.portfolio_observed_at_utc is None
    assert "portfolio refresh unavailable" in result.operator_issues
    assert service.cowswap_evm_reader.balance_calls == [
        ("WETH", "0xowner"),
        ("USDC", "0xowner"),
    ]
    rows = result.portfolio["master_account"]["cowswap"]
    assert [(row["token"], row["units"]) for row in rows] == [
        ("WETH", 100.0),
        ("USDC", 50000.0),
    ]
    for row in rows:
        assert "balance_source" not in row, f"unexpected balance_source in {row}"
        assert "network" not in row, f"unexpected network in {row}"
        assert "wallet_ref" not in row, f"unexpected wallet_ref in {row}"


def test_cowswap_fallback_account_rows_are_not_scoped_when_signer_mismatch():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["cowswap"] = []
    request, _market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="cowswap",
                network="ethereum-mainnet",
                route_id="cowswap-eth-usdc-eth",
                trading_pair="USDC-WETH",
                wallet_ref="ethereum:mainnet:evm_gateway",
            ),
            request,
            service,
        ),
    )

    assert result.status == "available"
    rows = result.portfolio["master_account"]["cowswap"]
    for row in rows:
        assert "balance_source" not in row, f"unexpected balance_source in {row}"
        assert "network" not in row, f"unexpected network in {row}"
        assert "wallet_ref" not in row, f"unexpected wallet_ref in {row}"


def test_cowswap_fallback_account_rows_preserve_stale_metadata_stripped():
    provider_boundary = _provider_boundary_module()
    service = FakeAccountsService()
    service.accounts_state["master_account"]["cowswap"] = [
        {"available_units": 100.0, "token": "WETH", "units": 100.0, "value": 200000.0, "balance_source": "gateway", "network": "base", "wallet_ref": "base:mainnet:evm_gateway"},
        {"available_units": 50000.0, "token": "USDC", "units": 50000.0, "value": 50000.0, "balance_source": "gateway", "network": "base", "wallet_ref": "base:mainnet:evm_gateway"},
    ]
    service.cowswap_evm_reader = None
    service._cowswap_runtime_dependencies.evm_reader = None
    request, market_data_service = _request_with_market_data()

    result = asyncio.run(
        provider_boundary.provider_snapshot(
            provider_boundary.ProviderSnapshotRequest(
                account_name="master_account",
                connector_name="cowswap",
                network="base",
                route_id="cowswap-usdc-weth-base",
                trading_pair="USDC-WETH",
                wallet_ref="base:mainnet:evm_gateway",
            ),
            request,
            service,
        ),
    )

    assert result.status == "issues"
    assert result.portfolio_observed_at_utc is None
    assert "portfolio refresh unavailable" in result.operator_issues
    rows = result.portfolio["master_account"]["cowswap"]
    for row in rows:
        assert "balance_source" not in row, f"unexpected balance_source in {row}"
        assert "network" not in row, f"unexpected network in {row}"
        assert "wallet_ref" not in row, f"unexpected wallet_ref in {row}"
