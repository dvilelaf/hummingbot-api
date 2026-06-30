import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "cowswap_runtime.py"
ROOT = MODULE_PATH.parents[1]
spec = importlib.util.spec_from_file_location("cowswap_runtime_under_test", MODULE_PATH)
cowswap_runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cowswap_runtime)

COWSWAP_CONNECTOR_NAME = cowswap_runtime.COWSWAP_CONNECTOR_NAME
cancel_cowswap_order = cowswap_runtime.cancel_cowswap_order
cowswap_connector_config_map = cowswap_runtime.cowswap_connector_config_map
cowswap_connector_metadata = cowswap_runtime.cowswap_connector_metadata
cowswap_order_records = cowswap_runtime.cowswap_order_records
cowswap_order_submission_blocker = cowswap_runtime.cowswap_order_submission_blocker
cowswap_supported_order_types = cowswap_runtime.cowswap_supported_order_types
cowswap_token_map_from_json = cowswap_runtime.cowswap_token_map_from_json
get_cowswap_runtime_status = cowswap_runtime.get_cowswap_runtime_status
poll_cowswap_order = cowswap_runtime.poll_cowswap_order
place_cowswap_market_order = cowswap_runtime.place_cowswap_market_order
build_cowswap_runtime = cowswap_runtime.build_cowswap_runtime
CowSwapRuntimeDependencies = cowswap_runtime.CowSwapRuntimeDependencies
CowSwapRuntimeUnavailableError = cowswap_runtime.CowSwapRuntimeUnavailableError


def missing_importer(name):
    raise ModuleNotFoundError(name)


def metadata_importer(metadata):
    def importer(name):
        if name == "hummingbot_cowswap.runtime_metadata":
            return SimpleNamespace(connector_metadata=lambda: metadata)
        raise ModuleNotFoundError(name)

    return importer


def runtime_importer(metadata=None):
    metadata = metadata or {
        "connector": COWSWAP_CONNECTOR_NAME,
        "config_map": {"uses_raw_private_key": False},
        "order_types": ["MARKET"],
    }

    def importer(name):
        if name == "hummingbot_cowswap.runtime_metadata":
            return SimpleNamespace(connector_metadata=lambda: metadata)
        raise ModuleNotFoundError(name)

    return importer


def test_cowswap_is_not_registered_when_external_package_is_missing():
    status = get_cowswap_runtime_status(import_module=missing_importer)

    assert status.registration_available is False
    assert status.metadata is None
    assert "hummingbot_cowswap is not installed" in status.blockers


def test_cowswap_registration_rejects_raw_private_key_config_fields():
    metadata = {
        "connector": COWSWAP_CONNECTOR_NAME,
        "config_map": {
            "owner_address": {"type": "str", "required": True},
            "private_key": {"type": "SecretStr", "required": True},
        },
        "order_types": ["MARKET"],
    }

    status = get_cowswap_runtime_status(import_module=metadata_importer(metadata))

    assert status.registration_available is False
    assert "raw private-key config fields are not accepted by the CowSwap API registration" in status.blockers


def test_cowswap_safe_metadata_is_exposed_without_claiming_runtime_readiness():
    metadata = {
        "connector": COWSWAP_CONNECTOR_NAME,
        "config_map": {
            "owner_address": {"type": "str", "required": True},
            "uses_raw_private_key": False,
        },
        "order_types": ["MARKET"],
    }
    importer = runtime_importer(metadata=metadata)

    status = get_cowswap_runtime_status(import_module=importer)

    assert status.registration_available is True
    assert status.runtime_available is False
    assert cowswap_connector_metadata(import_module=importer) == metadata
    assert cowswap_connector_config_map(import_module=importer) == {
        "owner_address": {"type": "str", "required": True},
        "uses_raw_private_key": {"type": "bool", "required": False, "default": False},
    }
    assert cowswap_supported_order_types(import_module=importer) == ["MARKET"]
    blocker = cowswap_order_submission_blocker(COWSWAP_CONNECTOR_NAME, import_module=importer)
    assert "secure EIP-712 signer" in blocker
    assert "CoW runtime order store" in blocker
    assert "EVM balance/allowance reader" in blocker
    assert "configured token map" in blocker
    assert "raw private keys in config/env are rejected" in blocker


def test_cowswap_runtime_status_reports_unwired_execution_when_metadata_exists():
    metadata = {
        "connector": COWSWAP_CONNECTOR_NAME,
        "config_map": {"uses_raw_private_key": False},
        "order_types": ["MARKET"],
    }

    status = get_cowswap_runtime_status(import_module=metadata_importer(metadata))

    assert status.registration_available is True
    assert status.runtime_available is False
    assert len(status.blockers) == 1
    assert "order submission is disabled" in status.blockers[0]


def test_cowswap_runtime_status_can_report_ready_with_explicit_dependencies():
    metadata = {
        "connector": COWSWAP_CONNECTOR_NAME,
        "config_map": {"uses_raw_private_key": False},
        "order_types": ["MARKET"],
    }
    dependencies = CowSwapRuntimeDependencies(
        signer_provider=object(),
        evm_reader=object(),
        token_map={"WETH-USDC": object()},
        order_store=object(),
        owner_address="0x00000000000000000000000000000000000000aa",
    )

    status = get_cowswap_runtime_status(
        import_module=metadata_importer(metadata),
        runtime_dependencies=dependencies,
    )

    assert status.registration_available is True
    assert status.runtime_available is True
    assert status.blockers == ()


def test_cowswap_order_blocker_clears_only_with_explicit_runtime_dependencies():
    metadata = {
        "connector": COWSWAP_CONNECTOR_NAME,
        "config_map": {"uses_raw_private_key": False},
        "order_types": ["MARKET"],
    }
    dependencies = CowSwapRuntimeDependencies(
        signer_provider=object(),
        evm_reader=object(),
        token_map={"WETH-USDC": object()},
        order_store=object(),
        owner_address="0x00000000000000000000000000000000000000aa",
    )

    blocker = cowswap_order_submission_blocker(
        COWSWAP_CONNECTOR_NAME,
        import_module=metadata_importer(metadata),
        runtime_dependencies=dependencies,
    )

    assert blocker is None


def test_cowswap_runtime_status_names_missing_dependencies():
    metadata = {
        "connector": COWSWAP_CONNECTOR_NAME,
        "config_map": {"uses_raw_private_key": False},
        "order_types": ["MARKET"],
    }
    dependencies = CowSwapRuntimeDependencies(
        signer_provider=None,
        evm_reader=object(),
        token_map={},
        order_store=None,
        owner_address="",
    )

    status = get_cowswap_runtime_status(
        import_module=metadata_importer(metadata),
        runtime_dependencies=dependencies,
    )

    assert status.runtime_available is False
    assert any("secure EIP-712 signer" in blocker for blocker in status.blockers)
    assert any("configured token map" in blocker for blocker in status.blockers)
    assert any("CoW runtime order store" in blocker for blocker in status.blockers)
    assert any("owner address" in blocker for blocker in status.blockers)


class FakeCowSwapRuntime:
    def __init__(self):
        self.calls = []
        self.signer_authorizations = []
        self._connector = SimpleNamespace(signer=self)

    def set_live_action_authorization(self, authorization):
        self.signer_authorizations.append(authorization)

    async def sell(self, *, trading_pair, amount):
        self.calls.append(("sell", trading_pair, amount))
        return SimpleNamespace(client_order_id="sell-1")

    async def buy(self, *, trading_pair, amount):
        self.calls.append(("buy", trading_pair, amount))
        return {"client_order_id": "buy-1"}

    async def cancel(self, client_order_id):
        self.calls.append(("cancel", client_order_id))
        return {"client_order_id": client_order_id, "state": "CANCELLED"}

    async def poll(self, client_order_id):
        self.calls.append(("poll", client_order_id))
        return {
            "client_order_id": client_order_id,
            "order_uid": "0xuid",
            "state": "SUBMITTED",
        }


def test_place_cowswap_market_order_delegates_sell():
    runtime = FakeCowSwapRuntime()

    client_order_id = asyncio.run(
        place_cowswap_market_order(
            runtime=runtime,
            trading_pair="WETH-USDC",
            side="SELL",
            amount="0.01",
        ),
    )

    assert client_order_id == "sell-1"
    assert runtime.calls == [("sell", "WETH-USDC", "0.01")]


def test_place_cowswap_market_order_delegates_buy():
    runtime = FakeCowSwapRuntime()

    client_order_id = asyncio.run(
        place_cowswap_market_order(
            runtime=runtime,
            trading_pair="WETH-USDC",
            side="BUY",
            amount="5",
        ),
    )

    assert client_order_id == "buy-1"
    assert runtime.calls == [("buy", "WETH-USDC", "5")]


def test_place_cowswap_market_order_fails_closed_without_runtime():
    try:
        asyncio.run(
            place_cowswap_market_order(
                runtime=None,
                trading_pair="WETH-USDC",
                side="SELL",
                amount="0.01",
            ),
        )
    except CowSwapRuntimeUnavailableError as exc:
        assert "runtime is not initialized" in str(exc)
    else:
        raise AssertionError("expected CowSwapRuntimeUnavailableError")


def test_place_cowswap_market_order_rejects_unsupported_side():
    try:
        asyncio.run(
            place_cowswap_market_order(
                runtime=FakeCowSwapRuntime(),
                trading_pair="WETH-USDC",
                side="HOLD",
                amount="0.01",
            ),
        )
    except ValueError as exc:
        assert "side must be BUY or SELL" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_cancel_cowswap_order_delegates_to_runtime_adapter():
    runtime = FakeCowSwapRuntime()

    cancelled = asyncio.run(cancel_cowswap_order(runtime=runtime, client_order_id="cow-1"))

    assert cancelled == "cow-1"
    assert runtime.calls == [("cancel", "cow-1")]


def test_cancel_cowswap_order_ignores_live_authorization():
    runtime = FakeCowSwapRuntime()
    authorization = {"status": "approved", "action": "order_cancel"}

    cancelled = asyncio.run(
        cancel_cowswap_order(
            live_action_authorization=authorization,
            runtime=runtime,
            client_order_id="cow-1",
        ),
    )

    assert cancelled == "cow-1"
    assert runtime.calls == [("cancel", "cow-1")]
    assert runtime.signer_authorizations == []


def test_cancel_cowswap_order_treats_fully_executed_response_as_terminal():
    class FullyExecutedRuntime(FakeCowSwapRuntime):
        async def cancel(self, client_order_id):
            self.calls.append(("cancel", client_order_id))
            raise RuntimeError(
                'HTTP error 400: {"errorType":"OrderFullyExecuted","description":"Order is fully executed"}',
            )

    runtime = FullyExecutedRuntime()

    cancelled = asyncio.run(cancel_cowswap_order(runtime=runtime, client_order_id="cow-1"))

    assert cancelled == "cow-1"
    assert runtime.calls == [("cancel", "cow-1")]


def test_poll_cowswap_order_serializes_order_evidence():
    runtime = FakeCowSwapRuntime()

    payload = asyncio.run(poll_cowswap_order(runtime=runtime, client_order_id="cow-1"))

    assert payload["connector_name"] == COWSWAP_CONNECTOR_NAME
    assert payload["client_order_id"] == "cow-1"
    assert payload["exchange_order_id"] == "0xuid"


def test_cowswap_order_records_reads_json_store(tmp_path):
    store_path = tmp_path / "cowswap-orders.json"
    store_path.write_text(
        (
            '{"cow-1": {"client_order_id": "cow-1", "trading_pair": "WETH-USDC",'
            ' "order_uid": "0xuid", "state": "SUBMITTED"}}'
        ),
        encoding="utf-8",
    )
    dependencies = CowSwapRuntimeDependencies(
        signer_provider=object(),
        evm_reader=object(),
        token_map={"WETH-USDC": object()},
        order_store=SimpleNamespace(path=store_path),
        owner_address="0x00000000000000000000000000000000000000aa",
    )

    records = cowswap_order_records(runtime=None, runtime_dependencies=dependencies)

    assert records == [
        {
            "client_order_id": "cow-1",
            "connector_name": COWSWAP_CONNECTOR_NAME,
            "exchange_order_id": "0xuid",
            "order_uid": "0xuid",
            "state": "SUBMITTED",
            "trading_pair": "WETH-USDC",
        },
    ]


def test_cowswap_token_map_from_json_accepts_base_quote_object():
    token_map = cowswap_token_map_from_json(
        (
            '{"USDC-WETH": {'
            '"base": {"symbol": "USDC", "address": "0x1", "decimals": 6},'
            '"quote": {"symbol": "WETH", "address": "0x2", "decimals": 18}'
            "}}"
        ),
    )

    base_token, quote_token = token_map["USDC-WETH"]
    assert base_token["symbol"] == "USDC"
    assert quote_token["symbol"] == "WETH"


def test_cowswap_token_map_from_json_accepts_pair_array():
    token_map = cowswap_token_map_from_json(
        (
            '{"WETH-USDC": ['
            '{"symbol": "WETH", "address": "0x2", "decimals": 18},'
            '{"symbol": "USDC", "address": "0x1", "decimals": 6}'
            "]}"
        ),
    )

    base_token, quote_token = token_map["WETH-USDC"]
    assert base_token["symbol"] == "WETH"
    assert quote_token["symbol"] == "USDC"


def test_cowswap_token_map_from_json_rejects_non_object():
    try:
        cowswap_token_map_from_json("[]")
    except ValueError as exc:
        assert "must be a JSON object" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_non_cowswap_orders_have_no_cowswap_blocker():
    assert cowswap_order_submission_blocker("binance", import_module=missing_importer) is None


def test_api_files_wire_cowswap_through_fail_closed_gate():
    connectors_source = (ROOT / "routers" / "connectors.py").read_text()
    accounts_source = (ROOT / "services" / "accounts_service.py").read_text()
    unified_source = (ROOT / "services" / "unified_connector_service.py").read_text()

    assert "cowswap_connector_metadata" in connectors_source
    assert "COWSWAP_CONNECTOR_NAME" in connectors_source
    assert "cowswap_connector_config_map" in connectors_source
    assert "cowswap_supported_order_types" in connectors_source

    place_trade_index = accounts_source.index("async def place_trade")
    blocker_index = accounts_source.index("cowswap_order_submission_blocker", place_trade_index)
    connector_lookup_index = accounts_source.index(
        "get_trading_connector(account_name, connector_name)",
        blocker_index,
    )
    pre_lookup_source = accounts_source[blocker_index:connector_lookup_index]
    assert "status_code=503" in pre_lookup_source

    assert "CowSwapRuntimeUnavailableError" in unified_source
    assert "connector_name == COWSWAP_CONNECTOR_NAME" in unified_source


def test_accounts_service_uses_runtime_delegate_only_after_cowswap_dependency_gate():
    accounts_source = (ROOT / "services" / "accounts_service.py").read_text()

    assert "place_cowswap_market_order" in accounts_source
    assert "CowSwapRuntimeDependencies" in accounts_source

    place_trade_index = accounts_source.index("async def place_trade")
    dependencies_index = accounts_source.index("_cowswap_runtime_dependencies", place_trade_index)
    blocker_index = accounts_source.index("cowswap_order_submission_blocker", dependencies_index)
    runtime_delegate_index = accounts_source.index("place_cowswap_market_order", blocker_index)
    connector_lookup_index = accounts_source.index(
        "get_trading_connector(account_name, connector_name)",
        blocker_index,
    )
    cowswap_branch_source = accounts_source[dependencies_index:connector_lookup_index]

    assert "_cowswap_runtime_dependencies" in cowswap_branch_source
    assert "order_type != OrderType.MARKET" in cowswap_branch_source
    assert runtime_delegate_index < connector_lookup_index


_score_counter = 0


def _counted_ctor(returned, name):
    global _score_counter
    _score_counter += 1

    def ctor(*args, **kwargs):
        result = returned(*args, **kwargs) if callable(returned) else returned
        return result

    return ctor


def _build_runtime_importer():
    def models_ctor(**kwargs):
        return SimpleNamespace(**kwargs)

    connector_instance = SimpleNamespace()
    adapter_instance = SimpleNamespace()

    def connector_ctor(*args, **kwargs):
        return connector_instance

    def adapter_ctor(*args, **kwargs):
        return adapter_instance

    order_store_path = None

    def json_store_ctor(path):
        nonlocal order_store_path
        order_store_path = Path(path)
        return SimpleNamespace(path=order_store_path)

    modules = {
        "hummingbot_cowswap.models": SimpleNamespace(
            CoWConfig=models_ctor,
            CoWToken=models_ctor,
        ),
        "hummingbot_cowswap.connector": SimpleNamespace(
            CoWConnector=connector_ctor,
        ),
        "hummingbot_cowswap.hummingbot_adapter": SimpleNamespace(
            HummingbotCoWAdapter=adapter_ctor,
        ),
        "hummingbot_cowswap.persistence": SimpleNamespace(
            JsonOrderStore=json_store_ctor,
        ),
    }

    def importer(name):
        if name in modules:
            return modules[name]
        raise ModuleNotFoundError(name)

    return importer, connector_instance, adapter_instance


def test_build_cowswap_runtime_with_mocked_imports(tmp_path):
    importer, _, _ = _build_runtime_importer()
    data_dir = tmp_path / "data"

    runtime, dependencies = build_cowswap_runtime(
        gateway_url="http://localhost:15888",
        owner_address="0x00000000000000000000000000000000000000ab",
        data_dir=data_dir,
        chain_id=8453,
        chain_name="base",
        network="base",
        env="staging",
        app_data="0x" + "00" * 32,
        slippage_bps=50,
        import_module=importer,
    )

    assert runtime is not None
    assert dependencies.signer_provider is not None
    assert dependencies.evm_reader is not None
    assert dependencies.token_map is not None
    assert dependencies.order_store is not None
    assert dependencies.owner_address == "0x00000000000000000000000000000000000000ab"
    assert (data_dir / "cowswap-orders.json").parent.exists()


def test_cowswap_default_dependencies_are_specific_not_unwired():
    deps = CowSwapRuntimeDependencies()
    metadata = {
        "connector": COWSWAP_CONNECTOR_NAME,
        "config_map": {"uses_raw_private_key": False},
        "order_types": ["MARKET"],
    }

    status = get_cowswap_runtime_status(
        import_module=metadata_importer(metadata),
        runtime_dependencies=deps,
    )

    assert status.runtime_available is False
    assert any("secure EIP-712 signer" in b for b in status.blockers)
    assert any("EVM balance/allowance reader" in b for b in status.blockers)
    assert any("configured token map" in b for b in status.blockers)
    assert any("CoW runtime order store" in b for b in status.blockers)
    assert any("owner address" in b for b in status.blockers)


def test_cowswap_partial_dependencies_report_missing_components():
    deps = CowSwapRuntimeDependencies(
        owner_address="0x00000000000000000000000000000000000000ac",
    )
    metadata = {
        "connector": COWSWAP_CONNECTOR_NAME,
        "config_map": {"uses_raw_private_key": False},
        "order_types": ["MARKET"],
    }

    status = get_cowswap_runtime_status(
        import_module=metadata_importer(metadata),
        runtime_dependencies=deps,
    )

    assert status.runtime_available is False
    assert any("secure EIP-712 signer" in b for b in status.blockers)
    assert not any("owner address" in b for b in status.blockers)
    assert not any("order submission is disabled" in b for b in status.blockers)
