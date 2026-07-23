"""Optional CowSwap runtime registration and fail-closed order gate."""

from __future__ import annotations

import importlib
import hashlib
import json
import math
import os
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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


class CowSwapApprovalReceiptError(CowSwapRuntimeUnavailableError):
    """Raised when Gateway confirms an approval transaction but its receipt is incomplete."""

    def __init__(self, message: str, *, confirmed_tx_hash: str | None) -> None:
        super().__init__(message)
        self.confirmed_tx_hash = confirmed_tx_hash


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


def cowswap_runtime_balance_rows(
    runtime_dependencies: CowSwapRuntimeDependencies | None,
) -> list[dict[str, Any]]:
    """Read configured CowSwap token balances into Hummingbot API rows."""
    if runtime_dependencies is None:
        raise CowSwapRuntimeUnavailableError("CowSwap balance runtime is not initialized")
    blocker = cowswap_order_submission_blocker(
        COWSWAP_CONNECTOR_NAME,
        runtime_dependencies=runtime_dependencies,
    )
    if blocker:
        raise CowSwapRuntimeUnavailableError(blocker)
    owner_address = runtime_dependencies.owner_address
    signer_owner = getattr(runtime_dependencies.signer_provider, "owner_address", None)
    if (
        not callable(getattr(runtime_dependencies.evm_reader, "balance_of", None))
        or not runtime_dependencies.token_map
        or not isinstance(owner_address, str)
        or not owner_address
        or not isinstance(signer_owner, str)
        or signer_owner.casefold() != owner_address.casefold()
    ):
        raise CowSwapRuntimeUnavailableError("CowSwap balance runtime owner or reader is invalid")

    tokens_by_symbol: dict[str, Any] = {}
    for mapped_tokens in runtime_dependencies.token_map.values():
        if isinstance(mapped_tokens, Mapping):
            tokens = mapped_tokens.values()
        else:
            tokens = mapped_tokens
        for token in tokens:
            symbol = str(getattr(token, "symbol", "")).strip().upper()
            if symbol:
                tokens_by_symbol.setdefault(symbol, token)

    if not tokens_by_symbol:
        raise CowSwapRuntimeUnavailableError("CowSwap token map contains no readable tokens")

    rows = []
    for symbol, token in tokens_by_symbol.items():
        atomic = runtime_dependencies.evm_reader.balance_of(
            token,
            owner_address,
        )
        if not isinstance(atomic, str) or not atomic.isascii() or not atomic.isdecimal():
            raise CowSwapRuntimeUnavailableError(f"CowSwap token {symbol} returned malformed atomic balance")
        decimals = int(token.decimals)
        if not 0 <= decimals <= 255:
            raise CowSwapRuntimeUnavailableError(f"CowSwap token {symbol} has invalid decimals")
        units = Decimal(atomic) / (Decimal(10) ** decimals)
        units_float = float(units)
        if not math.isfinite(units_float):
            raise CowSwapRuntimeUnavailableError(f"CowSwap token {symbol} returned an unrepresentable balance")
        rows.append({"token": symbol, "units": units_float, "available_units": units_float, "value": 0.0})
    return rows


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
        signing_types = {key: item for key, item in types.items() if key != "EIP712Domain"}
        signing_type = next(iter(signing_types), "")
        payload: dict[str, Any] = {
            "chain": "ethereum",
            "network": self.network,
            "address": self.owner_address,
            "walletRef": self.wallet_ref,
            "domain": dict(domain),
            "types": dict(types),
            "value": dict(value),
            "liveActionAuthorization": {
                "action": "cowswap_sign_typed_data",
                "connector_id": COWSWAP_CONNECTOR_NAME,
                "network": self.network,
                "payload_hash": _canonical_payload_hash(
                    {
                        "domain": dict(domain),
                        "types": dict(types),
                        "value": dict(value),
                    },
                ),
                "scope": "provider_intent",
                "signing_type": signing_type,
                "source": "marlin",
                "wallet_address": self.owner_address,
            },
        }
        headers = {}
        token = _marlin_gateway_provider_intent_token()
        if token:
            headers["x-marlin-gateway-provider-intent-token"] = token
        response = _gateway_post(
            self.gateway_url,
            "wallet/marlin-cow/sign-typed-data",
            payload,
            headers=headers,
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
        self._transaction_timestamps: dict[str, str] = {}

    def balance_of(self, token: Any, owner: str) -> str:
        response = _gateway_post(
            self.gateway_url,
            "chains/ethereum/balances",
            {"network": self.network, "address": owner, "tokens": [token.symbol]},
        )
        balances = response.get("balances")
        if not isinstance(balances, Mapping):
            raise CowSwapRuntimeUnavailableError("Gateway balances response is missing balances")
        if token.symbol not in balances:
            raise CowSwapRuntimeUnavailableError(f"Gateway balances response is missing {token.symbol}")
        return _human_amount_to_atomic(str(balances[token.symbol]), int(token.decimals))

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

    def transaction_timestamp(self, tx_hash: str) -> str:
        """Return the authoritative confirmed block timestamp for a transaction."""
        if tx_hash in self._transaction_timestamps:
            return self._transaction_timestamps[tx_hash]
        response = _gateway_post(
            self.gateway_url,
            "chains/ethereum/poll",
            {"network": self.network, "signature": tx_hash},
        )
        status = response.get("txStatus")
        timestamp = response.get("blockTimestamp")
        if type(status) is not int or status != 1 or type(timestamp) is not int or timestamp <= 0:
            raise CowSwapRuntimeUnavailableError("Gateway transaction timestamp is not confirmed")
        try:
            value = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError) as exc:
            raise CowSwapRuntimeUnavailableError(
                "Gateway transaction timestamp is invalid",
            ) from exc
        self._transaction_timestamps[tx_hash] = value
        return value

    def approve_cowswap_allowance(self, token: Any, owner: str, spender: str, amount_atomic: str) -> dict[str, Any]:
        """Approve the exact CoW VaultRelayer spend amount through Gateway."""
        token_address = str(getattr(token, "address", "")).strip()
        owner = str(owner).strip()
        spender = str(spender).strip()
        amount_atomic = str(amount_atomic).strip()
        if not token_address or not owner or not spender or not amount_atomic.isascii() or not amount_atomic.isdecimal():
            raise CowSwapRuntimeUnavailableError("Gateway CoW approval requires non-empty addresses and atomic amount")
        if int(amount_atomic) <= 0:
            raise CowSwapRuntimeUnavailableError("Gateway CoW approval amount must be positive")

        response = _gateway_post(
            self.gateway_url,
            "wallet/marlin-cow/approve",
            {
                "chain": "ethereum",
                "network": self.network,
                "address": owner,
                "walletRef": "base:mainnet:evm_gateway",
                "tokenAddress": token_address,
                "spender": spender,
                "amountAtomic": amount_atomic,
                "liveActionAuthorization": {
                    "source": "marlin",
                    "scope": "provider_intent",
                    "action": "cowswap_approve",
                    "connector_id": COWSWAP_CONNECTOR_NAME,
                    "network": self.network,
                    "wallet_address": owner,
                    "token_address": token_address,
                    "spender_address": spender,
                    "amount_atomic": amount_atomic,
                },
            },
            headers={"x-marlin-gateway-provider-intent-token": _marlin_gateway_provider_intent_token()},
            timeout=90,
        )
        status = response.get("status")
        signature = response.get("signature")
        data = response.get("data")
        if type(status) is not int or status != 1 or not isinstance(signature, str):
            raise CowSwapRuntimeUnavailableError("Gateway CoW approval returned malformed success response")
        if re.fullmatch(r"0x[0-9a-fA-F]{64}", signature) is None:
            raise CowSwapRuntimeUnavailableError("Gateway CoW approval returned an invalid signature")
        confirmed_tx_hash = None if signature.casefold() == "0x" + "0" * 64 else signature
        if not isinstance(data, Mapping):
            raise CowSwapApprovalReceiptError(
                "Gateway CoW approval returned malformed success response",
                confirmed_tx_hash=confirmed_tx_hash,
            )
        if (
            not isinstance(data.get("tokenAddress"), str)
            or data["tokenAddress"].lower() != token_address.lower()
            or not isinstance(data.get("spender"), str)
            or data["spender"].lower() != spender.lower()
            or not isinstance(data.get("amountAtomic"), str)
            or data["amountAtomic"] != amount_atomic
        ):
            raise CowSwapApprovalReceiptError(
                "Gateway CoW approval response does not match requested binding",
                confirmed_tx_hash=confirmed_tx_hash,
            )
        try:
            fee = Decimal(str(data["fee"]))
        except (ArithmeticError, TypeError, ValueError, KeyError) as exc:
            raise CowSwapApprovalReceiptError(
                "Gateway CoW approval returned an invalid fee",
                confirmed_tx_hash=confirmed_tx_hash,
            ) from exc
        if not fee.is_finite() or fee < 0:
            raise CowSwapApprovalReceiptError(
                "Gateway CoW approval returned an invalid fee",
                confirmed_tx_hash=confirmed_tx_hash,
            )
        if signature.casefold() == "0x" + "0" * 64 and fee != 0:
            raise CowSwapRuntimeUnavailableError("Gateway CoW approval zero hash must have zero fee")
        return {"tx_hash": signature, "fee": fee}


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

    normalized_token_map = token_map or {
        "WETH-USDC": (BASE_WETH, BASE_USDC),
        "USDC-WETH": (BASE_USDC, BASE_WETH),
    }
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


async def place_cowswap_order(
    *,
    live_action_authorization: Mapping[str, Any] | None = None,
    runtime: Any | None,
    trading_pair: str,
    side: str,
    amount: str,
    order_type: str = "MARKET",
    price: str | None = None,
) -> str:
    """Delegate a CowSwap order to an initialized runtime bridge."""
    if runtime is None:
        raise CowSwapRuntimeUnavailableError("CowSwap runtime is not initialized")

    normalized_order_type = str(order_type).upper()
    if normalized_order_type not in {"LIMIT", "MARKET"}:
        raise ValueError("CowSwap order type must be LIMIT or MARKET")
    normalized_price = str(price) if price is not None else None
    if normalized_order_type == "LIMIT":
        try:
            parsed_price = Decimal(normalized_price) if normalized_price is not None else Decimal("0")
        except ArithmeticError as exc:
            raise ValueError("CowSwap LIMIT orders require a positive price") from exc
        if not parsed_price.is_finite() or parsed_price <= 0:
            raise ValueError("CowSwap LIMIT orders require a positive price")

    normalized_side = side.upper()
    if normalized_side == "SELL":
        result = await runtime.sell(
            trading_pair=trading_pair,
            amount=amount,
            order_type=normalized_order_type,
            price=normalized_price,
        )
    elif normalized_side == "BUY":
        result = await runtime.buy(
            trading_pair=trading_pair,
            amount=amount,
            order_type=normalized_order_type,
            price=normalized_price,
        )
    else:
        raise ValueError("CowSwap side must be BUY or SELL")

    client_order_id = _extract_client_order_id(result)
    if not client_order_id:
        raise CowSwapRuntimeUnavailableError(
            "CowSwap runtime did not return a client_order_id",
        )
    return client_order_id


async def cowswap_runtime_prices(
    *,
    runtime: Any | None,
    trading_pairs: list[str],
) -> dict[str, float]:
    """Quote configured CowSwap pairs and return Hummingbot-style last prices."""
    if runtime is None:
        return {"error": "CowSwap runtime is not initialized"}

    prices: dict[str, float] = {}
    errors: dict[str, str] = {}
    for trading_pair in trading_pairs:
        try:
            base_token, quote_token = runtime._tokens_for_pair(trading_pair)  # noqa: SLF001 - runtime adapter owns pair mapping.
            quote, _minimum_buy = await runtime._connector.quote_sell(  # noqa: SLF001 - quote path is connector-owned.
                base_token,
                quote_token,
                "1",
            )
            buy_amount = Decimal(_quote_field(quote, "buyAmount"))
            quote_units = buy_amount / (Decimal(10) ** int(quote_token.decimals))
            if quote_units <= 0:
                raise CowSwapRuntimeUnavailableError("CowSwap quote returned non-positive buy amount")
            prices[str(trading_pair)] = float(quote_units)
        except Exception as exc:  # noqa: BLE001 - returned as market-data error, not hidden.
            errors[str(trading_pair)] = str(exc)
    if prices:
        return prices
    return {"error": "; ".join(f"{pair}: {reason}" for pair, reason in errors.items())}


async def cowswap_runtime_order_book(
    *,
    runtime: Any | None,
    trading_pair: str,
) -> dict[str, Any]:
    """Return one executable bid and ask from independent CowSwap quotes."""
    if runtime is None:
        return {"error": "CowSwap runtime is not initialized"}
    try:
        base_token, quote_token = runtime._tokens_for_pair(trading_pair)  # noqa: SLF001
        sell_quote, _ = await runtime._connector.quote_sell(  # noqa: SLF001
            base_token,
            quote_token,
            "1",
        )
        if _object_field(sell_quote, "verified", False) is not True:
            raise CowSwapRuntimeUnavailableError("CowSwap quote is not verified")
        buy_quote, _ = await runtime._connector.quote_buy(  # noqa: SLF001
            quote_token,
            base_token,
            "1",
        )
        if _object_field(buy_quote, "verified", False) is not True:
            raise CowSwapRuntimeUnavailableError("CowSwap quote is not verified")
        now = int(datetime.now(timezone.utc).timestamp())
        if any(
            int(_quote_field(quote, "validTo")) <= now
            for quote in (sell_quote, buy_quote)
        ):
            raise CowSwapRuntimeUnavailableError("CowSwap quote is stale")
        base_scale = Decimal(10) ** int(base_token.decimals)
        quote_scale = Decimal(10) ** int(quote_token.decimals)
        bid_amount = (
            Decimal(_quote_field(sell_quote, "sellAmount"))
            + Decimal(_quote_field(sell_quote, "feeAmount"))
        ) / base_scale
        bid_quote = Decimal(_quote_field(sell_quote, "buyAmount")) / quote_scale
        ask_amount = Decimal(_quote_field(buy_quote, "buyAmount")) / base_scale
        ask_quote = (
            Decimal(_quote_field(buy_quote, "sellAmount"))
            + Decimal(_quote_field(buy_quote, "feeAmount"))
        ) / quote_scale
        bid = bid_quote / bid_amount
        ask = ask_quote / ask_amount
        if bid_amount <= 0 or ask_amount <= 0 or bid <= 0 or ask <= 0 or bid > ask:
            raise CowSwapRuntimeUnavailableError("CowSwap quotes returned an invalid market")
        return {
            "trading_pair": trading_pair,
            "bids": [[float(bid), float(bid_amount)]],
            "asks": [[float(ask), float(ask_amount)]],
            "timestamp": datetime.now(timezone.utc).timestamp(),
        }
    except Exception as exc:  # noqa: BLE001 - returned as market-data error, not hidden.
        return {"error": str(exc)}


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


def cowswap_trade_records(
    records: list[dict[str, Any]],
    *,
    account_name: str,
    evm_reader: Any,
) -> list[dict[str, Any]]:
    """Normalize settled, non-partial CoW orders as provider trade fills."""
    if not callable(getattr(evm_reader, "transaction_timestamp", None)):
        raise CowSwapRuntimeUnavailableError("CowSwap transaction timestamp reader is unavailable")
    trades: list[dict[str, Any]] = []
    for record in records:
        if str(record.get("state", "")).lower() != "filled" or record.get("partially_fillable") is True:
            continue
        trade = _cowswap_filled_trade(
            record,
            account_name=account_name,
            evm_reader=evm_reader,
        )
        if trade is not None:
            trades.append(trade)
    return trades


async def refreshed_cowswap_order_records(
    *,
    runtime: Any | None,
    runtime_dependencies: CowSwapRuntimeDependencies | None,
    trading_pair: str | None,
) -> list[dict[str, Any]]:
    """Refresh non-terminal CoW orders before exposing provider state."""
    records = cowswap_order_records(
        runtime=runtime,
        runtime_dependencies=runtime_dependencies,
    )
    if runtime is None or trading_pair is None:
        return records
    refreshed: list[dict[str, Any]] = []
    for record in records:
        client_order_id = str(record.get("client_order_id", ""))
        state = str(record.get("state", "")).lower()
        if (
            client_order_id
            and record.get("trading_pair") == trading_pair
            and state not in {"filled", "cancelled", "canceled", "expired", "failed"}
        ):
            record = await poll_cowswap_order(
                runtime=runtime,
                client_order_id=client_order_id,
            )
        refreshed.append(record)
    return refreshed


def _quote_field(quote: object, field_name: str) -> str:
    quote_payload = _object_field(quote, "quote", None)
    if quote_payload is None:
        raise CowSwapRuntimeUnavailableError("CowSwap quote payload missing quote")
    value = _object_field(quote_payload, field_name, None)
    if value is None:
        raise CowSwapRuntimeUnavailableError(f"CowSwap quote payload missing {field_name}")
    return _object_root(value)


def _object_field(value: object, field_name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(field_name, default)
    return getattr(value, field_name, default)


def _object_root(value: object) -> str:
    return str(_object_field(value, "root", value))


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


def _cowswap_filled_trade(
    record: Mapping[str, Any],
    *,
    account_name: str,
    evm_reader: Any,
) -> dict[str, Any] | None:
    trading_pair = str(record.get("trading_pair", ""))
    pair_tokens = trading_pair.split("-")
    if len(pair_tokens) != 2:
        return None
    base_symbol, quote_symbol = pair_tokens
    sell_token = _object_field(record, "sell_token", {})
    buy_token = _object_field(record, "buy_token", {})
    sell_symbol = str(_object_field(sell_token, "symbol", ""))
    buy_symbol = str(_object_field(buy_token, "symbol", ""))
    try:
        executed_sell = Decimal(str(record.get("executed_sell", "0"))).scaleb(
            -int(_object_field(sell_token, "decimals", 0)),
        )
        executed_buy = Decimal(str(record.get("executed_buy", "0"))).scaleb(
            -int(_object_field(buy_token, "decimals", 0)),
        )
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (
        not executed_sell.is_finite()
        or not executed_buy.is_finite()
        or executed_sell <= 0
        or executed_buy <= 0
    ):
        return None

    if (sell_symbol, buy_symbol) == (base_symbol, quote_symbol):
        trade_type = "SELL"
        amount = executed_sell
        quote_amount = executed_buy
    elif (sell_symbol, buy_symbol) == (quote_symbol, base_symbol):
        trade_type = "BUY"
        amount = executed_buy
        quote_amount = executed_sell
    else:
        return None

    order_uid = str(record.get("order_uid", ""))
    client_order_id = str(record.get("client_order_id", ""))
    if not order_uid or not client_order_id:
        return None
    settlement_tx_hash = str(record.get("settlement_tx_hash", ""))
    if not settlement_tx_hash:
        return None
    try:
        executed_at = evm_reader.transaction_timestamp(settlement_tx_hash)
    except CowSwapRuntimeUnavailableError:
        return None
    trade = {
        "trade_id": order_uid,
        "order_id": client_order_id,
        "client_order_id": client_order_id,
        "account_name": account_name,
        "connector_name": COWSWAP_CONNECTOR_NAME,
        "trading_pair": trading_pair,
        "trade_type": trade_type,
        "amount": format(amount, "f"),
        "price": format(quote_amount / amount, "f"),
        "fee_paid": "0",
        "fee_currency": quote_symbol,
        "settlement_tx_hash": settlement_tx_hash,
        "external_tx_id": settlement_tx_hash,
        "timestamp": executed_at,
        "executed_at": executed_at,
    }
    return trade


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


def _gateway_post(
    gateway_url: str,
    path: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 15,
) -> dict[str, Any]:
    request_headers = {"content-type": "application/json"}
    if headers is not None:
        request_headers.update(headers)
    request = urllib.request.Request(
        f"{gateway_url.rstrip('/')}/{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        context = _gateway_ssl_context() if gateway_url.rstrip("/").startswith("https://") else None
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
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


def _marlin_gateway_provider_intent_token() -> str:
    value = os.getenv("MARLIN_GATEWAY_PROVIDER_INTENT_TOKEN", "").strip()
    if value:
        return value
    file_path = os.getenv("MARLIN_GATEWAY_PROVIDER_INTENT_TOKEN_FILE", "").strip()
    if not file_path:
        return ""
    try:
        with open(file_path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _canonical_payload_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _human_amount_to_atomic(amount: str, decimals: int) -> str:
    try:
        parsed = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise CowSwapRuntimeUnavailableError("Gateway amount is not a decimal") from exc
    if not parsed.is_finite() or parsed < 0 or not 0 <= decimals <= 255:
        raise CowSwapRuntimeUnavailableError("Gateway amount must be finite and non-negative")
    _sign, digits, exponent = parsed.as_tuple()
    coefficient = "".join(map(str, digits)).lstrip("0")
    if not coefficient:
        return "0"
    scale_exponent = exponent + decimals
    if scale_exponent >= 0:
        atomic_digits = len(coefficient) + scale_exponent
        if atomic_digits > 78:
            raise CowSwapRuntimeUnavailableError("Gateway amount exceeds uint256")
        atomic = coefficient + ("0" * scale_exponent)
    else:
        fractional_digits = -scale_exponent
        if fractional_digits >= len(coefficient):
            raise CowSwapRuntimeUnavailableError("Gateway amount has more precision than token decimals")
        remainder = coefficient[-fractional_digits:]
        if any(digit != "0" for digit in remainder):
            raise CowSwapRuntimeUnavailableError("Gateway amount has more precision than token decimals")
        atomic = coefficient[:-fractional_digits]
    max_uint256 = str(2**256 - 1)
    if len(atomic) > len(max_uint256) or (
        len(atomic) == len(max_uint256) and atomic > max_uint256
    ):
        raise CowSwapRuntimeUnavailableError("Gateway amount exceeds uint256")
    return atomic


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
