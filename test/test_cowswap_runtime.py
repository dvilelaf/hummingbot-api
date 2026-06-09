import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "cowswap_runtime.py"
ROOT = MODULE_PATH.parents[1]
spec = importlib.util.spec_from_file_location("cowswap_runtime_under_test", MODULE_PATH)
cowswap_runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cowswap_runtime)

COWSWAP_CONNECTOR_NAME = cowswap_runtime.COWSWAP_CONNECTOR_NAME
CowSwapRuntimeUnavailableError = cowswap_runtime.CowSwapRuntimeUnavailableError
build_cowswap_runtime_connector = cowswap_runtime.build_cowswap_runtime_connector
cowswap_connector_config_map = cowswap_runtime.cowswap_connector_config_map
cowswap_connector_metadata = cowswap_runtime.cowswap_connector_metadata
cowswap_order_submission_blocker = cowswap_runtime.cowswap_order_submission_blocker
cowswap_supported_order_types = cowswap_runtime.cowswap_supported_order_types
get_cowswap_runtime_status = cowswap_runtime.get_cowswap_runtime_status


def missing_importer(name):
    raise ModuleNotFoundError(name)


def metadata_importer(metadata):
    def importer(name):
        if name == "hummingbot_cowswap.runtime_metadata":
            return SimpleNamespace(connector_metadata=lambda: metadata)
        raise ModuleNotFoundError(name)

    return importer


def runtime_importer(metadata=None, build_result=None):
    metadata = metadata or {
        "connector": COWSWAP_CONNECTOR_NAME,
        "config_map": {"uses_raw_private_key": False},
        "order_types": ["MARKET"],
    }

    def importer(name):
        if name == "hummingbot_cowswap.runtime_metadata":
            return SimpleNamespace(connector_metadata=lambda: metadata)
        if name == "hummingbot_cowswap.runtime_bridge":
            return SimpleNamespace(
                build_cowswap_runtime_bridge=lambda **kwargs: build_result or {"bridge_kwargs": kwargs}
            )
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
    importer = metadata_importer(metadata)

    status = get_cowswap_runtime_status(import_module=importer)

    assert status.registration_available is True
    assert status.runtime_available is False
    assert cowswap_connector_metadata(import_module=importer) == metadata
    assert cowswap_connector_config_map(import_module=importer) == {
        "owner_address": {"type": "str", "required": True},
        "uses_raw_private_key": {"type": "bool", "required": False, "default": False},
    }
    assert cowswap_supported_order_types(import_module=importer) == ["MARKET"]
    assert "CowSwap runtime signer/EVM reader/token map is not wired" in cowswap_order_submission_blocker(
        COWSWAP_CONNECTOR_NAME,
        import_module=importer,
    )


def test_non_cowswap_orders_have_no_cowswap_blocker():
    assert cowswap_order_submission_blocker("binance", import_module=missing_importer) is None


def test_cowswap_runtime_connector_requires_real_runtime_dependencies():
    with pytest.raises(CowSwapRuntimeUnavailableError, match="missing signer"):
        build_cowswap_runtime_connector(
            config=object(),
            store=object(),
            signer=None,
            evm_reader=object(),
            tokens_by_pair={"USDC-WETH": (object(), object())},
            import_module=runtime_importer(),
        )


def test_cowswap_runtime_connector_delegates_to_external_bridge_when_all_dependencies_are_present():
    expected = object()

    result = build_cowswap_runtime_connector(
        config=object(),
        store=object(),
        signer=object(),
        evm_reader=object(),
        tokens_by_pair={"USDC-WETH": (object(), object())},
        import_module=runtime_importer(build_result=expected),
    )

    assert result is expected


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
