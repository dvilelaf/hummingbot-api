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
        return {
            "idempotencyKey": kwargs["idempotency_key"],
            "provider": "hyperliquid_bridge2",
        }

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
        wallet_ref="arbitrum:mainnet:evm_gateway",
    ):
        self.gateway_client = FakeGatewayClient()
        self.address = address
        self.arbitrum_address = arbitrum_address or address
        self.network_addresses = network_addresses or {}
        self.wallet_ref = wallet_ref

    def _marlin_gateway_wallet_identity(self, *, chain, network):
        if self.address is None:
            return None
        wallet_ref = self.wallet_ref
        if wallet_ref == "auto":
            wallet_ref_network = {
                "arbitrum-mainnet": "arbitrum",
                "avalanche": "avalanche",
                "base": "base",
                "mainnet": "mainnet",
                "optimism": "optimism",
                "polygon": "polygon",
            }.get(network, network)
            wallet_ref = f"{wallet_ref_network}:mainnet:evm_gateway"
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
            provider_treasury._wallet_identity_network(gateway_destination): "0x2222222222222222222222222222222222222222",
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
                destination_account="0x2222222222222222222222222222222222222222",
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
            "network": provider_treasury._wallet_identity_network(gateway_source),
            "wallet_ref": _evm_wallet_ref(provider_treasury._wallet_identity_network(gateway_source)),
        },
        {
            "address": "0x2222222222222222222222222222222222222222",
            "chain": "ethereum",
            "network": provider_treasury._wallet_identity_network(gateway_destination),
            "wallet_ref": _evm_wallet_ref(provider_treasury._wallet_identity_network(gateway_destination)),
        },
    ]
    assert service.gateway_client.build_calls == [
        {
            "idempotency_key": "rebalance-idem-001",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "destination_address": "0x2222222222222222222222222222222222222222",
            "amount": "1.5",
            "provider": "cctp_usdc",
            "source_network": gateway_source,
            "destination_network": gateway_destination,
        }
    ]


@pytest.mark.parametrize("bad_source,bad_destination", [("bsc", "base"), ("base", "linea")])
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
