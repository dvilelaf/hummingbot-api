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
            "status": "built",
            "provider": "hyperliquid_bridge2",
        }

    async def execute_treasury_rebalance(self, **kwargs):
        if self.execute_delay:
            await asyncio.sleep(self.execute_delay)
        self.execute_calls.append(kwargs)
        return {
            "status": "submitted",
            "transactionHash": "0xabc",
        }

    async def get_treasury_rebalance(self, rebalance_id):
        self.status_calls.append(rebalance_id)
        return {
            "id": rebalance_id,
            "status": "confirmed",
            "transactionHash": "0xabc",
        }


class FakeAccountsService:
    def __init__(
        self,
        *,
        address="0x1111111111111111111111111111111111111111",
        wallet_ref="arbitrum:mainnet:evm_gateway",
    ):
        self.gateway_client = FakeGatewayClient()
        self.address = address
        self.wallet_ref = wallet_ref

    def _marlin_gateway_wallet_identity(self, *, chain, network):
        if self.address is None:
            return None
        return {
            "address": self.address,
            "chain": chain,
            "network": network,
            "wallet_ref": self.wallet_ref,
        }


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

    assert execute_result.status == "submitted"
    assert execute_result.transaction_hash == "0xabc"
    assert status_result.status == "confirmed"
    assert status_result.transaction_hash == "0xabc"
    assert service.gateway_client.execute_calls == [
        {
            "idempotency_key": "rebalance-idem-001",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "destination_address": "0x1111111111111111111111111111111111111111",
            "amount": "25.5",
            "live_action_authorization": {
                "action": "gateway_rebalance",
                "connector_id": "hyperliquid",
                "network": "arbitrum",
                "notional": "25.5",
                "scope": "provider_treasury",
                "source": "marlin",
                "wallet_address": "0x1111111111111111111111111111111111111111",
            },
            "marlin_provider_intent_authorized": True,
        }
    ]
    assert service.gateway_client.status_calls == ["rebalance-123"]


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

    with pytest.raises(provider_treasury.HTTPException) as exc:
        asyncio.run(
            provider_treasury.execute_provider_treasury_rebalance(
                "rebalance-idem-001",
                provider_treasury.ProviderTreasuryRebalanceExecuteRequest(idempotency_key="second-execute"),
                _authorized_request(),
                service,
            ),
        )

    assert exc.value.status_code == 409
    assert service.gateway_client.execute_calls == [
        {
            "idempotency_key": "rebalance-idem-001",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "destination_address": "0x1111111111111111111111111111111111111111",
            "amount": "25.5",
            "live_action_authorization": {
                "action": "gateway_rebalance",
                "connector_id": "hyperliquid",
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
    failures = [result for result in results if isinstance(result, provider_treasury.HTTPException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].status_code == 409
    assert len(service.gateway_client.execute_calls) == 1
