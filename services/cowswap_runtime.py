"""Optional CowSwap runtime registration and fail-closed order gate."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

COWSWAP_CONNECTOR_NAME = "cowswap"
PACKAGE_MISSING_BLOCKER = "hummingbot_cowswap is not installed"
UNWIRED_RUNTIME_BLOCKER = (
    "CowSwap order submission is disabled: Hummingbot API only exposes safe CowSwap connector metadata. "
    "Missing runtime wiring for secure EIP-712 signer, CoW runtime order store, "
    "EVM balance/allowance reader, configured token map, and API lifecycle calls; "
    "raw private keys in config/env are rejected"
)
PRIVATE_KEY_BLOCKER = "raw private-key config fields are not accepted by the CowSwap API registration"


class CowSwapRuntimeUnavailableError(RuntimeError):
    """Raised when CowSwap runtime wiring is missing or unsafe."""


class CowSwapRuntimeStatus(NamedTuple):
    """Current optional CowSwap package and runtime readiness state."""

    registration_available: bool
    runtime_available: bool
    metadata: Mapping[str, Any] | None
    blockers: tuple[str, ...]


ImportModule = Callable[[str], Any]


class CowSwapRuntimeDependencies(NamedTuple):
    """Explicit dependencies required before CowSwap order runtime is enabled."""

    signer_provider: Any | None = None
    evm_reader: Any | None = None
    token_map: Mapping[str, Any] | None = None
    order_store: Any | None = None
    owner_address: str = ""


def get_cowswap_runtime_status(
    *,
    import_module: ImportModule = importlib.import_module,
    runtime_dependencies: CowSwapRuntimeDependencies | None = None,
) -> CowSwapRuntimeStatus:
    """Return whether CowSwap can be registered without claiming order readiness."""
    metadata = _load_connector_metadata(import_module)
    if metadata is None:
        return CowSwapRuntimeStatus(
            registration_available=False,
            runtime_available=False,
            metadata=None,
            blockers=(PACKAGE_MISSING_BLOCKER,),
        )

    blockers = _registration_blockers(metadata)
    registration_available = len(blockers) == 0
    runtime_blockers = (
        ()
        if not registration_available
        else _runtime_dependency_blockers(runtime_dependencies)
    )
    return CowSwapRuntimeStatus(
        registration_available=registration_available,
        runtime_available=registration_available and not runtime_blockers,
        metadata=metadata if registration_available else None,
        blockers=tuple(blockers) + runtime_blockers,
    )


def cowswap_connector_metadata(
    *,
    import_module: ImportModule = importlib.import_module,
) -> Mapping[str, Any] | None:
    """Return safe CowSwap connector metadata if the optional package is present."""
    status = get_cowswap_runtime_status(import_module=import_module)
    return status.metadata if status.registration_available else None


def cowswap_connector_config_map(
    *,
    import_module: ImportModule = importlib.import_module,
) -> dict[str, dict[str, Any]] | None:
    """Return the CowSwap config map advertised by the optional package."""
    metadata = cowswap_connector_metadata(import_module=import_module)
    if metadata is None:
        return None
    config_map = metadata.get("config_map")
    if not isinstance(config_map, Mapping):
        return None
    return {
        str(key): _config_field_info(value)
        for key, value in config_map.items()
    }


def cowswap_supported_order_types(
    *,
    import_module: ImportModule = importlib.import_module,
) -> list[str] | None:
    """Return CowSwap supported order types from package metadata."""
    metadata = cowswap_connector_metadata(import_module=import_module)
    if metadata is None:
        return None
    order_types = metadata.get("order_types")
    if not isinstance(order_types, (list, tuple)):
        return None
    return [str(order_type) for order_type in order_types]


def cowswap_order_submission_blocker(
    connector_name: str,
    *,
    import_module: ImportModule = importlib.import_module,
    runtime_dependencies: CowSwapRuntimeDependencies | None = None,
) -> str | None:
    """Return a pre-submit blocker for CowSwap orders, or None for other connectors."""
    if connector_name != COWSWAP_CONNECTOR_NAME:
        return None

    status = get_cowswap_runtime_status(
        import_module=import_module,
        runtime_dependencies=runtime_dependencies,
    )
    if not status.blockers:
        return None
    return "; ".join(status.blockers)


async def place_cowswap_market_order(
    *,
    runtime: Any | None,
    trading_pair: str,
    side: str,
    amount: str,
) -> str:
    """Delegate a CowSwap MARKET order to an initialized runtime bridge."""
    if runtime is None:
        raise CowSwapRuntimeUnavailableError("CowSwap runtime is not initialized")

    normalized_side = side.upper()
    if normalized_side == "SELL":
        result = await runtime.sell(
            trading_pair=trading_pair,
            amount=amount,
        )
    elif normalized_side == "BUY":
        result = await runtime.buy(
            trading_pair=trading_pair,
            amount=amount,
        )
    else:
        raise ValueError("CowSwap side must be BUY or SELL")

    client_order_id = _extract_client_order_id(result)
    if not client_order_id:
        raise CowSwapRuntimeUnavailableError(
            "CowSwap runtime did not return a client_order_id",
        )
    return client_order_id


def _load_connector_metadata(import_module: ImportModule) -> Mapping[str, Any] | None:
    try:
        metadata_module = import_module("hummingbot_cowswap.runtime_metadata")
    except ImportError:
        return None

    connector_metadata = getattr(metadata_module, "connector_metadata", None)
    if not callable(connector_metadata):
        return None

    try:
        metadata = connector_metadata()
    except ImportError:
        return None
    return metadata if isinstance(metadata, Mapping) else None


def _registration_blockers(metadata: Mapping[str, Any]) -> list[str]:
    blockers = []
    if metadata.get("connector") != COWSWAP_CONNECTOR_NAME:
        blockers.append("CowSwap metadata does not describe the cowswap connector")

    config_map = metadata.get("config_map")
    if not isinstance(config_map, Mapping):
        blockers.append("CowSwap metadata is missing a config_map")
    elif _has_raw_private_key_material(config_map):
        blockers.append(PRIVATE_KEY_BLOCKER)

    order_types = metadata.get("order_types")
    if not isinstance(order_types, (list, tuple)) or "MARKET" not in [str(order_type) for order_type in order_types]:
        blockers.append("CowSwap metadata does not advertise MARKET orders")

    return blockers


def _runtime_dependency_blockers(
    dependencies: CowSwapRuntimeDependencies | None,
) -> tuple[str, ...]:
    if dependencies is None:
        return (UNWIRED_RUNTIME_BLOCKER,)

    blockers: list[str] = []
    if dependencies.signer_provider is None:
        blockers.append("secure EIP-712 signer is missing")
    if dependencies.evm_reader is None:
        blockers.append("EVM balance/allowance reader is missing")
    if not dependencies.token_map:
        blockers.append("configured token map is missing")
    if dependencies.order_store is None:
        blockers.append("CoW runtime order store is missing")
    if not dependencies.owner_address:
        blockers.append("owner address is missing")
    return tuple(blockers)


def _extract_client_order_id(result: Any) -> str | None:
    if isinstance(result, Mapping):
        value = result.get("client_order_id")
    else:
        value = getattr(result, "client_order_id", None)
    return str(value) if value else None


def _has_raw_private_key_material(mapping: Mapping[str, Any] | None) -> bool:
    if mapping is None:
        return False

    for key, value in _walk_mapping(mapping):
        normalized = key.casefold().replace("-", "_")
        if normalized == "uses_raw_private_key":
            if value is True:
                return True
            continue

        collapsed = normalized.replace("_", "")
        if (
            "private_key" in normalized
            or "raw_private" in normalized
            or "privatekey" in collapsed
            or "rawprivate" in collapsed
        ):
            return True

    return False


def _walk_mapping(mapping: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    fields: list[tuple[str, Any]] = []
    for key, value in mapping.items():
        fields.append((str(key), value))
        if isinstance(value, Mapping):
            fields.extend(_walk_mapping(value))
    return tuple(fields)


def _config_field_info(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bool):
        return {"type": "bool", "required": False, "default": value}
    if isinstance(value, (list, tuple)):
        return {
            "type": "list",
            "required": False,
            "default": [str(item) for item in value],
        }
    return {
        "type": type(value).__name__,
        "required": False,
        "default": str(value),
    }
