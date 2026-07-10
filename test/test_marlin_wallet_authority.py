import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "services" / "marlin_runtime.py"
spec = importlib.util.spec_from_file_location("marlin_runtime_under_test", MODULE_PATH)
marlin_runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(marlin_runtime)

MARLIN_RUNTIME_PROFILE_ENV = marlin_runtime.MARLIN_RUNTIME_PROFILE_ENV
assert_gateway_config_update_allowed = marlin_runtime.assert_gateway_config_update_allowed
assert_connector_credential_deletion_allowed = marlin_runtime.assert_connector_credential_deletion_allowed
assert_not_marlin_wallet_authority_surface = marlin_runtime.assert_not_marlin_wallet_authority_surface
sanitize_account_credential_update = marlin_runtime.sanitize_account_credential_update


def _load_accounts_router(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    deps_module = types.ModuleType("deps")

    def get_accounts_service() -> object:
        raise AssertionError("test must override get_accounts_service")

    deps_module.get_accounts_service = get_accounts_service
    services_module = types.ModuleType("services")
    services_module.__path__ = []
    accounts_service_module = types.ModuleType("services.accounts_service")

    class AccountsService:
        pass

    accounts_service_module.AccountsService = AccountsService
    models_module = types.ModuleType("models")

    class GatewayWalletCredential(BaseModel):
        chain: str
        private_key: str
        set_default: bool = True

    class MarlinDefaultWalletRequest(BaseModel):
        chain: str
        network: str
        address: str
        wallet_ref: str

    class SetDefaultWalletRequest(BaseModel):
        chain: str
        address: str

    models_module.GatewayWalletCredential = GatewayWalletCredential
    models_module.MarlinDefaultWalletRequest = MarlinDefaultWalletRequest
    models_module.SetDefaultWalletRequest = SetDefaultWalletRequest
    monkeypatch.setitem(sys.modules, "deps", deps_module)
    monkeypatch.setitem(sys.modules, "models", models_module)
    monkeypatch.setitem(sys.modules, "services", services_module)
    monkeypatch.setitem(sys.modules, "services.accounts_service", accounts_service_module)
    monkeypatch.setitem(sys.modules, "services.marlin_runtime", marlin_runtime)

    module_path = ROOT / "routers" / "accounts.py"
    spec = importlib.util.spec_from_file_location("accounts_router_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_marlin_runtime_blocks_wallet_authority_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, "marlin")

    with pytest.raises(HTTPException, match="wallet authority comes only from MARLIN_MNEMONIC"):
        assert_not_marlin_wallet_authority_surface("Gateway wallet send")


def test_marlin_runtime_blocks_wallet_authority_config_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, "marlin")

    with pytest.raises(HTTPException, match="wallet authority config updates are disabled"):
        assert_gateway_config_update_allowed(
            namespace="ethereum",
            updates={"defaultWallet": "0x1111111111111111111111111111111111111111"},
        )


@pytest.mark.parametrize(
    ("namespace", "updates"),
    [
        ("xrpl", {"xrpl_secret_key": "s...secret"}),
        ("xrp-ledger", {"xrpl_secret_key": "s...secret"}),
        ("hyperliquid", {"hyperliquid_secret_key": "0xsecret"}),
        ("hyperliquid_testnet", {"hyperliquid_testnet_secret_key": "0xsecret"}),
    ],
)
def test_marlin_runtime_blocks_connector_wallet_secret_config_updates(
    monkeypatch: pytest.MonkeyPatch,
    namespace: str,
    updates: dict[str, str],
) -> None:
    monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, "marlin")

    with pytest.raises(HTTPException, match="wallet authority config updates are disabled"):
        assert_gateway_config_update_allowed(namespace=namespace, updates=updates)


def test_wallet_admin_routes_are_absent() -> None:
    accounts_source = (ROOT / "routers" / "accounts.py").read_text()
    gateway_source = (ROOT / "routers" / "gateway.py").read_text()

    for function_name in (
        "add_gateway_wallet",
        "set_default_gateway_wallet",
        "remove_gateway_wallet",
    ):
        assert f"async def {function_name}" not in accounts_source
    for function_name in ("create_wallet", "show_private_key", "send_transaction"):
        assert f"async def {function_name}" not in gateway_source


def test_marlin_scoped_default_wallet_endpoint_is_public_identity_only() -> None:
    accounts_source = (ROOT / "routers" / "accounts.py").read_text()
    function_source = accounts_source[accounts_source.index("async def set_marlin_default_gateway_wallet") :]

    assert "MarlinDefaultWalletRequest" in function_source
    assert "is_marlin_runtime()" in function_source
    assert "wallet_ref is required" in function_source
    assert "network is required" in function_source
    assert "private_key" not in function_source
    assert "set_marlin_default_wallet(" in function_source


def test_gateway_config_update_blocks_wallet_authority_paths_before_gateway_call() -> None:
    gateway_source = (ROOT / "routers" / "gateway.py").read_text()
    function_source = gateway_source[gateway_source.index("async def update_connector_config") :]

    assert "assert_gateway_config_update_allowed(" in function_source
    assert function_source.index("assert_gateway_config_update_allowed(") < function_source.index(
        "gateway_client.ping(",
    )
    network_source = gateway_source[gateway_source.index("async def update_network_config") :]
    assert network_source.index("assert_gateway_config_update_allowed(") < network_source.index(
        "gateway_client.ping(",
    )


@pytest.mark.parametrize(
    "connector",
    [
        "hyperliquid",
        "hyperliquid_perpetual",
        "hyperliquid-perpetual",
        "hyperliquid.perpetual",
        "hyperliquid/perpetual",
        "hyperliquid_testnet",
        "xrpl",
    ],
)
def test_mnemonic_credential_deletion_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    connector: str,
) -> None:
    monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, "marlin")

    with pytest.raises(HTTPException, match="cannot be deleted"):
        assert_connector_credential_deletion_allowed(connector)


def test_account_credential_route_blocks_wallet_secret_updates_before_service_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAccountsService:
        def __init__(self) -> None:
            self.add_calls: list[dict[str, object]] = []

        async def add_credentials(
            self,
            account_name: str,
            connector_name: str,
            credentials: dict[str, object],
        ) -> None:
            self.add_calls.append(
                {
                    "account_name": account_name,
                    "connector_name": connector_name,
                    "credentials": credentials,
                },
            )

        async def delete_credentials(self, account_name: str, connector_name: str) -> None:
            raise AssertionError("delete_credentials should not be called for guarded credentials")

    accounts_module = _load_accounts_router(monkeypatch)
    fake_service = FakeAccountsService()
    app = FastAPI()
    app.include_router(accounts_module.credential_router, prefix="/accounts")
    app.dependency_overrides[accounts_module.get_accounts_service] = lambda: fake_service
    monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, "marlin")
    monkeypatch.setenv(
        "MARLIN_MNEMONIC",
        "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
    )

    client = TestClient(app)
    for connector, payload in [
        ("hyperliquid", {"hyperliquid_secret_key": "0xsecret"}),
        ("xrpl", {"xrpl_secret_key": "s...secret"}),
    ]:
        response = client.post(f"/accounts/add-credential/master_account/{connector}", json=payload)

        assert response.status_code == 403
        assert "MARLIN_MNEMONIC" in response.json()["detail"]

    assert fake_service.add_calls == []


@pytest.mark.parametrize(
    ("connector", "credential_prefix"),
    [
        ("hyperliquid", "hyperliquid"),
        ("hyperliquid_perpetual", "hyperliquid_perpetual"),
    ],
)
def test_account_credential_route_allows_marked_mnemonic_derived_credentials(
    monkeypatch: pytest.MonkeyPatch,
    connector: str,
    credential_prefix: str,
) -> None:
    class FakeAccountsService:
        def __init__(self) -> None:
            self.add_calls: list[dict[str, object]] = []

        async def add_credentials(
            self,
            account_name: str,
            connector_name: str,
            credentials: dict[str, object],
        ) -> None:
            self.add_calls.append(
                {
                    "account_name": account_name,
                    "connector_name": connector_name,
                    "credentials": credentials,
                },
            )

    accounts_module = _load_accounts_router(monkeypatch)
    fake_service = FakeAccountsService()
    app = FastAPI()
    app.include_router(accounts_module.credential_router, prefix="/accounts")
    app.dependency_overrides[accounts_module.get_accounts_service] = lambda: fake_service
    monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, "marlin")
    monkeypatch.setenv(
        "MARLIN_MNEMONIC",
        "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
    )

    response = TestClient(app).post(
        f"/accounts/add-credential/master_account/{connector}",
        json={
            "__marlin_mnemonic_derived__": True,
            f"{credential_prefix}_address": "0x60685341Bd52B4048e647F00C204138B2D1a211f",
            f"{credential_prefix}_secret_key": (
                "0x11a0c3518e4720ac640ee5bda16a5926cfac1edad0dcae96d1f32ab5a463c5f2"
            ),
        },
    )

    assert response.status_code == 201
    assert fake_service.add_calls == [
        {
            "account_name": "master_account",
            "connector_name": connector,
            "credentials": {
                f"{credential_prefix}_address": "0x60685341Bd52B4048e647F00C204138B2D1a211f",
                f"{credential_prefix}_secret_key": (
                    "0x11a0c3518e4720ac640ee5bda16a5926cfac1edad0dcae96d1f32ab5a463c5f2"
                ),
            },
        },
    ]


@pytest.mark.parametrize(
    ("connector", "credentials"),
    [
        (
            "hyperliquid",
            {
                "hyperliquid_address": "0x0000000000000000000000000000000000000123",
                "hyperliquid_secret_key": "0xsecret",
            },
        ),
        (
            "hyperliquid_perpetual",
            {
                "hyperliquid_perpetual_address": "0x0000000000000000000000000000000000000123",
                "hyperliquid_perpetual_secret_key": "0xsecret",
            },
        ),
        (
            "hyperliquid_testnet",
            {
                "hyperliquid_testnet_address": "0x0000000000000000000000000000000000000123",
                "hyperliquid_testnet_secret_key": "0xsecret",
            },
        ),
        ("xrpl", {"xrpl_secret_key": "00" + ("0" * 64)}),
    ],
)
def test_marked_wallet_credentials_must_match_fresh_mnemonic_derivation(
    monkeypatch: pytest.MonkeyPatch,
    connector: str,
    credentials: dict[str, str],
) -> None:
    monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, "marlin")
    monkeypatch.setenv(
        "MARLIN_MNEMONIC",
        "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
    )

    with pytest.raises(HTTPException, match="does not match MARLIN_MNEMONIC-derived"):
        sanitize_account_credential_update(
            connector_name=connector,
            credentials={"__marlin_mnemonic_derived__": True, **credentials},
        )


@pytest.mark.parametrize("api_profile", ["provider", "marlin"])
def test_provider_profile_alone_rejects_arbitrary_wallet_credentials(
    monkeypatch: pytest.MonkeyPatch,
    api_profile: str,
) -> None:
    monkeypatch.delenv(MARLIN_RUNTIME_PROFILE_ENV, raising=False)
    monkeypatch.setenv("HUMMINGBOT_API_RUNTIME_PROFILE", api_profile)
    monkeypatch.setenv(
        "MARLIN_MNEMONIC",
        "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
    )

    with pytest.raises(HTTPException, match="does not match MARLIN_MNEMONIC-derived"):
        sanitize_account_credential_update(
            connector_name="hyperliquid",
            credentials={
                "__marlin_mnemonic_derived__": True,
                "hyperliquid_address": "0x0000000000000000000000000000000000000123",
                "hyperliquid_secret_key": "0xsecret",
            },
        )


@pytest.mark.parametrize(
    "marlin_profile",
    [
        True,
        False,
    ],
)
@pytest.mark.parametrize(
    ("connector", "credential_prefix"),
    [
        ("hyperliquid", "hyperliquid"),
        ("hyperliquid_perpetual", "hyperliquid_perpetual"),
    ],
)
def test_failed_credential_refresh_uses_runtime_scoped_rollback(
    monkeypatch: pytest.MonkeyPatch,
    marlin_profile: bool,
    connector: str,
    credential_prefix: str,
) -> None:
    class FakeAccountsService:
        def __init__(self) -> None:
            self.delete_calls: list[tuple[str, str]] = []

        async def add_credentials(
            self,
            account_name: str,
            connector_name: str,
            credentials: dict[str, object],
        ) -> None:
            raise RuntimeError("transient connector initialization failure")

        async def delete_credentials(self, account_name: str, connector_name: str) -> None:
            self.delete_calls.append((account_name, connector_name))

    accounts_module = _load_accounts_router(monkeypatch)
    fake_service = FakeAccountsService()
    app = FastAPI()
    app.include_router(accounts_module.credential_router, prefix="/accounts")
    app.dependency_overrides[accounts_module.get_accounts_service] = lambda: fake_service
    if marlin_profile:
        monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, "marlin")
    else:
        monkeypatch.delenv(MARLIN_RUNTIME_PROFILE_ENV, raising=False)
        monkeypatch.delenv("HUMMINGBOT_API_RUNTIME_PROFILE", raising=False)
    monkeypatch.setenv(
        "MARLIN_MNEMONIC",
        "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
    )

    response = TestClient(app).post(
        f"/accounts/add-credential/master_account/{connector}",
        json={
            "__marlin_mnemonic_derived__": True,
            f"{credential_prefix}_address": "0x60685341Bd52B4048e647F00C204138B2D1a211f",
            f"{credential_prefix}_secret_key": (
                "0x11a0c3518e4720ac640ee5bda16a5926cfac1edad0dcae96d1f32ab5a463c5f2"
            ),
        },
    )

    assert response.status_code == 400
    expected_delete_calls = [] if marlin_profile else [("master_account", connector)]
    assert fake_service.delete_calls == expected_delete_calls
    service_source = (ROOT / "services" / "accounts_service.py").read_text()
    assert "if not (is_marlin_runtime() and is_mnemonic_credential_connector(connector_name)):" in service_source


def test_hyperliquid_testnet_uses_the_same_chain_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, "marlin")
    monkeypatch.setenv(
        "MARLIN_MNEMONIC",
        "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
    )
    credentials = {
        "__marlin_mnemonic_derived__": True,
        "hyperliquid_testnet_address": "0x60685341Bd52B4048e647F00C204138B2D1a211f",
        "hyperliquid_testnet_secret_key": (
            "0x11a0c3518e4720ac640ee5bda16a5926cfac1edad0dcae96d1f32ab5a463c5f2"
        ),
    }

    result = sanitize_account_credential_update(
        connector_name="hyperliquid_testnet",
        credentials=credentials,
    )

    assert "__marlin_mnemonic_derived__" not in result


@pytest.mark.parametrize(
    "connector",
    [
        "hyperliquid_perpetual",
        "hyperliquid-perpetual",
        "hyperliquid.perpetual",
        "hyperliquid/perpetual",
    ],
)
def test_hyperliquid_perpetual_aliases_use_the_spot_mnemonic_derivation(
    monkeypatch: pytest.MonkeyPatch,
    connector: str,
) -> None:
    monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, "marlin")
    monkeypatch.setenv(
        "MARLIN_MNEMONIC",
        "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
    )

    result = sanitize_account_credential_update(
        connector_name=connector,
        credentials={
            "__marlin_mnemonic_derived__": True,
            "hyperliquid_perpetual_address": "0x60685341Bd52B4048e647F00C204138B2D1a211f",
            "hyperliquid_perpetual_secret_key": (
                "0x11a0c3518e4720ac640ee5bda16a5926cfac1edad0dcae96d1f32ab5a463c5f2"
            ),
        },
    )

    assert result == {
        "hyperliquid_perpetual_address": "0x60685341Bd52B4048e647F00C204138B2D1a211f",
        "hyperliquid_perpetual_secret_key": (
            "0x11a0c3518e4720ac640ee5bda16a5926cfac1edad0dcae96d1f32ab5a463c5f2"
        ),
    }


def test_marlin_scoped_default_wallet_route_forwards_public_identity_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGatewayClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def ping(self) -> bool:
            return True

        async def set_marlin_default_wallet(
            self,
            *,
            chain: str,
            network: str,
            address: str,
            wallet_ref: str,
        ) -> dict[str, object]:
            self.calls.append(
                {
                    "chain": chain,
                    "network": network,
                    "address": address,
                    "wallet_ref": wallet_ref,
                },
            )
            return {"status": "ok"}

    class FakeAccountsService:
        def __init__(self) -> None:
            self.gateway_client = FakeGatewayClient()

    accounts_module = _load_accounts_router(monkeypatch)
    fake_service = FakeAccountsService()
    app = FastAPI()
    app.include_router(accounts_module.router)
    app.dependency_overrides[accounts_module.get_accounts_service] = lambda: fake_service
    monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, "marlin")
    monkeypatch.setenv(
        "MARLIN_MNEMONIC",
        "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
    )

    response = TestClient(app).post(
        "/accounts/gateway/wallets/default",
        json={
            "chain": "ethereum",
            "network": "ethereum-base",
            "address": "0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
            "wallet_ref": "base:mainnet:evm_gateway",
        },
    )

    assert response.status_code == 200
    assert fake_service.gateway_client.calls == [
        {
            "chain": "ethereum",
            "network": "ethereum-base",
            "address": "0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
            "wallet_ref": "base:mainnet:evm_gateway",
        },
    ]


def test_marlin_scoped_default_wallet_route_rejects_non_derived_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAccountsService:
        gateway_client = object()

    accounts_module = _load_accounts_router(monkeypatch)
    app = FastAPI()
    app.include_router(accounts_module.router)
    app.dependency_overrides[accounts_module.get_accounts_service] = lambda: FakeAccountsService()
    monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, "marlin")
    monkeypatch.setenv(
        "MARLIN_MNEMONIC",
        "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
    )

    response = TestClient(app).post(
        "/accounts/gateway/wallets/default",
        json={
            "chain": "ethereum",
            "network": "ethereum-base",
            "address": "0x0000000000000000000000000000000000000123",
            "wallet_ref": "base:mainnet:evm_gateway",
        },
    )

    assert response.status_code == 403
    assert "MARLIN_MNEMONIC-derived" in response.json()["detail"]


@pytest.mark.parametrize(
    ("network", "account_index"),
    [
        ("unichain", 40),
        ("linea", 41),
        ("codex", 42),
        ("sonic", 43),
        ("world-chain", 44),
        ("monad", 45),
        ("sei", 46),
        ("xdc", 47),
        ("hyperevm", 48),
        ("ink", 49),
        ("plume", 50),
        ("edge", 51),
        ("injective", 52),
        ("morph", 53),
        ("pharos", 54),
        ("cronos", 55),
    ],
)
def test_cctp_evm_gateway_wallet_policies_cover_added_networks(network: str, account_index: int) -> None:
    canonical = marlin_runtime._canonical_gateway_wallet_context(
        chain="ethereum",
        network=f"{network}-mainnet",
    )
    derivation_path, wallet_ref, coin = marlin_runtime.GATEWAY_WALLET_POLICIES[canonical]

    assert canonical == (network, "mainnet")
    assert derivation_path == f"m/44'/60'/{account_index}'/0/0"
    assert wallet_ref == f"{network}:mainnet:evm_gateway"
    assert coin is marlin_runtime.Bip44Coins.ETHEREUM
