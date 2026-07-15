import asyncio
import importlib.util
import sys
import types
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account
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
            if record.status == "confirmed" and status != "confirmed":
                return record
            payload = dict(response_payload)
            if isinstance(record.response_payload, dict):
                baseline = record.response_payload.get("_hl_baseline_usdc")
                if baseline is not None:
                    payload.setdefault("_hl_baseline_usdc", baseline)
            if status != "built":
                record.status = status
            record.response_payload = payload
        return record


class FakeGatewayClient:
    def __init__(self):
        self.ping_calls = 0
        self.target_calls = []
        self.execute_calls = []
        self.status_calls = []
        self.statuses_at_target = []
        self.wallet_calls = []
        self.wallet_result = {"status": "default_set"}
        self.balance_calls = []
        self.events = []
        self.balance_result = {"balances": {"USDC": "0"}}
        self.statuses_at_execute = []
        self.execute_delay = 0
        self.target_result = {"idempotencyKey": "target-funding-1", "status": "built"}
        self.status_results = []

    async def ping(self):
        self.ping_calls += 1
        return True

    async def set_marlin_default_wallet(self, **kwargs):
        self.wallet_calls.append(kwargs)
        self.events.append(("wallet", kwargs["network"], kwargs["address"]))
        return self.wallet_result

    async def create_treasury_rebalance_target(self, **kwargs):
        record = _REBALANCE_RECORDS.get(kwargs["idempotency_key"])
        self.statuses_at_target.append(None if record is None else record.status)
        self.target_calls.append(kwargs)
        return dict(self.target_result)

    async def get_balances(self, chain, network, address, tokens=None):
        self.balance_calls.append((chain, network, address, tokens))
        self.events.append(("balance", network, address))
        return dict(self.balance_result)

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
        self._connector_balance_refresh_errors = {}
        self.accounts_state = {}
        self._hl_balance = None
        self.fresh_balance_calls = []
        self._refresh_error = None
        self.update_account_state_calls = []
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

    def get_accounts_state(self):
        return self.accounts_state

    def connector_balance_refresh_error(self, connector_name):
        return self._connector_balance_refresh_errors.get(connector_name)

    async def update_account_state(self, skip_gateway=False, account_names=None, connector_names=None):
        self.update_account_state_calls.append((skip_gateway, account_names, connector_names))
        if self._refresh_error:
            self._connector_balance_refresh_errors["hyperliquid_perpetual"] = self._refresh_error
        else:
            self._connector_balance_refresh_errors.pop("hyperliquid_perpetual", None)
            if account_names:
                for acct in account_names:
                    if acct not in self.accounts_state:
                        self.accounts_state[acct] = {}
                    if self._hl_balance is not None:
                        self.accounts_state[acct]["hyperliquid_perpetual"] = [
                            {
                                "token": "USDC",
                                "units": float(self._hl_balance),
                                "available_units": float(self._hl_balance),
                                "price": 1.0,
                                "value": float(self._hl_balance),
                            }
                        ]
                    elif "hyperliquid_perpetual" not in self.accounts_state.get(acct, {}):
                        self.accounts_state[acct]["hyperliquid_perpetual"] = []

    async def get_fresh_available_balance(self, account_name, connector_name, token):
        self.fresh_balance_calls.append((account_name, connector_name, token))
        await self.update_account_state(
            skip_gateway=True,
            account_names=[account_name],
            connector_names=[connector_name],
        )
        if self._refresh_error:
            raise RuntimeError(self._refresh_error)
        return self._hl_balance or Decimal("0")


def _neutral_request(module, **overrides):
    data = {
        "account_name": "master_account",
        "target_notional_eur": "6",
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
        "destination_amount",
        "destination_asset",
        "destination_chain",
        "destination_network",
        "destination_wallet_ref",
        "idempotency_key",
        "max_cost_bps",
        "route_id",
        "target_notional_eur",
    }
    with pytest.raises(ValidationError):
        module.ProviderTreasuryRebalanceRequest(**body.model_dump(), provider="squid_router")
    with pytest.raises(ValidationError):
        module.ProviderTreasuryRebalanceRequest(**body.model_dump(), source_network="arbitrum")
    with pytest.raises(ValidationError):
        module.ProviderTreasuryRebalanceRequest(**body.model_dump(), destination_address="0xcaller")
    with pytest.raises(ValidationError):
        module.ProviderTreasuryRebalanceRequest(**body.model_dump(), amount="6")


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
            "destination_address": "0x00000000000000000000000000000000000000B1",
            "destination_asset": "USDC",
            "destination_chain": "ethereum",
            "destination_network": "base",
            "idempotency_key": "target-funding-1",
            "max_cost_bps": "100",
            "target_notional_eur": "6",
        }
    ]
    assert service.gateway_client.statuses_at_target == ["built"]
    stored = _REBALANCE_RECORDS["target-funding-1"].request_payload
    assert stored == {
        "account_name": "master_account",
        "target_notional_eur": "6",
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


def test_create_forwards_optional_destination_amount(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    asyncio.run(
        module.create_provider_treasury_rebalance(
            _neutral_request(module, destination_amount=Decimal("1000")),
            _authorized_request(),
            service,
        )
    )

    assert service.gateway_client.target_calls[0]["destination_amount"] == "1000"
    assert "amount" not in service.gateway_client.target_calls[0]
    assert service.gateway_client.target_calls[0]["target_notional_eur"] == "6"
    stored = _REBALANCE_RECORDS["target-funding-1"].request_payload
    assert stored["target_notional_eur"] == "6"
    assert stored["destination_amount"] == "1000"
    assert "amount" not in stored


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
        "destination_address": "0x00000000000000000000000000000000000000a1",
        "destination_asset": "USDC",
        "destination_chain": "hyperliquid",
        "destination_network": "mainnet",
        "idempotency_key": "target-funding-1",
        "max_cost_bps": "100",
        "target_notional_eur": "6",
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


def _enable_hyperliquid_egress(module, service, monkeypatch):
    private_key = bytes.fromhex("0123456789" * 6 + "0123")
    address = Account.from_key(private_key).address
    service.identities[("ethereum", "arbitrum-mainnet")] = {
        "address": address,
        "wallet_ref": "arbitrum:mainnet:evm_gateway",
    }
    service._hl_balance = Decimal("0")
    service._hl_withdrawable = Decimal("8")
    service.hl_withdrawable_calls = []
    service.gateway_client.target_result = {
        "error": "insufficient_source_or_gas",
        "status": 409,
    }
    monkeypatch.setattr(
        module,
        "_derive_marlin_credential_values",
        lambda namespace: {
            "hyperliquid_address": address,
            "hyperliquid_secret_key": "0x" + private_key.hex(),
        }
        if namespace == "hyperliquid"
        else None,
    )
    async def withdrawable_balance(_source_address):
        service.hl_withdrawable_calls.append(_source_address)
        return service._hl_withdrawable

    monkeypatch.setattr(module, "_hl_withdrawable_balance", withdrawable_balance)
    return address


def test_insufficient_gateway_source_builds_durable_hyperliquid_egress(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    address = _enable_hyperliquid_egress(module, service, monkeypatch)
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)

    result = asyncio.run(
        module.create_provider_treasury_rebalance(
            _neutral_request(module, max_cost_bps=2000),
            _authorized_request(),
            service,
        )
    )

    record = _REBALANCE_RECORDS["target-funding-1"]
    egress = record.response_payload[module.HL_EGRESS_FIELD]
    assert result.status == "built"
    assert egress["status"] == "built"
    assert egress["action"]["amount"] == "7"
    assert egress["source_debit_usdc"] == "7"
    assert egress["destination_address"] == address
    assert service.hl_withdrawable_calls == [address]
    assert service.fresh_balance_calls == []
    assert service.gateway_client.balance_calls == [
        ("ethereum", "arbitrum", address, ["USDC"])
    ]
    assert service.gateway_client.wallet_calls[-1] == {
        "address": address,
        "chain": "ethereum",
        "network": "arbitrum-mainnet",
        "wallet_ref": "arbitrum:mainnet:evm_gateway",
    }
    assert service.gateway_client.events[-2:] == [
        ("wallet", "arbitrum-mainnet", address),
        ("balance", "arbitrum", address),
    ]
    assert "private" not in str(egress).lower()
    assert "secret" not in str(egress).lower()
    assert result.model_dump().get(module.HL_EGRESS_FIELD) is None


def test_hyperliquid_egress_maps_provider_balance_failure_to_502(monkeypatch):
    module = _provider_treasury_module()

    async def unavailable(_source_address, _session):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(module, "fetch_withdrawable_balance", unavailable)

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(module._hl_withdrawable_balance("0x00000000000000000000000000000000000000A1"))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Hyperliquid balance refresh failed"


def test_hyperliquid_egress_fails_before_balance_read_when_wallet_provisioning_is_unavailable():
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service.gateway_client.wallet_result = None

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module._provision_hyperliquid_egress_destination(
                service,
                "0x00000000000000000000000000000000000000A1",
            )
        )

    assert exc.value.status_code == 400
    assert exc.value.detail.startswith("destination_wallet_default_failed")
    assert service.gateway_client.balance_calls == []


def test_hyperliquid_egress_rejects_identity_mismatch_before_wallet_mutation():
    module = _provider_treasury_module()
    service = FakeAccountsService()

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module._provision_hyperliquid_egress_destination(
                service,
                "0x00000000000000000000000000000000000000B2",
            )
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "destination_wallet_identity_unavailable"
    assert service.gateway_client.wallet_calls == []


def test_hyperliquid_egress_confirms_balances_then_resumes_gateway(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    _enable_hyperliquid_egress(module, service, monkeypatch)
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    submitted = []

    async def accept(envelope, _session):
        submitted.append(envelope.payload())
        return {"status": "ok", "response": {"type": "default"}}

    monkeypatch.setattr(module, "submit_withdrawal", accept)
    body = _neutral_request(module, max_cost_bps=2000)
    asyncio.run(
        module.create_provider_treasury_rebalance(body, _authorized_request(), service)
    )
    # Recover an envelope built before withdraw3 fee semantics were corrected:
    # action.amount is the actual debit and the destination receives amount-fee.
    _REBALANCE_RECORDS[body.idempotency_key].response_payload[module.HL_EGRESS_FIELD]["action"]["amount"] = "6"

    pending = asyncio.run(
        module.execute_provider_treasury_rebalance(
            body.idempotency_key,
            module.ProviderTreasuryRebalanceExecuteRequest(idempotency_key=body.idempotency_key),
            _authorized_request(),
            service,
        )
    )
    assert pending.status == "source_pending"
    assert pending.source_amount == Decimal("6")
    assert pending.destination_amount == Decimal("5")

    service._hl_withdrawable = Decimal("2")
    service.gateway_client.balance_result = {"balances": {"USDC": "5"}}
    service.gateway_client.target_result = {
        "idempotencyKey": body.idempotency_key,
    }
    confirmed = asyncio.run(
        module.execute_provider_treasury_rebalance(
            body.idempotency_key,
            module.ProviderTreasuryRebalanceExecuteRequest(idempotency_key=body.idempotency_key),
            _authorized_request(),
            service,
        )
    )

    egress = _REBALANCE_RECORDS[body.idempotency_key].response_payload[module.HL_EGRESS_FIELD]
    assert confirmed.status == "confirmed"
    assert egress["actual_fee_usdc"] == "1"
    assert egress["actual_source_debit_usdc"] == "6"
    assert egress["actual_destination_usdc"] == "5"
    assert egress["source_debit_usdc"] == "6"
    assert egress["destination_target_usdc"] == "5"
    assert service.fresh_balance_calls == []
    assert service.gateway_client.wallet_calls[-1]["network"] == "arbitrum-mainnet"
    assert service.gateway_client.events[-2:] == [
        ("wallet", "arbitrum-mainnet", egress["destination_address"]),
        ("balance", "arbitrum", egress["destination_address"]),
    ]
    assert len(submitted) == 1
    assert len(service.gateway_client.target_calls) == 2
    assert service.gateway_client.execute_calls == [body.idempotency_key]


@pytest.mark.parametrize(
    "stored_status,should_execute",
    [("unknown", True), ("built", True), ("failed", False)],
)
def test_gateway_built_missing_status_recovers_only_unknown_record(
    monkeypatch,
    stored_status,
    should_execute,
):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    _enable_hyperliquid_egress(module, service, monkeypatch)
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    body = _neutral_request(module, max_cost_bps=2000)
    asyncio.run(module.create_provider_treasury_rebalance(body, _authorized_request(), service))
    record = _REBALANCE_RECORDS[body.idempotency_key]
    record.status = stored_status
    record.response_payload[module.HL_EGRESS_FIELD]["status"] = "gateway_built"
    service.gateway_client.status_results = [{"idempotencyKey": body.idempotency_key}]

    asyncio.run(
        module.execute_provider_treasury_rebalance(
            body.idempotency_key,
            module.ProviderTreasuryRebalanceExecuteRequest(idempotency_key=body.idempotency_key),
            _authorized_request(),
            service,
        )
    )

    assert service.gateway_client.execute_calls == (
        [body.idempotency_key] if should_execute else []
    )


def test_gateway_built_provider_error_does_not_execute(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    _enable_hyperliquid_egress(module, service, monkeypatch)
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    body = _neutral_request(module, max_cost_bps=2000)
    asyncio.run(module.create_provider_treasury_rebalance(body, _authorized_request(), service))
    record = _REBALANCE_RECORDS[body.idempotency_key]
    record.status = "unknown"
    record.response_payload[module.HL_EGRESS_FIELD]["status"] = "gateway_built"
    service.gateway_client.status_results = [
        {
            "idempotencyKey": body.idempotency_key,
            "providerError": "provider rejected route",
            "status": "built",
        }
    ]

    result = asyncio.run(
        module.execute_provider_treasury_rebalance(
            body.idempotency_key,
            module.ProviderTreasuryRebalanceExecuteRequest(idempotency_key=body.idempotency_key),
            _authorized_request(),
            service,
        )
    )

    assert result.error == "provider rejected route"
    assert service.gateway_client.execute_calls == []


def test_claimed_hyperliquid_provider_error_does_not_execute_gateway(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    _enable_hyperliquid_egress(module, service, monkeypatch)
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    body = _neutral_request(module, max_cost_bps=2000)
    asyncio.run(module.create_provider_treasury_rebalance(body, _authorized_request(), service))

    async def provider_error(*_args):
        return module.ProviderTreasuryRebalanceResponse(
            id=body.idempotency_key,
            status="built",
            error="provider rejected route",
        )

    monkeypatch.setattr(module, "_submit_hyperliquid_egress", provider_error)
    result = asyncio.run(
        module.execute_provider_treasury_rebalance(
            body.idempotency_key,
            module.ProviderTreasuryRebalanceExecuteRequest(idempotency_key=body.idempotency_key),
            _authorized_request(),
            service,
        )
    )

    assert result.error == "provider rejected route"
    assert service.gateway_client.execute_calls == []


def test_gateway_build_provider_error_does_not_mark_egress_executable(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    _enable_hyperliquid_egress(module, service, monkeypatch)
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    body = _neutral_request(module, max_cost_bps=2000)
    asyncio.run(module.create_provider_treasury_rebalance(body, _authorized_request(), service))
    record = _REBALANCE_RECORDS[body.idempotency_key]
    record.response_payload[module.HL_EGRESS_FIELD]["status"] = "accepted"
    service._hl_withdrawable = Decimal("1")
    service.gateway_client.balance_result = {"balances": {"USDC": "6"}}
    service.gateway_client.target_result = {
        "idempotencyKey": body.idempotency_key,
        "providerError": "provider rejected route",
        "status": "built",
    }

    result = asyncio.run(
        module._refresh_hyperliquid_egress(
            service,
            _authorized_request().app.state.db_manager,
            body.idempotency_key,
        )
    )

    egress = _REBALANCE_RECORDS[body.idempotency_key].response_payload[module.HL_EGRESS_FIELD]
    assert result.error == "provider rejected route"
    assert egress["status"] == "source_confirmed"
    assert service.gateway_client.execute_calls == []

    async def forbidden_submit(*_args):
        raise AssertionError("settled Hyperliquid withdrawal must not be resubmitted")

    monkeypatch.setattr(module, "_submit_hyperliquid_egress", forbidden_submit)
    asyncio.run(
        module.execute_provider_treasury_rebalance(
            body.idempotency_key,
            module.ProviderTreasuryRebalanceExecuteRequest(idempotency_key=body.idempotency_key),
            _authorized_request(),
            service,
        )
    )
    assert service.gateway_client.execute_calls == []


def test_ambiguous_hyperliquid_retry_resubmits_identical_envelope(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    _enable_hyperliquid_egress(module, service, monkeypatch)
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    submitted = []

    async def ambiguous_then_accept(envelope, _session):
        submitted.append(envelope.payload())
        if len(submitted) == 1:
            raise module.SubmissionAmbiguous("timeout")
        return {"status": "ok", "response": {"type": "default"}}

    monkeypatch.setattr(module, "submit_withdrawal", ambiguous_then_accept)
    body = _neutral_request(module, max_cost_bps=2000)
    asyncio.run(
        module.create_provider_treasury_rebalance(body, _authorized_request(), service)
    )
    execute_body = module.ProviderTreasuryRebalanceExecuteRequest(
        idempotency_key=body.idempotency_key
    )

    first = asyncio.run(
        module.execute_provider_treasury_rebalance(
            body.idempotency_key, execute_body, _authorized_request(), service
        )
    )
    second = asyncio.run(
        module.execute_provider_treasury_rebalance(
            body.idempotency_key, execute_body, _authorized_request(), service
        )
    )

    assert first.status == "source_pending"
    assert second.status == "source_pending"
    assert len(submitted) == 2
    assert submitted[0] == submitted[1]


def test_malformed_hyperliquid_amount_fails_before_submit(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    _enable_hyperliquid_egress(module, service, monkeypatch)
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    submitted = []

    async def submit(envelope, _session):
        submitted.append(envelope.payload())

    monkeypatch.setattr(module, "submit_withdrawal", submit)
    body = _neutral_request(module, max_cost_bps=2000)
    asyncio.run(module.create_provider_treasury_rebalance(body, _authorized_request(), service))
    _REBALANCE_RECORDS[body.idempotency_key].response_payload[module.HL_EGRESS_FIELD]["action"][
        "amount"
    ] = "1.0000001"

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module.execute_provider_treasury_rebalance(
                body.idempotency_key,
                module.ProviderTreasuryRebalanceExecuteRequest(idempotency_key=body.idempotency_key),
                _authorized_request(),
                service,
            )
        )

    assert exc.value.status_code == 502
    assert submitted == []


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
                _neutral_request(module, target_notional_eur="7"),
                _authorized_request(),
                service,
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "idempotency_key_conflict"
    assert len(service.gateway_client.wallet_calls) == 1
    assert len(service.gateway_client.target_calls) == 1


def test_same_idempotency_key_with_different_destination_amount_fails_before_gateway(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    asyncio.run(
        module.create_provider_treasury_rebalance(
            _neutral_request(module, destination_amount=Decimal("1000")),
            _authorized_request(),
            service,
        )
    )

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module.create_provider_treasury_rebalance(
                _neutral_request(module, destination_amount=Decimal("2000")),
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
    assert service.gateway_client.status_calls == ["target-funding-1"]
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


@pytest.mark.parametrize(
    "gateway_status",
    [
        "built",
        "approval_submission_pending",
        "approval_submission_ambiguous",
        "approval_submitted",
        "approval_confirmed",
        "submission_pending",
        "submission_ambiguous",
        "submission_insufficient_funds",
        "submitted",
        "wrap_submission_pending",
        "wrap_submission_ambiguous",
        "wrap_submitted",
        "wrap_confirmed",
    ],
)
def test_execute_restart_recovers_pending_claim_from_gateway_recoverable_status(
    monkeypatch,
    gateway_status,
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
    _REBALANCE_RECORDS["target-funding-1"].status = "pending"

    recreated_module = _provider_treasury_module()
    recreated_service = FakeAccountsService()
    recreated_service.gateway_client.status_results = [
        {"idempotencyKey": "target-funding-1", "status": gateway_status},
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
    expected_stored_status = "pending" if gateway_status == "built" else gateway_status
    assert recreated_service.gateway_client.statuses_at_execute == [expected_stored_status]
    assert _REBALANCE_RECORDS["target-funding-1"].status == "confirmed"


@pytest.mark.parametrize("gateway_status", ["submission_ambiguous", "submission_insufficient_funds"])
def test_execute_retry_recovers_persisted_gateway_recoverable_status(monkeypatch, gateway_status):
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
    first_service.gateway_client.status_results = [
        {"idempotencyKey": "target-funding-1", "status": gateway_status},
    ]
    refresh_status = first_module._refresh_rebalance_status

    async def persist_status_then_fail(*args, **kwargs):
        await refresh_status(*args, **kwargs)
        raise RuntimeError("response interrupted")

    monkeypatch.setattr(first_module, "_refresh_rebalance_status", persist_status_then_fail)
    body = first_module.ProviderTreasuryRebalanceExecuteRequest(
        idempotency_key="target-funding-1",
    )

    with pytest.raises(first_module.HTTPException) as exc:
        asyncio.run(
            first_module.execute_provider_treasury_rebalance(
                "target-funding-1",
                body,
                _authorized_request(),
                first_service,
            )
        )

    assert exc.value.status_code == 500
    assert first_service.gateway_client.execute_calls == ["target-funding-1"]
    assert _REBALANCE_RECORDS["target-funding-1"].status == gateway_status

    recreated_module = _provider_treasury_module()
    recreated_service = FakeAccountsService()
    recreated_service.gateway_client.status_results = [
        {"idempotencyKey": "target-funding-1", "status": gateway_status},
        {"idempotencyKey": "target-funding-1", "status": "confirmed"},
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
    assert recreated_service.gateway_client.execute_calls == ["target-funding-1"]
    assert recreated_service.gateway_client.statuses_at_execute == [gateway_status]
    assert _REBALANCE_RECORDS["target-funding-1"].status == "confirmed"


@pytest.mark.parametrize(
    "gateway_status",
    ["submission_ambiguous", "submission_insufficient_funds"],
)
def test_execute_retry_delegates_absence_recovery_status_with_provider_error(
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
    _REBALANCE_RECORDS["target-funding-1"].status = gateway_status
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": gateway_status,
            "providerError": "Squid 429",
        },
        {
            "idempotencyKey": "target-funding-1",
            "status": "confirmed",
            "transactionHash": "0xconfirmed",
        },
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

    assert result.status == "confirmed"
    assert service.gateway_client.execute_calls == ["target-funding-1"]
    assert service.gateway_client.statuses_at_execute == [gateway_status]
    assert _REBALANCE_RECORDS["target-funding-1"].status == "confirmed"


@pytest.mark.parametrize(
    "gateway_status",
    ["confirmed", "destination_pending", "failed", "unknown", "cancelled"],
)
def test_execute_pending_claim_never_resubmits_non_recoverable_gateway_status(
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


def test_status_transports_submission_insufficient_funds(monkeypatch):
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
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "submission_insufficient_funds",
        }
    ]

    result = asyncio.run(
        module.get_provider_treasury_rebalance(
            "target-funding-1",
            _authorized_request(),
            service,
        )
    )

    assert result.status == "submission_insufficient_funds"
    assert result.error is None
    assert _REBALANCE_RECORDS["target-funding-1"].status == "submission_insufficient_funds"


def test_response_preserves_source_context_and_redacts_error():
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
            "sourceChain": "ethereum",
            "sourceNetwork": "arbitrum",
            "status": "failed",
            "transactionHash": "0xtx",
        }
    )

    assert response.model_dump(exclude_none=True) == {
        "error": "failed bearer [redacted] token [redacted]",
        "id": "target-funding-1",
        "metadata": {
            "phase": "failed",
            "source_chain": "ethereum",
            "source_network": "arbitrum",
        },
        "status": "failed",
        "transaction_hash": "0xtx",
    }


@pytest.mark.parametrize(
    ("gateway_field", "hba_field", "gateway_value", "expected_value"),
    [
        ("sourceAmount", "source_amount", "1000.5", Decimal("1000.5")),
        ("sourceAsset", "source_asset", "USDC", "USDC"),
        ("destinationAmount", "destination_amount", "999.99", Decimal("999.99")),
        ("destinationAsset", "destination_asset", "USDC", "USDC"),
        ("quotedProviderCostUsd", "quoted_provider_cost_usd", "12.34", Decimal("12.34")),
        ("quotedGasCostUsd", "quoted_gas_cost_usd", "5.67", Decimal("5.67")),
        ("quotedNativeGasAmount", "quoted_native_gas_amount", "0.001", Decimal("0.001")),
        ("quotedNativeGasAsset", "quoted_native_gas_asset", "ETH", "ETH"),
        ("quotedAt", "quoted_at", "2025-06-15T10:30:00+00:00", None),
    ],
)
def test_rebalance_response_quote_economics_parsed_correctly(
    gateway_field,
    hba_field,
    gateway_value,
    expected_value,
):
    module = _provider_treasury_module()

    payload = {
        "idempotencyKey": "target-funding-1",
        "status": "built",
        gateway_field: gateway_value,
    }

    response = module._rebalance_response(payload)

    actual = getattr(response, hba_field)
    if hba_field == "quoted_at":
        assert actual == datetime(2025, 6, 15, 10, 30, tzinfo=timezone.utc)
    else:
        assert actual == expected_value


def test_rebalance_response_all_quote_economics_passthrough():
    module = _provider_treasury_module()

    response = module._rebalance_response(
        {
            "idempotencyKey": "target-funding-1",
            "status": "built",
            "sourceAmount": "5000.00",
            "sourceAsset": "DAI",
            "destinationAmount": "4995.00",
            "destinationAsset": "USDC",
            "quotedProviderCostUsd": "25.00",
            "quotedGasCostUsd": "3.50",
            "quotedNativeGasAmount": "0.0021",
            "quotedNativeGasAsset": "ETH",
            "quotedAt": "2025-06-15T10:30:00+00:00",
        }
    )

    dumped = response.model_dump(mode="json", exclude_none=True)
    assert dumped["source_amount"] == "5000.00"
    assert dumped["source_asset"] == "DAI"
    assert dumped["destination_amount"] == "4995.00"
    assert dumped["destination_asset"] == "USDC"
    assert dumped["quoted_provider_cost_usd"] == "25.00"
    assert dumped["quoted_gas_cost_usd"] == "3.50"
    assert dumped["quoted_native_gas_amount"] == "0.0021"
    assert dumped["quoted_native_gas_asset"] == "ETH"
    assert "quoted_at" in dumped


def test_rebalance_response_quote_economics_default_to_none():
    module = _provider_treasury_module()

    response = module._rebalance_response(
        {
            "idempotencyKey": "target-funding-1",
            "status": "built",
        }
    )

    dumped = response.model_dump(mode="json", exclude_none=True)
    for field in (
        "source_amount",
        "source_asset",
        "destination_amount",
        "destination_asset",
        "quoted_provider_cost_usd",
        "quoted_gas_cost_usd",
        "quoted_native_gas_amount",
        "quoted_native_gas_asset",
        "quoted_at",
    ):
        assert field not in dumped, f"{field} should be absent when None"


def test_malformed_gateway_decimal_raises_502():
    module = _provider_treasury_module()

    with pytest.raises(module.HTTPException) as exc:
        module._rebalance_response(
            {
                "idempotencyKey": "target-funding-1",
                "status": "built",
                "sourceAmount": "not-a-number",
            }
        )
    assert exc.value.status_code == 502


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("sourceAmount", "NaN", "non-finite decimal"),
        ("sourceAmount", "Infinity", "non-finite decimal"),
        ("sourceAmount", "-Infinity", "non-finite decimal"),
        ("destinationAmount", "NaN", "non-finite decimal"),
        ("destinationAmount", "Infinity", "non-finite decimal"),
        ("destinationAmount", "-Infinity", "non-finite decimal"),
        ("quotedProviderCostUsd", "NaN", "non-finite decimal"),
        ("quotedProviderCostUsd", "Infinity", "non-finite decimal"),
        ("quotedGasCostUsd", "NaN", "non-finite decimal"),
        ("quotedGasCostUsd", "Infinity", "non-finite decimal"),
        ("quotedNativeGasAmount", "NaN", "non-finite decimal"),
        ("quotedNativeGasAmount", "Infinity", "non-finite decimal"),
        ("sourceAmount", "-1", "non-positive"),
        ("sourceAmount", "0", "non-positive"),
        ("destinationAmount", "-50", "non-positive"),
        ("destinationAmount", "0", "non-positive"),
        ("sourceAsset", "", "empty"),
        ("sourceAsset", "  ", "empty"),
        ("destinationAsset", "", "empty"),
        ("destinationAsset", "  ", "empty"),
        ("quotedNativeGasAsset", "", "empty"),
        ("quotedNativeGasAsset", "  ", "empty"),
        ("sourceAsset", {"symbol": "USDC"}, "non-string"),
        ("sourceAmount", 1.5, "non-string"),
    ],
)
def test_malformed_economics_rejected_with_502(field, value, reason):
    module = _provider_treasury_module()

    with pytest.raises(module.HTTPException) as exc:
        module._rebalance_response(
            {
                "idempotencyKey": "target-funding-1",
                "status": "built",
                field: value,
            }
        )
    assert exc.value.status_code == 502


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("quotedProviderCostUsd", "0", Decimal("0")),
        ("quotedGasCostUsd", "0", Decimal("0")),
        ("quotedNativeGasAmount", "0", Decimal("0")),
    ],
)
def test_zero_quoted_costs_accepted(field, value, expected):
    module = _provider_treasury_module()

    response = module._rebalance_response(
        {
            "idempotencyKey": "target-funding-1",
            "status": "built",
            field: value,
        }
    )

    assert getattr(response, {
        "quotedProviderCostUsd": "quoted_provider_cost_usd",
        "quotedGasCostUsd": "quoted_gas_cost_usd",
        "quotedNativeGasAmount": "quoted_native_gas_amount",
    }[field]) == expected


def test_response_model_rejects_nonfinite_and_naive_economics_directly():
    module = _provider_treasury_module()

    with pytest.raises(ValueError):
        module.ProviderTreasuryRebalanceResponse(
            id="target-funding-1",
            status="built",
            quoted_provider_cost_usd=Decimal("Infinity"),
        )
    with pytest.raises(ValueError):
        module.ProviderTreasuryRebalanceResponse(
            id="target-funding-1",
            status="built",
            quoted_at=datetime(2025, 6, 15, 10, 30),
        )


def test_malformed_gateway_quoted_at_raises_502():
    module = _provider_treasury_module()

    with pytest.raises(module.HTTPException) as exc:
        module._rebalance_response(
            {
                "idempotencyKey": "target-funding-1",
                "status": "built",
                "quotedAt": "not-a-date",
            }
        )
    assert exc.value.status_code == 502


def test_naive_quoted_at_raises_502():
    module = _provider_treasury_module()

    with pytest.raises(module.HTTPException) as exc:
        module._rebalance_response(
            {
                "idempotencyKey": "target-funding-1",
                "status": "built",
                "quotedAt": "2025-06-15T10:30:00",
            }
        )
    assert exc.value.status_code == 502


def test_rebalance_response_maps_stage_fields():
    module = _provider_treasury_module()

    response = module._rebalance_response(
        {
            "idempotencyKey": "target-funding-1",
            "status": "built",
            "stageIndex": 0,
            "stageCount": 2,
            "stageStatus": "built",
        }
    )

    assert response.stage_index == 0
    assert response.stage_count == 2
    assert response.stage_status == "built"


def test_rebalance_response_maps_provider_neutral_stages_and_serializes_snake_case():
    module = _provider_treasury_module()

    response = module._rebalance_response(
        {
            "idempotencyKey": "target-funding-1",
            "status": "destination_pending",
            "stages": [
                {
                    "index": 0,
                    "kind": "conversion",
                    "status": "confirmed",
                    "sourceAmount": "100.50",
                    "sourceAsset": "USDC",
                    "destinationAmount": "99.50",
                    "destinationAsset": "USDC.e",
                    "transactionHash": "0xconversion",
                },
                {
                    "index": 1,
                    "kind": "funding",
                    "status": "pending",
                    "destinationAmount": "99.50",
                    "destinationAsset": "USDC",
                },
            ],
        }
    )

    assert response.stages[0].index == 0
    assert response.stages[0].kind == "conversion"
    assert response.stages[0].status == "confirmed"
    assert response.stages[0].source_amount == Decimal("100.50")
    assert response.stages[0].destination_asset == "USDC.e"
    assert response.stages[0].transaction_hash == "0xconversion"
    assert response.stages[1].kind == "funding"

    assert module._rebalance_response_payload(response)["stages"] == [
        {
            "index": 0,
            "kind": "conversion",
            "status": "confirmed",
            "source_amount": "100.50",
            "source_asset": "USDC",
            "destination_amount": "99.50",
            "destination_asset": "USDC.e",
            "transaction_hash": "0xconversion",
        },
        {
            "index": 1,
            "kind": "funding",
            "status": "pending",
            "destination_amount": "99.50",
            "destination_asset": "USDC",
        },
    ]


def test_rebalance_response_stage_list_can_be_empty():
    module = _provider_treasury_module()

    response = module._rebalance_response(
        {
            "idempotencyKey": "target-funding-1",
            "status": "built",
            "stages": [],
        }
    )

    assert response.stages == []
    assert module._rebalance_response_payload(response)["stages"] == []


def test_rebalance_response_stage_fields_default_to_none():
    module = _provider_treasury_module()

    response = module._rebalance_response(
        {
            "idempotencyKey": "target-funding-1",
            "status": "built",
        }
    )

    assert response.stage_index is None
    assert response.stage_count is None
    assert response.stage_status is None
    assert response.stages is None


def test_rebalance_response_payload_persists_stage_fields():
    module = _provider_treasury_module()

    response = module._rebalance_response(
        {
            "idempotencyKey": "target-funding-1",
            "status": "built",
            "stageIndex": 0,
            "stageCount": 2,
            "stageStatus": "built",
        }
    )

    payload = module._rebalance_response_payload(response)
    assert payload["stage_index"] == 0
    assert payload["stage_count"] == 2
    assert payload["stage_status"] == "built"


def test_persisted_rebalance_response_round_trips_snake_case_stages():
    module = _provider_treasury_module()
    service = FakeAccountsService()
    response = module._rebalance_response(
        {
            "idempotencyKey": "target-funding-1",
            "status": "confirmed",
            "sourceChain": "ethereum",
            "sourceNetwork": "arbitrum",
            "stages": [
                {
                    "index": 0,
                    "kind": "funding",
                    "status": "confirmed",
                    "destinationAmount": "100.00",
                    "destinationAsset": "USDC",
                }
            ],
        }
    )
    _REBALANCE_RECORDS["target-funding-1"] = types.SimpleNamespace(
        status="confirmed",
        response_payload=module._rebalance_response_payload(response),
    )

    restored = asyncio.run(
        module._refresh_rebalance_status(
            service,
            _authorized_request().app.state.db_manager,
            "target-funding-1",
        )
    )

    assert restored.stages[0].destination_amount == Decimal("100.00")
    assert restored.metadata == {
        "source_chain": "ethereum",
        "source_network": "arbitrum",
    }
    assert service.gateway_client.status_calls == []


@pytest.mark.parametrize(
    ("field", "value", "expected_detail_fragment"),
    [
        ("stageIndex", "not-a-number", "non-integer stageIndex"),
        ("stageIndex", True, "non-integer stageIndex"),
        ("stageIndex", -1, "negative stageIndex"),
        ("stageIndex", {"some": "dict"}, "non-integer stageIndex"),
        ("stageCount", "not-a-number", "non-integer stageCount"),
        ("stageCount", "2", "non-integer stageCount"),
        ("stageCount", 0, "stageCount < 1"),
        ("stageCount", -5, "stageCount < 1"),
        ("stageCount", {"some": "dict"}, "non-integer stageCount"),
        ("stageStatus", "", "empty stageStatus"),
        ("stageStatus", "   ", "empty stageStatus"),
        ("stageStatus", {"some": "dict"}, "non-string stageStatus"),
    ],
)
def test_malformed_stage_fields_raise_502(field, value, expected_detail_fragment):
    module = _provider_treasury_module()

    with pytest.raises(module.HTTPException) as exc:
        module._rebalance_response(
            {
                "idempotencyKey": "target-funding-1",
                "status": "built",
                field: value,
            }
        )
    assert exc.value.status_code == 502
    assert expected_detail_fragment in str(exc.value.detail)


def test_response_model_rejects_noncompliant_stage_values_directly():
    module = _provider_treasury_module()

    with pytest.raises(ValueError):
        module.ProviderTreasuryRebalanceResponse(
            id="target-funding-1", status="built", stage_index=-1
        )
    with pytest.raises(ValueError):
        module.ProviderTreasuryRebalanceResponse(
            id="target-funding-1", status="built", stage_count=0
        )
    with pytest.raises(ValueError):
        module.ProviderTreasuryRebalanceResponse(
            id="target-funding-1", status="built", stage_status=""
        )
    for field, value in (("stage_index", "1"), ("stage_count", True), ("stage_status", b"built")):
        with pytest.raises(ValueError):
            module.ProviderTreasuryRebalanceResponse(id="target-funding-1", status="built", **{field: value})
    with pytest.raises(ValueError):
        module.ProviderTreasuryRebalanceResponse(id="target-funding-1", status="built", stage_status="   ")


def test_stage_model_rejects_invalid_values_and_extra_fields_directly():
    module = _provider_treasury_module()
    stage = {
        "index": 0,
        "kind": "conversion",
        "status": "built",
    }

    with pytest.raises(ValueError):
        module.ProviderTreasuryStage(**{**stage, "index": -1})
    with pytest.raises(ValueError):
        module.ProviderTreasuryStage(**{**stage, "kind": "bridge"})
    with pytest.raises(ValueError):
        module.ProviderTreasuryStage(**{**stage, "status": "   "})
    with pytest.raises(ValueError):
        module.ProviderTreasuryStage(**{**stage, "source_amount": Decimal("0")})
    with pytest.raises(ValueError):
        module.ProviderTreasuryStage(**{**stage, "transaction_hash": "  "})
    with pytest.raises(ValueError):
        module.ProviderTreasuryStage(**{**stage, "provider_payload": {"secret": "value"}})


@pytest.mark.parametrize(
    "stages",
    [
        None,
        {},
        "not-a-list",
        [None],
        [{"index": -1, "kind": "conversion", "status": "built"}],
        [{"index": 0, "kind": "bridge", "status": "built"}],
        [{"index": 0, "kind": "conversion", "status": "   "}],
        [{"index": 0, "kind": "conversion", "status": "built", "sourceAmount": "0"}],
        [{"index": 0, "kind": "conversion", "status": "built", "sourceAmount": "NaN"}],
        [{"index": 0, "kind": "conversion", "status": "built", "sourceAsset": "   "}],
        [{"index": 0, "kind": "conversion", "status": "built", "transactionHash": "   "}],
        [{"index": 0, "kind": "conversion", "status": "built", "error": "   "}],
        [{"index": 0, "kind": "conversion", "status": "built", "source_amount": "1"}],
        [{"index": 0, "kind": "conversion", "status": "built", "providerPayload": {"secret": "value"}}],
        [{"index": 0, "kind": "conversion"}],
    ],
)
def test_malformed_gateway_stages_raise_sanitized_502(stages):
    module = _provider_treasury_module()

    with pytest.raises(module.HTTPException) as exc:
        module._rebalance_response(
            {
                "idempotencyKey": "target-funding-1",
                "status": "built",
                "stages": stages,
            }
        )

    assert exc.value.status_code == 502
    assert "secret" not in str(exc.value.detail).lower()


@pytest.mark.parametrize(
    "stages",
    [
        [{"index": 1, "kind": "conversion", "status": "built"}],
        [
            {"index": 0, "kind": "conversion", "status": "confirmed"},
            {"index": 2, "kind": "funding", "status": "built"},
        ],
    ],
)
def test_gateway_stage_index_must_match_list_position(stages):
    module = _provider_treasury_module()

    with pytest.raises(module.HTTPException) as exc:
        module._rebalance_response(
            {
                "idempotencyKey": "target-funding-1",
                "status": "built",
                "stages": stages,
            }
        )

    assert exc.value.status_code == 502
    assert "must match list position" in str(exc.value.detail)


@pytest.mark.parametrize(
    "field",
    [
        "sourceAmount",
        "sourceAsset",
        "destinationAmount",
        "destinationAsset",
        "transactionHash",
        "error",
    ],
)
def test_gateway_stage_optional_fields_reject_explicit_null(field):
    module = _provider_treasury_module()

    with pytest.raises(module.HTTPException) as exc:
        module._rebalance_response(
            {
                "idempotencyKey": "target-funding-1",
                "status": "built",
                "stages": [
                    {
                        "index": 0,
                        "kind": "conversion",
                        "status": "built",
                        field: None,
                    }
                ],
            }
        )

    assert exc.value.status_code == 502
    assert field in str(exc.value.detail)


def test_execute_retry_stage0_built_repeated_after_claim(monkeypatch):
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

    recreated_service = FakeAccountsService()
    recreated_service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "built",
            "stageIndex": 0,
            "stageCount": 2,
            "stageStatus": "built",
        },
        {
            "idempotencyKey": "target-funding-1",
            "status": "built",
            "stageIndex": 1,
            "stageCount": 2,
            "stageStatus": "built",
        },
    ]
    recreated_module = _provider_treasury_module()

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

    assert result.status == "built"
    assert recreated_service.gateway_client.execute_calls == ["target-funding-1"]
    assert len(recreated_service.gateway_client.status_calls) == 2


def test_execute_retry_destination_pending_does_not_re_execute(monkeypatch):
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
            "status": "destination_pending",
            "stageIndex": 1,
            "stageCount": 2,
            "stageStatus": "destination_pending",
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

    assert result.status == "destination_pending"
    assert service.gateway_client.status_calls == ["target-funding-1"]
    assert service.gateway_client.execute_calls == []


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
            target_notional_eur="6",
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
                    "destinationAddress": "0x00000000000000000000000000000000000000B1",
                    "destinationAsset": "USDC",
                    "destinationChain": "ethereum",
                    "destinationNetwork": "base",
                    "idempotencyKey": "target-funding-1",
                    "maxCostBps": "100",
                    "mode": "mainnet",
                    "targetNotionalEur": "6",
                }
            },
        ),
        ("POST", "bridge/rebalance/targets/target-funding-1/execute", {"json": {}}),
        ("GET", "bridge/rebalance/target-funding-1", {}),
    ]


def _hl_create(module, service, monkeypatch, **overrides):
    """Create a Hyperliquid target and return the response."""
    data = dict(
        destination_chain="hyperliquid",
        destination_network="mainnet",
        destination_wallet_ref="hyperliquid:mainnet:hyperliquid_trader",
        route_id="hl-mainnet",
    )
    data.update(overrides)
    return asyncio.run(
        module.create_provider_treasury_rebalance(
            _neutral_request(module, **data),
            _authorized_request(),
            service,
        )
    )


def _stub_hl_credentials(module, monkeypatch):
    monkeypatch.setattr(
        module,
        "_derive_marlin_credential_values",
        lambda namespace: {
            "hyperliquid_address": "0x00000000000000000000000000000000000000A1",
            "hyperliquid_secret_key": "unused",
        },
    )


def test_hyperliquid_create_captures_usdc_baseline(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)

    result = _hl_create(module, service, monkeypatch)

    stored = _REBALANCE_RECORDS["target-funding-1"]
    assert stored.response_payload.get("_hl_baseline_usdc") is not None
    assert result.status == "built"
    assert "_hl_baseline_usdc" not in result.model_dump(exclude_none=True)


def test_hyperliquid_baseline_persists_across_status_updates(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch)

    asyncio.run(
        module._update_rebalance_status(
            _authorized_request().app.state.db_manager,
            "target-funding-1",
            {"status": "pending", "transaction_hash": "0xabc"},
        )
    )

    stored = _REBALANCE_RECORDS["target-funding-1"]
    assert stored.response_payload.get("status") == "pending"
    assert stored.response_payload.get("_hl_baseline_usdc") is not None


def test_hyperliquid_baseline_is_durable_before_gateway_side_effect(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)

    async def fail_wallet_setup(**kwargs):
        raise RuntimeError("gateway unavailable")

    service.gateway_client.set_marlin_default_wallet = fail_wallet_setup
    with pytest.raises(module.HTTPException):
        _hl_create(module, service, monkeypatch)

    assert _REBALANCE_RECORDS["target-funding-1"].response_payload["_hl_baseline_usdc"] == "500"


def test_hyperliquid_baseline_persists_after_module_recreation(monkeypatch):
    first_module = _provider_treasury_module()
    first_service = FakeAccountsService()
    first_service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(first_module, monkeypatch)
    _hl_create(first_module, first_service, monkeypatch)

    recreated_module = _provider_treasury_module()
    recreated_service = FakeAccountsService()
    recreated_service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "confirmed",
            "transactionHash": "0xconfirmed",
        }
    ]
    result = asyncio.run(
        recreated_module.get_provider_treasury_rebalance(
            "target-funding-1",
            _authorized_request(),
            recreated_service,
        )
    )

    assert result.status == "confirmed"
    stored = _REBALANCE_RECORDS["target-funding-1"]
    assert stored.response_payload.get("_hl_baseline_usdc") is not None


def test_hyperliquid_delta_exact_confirms(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch, destination_amount=Decimal("300"))

    service._hl_balance = Decimal("800")
    _REBALANCE_RECORDS["target-funding-1"].status = "destination_pending"
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "destination_pending",
            "stageIndex": 1,
            "stageCount": 2,
            "stageStatus": "source_confirmed",
            "destinationAmount": "300",
        }
    ]

    result = asyncio.run(
        module.get_provider_treasury_rebalance(
            "target-funding-1",
            _authorized_request(),
            service,
        )
    )

    assert result.status == "confirmed"


def test_hyperliquid_delta_uses_funding_stage_destination_amount(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch, destination_amount=Decimal("300"))

    service._hl_balance = Decimal("800")
    _REBALANCE_RECORDS["target-funding-1"].status = "destination_pending"
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "destination_pending",
            "stageIndex": 1,
            "stageCount": 2,
            "stageStatus": "source_confirmed",
            "stages": [
                {"index": 0, "kind": "conversion", "status": "confirmed"},
                {
                    "index": 1,
                    "kind": "funding",
                    "status": "source_confirmed",
                    "destinationAmount": "300",
                },
            ],
        }
    ]

    result = asyncio.run(
        module.get_provider_treasury_rebalance(
            "target-funding-1",
            _authorized_request(),
            service,
        )
    )

    assert result.status == "confirmed"


def test_hyperliquid_delta_greater_confirms(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch, destination_amount=Decimal("300"))

    service._hl_balance = Decimal("900")
    _REBALANCE_RECORDS["target-funding-1"].status = "destination_pending"
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "destination_pending",
            "stageIndex": 1,
            "stageCount": 2,
            "stageStatus": "source_confirmed",
            "destinationAmount": "300",
        }
    ]

    result = asyncio.run(
        module.get_provider_treasury_rebalance(
            "target-funding-1",
            _authorized_request(),
            service,
        )
    )

    assert result.status == "confirmed"


def test_hyperliquid_delta_under_remains_pending(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch, destination_amount=Decimal("300"))

    service._hl_balance = Decimal("700")
    _REBALANCE_RECORDS["target-funding-1"].status = "destination_pending"
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "destination_pending",
            "stageIndex": 1,
            "stageCount": 2,
            "stageStatus": "source_confirmed",
            "destinationAmount": "300",
        }
    ]

    result = asyncio.run(
        module.get_provider_treasury_rebalance(
            "target-funding-1",
            _authorized_request(),
            service,
        )
    )

    assert result.status == "destination_pending"
    assert service.gateway_client.execute_calls == []


def test_hyperliquid_exact_decimal_below_target_remains_pending(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch, destination_amount=Decimal("300"))

    service._hl_balance = Decimal("799.999999999999999999")
    service.gateway_client.status_results = [{
        "idempotencyKey": "target-funding-1",
        "status": "destination_pending",
        "stageIndex": 1,
        "stageCount": 2,
        "stageStatus": "source_confirmed",
        "destinationAmount": "300",
    }]

    result = asyncio.run(module.get_provider_treasury_rebalance(
        "target-funding-1", _authorized_request(), service,
    ))

    assert result.status == "destination_pending"


def test_hyperliquid_zero_delta_remains_pending(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch, destination_amount=Decimal("300"))

    service._hl_balance = Decimal("500")
    _REBALANCE_RECORDS["target-funding-1"].status = "destination_pending"
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "destination_pending",
            "stageIndex": 1,
            "stageCount": 2,
            "stageStatus": "source_confirmed",
            "destinationAmount": "300",
        }
    ]

    result = asyncio.run(
        module.get_provider_treasury_rebalance(
            "target-funding-1",
            _authorized_request(),
            service,
        )
    )

    assert result.status == "destination_pending"


def test_hyperliquid_refresh_failure_fails_closed(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch, destination_amount=Decimal("300"))

    service._refresh_error = "connection timeout"
    _REBALANCE_RECORDS["target-funding-1"].status = "destination_pending"
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "destination_pending",
            "stageIndex": 1,
            "stageCount": 2,
            "stageStatus": "source_confirmed",
            "destinationAmount": "300",
        }
    ]

    with pytest.raises(module.HTTPException) as exc:
        asyncio.run(
            module.get_provider_treasury_rebalance(
                "target-funding-1",
                _authorized_request(),
                service,
            )
        )

    assert exc.value.status_code == 502
    detail = str(exc.value.detail)
    assert "Hyperliquid" in detail
    assert "connection timeout" not in detail


def test_hyperliquid_no_usdc_row_returns_zero_and_no_confirm(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch, destination_amount=Decimal("300"))

    service._hl_balance = None
    _REBALANCE_RECORDS["target-funding-1"].status = "destination_pending"
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "destination_pending",
            "stageIndex": 1,
            "stageCount": 2,
            "stageStatus": "source_confirmed",
            "destinationAmount": "300",
        }
    ]

    result = asyncio.run(
        module.get_provider_treasury_rebalance(
            "target-funding-1",
            _authorized_request(),
            service,
        )
    )

    assert result.status == "destination_pending"


def test_non_hyperliquid_destination_pending_unchanged(monkeypatch):
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

    _REBALANCE_RECORDS["target-funding-1"].status = "destination_pending"
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "destination_pending",
            "stageIndex": 1,
            "stageCount": 2,
            "stageStatus": "source_confirmed",
        }
    ]

    result = asyncio.run(
        module.get_provider_treasury_rebalance(
            "target-funding-1",
            _authorized_request(),
            service,
        )
    )

    assert result.status == "destination_pending"
    assert len(service.update_account_state_calls) == 0


def test_hyperliquid_destination_pending_wrong_stage_ignored(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch, destination_amount=Decimal("300"))

    _REBALANCE_RECORDS["target-funding-1"].status = "destination_pending"
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "destination_pending",
            "stageIndex": 0,
            "stageCount": 2,
            "stageStatus": "built",
        }
    ]

    result = asyncio.run(
        module.get_provider_treasury_rebalance(
            "target-funding-1",
            _authorized_request(),
            service,
        )
    )

    assert result.status == "destination_pending"


def test_hyperliquid_non_usdc_destination_does_not_reconcile(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch, destination_asset="ETH", destination_amount=Decimal("300"))

    service._hl_balance = Decimal("800")
    service.gateway_client.status_results = [{
        "idempotencyKey": "target-funding-1",
        "status": "destination_pending",
        "stageIndex": 1,
        "stageCount": 2,
        "stageStatus": "source_confirmed",
        "destinationAmount": "300",
    }]

    result = asyncio.run(module.get_provider_treasury_rebalance(
        "target-funding-1", _authorized_request(), service,
    ))

    assert result.status == "destination_pending"


def test_hyperliquid_execute_retry_balance_check_confirms(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch, destination_amount=Decimal("300"))

    service._hl_balance = Decimal("800")
    _REBALANCE_RECORDS["target-funding-1"].status = "pending"
    service.gateway_client.status_results = [
        {
            "idempotencyKey": "target-funding-1",
            "status": "destination_pending",
            "stageIndex": 1,
            "stageCount": 2,
            "stageStatus": "source_confirmed",
            "destinationAmount": "300",
        },
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

    assert result.status == "confirmed"
    assert service.gateway_client.execute_calls == []


def test_hyperliquid_local_confirmation_never_regresses(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch, destination_amount=Decimal("300"))

    staged = {
        "idempotencyKey": "target-funding-1",
        "status": "destination_pending",
        "stageIndex": 1,
        "stageCount": 2,
        "stageStatus": "source_confirmed",
        "destinationAmount": "300",
    }
    service._hl_balance = Decimal("800")
    service.gateway_client.status_results = [staged]
    first = asyncio.run(module.get_provider_treasury_rebalance(
        "target-funding-1", _authorized_request(), service,
    ))
    service._hl_balance = Decimal("500")
    service.gateway_client.status_results = [staged]
    second = asyncio.run(module.get_provider_treasury_rebalance(
        "target-funding-1", _authorized_request(), service,
    ))

    assert first.status == "confirmed"
    assert second.status == "confirmed"
    assert _REBALANCE_RECORDS["target-funding-1"].status == "confirmed"


def test_confirmed_rebalance_rejects_stale_pending_writer(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    _hl_create(module, service, monkeypatch)
    record = _REBALANCE_RECORDS["target-funding-1"]
    record.status = "confirmed"
    record.response_payload = {"id": "target-funding-1", "status": "confirmed"}

    updated = asyncio.run(module._update_rebalance_status(
        _authorized_request().app.state.db_manager,
        "target-funding-1",
        {"id": "target-funding-1", "status": "destination_pending"},
    ))

    assert updated.status == "confirmed"
    assert updated.response_payload["status"] == "confirmed"


def test_hyperliquid_response_does_not_leak_baseline(monkeypatch):
    module = _provider_treasury_module()
    service = FakeAccountsService()
    service._hl_balance = Decimal("500")
    monkeypatch.setenv("MARLIN_PROVIDER_INTENT_TOKEN", PROVIDER_INTENT_TOKEN)
    _stub_hl_credentials(module, monkeypatch)
    result = _hl_create(module, service, monkeypatch)

    dumped = result.model_dump(exclude_none=True)
    assert "_hl_baseline_usdc" not in dumped
    assert "USDC" not in str(dumped.get("metadata", {}))
    assert "baseline" not in str(dumped)


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
            {"headers": {"x-marlin-gateway-provider-intent-token": "gateway-token"}, "json": {}},
        )
    ]
