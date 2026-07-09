import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_INTENT_TOKEN = "test-provider-intent-token"
STUBBED_MODULES = (
    "deps",
    "fastapi",
    "models",
    "services.accounts_service",
)


@pytest.fixture(autouse=True)
def _restore_stubbed_modules():
    previous = {name: sys.modules.get(name) for name in STUBBED_MODULES}
    yield
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _install_provider_treasury_stubs():
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
    fastapi.Request = object
    sys.modules["fastapi"] = fastapi

    deps = types.ModuleType("deps")
    deps.get_accounts_service = lambda: None
    sys.modules["deps"] = deps

    models = types.ModuleType("models")
    models.__path__ = [str(ROOT / "models")]
    sys.modules["models"] = models

    accounts_service = types.ModuleType("services.accounts_service")
    accounts_service.AccountsService = object
    sys.modules["services.accounts_service"] = accounts_service


def _provider_treasury_module():
    _install_provider_treasury_stubs()
    spec = importlib.util.spec_from_file_location(
        "provider_treasury_under_test",
        ROOT / "routers" / "provider_treasury.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _authorized_request():
    return types.SimpleNamespace(headers={"x-marlin-provider-intent-token": PROVIDER_INTENT_TOKEN})


class FakeGatewayClient:
    def __init__(self):
        self.build_calls = []
        self.execute_calls = []
        self.status_calls = []
        self.wallet_calls = []
        self.execute_delay = 0

    async def ping(self):
        return True

    async def set_marlin_default_wallet(self, **kwargs):
        self.wallet_calls.append(kwargs)
        return {"status": "default_set"}

    async def build_treasury_rebalance(self, **kwargs):
        self.build_calls.append(kwargs)
        result = {
            "idempotencyKey": kwargs["idempotency_key"],
            "provider": kwargs["provider"],
        }
        if kwargs.get("destination_network") == "solana":
            result["destinationNetwork"] = "mainnet-beta"
        return result

    async def execute_treasury_rebalance(self, **kwargs):
        if self.execute_delay:
            await asyncio.sleep(self.execute_delay)
        self.execute_calls.append(kwargs)
        return {
            "signature": "0xabc",
            "status": 0,
        }

    async def get_treasury_rebalance(self, rebalance_id):
        self.status_calls.append(rebalance_id)
        return {
            "id": rebalance_id,
            "status": "confirmed",
            "transactionHash": "0xstatus",
            "approval_transaction_hash": "0xstatus-approve",
            "burnTransactionHash": "0xstatus-burn",
            "finalizeTransactionHash": "0xstatus-finalize",
            "provider_status": "complete",
            "providerError": "provider warning",
            "metadata": {"phase": "complete"},
        }


class FakeAccountsService:
    def __init__(
        self,
        *,
        address="0x1111111111111111111111111111111111111111",
        arbitrum_address=None,
        network_addresses=None,
        unsupported_contexts=None,
        wallet_ref="arbitrum:mainnet:evm_gateway",
    ):
        self.gateway_client = FakeGatewayClient()
        self.address = address
        self.arbitrum_address = arbitrum_address or address
        self.network_addresses = network_addresses or {}
        self.unsupported_contexts = unsupported_contexts or set()
        self.wallet_ref = wallet_ref

    def _marlin_gateway_wallet_identity(self, *, chain, network):
        if self.address is None:
            return None
        if (chain, network) in self.unsupported_contexts:
            return None
        wallet_ref = self.wallet_ref
        if wallet_ref == "auto":
            wallet_ref_network = {
                "arbitrum-mainnet": "arbitrum",
                "avalanche": "avalanche",
                "base": "base",
                "bsc": "bsc",
                "codex": "codex",
                "cronos": "cronos",
                "edge": "edge",
                "hyperevm": "hyperevm",
                "ink": "ink",
                "injective": "injective",
                "linea": "linea",
                "mainnet": "mainnet",
                "monad": "monad",
                "morph": "morph",
                "optimism": "optimism",
                "pharos": "pharos",
                "plume": "plume",
                "polygon": "polygon",
                "sei": "sei",
                "sonic": "sonic",
                "solana": "solana",
                "unichain": "unichain",
                "world-chain": "world-chain",
                "xrpl": "xrpl",
                "xdc": "xdc",
            }.get(network, network)
            wallet_ref = (
                "solana:mainnet-beta:solana_gateway"
                if chain == "solana"
                else "xrpl:mainnet:xrpl_gateway"
                if chain == "xrpl"
                else f"{wallet_ref_network}:mainnet:evm_gateway"
            )
        address = self.network_addresses.get(
            network,
            self.arbitrum_address if network == "arbitrum-mainnet" else self.address,
        )
        return {
            "address": address,
            "chain": chain,
            "network": network,
            "wallet_ref": wallet_ref,
        }


def _evm_wallet_ref(network: str) -> str:
    wallet_ref_network = "arbitrum" if network == "arbitrum-mainnet" else network
    return f"{wallet_ref_network}:mainnet:evm_gateway"


def _hyperliquid_bridge2_request(module, **overrides):
    data = {
        "account_name": "master_account",
        "source_venue": "gateway",
        "source_network": "arbitrum-mainnet",
        "source_asset": "USDC",
        "destination_venue": "hyperliquid",
        "destination_network": "mainnet",
        "destination_asset": "USDC",
        "destination_account": "0x1111111111111111111111111111111111111111",
        "amount": "25.5",
        "route": "hyperliquid_bridge2",
        "idempotency_key": "rebalance-idem-001",
    }
    data.update(overrides)
    return module.ProviderTreasuryRebalanceRequest(**data)


def _squid_router_request(module, **overrides):
    data = {
        "account_name": "master_account",
        "source_venue": "gateway",
        "source_network": "base-mainnet",
        "source_asset": "ETH",
        "destination_venue": "gateway",
        "destination_network": "arbitrum-mainnet",
        "destination_asset": "ETH",
        "destination_account": "0x2222222222222222222222222222222222222222",
        "amount": "0.25",
        "route": "squid_router",
        "idempotency_key": "squid-rebalance-idem-001",
    }
    data.update(overrides)
    return module.ProviderTreasuryRebalanceRequest(**data)


def _same_chain_swap_request(module, **overrides):
    data = {
        "account_name": "master_account",
        "source_venue": "gateway",
        "source_network": "arbitrum-mainnet",
        "source_asset": "ETH",
        "source_asset_decimals": 18,
        "destination_venue": "gateway",
        "destination_network": "arbitrum-mainnet",
        "destination_asset": "USDC",
        "destination_account": "0x1111111111111111111111111111111111111111",
        "amount": "0.002",
        "route": "provider_treasury_same_chain_swap",
        "idempotency_key": "same-chain-swap-idem-001",
    }
    data.update(overrides)
    return module.ProviderTreasuryRebalanceRequest(**data)


def test_hyperliquid_bridge2_rebalance_build_forwards_semantic_gateway_request(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    result = asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _hyperliquid_bridge2_request(provider_treasury),
            _authorized_request(),
            service,
        ),
    )

    assert result.id == "rebalance-idem-001"
    assert result.status == "built"
    assert service.gateway_client.wallet_calls == [
        {
            "address": "0x1111111111111111111111111111111111111111",
            "chain": "ethereum",
            "network": "arbitrum-mainnet",
            "wallet_ref": "arbitrum:mainnet:evm_gateway",
        }
    ]
    assert service.gateway_client.build_calls == [
        {
            "idempotency_key": "rebalance-idem-001",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "destination_address": "0x1111111111111111111111111111111111111111",
            "amount": "25.5",
            "provider": "hyperliquid_bridge2",
            "source_network": "arbitrum",
            "destination_network": None,
        }
    ]


def test_unsupported_rebalance_route_fails_closed_with_exact_blocker(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.create_provider_treasury_rebalance(
                _hyperliquid_bridge2_request(provider_treasury, route="generic_bridge"),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == provider_treasury.UNSUPPORTED_TREASURY_REBALANCE_ROUTE_BLOCKER
    assert service.gateway_client.build_calls == []


def test_hyperliquid_bridge2_identity_mismatch_fails_closed_with_exact_blocker(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(address="0x2222222222222222222222222222222222222222")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.create_provider_treasury_rebalance(
                _hyperliquid_bridge2_request(provider_treasury),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == provider_treasury.HYPERLIQUID_BRIDGE2_IDENTITY_MISMATCH_BLOCKER
    assert service.gateway_client.build_calls == []


def test_squid_router_rebalance_build_forwards_semantic_gateway_request(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(
        address="0x1111111111111111111111111111111111111111",
        network_addresses={"arbitrum-mainnet": "0x2222222222222222222222222222222222222222"},
        wallet_ref="auto",
    )
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    result = asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _squid_router_request(provider_treasury),
            _authorized_request(),
            service,
        ),
    )

    assert result.id == "squid-rebalance-idem-001"
    assert result.status == "built"
    assert result.route == "squid_router"
    assert service.gateway_client.wallet_calls == [
        {
            "address": "0x1111111111111111111111111111111111111111",
            "chain": "ethereum",
            "network": "base",
            "wallet_ref": "base:mainnet:evm_gateway",
        },
        {
            "address": "0x2222222222222222222222222222222222222222",
            "chain": "ethereum",
            "network": "arbitrum-mainnet",
            "wallet_ref": "arbitrum:mainnet:evm_gateway",
        },
    ]
    assert service.gateway_client.build_calls == [
        {
            "idempotency_key": "squid-rebalance-idem-001",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "destination_address": "0x2222222222222222222222222222222222222222",
            "amount": "0.25",
            "provider": "squid_router",
            "source_chain": "ethereum",
            "source_network": "base",
            "source_asset": provider_treasury.SQUID_NATIVE_TOKEN_ADDRESS,
            "destination_chain": "ethereum",
            "destination_network": "arbitrum",
            "destination_asset": provider_treasury.SQUID_NATIVE_TOKEN_ADDRESS,
            "destination_venue": "gateway",
        }
    ]


def test_squid_router_non_gateway_context_fails_closed_before_gateway_side_effects(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(wallet_ref="auto")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.create_provider_treasury_rebalance(
                _squid_router_request(provider_treasury, destination_venue="binance"),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == provider_treasury.SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER
    assert service.gateway_client.wallet_calls == []
    assert service.gateway_client.build_calls == []


def test_squid_router_base_to_solana_forwards_non_evm_destination_semantics(monkeypatch):
    provider_treasury = _provider_treasury_module()
    solana_destination = "So11111111111111111111111111111111111111112"
    service = FakeAccountsService(
        address="0x1111111111111111111111111111111111111111",
        network_addresses={"mainnet-beta": solana_destination},
        wallet_ref="auto",
    )
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    result = asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _squid_router_request(
                provider_treasury,
                destination_network="solana-mainnet-beta",
                destination_account=solana_destination,
                destination_asset="SOL",
            ),
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "built"
    assert service.gateway_client.wallet_calls == [
        {
            "address": "0x1111111111111111111111111111111111111111",
            "chain": "ethereum",
            "network": "base",
            "wallet_ref": "base:mainnet:evm_gateway",
        },
        {
            "address": solana_destination,
            "chain": "solana",
            "network": "mainnet-beta",
            "wallet_ref": "solana:mainnet-beta:solana_gateway",
        },
    ]
    assert service.gateway_client.build_calls[0] == {
        "idempotency_key": "squid-rebalance-idem-001",
        "wallet_address": "0x1111111111111111111111111111111111111111",
        "destination_address": solana_destination,
        "amount": "0.25",
        "provider": "squid_router",
        "source_chain": "ethereum",
        "source_network": "base",
        "source_asset": provider_treasury.SQUID_NATIVE_TOKEN_ADDRESS,
        "destination_chain": "solana",
        "destination_network": "mainnet-beta",
        "destination_asset": provider_treasury.SQUID_NATIVE_TOKEN_ADDRESS,
        "destination_venue": "gateway",
    }


def test_squid_router_solana_destination_account_is_case_sensitive(monkeypatch):
    provider_treasury = _provider_treasury_module()
    solana_destination = "So11111111111111111111111111111111111111112"
    service = FakeAccountsService(
        address="0x1111111111111111111111111111111111111111",
        network_addresses={"mainnet-beta": solana_destination},
        wallet_ref="auto",
    )
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.create_provider_treasury_rebalance(
                _squid_router_request(
                    provider_treasury,
                    destination_network="solana-mainnet-beta",
                    destination_account=solana_destination.lower(),
                    destination_asset="SOL",
                ),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == provider_treasury.SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER
    assert service.gateway_client.build_calls == []


def test_squid_router_xrpl_destination_fails_closed_without_wallet_identity(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(
        address="0x1111111111111111111111111111111111111111",
        unsupported_contexts={("xrpl", "mainnet")},
        wallet_ref="auto",
    )
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.create_provider_treasury_rebalance(
                _squid_router_request(
                    provider_treasury,
                    destination_network="xrpl-mainnet",
                    destination_account="rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
                    destination_asset="XRP",
                ),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == provider_treasury.SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER
    assert service.gateway_client.wallet_calls == []
    assert service.gateway_client.build_calls == []


def test_squid_router_bsc_source_alias_forwards_when_wallet_identity_exists(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(
        address="0x1111111111111111111111111111111111111111",
        network_addresses={"arbitrum-mainnet": "0x2222222222222222222222222222222222222222"},
        wallet_ref="auto",
    )
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _squid_router_request(provider_treasury, source_network="ethereum-bsc-mainnet", source_asset="BNB"),
            _authorized_request(),
            service,
        ),
    )

    assert service.gateway_client.wallet_calls[0] == {
        "address": "0x1111111111111111111111111111111111111111",
        "chain": "ethereum",
        "network": "bsc",
        "wallet_ref": "bsc:mainnet:evm_gateway",
    }
    assert service.gateway_client.build_calls[0]["source_network"] == "bsc"


def test_squid_router_base_usdc_source_forwards_token_address(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(
        address="0x1111111111111111111111111111111111111111",
        network_addresses={"arbitrum-mainnet": "0x2222222222222222222222222222222222222222"},
        wallet_ref="auto",
    )
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _squid_router_request(provider_treasury, source_asset="USDC"),
            _authorized_request(),
            service,
        ),
    )

    assert service.gateway_client.build_calls[0]["source_network"] == "base"
    assert service.gateway_client.build_calls[0]["source_asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def test_squid_router_arbitrum_usdc_destination_forwards_token_address(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(
        address="0x1111111111111111111111111111111111111111",
        network_addresses={"arbitrum-mainnet": "0x2222222222222222222222222222222222222222"},
        wallet_ref="auto",
    )
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _squid_router_request(provider_treasury, destination_asset="USDC"),
            _authorized_request(),
            service,
        ),
    )

    assert service.gateway_client.build_calls[0]["destination_network"] == "arbitrum"
    assert service.gateway_client.build_calls[0]["destination_asset"] == "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"


def test_same_chain_swap_build_forwards_arbitrum_eth_to_usdc(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    result = asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _same_chain_swap_request(provider_treasury),
            _authorized_request(),
            service,
        ),
    )

    assert result.route == "provider_treasury_same_chain_swap"
    assert service.gateway_client.wallet_calls == [
        {
            "address": "0x1111111111111111111111111111111111111111",
            "chain": "ethereum",
            "network": "arbitrum-mainnet",
            "wallet_ref": "arbitrum:mainnet:evm_gateway",
        },
        {
            "address": "0x1111111111111111111111111111111111111111",
            "chain": "ethereum",
            "network": "arbitrum-mainnet",
            "wallet_ref": "arbitrum:mainnet:evm_gateway",
        },
    ]
    assert service.gateway_client.build_calls == [
        {
            "idempotency_key": "same-chain-swap-idem-001",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "destination_address": "0x1111111111111111111111111111111111111111",
            "amount": "0.002",
            "provider": "provider_treasury_same_chain_swap",
            "source_chain": "ethereum",
            "source_network": "arbitrum",
            "source_asset": "ETH",
            "source_asset_decimals": "18",
            "destination_chain": "ethereum",
            "destination_network": "arbitrum",
            "destination_asset": "USDC",
            "destination_venue": "gateway",
        }
    ]


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("source_network", "base-mainnet"),
        ("destination_network", "base-mainnet"),
        ("source_asset", "SOL"),
        ("destination_asset", "ETH"),
        ("destination_venue", "hyperliquid"),
    ],
)
def test_same_chain_swap_rejects_non_arbitrum_eth_to_usdc(override, value, monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.create_provider_treasury_rebalance(
                _same_chain_swap_request(provider_treasury, **{override: value}),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == provider_treasury.SAME_CHAIN_SWAP_BLOCKER
    assert service.gateway_client.build_calls == []


def test_squid_router_wrong_native_alias_for_source_network_fails_closed(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(wallet_ref="auto")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.create_provider_treasury_rebalance(
                _squid_router_request(provider_treasury, source_asset="BNB"),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == provider_treasury.SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER
    assert service.gateway_client.wallet_calls == []
    assert service.gateway_client.build_calls == []


def test_squid_router_bsc_source_alias_fails_closed_without_wallet_identity(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(
        unsupported_contexts={("ethereum", "bsc")},
        wallet_ref="auto",
    )
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.create_provider_treasury_rebalance(
                _squid_router_request(provider_treasury, source_network="bsc-mainnet"),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == provider_treasury.SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER
    assert service.gateway_client.wallet_calls == []
    assert service.gateway_client.build_calls == []


def test_squid_router_rejects_non_evm_source_before_gateway_side_effects(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(wallet_ref="auto")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.create_provider_treasury_rebalance(
                _squid_router_request(provider_treasury, source_network="solana-mainnet-beta"),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == provider_treasury.SQUID_PROVIDER_OR_EXTERNAL_TREASURY_BLOCKER
    assert service.gateway_client.wallet_calls == []
    assert service.gateway_client.build_calls == []


def test_execute_and_status_forward_to_gateway_treasury_rebalance_endpoints(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _hyperliquid_bridge2_request(provider_treasury),
            _authorized_request(),
            service,
        ),
    )

    execute_result = asyncio.run(
        provider_treasury.execute_provider_treasury_rebalance(
            "rebalance-idem-001",
            provider_treasury.ProviderTreasuryRebalanceExecuteRequest(
                account_name="master_account",
                idempotency_key="execute-idem-001",
            ),
            _authorized_request(),
            service,
        ),
    )
    status_result = asyncio.run(
        provider_treasury.get_provider_treasury_rebalance(
            "rebalance-123",
            service,
        ),
    )

    assert execute_result.status == "confirmed"
    assert execute_result.transaction_hash == "0xstatus"
    assert execute_result.approval_transaction_hash == "0xstatus-approve"
    assert execute_result.burn_transaction_hash == "0xstatus-burn"
    assert execute_result.finalize_transaction_hash == "0xstatus-finalize"
    assert execute_result.provider_status == "complete"
    assert execute_result.metadata == {"phase": "complete"}
    assert status_result.status == "confirmed"
    assert status_result.transaction_hash == "0xstatus"
    assert status_result.approval_transaction_hash == "0xstatus-approve"
    assert status_result.burn_transaction_hash == "0xstatus-burn"
    assert status_result.finalize_transaction_hash == "0xstatus-finalize"
    assert status_result.provider_status == "complete"
    assert status_result.provider_error == "provider warning"
    assert status_result.metadata == {"phase": "complete"}
    assert service.gateway_client.execute_calls == [
        {
            "idempotency_key": "rebalance-idem-001",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "destination_address": "0x1111111111111111111111111111111111111111",
            "amount": "25.5",
            "provider": "hyperliquid_bridge2",
            "source_network": "arbitrum",
            "destination_network": None,
            "live_action_authorization": {
                "action": "gateway_rebalance",
                "connector_id": "hyperliquid",
                "destination_address": "0x1111111111111111111111111111111111111111",
                "destination_network": "",
                "network": "arbitrum",
                "notional": "25.5",
                "scope": "provider_treasury",
                "source": "marlin",
                "wallet_address": "0x1111111111111111111111111111111111111111",
            },
            "marlin_provider_intent_authorized": True,
        }
    ]
    assert service.gateway_client.status_calls == ["rebalance-idem-001", "rebalance-123"]


def test_execute_surfaces_gateway_rebalance_error_before_status_poll(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _hyperliquid_bridge2_request(provider_treasury),
            _authorized_request(),
            service,
        ),
    )

    async def execute_error(**kwargs):
        service.gateway_client.execute_calls.append(kwargs)
        return {"error": "Squid route lookup failed with HTTP 429", "status": 500}

    service.gateway_client.execute_treasury_rebalance = execute_error

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.execute_provider_treasury_rebalance(
                "rebalance-idem-001",
                provider_treasury.ProviderTreasuryRebalanceExecuteRequest(),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 502
    assert exc.value.detail == "Squid route lookup failed with HTTP 429"
    assert service.gateway_client.status_calls == []


def test_squid_router_execute_uses_treasury_authorization(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(
        address="0x1111111111111111111111111111111111111111",
        network_addresses={"arbitrum-mainnet": "0x2222222222222222222222222222222222222222"},
        wallet_ref="auto",
    )
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _squid_router_request(provider_treasury),
            _authorized_request(),
            service,
        ),
    )

    asyncio.run(
        provider_treasury.execute_provider_treasury_rebalance(
            "squid-rebalance-idem-001",
            provider_treasury.ProviderTreasuryRebalanceExecuteRequest(),
            _authorized_request(),
            service,
        ),
    )

    assert service.gateway_client.execute_calls == [
        {
            "idempotency_key": "squid-rebalance-idem-001",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "destination_address": "0x2222222222222222222222222222222222222222",
            "amount": "0.25",
            "provider": "squid_router",
            "source_chain": "ethereum",
            "source_network": "base",
            "source_asset": provider_treasury.SQUID_NATIVE_TOKEN_ADDRESS,
            "destination_chain": "ethereum",
            "destination_network": "arbitrum",
            "destination_asset": provider_treasury.SQUID_NATIVE_TOKEN_ADDRESS,
            "destination_venue": "gateway",
            "live_action_authorization": {
                "action": "gateway_rebalance",
                "connector_id": "treasury",
                "destination_address": "0x2222222222222222222222222222222222222222",
                "destination_network": "arbitrum",
                "network": "base",
                "notional": "0.25",
                "scope": "provider_treasury",
                "source": "marlin",
                "wallet_address": "0x1111111111111111111111111111111111111111",
            },
            "marlin_provider_intent_authorized": True,
        }
    ]


def test_same_chain_swap_execute_uses_treasury_authorization(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _same_chain_swap_request(provider_treasury),
            _authorized_request(),
            service,
        ),
    )

    asyncio.run(
        provider_treasury.execute_provider_treasury_rebalance(
            "same-chain-swap-idem-001",
            provider_treasury.ProviderTreasuryRebalanceExecuteRequest(),
            _authorized_request(),
            service,
        ),
    )

    assert service.gateway_client.execute_calls == [
        {
            "idempotency_key": "same-chain-swap-idem-001",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "destination_address": "0x1111111111111111111111111111111111111111",
            "amount": "0.002",
            "provider": "provider_treasury_same_chain_swap",
            "source_chain": "ethereum",
            "source_network": "arbitrum",
            "source_asset": "ETH",
            "source_asset_decimals": "18",
            "destination_chain": "ethereum",
            "destination_network": "arbitrum",
            "destination_asset": "USDC",
            "destination_venue": "gateway",
            "live_action_authorization": {
                "action": "gateway_rebalance",
                "connector_id": "treasury",
                "destination_address": "0x1111111111111111111111111111111111111111",
                "destination_network": "arbitrum",
                "network": "arbitrum",
                "notional": "0.002",
                "scope": "provider_treasury",
                "source": "marlin",
                "wallet_address": "0x1111111111111111111111111111111111111111",
            },
            "marlin_provider_intent_authorized": True,
        }
    ]


def test_gateway_client_squid_rebalance_payload_preserves_request_semantics(monkeypatch):
    provider_treasury = _provider_treasury_module()
    from services.gateway_client import GatewayClient

    captured = []

    async def fake_request(self, method, path, params=None, json=None, headers=None):
        captured.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "json": json,
                "headers": headers,
            }
        )
        return {"idempotencyKey": json["idempotencyKey"], "status": "built"}

    monkeypatch.setattr(GatewayClient, "_request", fake_request)
    client = GatewayClient()

    result = asyncio.run(
        client.build_treasury_rebalance(
            idempotency_key="squid-rebalance-idem-001",
            wallet_address="0x1111111111111111111111111111111111111111",
            destination_address="0x2222222222222222222222222222222222222222",
            amount="0.25",
            provider="squid_router",
            source_chain="ethereum",
            source_network="base",
            source_asset=provider_treasury.SQUID_NATIVE_TOKEN_ADDRESS,
            destination_chain="ethereum",
            destination_network="arbitrum",
            destination_asset=provider_treasury.SQUID_NATIVE_TOKEN_ADDRESS,
            destination_venue="gateway",
        )
    )

    assert result == {"idempotencyKey": "squid-rebalance-idem-001", "status": "built"}
    assert captured == [
        {
            "method": "POST",
            "path": "bridge/rebalance/build",
            "params": None,
            "json": {
                "provider": "squid_router",
                "idempotencyKey": "squid-rebalance-idem-001",
                "mode": "mainnet",
                "sourceChain": "ethereum",
                "sourceNetwork": "base",
                "sourceAsset": provider_treasury.SQUID_NATIVE_TOKEN_ADDRESS,
                "destinationChain": "ethereum",
                "destinationNetwork": "arbitrum",
                "destinationAsset": provider_treasury.SQUID_NATIVE_TOKEN_ADDRESS,
                "destinationVenue": "gateway",
                "walletAddress": "0x1111111111111111111111111111111111111111",
                "destinationAddress": "0x2222222222222222222222222222222222222222",
                "amount": "0.25",
            },
            "headers": None,
        }
    ]


def test_execute_rebalance_can_resume_stateless_gateway_owned_request(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    result = asyncio.run(
        provider_treasury.execute_provider_treasury_rebalance(
            "gateway-owned-rebalance-001",
            provider_treasury.ProviderTreasuryRebalanceExecuteRequest(),
            _authorized_request(),
            service,
        )
    )

    assert result.status == "confirmed"
    assert result.transaction_hash == "0xstatus"
    assert service.gateway_client.status_calls == ["gateway-owned-rebalance-001"]
    assert service.gateway_client.execute_calls == []


def test_rebalance_response_scrubs_provider_internal_metadata():
    provider_treasury = _provider_treasury_module()

    result = provider_treasury._rebalance_response(
        {
            "id": "rebalance-idem-001",
            "status": "confirmed",
            "metadata": {
                "phase": "complete",
                "txCalldata": "0xcalldata",
                "attestationBytes": "0xattestation",
                "privateKey": "secret",
                "mnemonic": "secret phrase",
                "route": {
                    "transactionRequest": {
                        "data": "0xdeadbeef",
                        "target": "0x00000000000000000000000000000000000000cc",
                        "value": "0",
                    }
                },
            },
        }
    )

    assert result.metadata == {"phase": "complete"}


def test_rebalance_response_redacts_gateway_error_details():
    provider_treasury = _provider_treasury_module()

    with pytest.raises(provider_treasury.HTTPException) as exc:
        provider_treasury._rebalance_response(
            {
                "error": "provider failed mnemonic abandon privateKey=abc token secret bearer xyz",
                "status": 502,
            }
        )

    assert "abandon" not in exc.value.detail
    assert "abc" not in exc.value.detail
    assert "secret" not in exc.value.detail
    assert "[redacted]" in exc.value.detail


def test_execute_rebalance_is_not_rebroadcast_with_new_execute_idempotency(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _hyperliquid_bridge2_request(provider_treasury),
            _authorized_request(),
            service,
        ),
    )
    asyncio.run(
        provider_treasury.execute_provider_treasury_rebalance(
            "rebalance-idem-001",
            provider_treasury.ProviderTreasuryRebalanceExecuteRequest(idempotency_key="first-execute"),
            _authorized_request(),
            service,
        ),
    )

    result = asyncio.run(
        provider_treasury.execute_provider_treasury_rebalance(
            "rebalance-idem-001",
            provider_treasury.ProviderTreasuryRebalanceExecuteRequest(idempotency_key="second-execute"),
            _authorized_request(),
            service,
        )
    )

    assert result.status == "confirmed"
    assert result.transaction_hash == "0xstatus"
    assert result.approval_transaction_hash == "0xstatus-approve"
    assert result.burn_transaction_hash == "0xstatus-burn"
    assert result.finalize_transaction_hash == "0xstatus-finalize"
    assert result.provider_status == "complete"
    assert service.gateway_client.status_calls == ["rebalance-idem-001", "rebalance-idem-001"]
    assert service.gateway_client.execute_calls == [
        {
            "idempotency_key": "rebalance-idem-001",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "destination_address": "0x1111111111111111111111111111111111111111",
            "amount": "25.5",
            "provider": "hyperliquid_bridge2",
            "source_network": "arbitrum",
            "destination_network": None,
            "live_action_authorization": {
                "action": "gateway_rebalance",
                "connector_id": "hyperliquid",
                "destination_address": "0x1111111111111111111111111111111111111111",
                "destination_network": "",
                "network": "arbitrum",
                "notional": "25.5",
                "scope": "provider_treasury",
                "source": "marlin",
                "wallet_address": "0x1111111111111111111111111111111111111111",
            },
            "marlin_provider_intent_authorized": True,
        }
    ]


def test_concurrent_execute_rebalance_marks_pending_before_gateway_submit(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService()
    service.gateway_client.execute_delay = 0.01
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _hyperliquid_bridge2_request(provider_treasury),
            _authorized_request(),
            service,
        ),
    )

    async def execute_once():
        return await provider_treasury.execute_provider_treasury_rebalance(
            "rebalance-idem-001",
            provider_treasury.ProviderTreasuryRebalanceExecuteRequest(),
            _authorized_request(),
            service,
        )

    async def execute_pair():
        return await asyncio.gather(execute_once(), execute_once(), return_exceptions=True)

    results = asyncio.run(execute_pair())

    successes = [result for result in results if not isinstance(result, Exception)]
    assert len(successes) == 2
    assert {result.status for result in successes} == {"confirmed"}
    assert len(service.gateway_client.execute_calls) == 1
    assert service.gateway_client.status_calls == ["rebalance-idem-001", "rebalance-idem-001"]


@pytest.mark.parametrize(
    ("route", "source_network", "destination_network", "gateway_source", "gateway_destination"),
    [
        ("cctp_base_arbitrum_usdc", "base", "arbitrum-mainnet", "base", "arbitrum"),
        ("cctp_usdc", "ethereum-mainnet", "base-mainnet", "mainnet", "base"),
        ("cctp_usdc", "avalanche-mainnet", "optimism-mainnet", "avalanche", "optimism"),
        ("cctp_usdc", "polygon-mainnet", "ethereum-mainnet", "polygon", "mainnet"),
        ("cctp_usdc", "linea-mainnet", "unichain-mainnet", "linea", "unichain"),
        ("cctp_usdc", "edge-mainnet", "sonic-mainnet", "edge", "sonic"),
        ("cctp_usdc", "worldchain-mainnet", "hyperevm-mainnet", "world-chain", "hyperevm"),
        ("cctp_usdc", "base-mainnet", "solana-mainnet-beta", "base", "solana"),
    ],
)
def test_cctp_rebalance_build_forwards_semantic_gateway_request(
    monkeypatch,
    route,
    source_network,
    destination_network,
    gateway_source,
    gateway_destination,
):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(
        address="0x1111111111111111111111111111111111111111",
        arbitrum_address="0x2222222222222222222222222222222222222222",
        network_addresses={
            provider_treasury._wallet_identity_context(gateway_destination)[1]: (
                "So11111111111111111111111111111111111111112"
                if gateway_destination == "solana"
                else "0x2222222222222222222222222222222222222222"
            ),
        },
        wallet_ref="auto",
    )
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    result = asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _hyperliquid_bridge2_request(
                provider_treasury,
                route=route,
                source_network=source_network,
                destination_venue="gateway",
                destination_network=destination_network,
                destination_account=(
                    "So11111111111111111111111111111111111111112"
                    if gateway_destination == "solana"
                    else "0x2222222222222222222222222222222222222222"
                ),
                amount="1.5",
            ),
            _authorized_request(),
            service,
        ),
    )

    assert result.status == "built"
    assert result.route == "cctp_usdc"
    assert service.gateway_client.wallet_calls == [
        {
            "address": "0x1111111111111111111111111111111111111111",
            "chain": "ethereum",
            "network": provider_treasury._wallet_identity_context(gateway_source)[1],
            "wallet_ref": _evm_wallet_ref(provider_treasury._wallet_identity_network(gateway_source)),
        },
        {
            "address": (
                "So11111111111111111111111111111111111111112"
                if gateway_destination == "solana"
                else "0x2222222222222222222222222222222222222222"
            ),
            "chain": provider_treasury._wallet_identity_context(gateway_destination)[0],
            "network": provider_treasury._wallet_identity_context(gateway_destination)[1],
            "wallet_ref": (
                "solana:mainnet-beta:solana_gateway"
                if gateway_destination == "solana"
                else _evm_wallet_ref(provider_treasury._wallet_identity_network(gateway_destination))
            ),
        },
    ]
    assert service.gateway_client.build_calls == [
        {
            "idempotency_key": "rebalance-idem-001",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "destination_address": (
                "So11111111111111111111111111111111111111112"
                if gateway_destination == "solana"
                else "0x2222222222222222222222222222222222222222"
            ),
            "amount": "1.5",
            "provider": "cctp_usdc",
            "source_network": gateway_source,
            "destination_network": gateway_destination,
        }
    ]


@pytest.mark.parametrize(
    "bad_source,bad_destination",
    [
        ("bsc", "base"),
        ("solana", "base"),
        ("base", "starknet"),
        ("base", "stellar"),
    ],
)
def test_cctp_rebalance_rejects_unsupported_gateway_networks(monkeypatch, bad_source, bad_destination):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(wallet_ref="auto")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.create_provider_treasury_rebalance(
                _hyperliquid_bridge2_request(
                    provider_treasury,
                    route="cctp_usdc",
                    source_network=bad_source,
                    destination_venue="gateway",
                    destination_network=bad_destination,
                    destination_account="0x2222222222222222222222222222222222222222",
                    amount="1.5",
                ),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 400
    assert "unsupported treasury rebalance route" in exc.value.detail
    assert service.gateway_client.wallet_calls == []
    assert service.gateway_client.build_calls == []


def test_cctp_rebalance_rejects_same_network_before_wallet_defaults(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(wallet_ref="auto")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.create_provider_treasury_rebalance(
                _hyperliquid_bridge2_request(
                    provider_treasury,
                    route="cctp_usdc",
                    source_network="base",
                    destination_venue="gateway",
                    destination_network="base-mainnet",
                    destination_account="0x2222222222222222222222222222222222222222",
                    amount="1.5",
                ),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "CCTP source and destination networks must differ"
    assert service.gateway_client.wallet_calls == []
    assert service.gateway_client.build_calls == []


@pytest.mark.parametrize(
    ("alias", "gateway_network", "wallet_network"),
    [
        ("arbitrum-one", "arbitrum", "arbitrum-mainnet"),
        ("ethereum-avalanche-mainnet", "avalanche", "avalanche"),
        ("ethereum-base-mainnet", "base", "base"),
        ("ethereum-codex-mainnet", "codex", "codex"),
        ("ethereum-cronos-mainnet", "cronos", "cronos"),
        ("ethereum-edge-mainnet", "edge", "edge"),
        ("ethereum-mainnet", "mainnet", "mainnet"),
        ("ethereum-hyperevm-mainnet", "hyperevm", "hyperevm"),
        ("ethereum-ink-mainnet", "ink", "ink"),
        ("ethereum-injective-mainnet", "injective", "injective"),
        ("ethereum-linea-mainnet", "linea", "linea"),
        ("ethereum-monad-mainnet", "monad", "monad"),
        ("ethereum-morph-mainnet", "morph", "morph"),
        ("ethereum-optimism-mainnet", "optimism", "optimism"),
        ("ethereum-pharos-mainnet", "pharos", "pharos"),
        ("ethereum-plume-mainnet", "plume", "plume"),
        ("polygon-pos", "polygon", "polygon"),
        ("ethereum-sei-mainnet", "sei", "sei"),
        ("ethereum-sonic-mainnet", "sonic", "sonic"),
        ("solana-mainnet-beta", "solana", "mainnet-beta"),
        ("ethereum-unichain-mainnet", "unichain", "unichain"),
        ("worldchain-mainnet", "world-chain", "world-chain"),
        ("ethereum-world-chain-mainnet", "world-chain", "world-chain"),
        ("ethereum-xdc-mainnet", "xdc", "xdc"),
    ],
)
def test_cctp_gateway_network_aliases_map_to_wallet_networks(alias, gateway_network, wallet_network):
    provider_treasury = _provider_treasury_module()

    assert provider_treasury._cctp_gateway_network(alias) == gateway_network
    assert provider_treasury._wallet_identity_context(gateway_network)[1] == wallet_network


def test_cctp_rebalance_response_redacts_sensitive_metadata():
    provider_treasury = _provider_treasury_module()

    response = provider_treasury._rebalance_response(
        {
            "id": "rebalance-idem-001",
            "status": "built",
            "metadata": {
                "phase": "built",
                "wallet_file": "/tmp/wallet.json",
                "api_key": "secret",
                "nested": {
                    "token": "secret",
                    "route": "cctp_usdc",
                },
            },
        },
    )

    assert response.metadata == {"phase": "built", "nested": {"route": "cctp_usdc"}}


def test_cctp_rebalance_response_redacts_provider_error():
    provider_treasury = _provider_treasury_module()

    response = provider_treasury._rebalance_response(
        {
            "id": "rebalance-idem-001",
            "status": "failed",
            "providerError": "failed bearer abc123 token secret-value",
        },
    )

    assert response.provider_error == "failed bearer [redacted] token [redacted]"


def test_cctp_rebalance_execute_authorization_uses_source_and_destination_networks(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(
        address="0x1111111111111111111111111111111111111111",
        arbitrum_address="0x2222222222222222222222222222222222222222",
        network_addresses={"base": "0x2222222222222222222222222222222222222222"},
        wallet_ref="auto",
    )
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _hyperliquid_bridge2_request(
                provider_treasury,
                route="cctp_usdc",
                source_network="ethereum-mainnet",
                destination_venue="gateway",
                destination_network="base-mainnet",
                destination_account="0x2222222222222222222222222222222222222222",
                amount="1.5",
            ),
            _authorized_request(),
            service,
        ),
    )

    asyncio.run(
        provider_treasury.execute_provider_treasury_rebalance(
            "rebalance-idem-001",
            provider_treasury.ProviderTreasuryRebalanceExecuteRequest(),
            _authorized_request(),
            service,
        ),
    )

    assert service.gateway_client.execute_calls == [
        {
            "idempotency_key": "rebalance-idem-001",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "destination_address": "0x2222222222222222222222222222222222222222",
            "amount": "1.5",
            "provider": "cctp_usdc",
            "source_network": "mainnet",
            "destination_network": "base",
            "live_action_authorization": {
                "action": "gateway_rebalance",
                "connector_id": "treasury",
                "destination_address": "0x2222222222222222222222222222222222222222",
                "destination_network": "base",
                "network": "mainnet",
                "notional": "1.5",
                "scope": "provider_treasury",
                "source": "marlin",
                "wallet_address": "0x1111111111111111111111111111111111111111",
            },
            "marlin_provider_intent_authorized": True,
        }
    ]


def test_cctp_rebalance_execute_authorization_uses_gateway_normalized_solana_destination(monkeypatch):
    provider_treasury = _provider_treasury_module()
    service = FakeAccountsService(
        address="0x1111111111111111111111111111111111111111",
        network_addresses={"mainnet-beta": "So11111111111111111111111111111111111111112"},
        wallet_ref="auto",
    )
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        provider_treasury.create_provider_treasury_rebalance(
            _hyperliquid_bridge2_request(
                provider_treasury,
                route="cctp_usdc",
                source_network="base-mainnet",
                destination_venue="gateway",
                destination_network="solana-mainnet-beta",
                destination_account="So11111111111111111111111111111111111111112",
                amount="1.5",
            ),
            _authorized_request(),
            service,
        ),
    )

    asyncio.run(
        provider_treasury.execute_provider_treasury_rebalance(
            "rebalance-idem-001",
            provider_treasury.ProviderTreasuryRebalanceExecuteRequest(),
            _authorized_request(),
            service,
        ),
    )

    assert service.gateway_client.execute_calls[0]["destination_network"] == "mainnet-beta"
    assert service.gateway_client.execute_calls[0]["live_action_authorization"]["destination_network"] == "mainnet-beta"
