import asyncio
import importlib.util
import sys
import types
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.gateway_client import GatewayClient

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_INTENT_TOKEN = "test-provider-intent-token"
STUBBED_MODULES = (
    "database",
    "database.repositories",
    "deps",
    "fastapi",
    "models",
    "services.accounts_service",
    "services.marlin_runtime",
)
_REBALANCE_RECORDS = {}


@pytest.fixture(autouse=True)
def _restore_stubbed_modules():
    _REBALANCE_RECORDS.clear()
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
    deps.get_database_manager = lambda: None
    sys.modules["deps"] = deps

    database = types.ModuleType("database")
    database.__path__ = [str(ROOT / "database")]
    sys.modules["database"] = database
    repositories = types.ModuleType("database.repositories")
    repositories.ProviderTreasuryRebalanceRepository = object
    sys.modules["database.repositories"] = repositories

    models = types.ModuleType("models")
    models.__path__ = [str(ROOT / "models")]
    sys.modules["models"] = models

    accounts_service = types.ModuleType("services.accounts_service")
    accounts_service.AccountsService = object
    sys.modules["services.accounts_service"] = accounts_service

    marlin_runtime = types.ModuleType("services.marlin_runtime")
    marlin_runtime._derive_marlin_credential_values = lambda namespace: None
    sys.modules["services.marlin_runtime"] = marlin_runtime


def _provider_treasury_module():
    _install_provider_treasury_stubs()
    spec = importlib.util.spec_from_file_location(
        "provider_treasury_under_test",
        ROOT / "routers" / "provider_treasury.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ProviderTreasuryRebalanceRepository = FakeProviderTreasuryRebalanceRepository
    return module


def _authorized_request():
    app = types.SimpleNamespace(state=types.SimpleNamespace(db_manager=FakeDatabaseManager()))
    return types.SimpleNamespace(
        app=app,
        headers={"x-marlin-provider-intent-token": PROVIDER_INTENT_TOKEN},
    )


class FakeDatabaseManager:
    @asynccontextmanager
    async def get_session_context(self):
        yield _REBALANCE_RECORDS


class FakeProviderTreasuryRebalanceRepository:
    def __init__(self, records):
        self.records = records

    async def get_rebalance(self, rebalance_id):
        return self.records.get(rebalance_id)

    async def create_built(self, rebalance_id, request_payload, response_payload):
        record = self.records.get(rebalance_id)
        if record is None:
            record = types.SimpleNamespace(
                rebalance_id=rebalance_id,
                status="built",
                request_payload=request_payload,
                response_payload=response_payload,
            )
            self.records[rebalance_id] = record
        return record

    async def claim_for_execution(self, rebalance_id):
        record = self.records.get(rebalance_id)
        if record is None or record.status != "built":
            return None
        record.status = "pending"
        return record

    async def update_status(self, rebalance_id, status, response_payload):
        record = self.records.get(rebalance_id)
        if record is not None:
            if status != "built":
                record.status = status
            record.response_payload = response_payload
        return record


class FakeGatewayClient:
    def __init__(self):
        self.ping_calls = 0
        self.target_calls = []
        self.execute_calls = []
        self.status_calls = []
        self.statuses_at_target = []
        self.wallet_calls = []
        self.statuses_at_execute = []
        self.execute_delay = 0
        self.target_result = {"idempotencyKey": "target-funding-1", "status": "built"}
        self.status_results = []

    async def ping(self):
        self.ping_calls += 1
        return True

    async def set_marlin_default_wallet(self, **kwargs):
        self.wallet_calls.append(kwargs)
        return {"status": "default_set"}

    async def create_treasury_rebalance_target(self, **kwargs):
        record = _REBALANCE_RECORDS.get(kwargs["idempotency_key"])
        self.statuses_at_target.append(None if record is None else record.status)
        self.target_calls.append(kwargs)
        return dict(self.target_result)

    async def execute_treasury_rebalance_target(self, rebalance_id):
        self.statuses_at_execute.append(_REBALANCE_RECORDS[rebalance_id].status)
        if self.execute_delay:
            await asyncio.sleep(self.execute_delay)
        self.execute_calls.append(rebalance_id)
        return {"signature": "0xsubmitted", "status": 1}

    async def get_treasury_rebalance(self, rebalance_id):
        self.status_calls.append(rebalance_id)
        if self.status_results:
            return self.status_results.pop(0)
        return {
            "idempotencyKey": rebalance_id,
            "metadata": {
                "phase": "complete",
                "provider": "squid_router",
                "sourceNetwork": "arbitrum",
            },
            "provider": "squid_router",
            "sourceNetwork": "arbitrum",
            "status": "confirmed",
            "transactionHash": "0xconfirmed",
        }


class FakeAccountsService:
    def __init__(self, identities=None):
        self.gateway_client = FakeGatewayClient()
        self.identity_calls = []
        self.identities = identities if identities is not None else {
            ("ethereum", "base"): {
                "address": "0x00000000000000000000000000000000000000B1",
                "wallet_ref": "base:mainnet:evm_gateway",
            },
            ("ethereum", "arbitrum-mainnet"): {
                "address": "0x00000000000000000000000000000000000000A1",
                "wallet_ref": "arbitrum:mainnet:evm_gateway",
            },
            ("solana", "mainnet-beta"): {
                "address": "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk",
                "wallet_ref": "solana:mainnet-beta:solana_gateway",
            },
        }

    def _marlin_gateway_wallet_identity(self, *, chain, network):
        self.identity_calls.append((chain, network))
        return self.identities.get((chain, network))


def _neutral_request(module, **overrides):
    data = {
        "account_name": "master_account",
        "amount": "6",
        "destination_asset": "USDC",
        "destination_chain": "ethereum",
        "destination_network": "base",
        "destination_wallet_ref": "base:mainnet:evm_gateway",
        "idempotency_key": "target-funding-1",
        "max_cost_bps": 100,
        "route_id": "base-aero",
    }
    data.update(overrides)
    return module.ProviderTreasuryRebalanceRequest(**data)


def test_request_contract_accepts_only_neutral_destination_fields():
    module = _provider_treasury_module()
    body = _neutral_request(module)

    assert set(body.model_dump()) == {
        "account_name",
        "amount",
        "destination_asset",
        "destination_chain",
        "destination_network",
        "destination_wallet_ref",
        "idempotency_key",
        "max_cost_bps",
        "route_id",
    }
    with pytest.raises(ValidationError):
        module.ProviderTreasuryRebalanceRequest(**body.model_dump(), provider="squid_router")
    with pytest.raises(ValidationError):
        module.ProviderTreasuryRebalanceRequest(**body.model_dump(), source_network="arbitrum")
    with pytest.raises(ValidationError):
        module.ProviderTreasuryRebalanceRequest(**body.model_dump(), destination_address="0xcaller")


def test_execute_request_accepts_idempotency_key_and_forbids_extra_fields():
    module = _provider_treasury_module()

    body = module.ProviderTreasuryRebalanceExecuteRequest(
        idempotency_key="target-funding-1",
    )

    assert body.model_dump(exclude_none=True) == {
        "idempotency_key": "target-funding-1",
    }
    with pytest.raises(ValidationError):
        module.ProviderTreasuryRebalanceExecuteRequest()
    with pytest.raises(ValidationError):
        module.ProviderTreasuryRebalanceExecuteRequest(
            idempotency_key="target-funding-1",
            provider="squid_router",
        )


def test_create_derives_destination_identity_and_forwards_only_target(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    result = asyncio.run(
        module.create_provider_treasury_rebalance(
            _neutral_request(module),
            _authorized_request(),
            service,
        )
    )

    assert result.model_dump(exclude_none=True) == {
        "id": "target-funding-1",
        "metadata": {},
        "status": "built",
    }
    assert service.identity_calls == [("ethereum", "base")]
    assert service.gateway_client.wallet_calls == [
        {
            "address": "0x00000000000000000000000000000000000000B1",
            "chain": "ethereum",
            "network": "base",
            "wallet_ref": "base:mainnet:evm_gateway",
        }
    ]
    assert service.gateway_client.target_calls == [
        {
            "amount": "6",
            "destination_address": "0x00000000000000000000000000000000000000B1",
            "destination_asset": "USDC",
            "destination_chain": "ethereum",
            "destination_network": "base",
            "idempotency_key": "target-funding-1",
            "max_cost_bps": "100",
        }
    ]
    assert service.gateway_client.statuses_at_target == ["built"]
    stored = _REBALANCE_RECORDS["target-funding-1"].request_payload
    assert stored == {
        "account_name": "master_account",
        "amount": "6",
        "destination_address": "0x00000000000000000000000000000000000000B1",
        "destination_asset": "USDC",
        "destination_chain": "ethereum",
        "destination_network": "base",
        "destination_wallet_ref": "base:mainnet:evm_gateway",
        "max_cost_bps": "100",
        "route_id": "base-aero",
    }


def test_create_forwards_fractional_max_cost_bps_without_precision_loss(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    asyncio.run(
        module.create_provider_treasury_rebalance(
            _neutral_request(module, max_cost_bps=Decimal("12.375")),
            _authorized_request(),
            service,
        )
    )

    assert service.gateway_client.target_calls[0]["max_cost_bps"] == "12.375"
    assert _REBALANCE_RECORDS["target-funding-1"].request_payload["max_cost_bps"] == "12.375"


def test_hyperliquid_target_validates_logical_identity_and_provisions_arbitrum_wallet(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    hyperliquid_identity_calls = []

    def derive_hyperliquid_credentials(namespace):
        hyperliquid_identity_calls.append(namespace)
        return {
            "hyperliquid_address": "0x00000000000000000000000000000000000000a1",
            "hyperliquid_secret_key": "unused",
        }

    monkeypatch.setattr(module, "_derive_marlin_credential_values", derive_hyperliquid_credentials)

    asyncio.run(
        module.create_provider_treasury_rebalance(
            _neutral_request(
                module,
                destination_chain="hyperliquid",
                destination_network="mainnet",
                destination_wallet_ref="hyperliquid:mainnet:hyperliquid_trader",
                route_id="hl-mainnet",
            ),
            _authorized_request(),
            service,
        )
    )

    assert hyperliquid_identity_calls == ["hyperliquid"]
    assert service.identity_calls == [("ethereum", "arbitrum-mainnet")]
    assert service.gateway_client.wallet_calls == [
        {
            "address": "0x00000000000000000000000000000000000000A1",
            "chain": "ethereum",
            "network": "arbitrum-mainnet",
            "wallet_ref": "arbitrum:mainnet:evm_gateway",
        }
    ]
    assert service.gateway_client.target_calls[0] == {
        "amount": "6",
        "destination_address": "0x00000000000000000000000000000000000000a1",
        "destination_asset": "USDC",
        "destination_chain": "hyperliquid",
        "destination_network": "mainnet",
        "idempotency_key": "target-funding-1",
        "max_cost_bps": "100",
    }
    assert _REBALANCE_RECORDS["target-funding-1"].request_payload["destination_wallet_ref"] == (
        "hyperliquid:mainnet:hyperliquid_trader"
    )


def test_hyperliquid_target_fails_closed_when_logical_and_arbitrum_addresses_differ(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    monkeypatch.setattr(
        module,
        "_derive_marlin_credential_values",
        lambda namespace: {
            "hyperliquid_address": "0x00000000000000000000000000000000000000B2",
            "hyperliquid_secret_key": "unused",
        },
    )

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module.create_provider_treasury_rebalance(
                _neutral_request(
                    module,
                    destination_chain="hyperliquid",
                    destination_network="mainnet",
                    destination_wallet_ref="hyperliquid:mainnet:hyperliquid_trader",
                    route_id="hl-mainnet",
                ),
                _authorized_request(),
                service,
            )
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "destination_wallet_identity_unavailable"
    assert _REBALANCE_RECORDS == {}
    assert service.gateway_client.wallet_calls == []
    assert service.gateway_client.target_calls == []


def test_hyperliquid_target_rejects_arbitrum_ref_as_logical_identity(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    monkeypatch.setattr(
        module,
        "_derive_marlin_credential_values",
        lambda namespace: {
            "hyperliquid_address": "0x00000000000000000000000000000000000000A1",
            "hyperliquid_secret_key": "unused",
        },
    )

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module.create_provider_treasury_rebalance(
                _neutral_request(
                    module,
                    destination_chain="hyperliquid",
                    destination_network="mainnet",
                    destination_wallet_ref="arbitrum:mainnet:evm_gateway",
                    route_id="hl-mainnet",
                ),
                _authorized_request(),
                service,
            )
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "destination_wallet_ref_mismatch"
    assert service.identity_calls == []
    assert service.gateway_client.wallet_calls == []


@pytest.mark.parametrize(
    ("destination_chain", "destination_network", "identity_chain", "identity_network"),
    [
        ("ethereum", "base", "ethereum", "base"),
        ("solana", "mainnet-beta", "solana", "mainnet-beta"),
        ("hyperliquid", "mainnet", "ethereum", "arbitrum-mainnet"),
    ],
)
def test_destination_identity_context_uses_only_logical_target_and_wallet_policy(
    destination_chain,
    destination_network,
    identity_chain,
    identity_network,
):
    module = _provider_treasury_module()

    context = module._destination_identity_context(destination_chain, destination_network)

    assert context == (identity_chain, identity_network)


def test_destination_identity_unavailable_returns_exact_neutral_blocker(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService(identities={})
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module.create_provider_treasury_rebalance(
                _neutral_request(module),
                _authorized_request(),
                service,
            )
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "destination_wallet_identity_unavailable"
    assert service.gateway_client.wallet_calls == []
    assert service.gateway_client.target_calls == []


def test_wallet_ref_mismatch_returns_exact_neutral_blocker(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module.create_provider_treasury_rebalance(
                _neutral_request(module, destination_wallet_ref="operator-wallet"),
                _authorized_request(),
                service,
            )
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "destination_wallet_ref_mismatch"
    assert service.gateway_client.wallet_calls == []
    assert service.gateway_client.target_calls == []


def test_create_requires_exact_marlin_provider_intent_authorization(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    request = _authorized_request()
    request.headers = {}

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module.create_provider_treasury_rebalance(
                _neutral_request(module),
                request,
                service,
            )
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Marlin provider intent token required for mainnet provider treasury rebalances"
    assert service.identity_calls == []
    assert service.gateway_client.wallet_calls == []
    assert service.gateway_client.target_calls == []


def test_gateway_neutral_blocker_and_status_code_are_preserved(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service.gateway_client.target_result = {"error": "insufficient_source_or_gas", "status": 409}
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module.create_provider_treasury_rebalance(
                _neutral_request(module),
                _authorized_request(),
                service,
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "insufficient_source_or_gas"
    assert _REBALANCE_RECORDS["target-funding-1"].status == "built"


def test_same_idempotency_key_with_different_target_fails_before_gateway(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        module.create_provider_treasury_rebalance(
            _neutral_request(module),
            _authorized_request(),
            service,
        )
    )

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module.create_provider_treasury_rebalance(
                _neutral_request(module, amount="7"),
                _authorized_request(),
                service,
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "idempotency_key_conflict"
    assert len(service.gateway_client.wallet_calls) == 1
    assert len(service.gateway_client.target_calls) == 1


def test_execute_claims_durable_record_once_and_forwards_only_id(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service.gateway_client.execute_delay = 0.01
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        module.create_provider_treasury_rebalance(
            _neutral_request(module),
            _authorized_request(),
            service,
        )
    )

    async def execute_twice():
        body = module.ProviderTreasuryRebalanceExecuteRequest(
            idempotency_key="target-funding-1",
        )
        return await asyncio.gather(
            module.execute_provider_treasury_rebalance(
                "target-funding-1",
                body,
                _authorized_request(),
                service,
            ),
            module.execute_provider_treasury_rebalance(
                "target-funding-1",
                body,
                _authorized_request(),
                service,
            ),
        )

    results = asyncio.run(execute_twice())

    assert [result.status for result in results] == ["confirmed", "confirmed"]
    assert service.gateway_client.execute_calls == ["target-funding-1"]
    assert service.gateway_client.statuses_at_execute == ["pending"]
    assert service.gateway_client.status_calls == ["target-funding-1", "target-funding-1"]
    assert _REBALANCE_RECORDS["target-funding-1"].status == "confirmed"


def test_execute_rejects_idempotency_key_mismatch_before_gateway(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module.execute_provider_treasury_rebalance(
                "target-funding-1",
                module.ProviderTreasuryRebalanceExecuteRequest(
                    idempotency_key="different-target",
                ),
                _authorized_request(),
                service,
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "idempotency_key_conflict"
    assert service.gateway_client.ping_calls == 0
    assert service.gateway_client.execute_calls == []
    assert service.gateway_client.status_calls == []


def test_execute_restart_recovers_pending_claim_when_gateway_is_still_built(monkeypatch):
    first_module = _provider_treasury_module()
    first_service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        first_module.create_provider_treasury_rebalance(
            _neutral_request(first_module),
            _authorized_request(),
            first_service,
        )
    )
    _REBALANCE_RECORDS["target-funding-1"].status = "pending"

    recreated_module = _provider_treasury_module()
    recreated_service = FakeAccountsService()
    recreated_service.gateway_client.status_results = [
        {"idempotencyKey": "target-funding-1", "status": "built"},
        {
            "idempotencyKey": "target-funding-1",
            "status": "confirmed",
            "transactionHash": "0xconfirmed",
        },
    ]

    result = asyncio.run(
        recreated_module.execute_provider_treasury_rebalance(
            "target-funding-1",
            recreated_module.ProviderTreasuryRebalanceExecuteRequest(
                idempotency_key="target-funding-1",
            ),
            _authorized_request(),
            recreated_service,
        )
    )

    assert result.status == "confirmed"
    assert recreated_service.gateway_client.status_calls == [
        "target-funding-1",
        "target-funding-1",
    ]
    assert recreated_service.gateway_client.execute_calls == ["target-funding-1"]
    assert recreated_service.gateway_client.statuses_at_execute == ["pending"]
    assert _REBALANCE_RECORDS["target-funding-1"].status == "confirmed"


@pytest.mark.parametrize(
    "gateway_status",
    ["submission_pending", "submission_ambiguous", "submitted", "confirmed", "failed"],
)
def test_execute_pending_claim_never_resubmits_non_built_gateway_status(
    monkeypatch,
    gateway_status,
):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        module.create_provider_treasury_rebalance(
            _neutral_request(module),
            _authorized_request(),
            service,
        )
    )
    _REBALANCE_RECORDS["target-funding-1"].status = "pending"
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": gateway_status,
        }
    ]

    result = asyncio.run(
        module.execute_provider_treasury_rebalance(
            "target-funding-1",
            module.ProviderTreasuryRebalanceExecuteRequest(
                idempotency_key="target-funding-1",
            ),
            _authorized_request(),
            service,
        )
    )

    assert result.status == gateway_status
    assert service.gateway_client.status_calls == ["target-funding-1"]
    assert service.gateway_client.execute_calls == []


@pytest.mark.parametrize("durable_status", ["pending", "confirmed"])
def test_execute_retry_reconciles_ambiguous_or_terminal_record_without_resubmit(
    monkeypatch,
    durable_status,
):
    first_module = _provider_treasury_module()
    first_service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        first_module.create_provider_treasury_rebalance(
            _neutral_request(first_module),
            _authorized_request(),
            first_service,
        )
    )
    _REBALANCE_RECORDS["target-funding-1"].status = durable_status

    recreated_module = _provider_treasury_module()
    recreated_service = FakeAccountsService()
    result = asyncio.run(
        recreated_module.execute_provider_treasury_rebalance(
            "target-funding-1",
            recreated_module.ProviderTreasuryRebalanceExecuteRequest(
                idempotency_key="target-funding-1",
            ),
            _authorized_request(),
            recreated_service,
        )
    )

    assert result.status == "confirmed"
    assert recreated_service.gateway_client.execute_calls == []
    assert recreated_service.gateway_client.status_calls == ["target-funding-1"]
    assert _REBALANCE_RECORDS["target-funding-1"].status == "confirmed"


def test_status_reconciles_durable_record_after_router_recreation(monkeypatch):
    first_module = _provider_treasury_module()
    first_service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        first_module.create_provider_treasury_rebalance(
            _neutral_request(first_module),
            _authorized_request(),
            first_service,
        )
    )

    recreated_module = _provider_treasury_module()
    recreated_service = FakeAccountsService()
    result = asyncio.run(
        recreated_module.get_provider_treasury_rebalance(
            "target-funding-1",
            _authorized_request(),
            recreated_service,
        )
    )

    assert result.status == "confirmed"
    assert result.transaction_hash == "0xconfirmed"
    assert recreated_service.gateway_client.status_calls == ["target-funding-1"]
    assert _REBALANCE_RECORDS["target-funding-1"].status == "confirmed"


def test_response_ignores_protocol_selection_fields_and_redacts_error():
    module = _provider_treasury_module()

    response = module._rebalance_response(
        {
            "idempotencyKey": "target-funding-1",
            "metadata": {
                "phase": "failed",
                "provider": "squid_router",
                "route": "raw-route",
                "sourceNetwork": "arbitrum",
            },
            "provider": "squid_router",
            "providerError": "failed bearer abc123 token secret-value",
            "sourceNetwork": "arbitrum",
            "status": "failed",
            "transactionHash": "0xtx",
        }
    )

    assert response.model_dump(exclude_none=True) == {
        "error": "failed bearer [redacted] token [redacted]",
        "id": "target-funding-1",
        "metadata": {"phase": "failed"},
        "status": "failed",
        "transaction_hash": "0xtx",
    }


def test_gateway_client_uses_exact_target_paths_and_payload(monkeypatch):
    calls = []
    client = GatewayClient()

    async def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"idempotencyKey": "target-funding-1", "status": "built"}

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.delenv("MARLIN_GATEWAY_PROVIDER_INTENT_TOKEN", raising=False)
    monkeypatch.delenv("MARLIN_GATEWAY_PROVIDER_INTENT_TOKEN_FILE", raising=False)
    asyncio.run(
        client.create_treasury_rebalance_target(
            idempotency_key="target-funding-1",
            destination_chain="ethereum",
            destination_network="base",
            destination_asset="USDC",
            destination_address="0x00000000000000000000000000000000000000B1",
            amount="6",
            max_cost_bps="100",
        )
    )
    asyncio.run(client.execute_treasury_rebalance_target("target-funding-1"))
    asyncio.run(client.get_treasury_rebalance("target-funding-1"))

    assert calls == [
        (
            "POST",
            "bridge/rebalance/targets",
            {
                "json": {
                    "amount": "6",
                    "destinationAddress": "0x00000000000000000000000000000000000000B1",
                    "destinationAsset": "USDC",
                    "destinationChain": "ethereum",
                    "destinationNetwork": "base",
                    "idempotencyKey": "target-funding-1",
                    "maxCostBps": "100",
                    "mode": "mainnet",
                }
            },
        ),
        ("POST", "bridge/rebalance/targets/target-funding-1/execute", {}),
        ("GET", "bridge/rebalance/target-funding-1", {}),
    ]


def test_gateway_execute_forwards_internal_provider_intent_token(monkeypatch):
    calls = []
    client = GatewayClient()

    async def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": 1}

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setenv("MARLIN_GATEWAY_PROVIDER_INTENT_TOKEN", "gateway-token")

    asyncio.run(client.execute_treasury_rebalance_target("target-funding-1"))

    assert calls == [
        (
            "POST",
            "bridge/rebalance/targets/target-funding-1/execute",
            {"headers": {"x-marlin-gateway-provider-intent-token": "gateway-token"}},
        )
    ]
