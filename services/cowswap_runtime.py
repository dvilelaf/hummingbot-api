"""Optional CowSwap runtime registration and fail-closed order gate."""

from __future__ import annotations

import importlib
import json
import os
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
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
BASE_WETH = {
    "symbol": "WETH",
    "address": "0x4200000000000000000000000000000000000006",
    "decimals": 18,
}
BASE_USDC = {
    "symbol": "USDC",
    "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "decimals": 6,
}


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


class GatewayCowSigner:
    """CoW EIP-712 signer backed by Gateway-managed Ethereum wallets."""

    def __init__(
        self,
        *,
        gateway_url: str,
        network: str,
        owner_address: str,
        config: Any,
        wallet_ref: str = "base:mainnet:evm_gateway",
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.network = network
        self.owner_address = owner_address
        self.config = config
        self.wallet_ref = wallet_ref

    def sign_order_payload(self, order: dict[str, object]) -> dict[str, object]:
        from hummingbot_cowswap.cowpy import ensure_cowpy_submodule_imports
        from hummingbot_cowswap.signing import _cow_order, _validate_order_payload, settlement_contract

        ensure_cowpy_submodule_imports()
        from cowdao_cowpy.contracts.order import ORDER_TYPE_FIELDS, compute_order_uid, hash_order, normalize_order

        _validate_order_payload(self.config, order)
        domain = _signing_domain_dict(self.config)
        cow_order = _cow_order(order)
        normalized_order = normalize_order(cow_order)
        signature = self._sign_typed_data(
            domain=domain,
            types={"Order": ORDER_TYPE_FIELDS},
            value=normalized_order,
        )
        order_digest = hash_order(_signing_domain_object(self.config), cow_order)
        expected_order_uid = compute_order_uid(_signing_domain_object(self.config), cow_order, self.config.owner)
        return {
            **order,
            "signature": signature,
            "signing_scheme": "eip712",
            "verifying_contract": settlement_contract(self.config),
            "order_digest": "0x" + order_digest.hex(),
            "expected_order_uid": expected_order_uid,
        }

    def sign_order_cancellation(self, order_uids: list[str]) -> dict[str, object]:
        if not order_uids:
            raise ValueError("order cancellation requires at least one order UID")
        from hummingbot_cowswap.cowpy import ensure_cowpy_submodule_imports

        ensure_cowpy_submodule_imports()
        from cowdao_cowpy.contracts.order import CANCELLATIONS_TYPE_FIELDS

        signature = self._sign_typed_data(
            domain=_signing_domain_dict(self.config),
            types={"OrderCancellations": CANCELLATIONS_TYPE_FIELDS},
            value={"orderUids": order_uids},
        )
        return {
            "order_uids": tuple(order_uids),
            "signature": signature,
            "signing_scheme": "eip712",
        }

    def _sign_typed_data(self, *, domain: Mapping[str, Any], types: Mapping[str, Any], value: Mapping[str, Any]) -> str:
        payload: dict[str, Any] = {
            "chain": "ethereum",
            "network": self.network,
            "address": self.owner_address,
            "walletRef": self.wallet_ref,
            "domain": dict(domain),
            "types": dict(types),
            "value": dict(value),
        }
        response = _gateway_post(
            self.gateway_url,
            "wallet/marlin-cow/sign-typed-data",
            payload,
        )
        signature = response.get("signature")
        if not signature:
            raise CowSwapRuntimeUnavailableError("Gateway did not return an EIP-712 signature")
        return str(signature)


class GatewayEvmReader:
    """Synchronous EVM balance/allowance reader backed by Gateway HTTP routes."""

    def __init__(self, *, gateway_url: str, network: str) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.network = network

    def balance_of(self, token: Any, owner: str) -> str:
        response = _gateway_post(
            self.gateway_url,
            "chains/ethereum/balances",
            {"network": self.network, "address": owner, "tokens": [token.symbol]},
        )
        balances = response.get("balances")
        if not isinstance(balances, Mapping):
            raise CowSwapRuntimeUnavailableError("Gateway balances response is missing balances")
        return _human_amount_to_atomic(str(balances.get(token.symbol, "0")), int(token.decimals))

    def allowance(self, token: Any, owner: str, spender: str) -> str:
        response = _gateway_post(
            self.gateway_url,
            "chains/ethereum/allowances",
            {"network": self.network, "address": owner, "spender": spender, "tokens": [token.symbol]},
        )
        approvals = response.get("approvals")
        if not isinstance(approvals, Mapping):
            raise CowSwapRuntimeUnavailableError("Gateway allowances response is missing approvals")
        return _human_amount_to_atomic(str(approvals.get(token.symbol, "0")), int(token.decimals))


def build_cowswap_runtime(
    *,
    gateway_url: str,
    owner_address: str,
    data_dir: str | Path,
    chain_id: int = 8453,
    chain_name: str = "base",
    network: str = "base",
    env: str = "staging",
    receiver_address: str | None = None,
    app_data: str = "0x" + "00" * 32,
    slippage_bps: int = 50,
    token_map: Mapping[str, tuple[Mapping[str, Any], Mapping[str, Any]]] | None = None,
    import_module: ImportModule = importlib.import_module,
) -> tuple[Any, CowSwapRuntimeDependencies]:
    """Build a CowSwap adapter runtime using Gateway-managed signing and reads."""
    CoWConfig = import_module("hummingbot_cowswap.models").CoWConfig
    CoWToken = import_module("hummingbot_cowswap.models").CoWToken
    CoWConnector = import_module("hummingbot_cowswap.connector").CoWConnector
    HummingbotCoWAdapter = import_module("hummingbot_cowswap.hummingbot_adapter").HummingbotCoWAdapter
    JsonOrderStore = import_module("hummingbot_cowswap.persistence").JsonOrderStore

    normalized_token_map = token_map or {"WETH-USDC": (BASE_WETH, BASE_USDC)}
    tokens_by_pair = {
        pair: (CoWToken(**base_token), CoWToken(**quote_token))
        for pair, (base_token, quote_token) in normalized_token_map.items()
    }
    config = CoWConfig(
        chain_id=chain_id,
        chain_name=chain_name,
        owner=owner_address,
        receiver=receiver_address or owner_address,
        app_data=app_data,
        slippage_bps=slippage_bps,
        env=env,
    )
    store_path = Path(data_dir) / "cowswap-orders.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    order_store = JsonOrderStore(store_path)
    signer = GatewayCowSigner(
        gateway_url=gateway_url,
        network=network,
        owner_address=owner_address,
        config=config,
    )
    evm_reader = GatewayEvmReader(gateway_url=gateway_url, network=network)
    connector = CoWConnector(
        config=config,
        store=order_store,
        signer=signer,
        evm_reader=evm_reader,
    )
    runtime = HummingbotCoWAdapter(connector, tokens_by_pair)
    dependencies = CowSwapRuntimeDependencies(
        signer_provider=signer,
        evm_reader=evm_reader,
        token_map=tokens_by_pair,
        order_store=order_store,
        owner_address=owner_address,
    )
    return runtime, dependencies


def cowswap_token_map_from_json(raw: str | None) -> Mapping[str, tuple[Mapping[str, Any], Mapping[str, Any]]] | None:
    """Parse optional token-map JSON from env without changing connector logic."""
    if raw is None or not raw.strip():
        return None
    payload = json.loads(raw)
    if not isinstance(payload, Mapping):
        raise ValueError("COWSWAP_TOKEN_MAP_JSON must be a JSON object")
    token_map: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for pair, value in payload.items():
        if isinstance(value, Mapping):
            base_token = value.get("base")
            quote_token = value.get("quote")
        elif isinstance(value, list | tuple) and len(value) == 2:
            base_token, quote_token = value
        else:
            raise ValueError(f"CowSwap token map entry for {pair} must define base and quote tokens")
        if not isinstance(base_token, Mapping) or not isinstance(quote_token, Mapping):
            raise ValueError(f"CowSwap token map entry for {pair} must contain token objects")
        token_map[str(pair)] = (base_token, quote_token)
    return token_map


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
    live_action_authorization: Mapping[str, Any] | None = None,
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


async def cancel_cowswap_order(
    *,
    live_action_authorization: Mapping[str, Any] | None = None,
    runtime: Any | None,
    client_order_id: str,
) -> str:
    """Cancel a CowSwap order through the initialized runtime adapter."""
    if runtime is None:
        raise CowSwapRuntimeUnavailableError("CowSwap runtime is not initialized")
    try:
        result = await runtime.cancel(client_order_id)
    except Exception as exc:
        if _is_terminal_cowswap_cancel_response(exc):
            return client_order_id
        raise
    cancelled_id = _extract_client_order_id(result) or client_order_id
    return cancelled_id


async def poll_cowswap_order(*, runtime: Any | None, client_order_id: str) -> dict[str, Any]:
    """Poll and serialize one CowSwap order through the initialized runtime adapter."""
    if runtime is None:
        raise CowSwapRuntimeUnavailableError("CowSwap runtime is not initialized")
    return _serialize_cowswap_order(await runtime.poll(client_order_id))


def cowswap_order_records(
    *,
    runtime: Any | None,
    runtime_dependencies: CowSwapRuntimeDependencies | None,
) -> list[dict[str, Any]]:
    """Return persisted CowSwap order evidence without claiming DB-backed orders."""
    records: list[dict[str, Any]] = []
    records.extend(_runtime_in_flight_orders(runtime))
    records.extend(_store_orders(runtime_dependencies.order_store if runtime_dependencies else None))
    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        client_order_id = str(record.get("client_order_id", ""))
        if client_order_id:
            deduped[client_order_id] = record
    return list(deduped.values())


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
    if os.environ.get("MARLIN_RUNTIME_PROFILE", "").strip().lower() == "marlin":
        if not isinstance(dependencies.signer_provider, GatewayCowSigner):
            blockers.append("CowSwap live order runtime requires a Marlin-scoped EIP-712 signer")
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


def _is_terminal_cowswap_cancel_response(exc: Exception) -> bool:
    text = str(exc)
    return "OrderFullyExecuted" in text or "Order is fully executed" in text


def _runtime_in_flight_orders(runtime: Any | None) -> list[dict[str, Any]]:
    if runtime is None:
        return []
    in_flight = getattr(runtime, "in_flight_orders", {})
    if not isinstance(in_flight, Mapping):
        return []
    return [_serialize_cowswap_order(order) for order in in_flight.values()]


def _store_orders(order_store: Any | None) -> list[dict[str, Any]]:
    if order_store is None:
        return []
    store_path = getattr(order_store, "path", None)
    if store_path is None:
        return []
    path = Path(store_path)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        return []
    return [
        _serialize_cowswap_order(raw_order)
        for raw_order in payload.values()
    ]


def _serialize_cowswap_order(order: Any) -> dict[str, Any]:
    if hasattr(order, "model_dump"):
        payload = order.model_dump(mode="json")
    elif isinstance(order, Mapping):
        payload = dict(order)
    else:
        payload = {
            name: getattr(order, name)
            for name in (
                "client_order_id",
                "trading_pair",
                "order_uid",
                "state",
                "raw_status",
                "executed_sell",
                "executed_buy",
                "settlement_tx_hash",
            )
            if hasattr(order, name)
        }
    normalized = dict(payload)
    state = normalized.get("state")
    if state is not None and not isinstance(state, str):
        normalized["state"] = getattr(state, "name", str(state))
    normalized["connector_name"] = COWSWAP_CONNECTOR_NAME
    normalized["exchange_order_id"] = normalized.get("order_uid")
    return normalized


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


def _signing_domain_object(config: Any) -> Any:
    from hummingbot_cowswap.signing import _signing_domain

    return _signing_domain(config)


def _signing_domain_dict(config: Any) -> dict[str, Any]:
    domain = _signing_domain_object(config)
    to_dict = getattr(domain, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return {
        "name": domain.name,
        "version": domain.version,
        "chainId": domain.chainId,
        "verifyingContract": domain.verifyingContract,
    }


def _gateway_ssl_context() -> ssl.SSLContext | None:
    ca_cert = os.getenv("GATEWAY_CA_CERT_FILE", "").strip()
    client_cert = os.getenv("GATEWAY_CLIENT_CERT_FILE", "").strip()
    client_key = os.getenv("GATEWAY_CLIENT_KEY_FILE", "").strip()
    if os.getenv("GATEWAY_TLS_SKIP_HOSTNAME_VERIFY", "").strip().lower() in {"1", "true", "yes", "on"}:
        context = ssl._create_unverified_context()
    else:
        context = ssl.create_default_context(cafile=ca_cert or None)
    if client_cert and client_key:
        context.load_cert_chain(certfile=client_cert, keyfile=client_key)
    return context


def _gateway_post(gateway_url: str, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{gateway_url.rstrip('/')}/{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        context = _gateway_ssl_context() if gateway_url.rstrip("/").startswith("https://") else None
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CowSwapRuntimeUnavailableError(f"Gateway {path} failed: HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise CowSwapRuntimeUnavailableError(f"Gateway {path} failed: {exc}") from exc

    if not isinstance(data, dict):
        raise CowSwapRuntimeUnavailableError(f"Gateway {path} returned a non-object response")
    if data.get("error"):
        raise CowSwapRuntimeUnavailableError(f"Gateway {path} failed: {data['error']}")
    return data


def _human_amount_to_atomic(amount: str, decimals: int) -> str:
    parsed = Decimal(amount)
    if parsed <= 0:
        return "0"
    scale = Decimal(10) ** decimals
    atomic = parsed * scale
    if atomic != atomic.to_integral_value():
        atomic = atomic.quantize(Decimal("1"))
    return str(int(atomic))


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
