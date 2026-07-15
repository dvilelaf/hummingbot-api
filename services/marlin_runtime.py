"""Marlin runtime profile guards for Hummingbot API surfaces."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from bip_utils import Bip39SeedGenerator, Bip44, Bip44Changes, Bip44Coins, MnemonicChecksumError
from fastapi import HTTPException

MARLIN_RUNTIME_PROFILE_ENV = "MARLIN_RUNTIME_PROFILE"
MARLIN_RUNTIME_PROFILE = "marlin"
HUMMINGBOT_API_RUNTIME_PROFILE_ENV = "HUMMINGBOT_API_RUNTIME_PROFILE"
HUMMINGBOT_API_PROVIDER_PROFILES = frozenset({"provider", "marlin"})
MARLIN_MNEMONIC_DERIVED_CREDENTIAL_FLAG = "__marlin_mnemonic_derived__"
HYPERLIQUID_CREDENTIAL_PREFIXES = {
    "hyperliquid": "hyperliquid",
    "hyperliquid_perpetual": "hyperliquid_perpetual",
    "hyperliquidperpetual": "hyperliquid_perpetual",
}
WALLET_AUTHORITY_CONFIG_KEYS = frozenset(
    {
        "defaultwallet",
        "default_wallet",
        "derivationpath",
        "derivation_path",
        "mnemonic",
        "privatekey",
        "private_key",
        "seed",
        "secretkey",
        "secret_key",
        "seedphrase",
        "seed_phrase",
        "wallet",
        "walletaddress",
        "wallet_address",
        "walletfile",
        "wallet_file",
        "walletkey",
        "wallet_key",
    },
)
WALLET_AUTHORITY_NAMESPACES = frozenset(
    {
        "arbitrum",
        "base",
        "ethereum",
        "evm",
        "hyperliquid",
        "hyperliquidtestnet",
        "hyperliquid_testnet",
        "solana",
        "xrp-ledger",
        "xrpledger",
        "xrpl",
    },
)
MNEMONIC_DERIVED_CREDENTIAL_KEYS = {
    "hyperliquid": frozenset({"hyperliquid_address", "hyperliquid_secret_key"}),
    "hyperliquid_perpetual": frozenset(
        {"hyperliquid_perpetual_address", "hyperliquid_perpetual_secret_key"},
    ),
    "hyperliquidperpetual": frozenset(
        {"hyperliquid_perpetual_address", "hyperliquid_perpetual_secret_key"},
    ),
    "hyperliquidtestnet": frozenset(
        {"hyperliquid_testnet_address", "hyperliquid_testnet_secret_key"},
    ),
    "hyperliquid_testnet": frozenset(
        {"hyperliquid_testnet_address", "hyperliquid_testnet_secret_key"},
    ),
    "xrpl": frozenset({"xrpl_secret_key"}),
    "xrpledger": frozenset({"xrpl_secret_key"}),
    "xrp-ledger": frozenset({"xrpl_secret_key"}),
}
GATEWAY_WALLET_POLICIES = {
    ("ethereum", "mainnet"): ("m/44'/60'/10'/0/0", "mainnet:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("ethereum", "sepolia"): ("m/44'/60'/1'/0/0", "ethereum:sepolia:evm_gateway", Bip44Coins.ETHEREUM),
    ("base", "mainnet"): ("m/44'/60'/0'/0/0", "base:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("base", "sepolia"): ("m/44'/60'/11'/0/0", "base:sepolia:evm_gateway", Bip44Coins.ETHEREUM),
    ("arbitrum", "mainnet"): ("m/44'/60'/20'/0/0", "arbitrum:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("arbitrum", "sepolia"): ("m/44'/60'/21'/0/0", "arbitrum:sepolia:evm_gateway", Bip44Coins.ETHEREUM),
    ("avalanche", "mainnet"): ("m/44'/60'/30'/0/0", "avalanche:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("optimism", "mainnet"): ("m/44'/60'/31'/0/0", "optimism:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("polygon", "mainnet"): ("m/44'/60'/32'/0/0", "polygon:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("unichain", "mainnet"): ("m/44'/60'/40'/0/0", "unichain:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("linea", "mainnet"): ("m/44'/60'/41'/0/0", "linea:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("codex", "mainnet"): ("m/44'/60'/42'/0/0", "codex:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("sonic", "mainnet"): ("m/44'/60'/43'/0/0", "sonic:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("world-chain", "mainnet"): ("m/44'/60'/44'/0/0", "world-chain:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("monad", "mainnet"): ("m/44'/60'/45'/0/0", "monad:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("sei", "mainnet"): ("m/44'/60'/46'/0/0", "sei:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("xdc", "mainnet"): ("m/44'/60'/47'/0/0", "xdc:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("hyperevm", "mainnet"): ("m/44'/60'/48'/0/0", "hyperevm:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("ink", "mainnet"): ("m/44'/60'/49'/0/0", "ink:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("plume", "mainnet"): ("m/44'/60'/50'/0/0", "plume:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("edge", "mainnet"): ("m/44'/60'/51'/0/0", "edge:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("injective", "mainnet"): ("m/44'/60'/52'/0/0", "injective:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("morph", "mainnet"): ("m/44'/60'/53'/0/0", "morph:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("pharos", "mainnet"): ("m/44'/60'/54'/0/0", "pharos:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("cronos", "mainnet"): ("m/44'/60'/55'/0/0", "cronos:mainnet:evm_gateway", Bip44Coins.ETHEREUM),
    ("solana", "mainnet-beta"): ("m/44'/501'/0'/0'", "solana:mainnet-beta:solana_gateway", Bip44Coins.SOLANA),
    ("solana", "devnet"): ("m/44'/501'/1'/0'", "solana:devnet:solana_gateway", Bip44Coins.SOLANA),
}
GATEWAY_WALLET_ALIASES = {
    ("ethereum", "base"): ("base", "mainnet"),
    ("ethereum", "base-mainnet"): ("base", "mainnet"),
    ("ethereum", "ethereum-base"): ("base", "mainnet"),
    ("ethereum", "ethereum-base-mainnet"): ("base", "mainnet"),
    ("ethereum", "base-sepolia"): ("base", "sepolia"),
    ("ethereum", "ethereum-base-sepolia"): ("base", "sepolia"),
    ("ethereum", "arbitrum"): ("arbitrum", "mainnet"),
    ("ethereum", "arbitrum-mainnet"): ("arbitrum", "mainnet"),
    ("ethereum", "arbitrum-sepolia"): ("arbitrum", "sepolia"),
    ("ethereum", "arbitrum-one"): ("arbitrum", "mainnet"),
    ("ethereum", "ethereum-arbitrum-mainnet"): ("arbitrum", "mainnet"),
    ("ethereum", "avalanche"): ("avalanche", "mainnet"),
    ("ethereum", "avalanche-mainnet"): ("avalanche", "mainnet"),
    ("ethereum", "ethereum-avalanche-mainnet"): ("avalanche", "mainnet"),
    ("ethereum", "codex"): ("codex", "mainnet"),
    ("ethereum", "codex-mainnet"): ("codex", "mainnet"),
    ("ethereum", "ethereum-codex-mainnet"): ("codex", "mainnet"),
    ("ethereum", "cronos"): ("cronos", "mainnet"),
    ("ethereum", "cronos-mainnet"): ("cronos", "mainnet"),
    ("ethereum", "ethereum-cronos-mainnet"): ("cronos", "mainnet"),
    ("ethereum", "edge"): ("edge", "mainnet"),
    ("ethereum", "edge-mainnet"): ("edge", "mainnet"),
    ("ethereum", "ethereum-edge-mainnet"): ("edge", "mainnet"),
    ("ethereum", "ethereum-mainnet"): ("ethereum", "mainnet"),
    ("ethereum", "mainnet"): ("ethereum", "mainnet"),
    ("ethereum", "hyperevm"): ("hyperevm", "mainnet"),
    ("ethereum", "hyperevm-mainnet"): ("hyperevm", "mainnet"),
    ("ethereum", "ethereum-hyperevm-mainnet"): ("hyperevm", "mainnet"),
    ("ethereum", "ink"): ("ink", "mainnet"),
    ("ethereum", "ink-mainnet"): ("ink", "mainnet"),
    ("ethereum", "ethereum-ink-mainnet"): ("ink", "mainnet"),
    ("ethereum", "injective"): ("injective", "mainnet"),
    ("ethereum", "injective-mainnet"): ("injective", "mainnet"),
    ("ethereum", "ethereum-injective-mainnet"): ("injective", "mainnet"),
    ("ethereum", "linea"): ("linea", "mainnet"),
    ("ethereum", "linea-mainnet"): ("linea", "mainnet"),
    ("ethereum", "ethereum-linea-mainnet"): ("linea", "mainnet"),
    ("ethereum", "monad"): ("monad", "mainnet"),
    ("ethereum", "monad-mainnet"): ("monad", "mainnet"),
    ("ethereum", "ethereum-monad-mainnet"): ("monad", "mainnet"),
    ("ethereum", "morph"): ("morph", "mainnet"),
    ("ethereum", "morph-mainnet"): ("morph", "mainnet"),
    ("ethereum", "ethereum-morph-mainnet"): ("morph", "mainnet"),
    ("ethereum", "op-mainnet"): ("optimism", "mainnet"),
    ("ethereum", "optimism"): ("optimism", "mainnet"),
    ("ethereum", "optimism-mainnet"): ("optimism", "mainnet"),
    ("ethereum", "ethereum-optimism-mainnet"): ("optimism", "mainnet"),
    ("ethereum", "pharos"): ("pharos", "mainnet"),
    ("ethereum", "pharos-mainnet"): ("pharos", "mainnet"),
    ("ethereum", "ethereum-pharos-mainnet"): ("pharos", "mainnet"),
    ("ethereum", "plume"): ("plume", "mainnet"),
    ("ethereum", "plume-mainnet"): ("plume", "mainnet"),
    ("ethereum", "ethereum-plume-mainnet"): ("plume", "mainnet"),
    ("ethereum", "polygon"): ("polygon", "mainnet"),
    ("ethereum", "polygon-mainnet"): ("polygon", "mainnet"),
    ("ethereum", "polygon-pos"): ("polygon", "mainnet"),
    ("ethereum", "ethereum-polygon-mainnet"): ("polygon", "mainnet"),
    ("ethereum", "sei"): ("sei", "mainnet"),
    ("ethereum", "sei-mainnet"): ("sei", "mainnet"),
    ("ethereum", "ethereum-sei-mainnet"): ("sei", "mainnet"),
    ("ethereum", "sonic"): ("sonic", "mainnet"),
    ("ethereum", "sonic-mainnet"): ("sonic", "mainnet"),
    ("ethereum", "ethereum-sonic-mainnet"): ("sonic", "mainnet"),
    ("ethereum", "unichain"): ("unichain", "mainnet"),
    ("ethereum", "unichain-mainnet"): ("unichain", "mainnet"),
    ("ethereum", "ethereum-unichain-mainnet"): ("unichain", "mainnet"),
    ("ethereum", "world-chain"): ("world-chain", "mainnet"),
    ("ethereum", "world-chain-mainnet"): ("world-chain", "mainnet"),
    ("ethereum", "worldchain"): ("world-chain", "mainnet"),
    ("ethereum", "worldchain-mainnet"): ("world-chain", "mainnet"),
    ("ethereum", "ethereum-world-chain-mainnet"): ("world-chain", "mainnet"),
    ("ethereum", "xdc"): ("xdc", "mainnet"),
    ("ethereum", "xdc-mainnet"): ("xdc", "mainnet"),
    ("ethereum", "ethereum-xdc-mainnet"): ("xdc", "mainnet"),
    ("solana", "solana-mainnet-beta"): ("solana", "mainnet-beta"),
    ("solana", "solana-devnet"): ("solana", "devnet"),
}


def is_marlin_runtime() -> bool:
    """Return true when this API instance is serving Marlin's runtime profile."""
    return (
        os.environ.get(MARLIN_RUNTIME_PROFILE_ENV, "").strip().lower() == MARLIN_RUNTIME_PROFILE
        or os.environ.get(HUMMINGBOT_API_RUNTIME_PROFILE_ENV, "").strip().lower()
        in HUMMINGBOT_API_PROVIDER_PROFILES
    )


def is_marlin_hyperliquid_bootstrap_scope(account_name: str, connector_name: str) -> bool:
    """Return whether the single mnemonic-backed Hyperliquid account may bootstrap."""
    return (
        os.environ.get(MARLIN_RUNTIME_PROFILE_ENV, "").strip().lower() == MARLIN_RUNTIME_PROFILE
        and account_name == "master_account"
        and connector_name == "hyperliquid_perpetual"
    )


def is_mnemonic_credential_connector(connector_name: str) -> bool:
    """Identify connectors whose signing authority is derived from MARLIN_MNEMONIC."""
    return _normalize(connector_name) in MNEMONIC_DERIVED_CREDENTIAL_KEYS


def assert_not_marlin_wallet_authority_surface(surface: str) -> None:
    """Block wallet authority mutation routes in Marlin runtime."""
    if not is_marlin_runtime():
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"{surface} is disabled in Marlin runtime; wallet authority comes only "
            "from MARLIN_MNEMONIC"
        ),
    )


def assert_gateway_config_update_allowed(
    *,
    namespace: str,
    updates: Mapping[str, Any],
) -> None:
    """Block config updates that can mutate wallet authority in Marlin runtime."""
    if not is_marlin_runtime():
        return
    namespace_key = _normalize(namespace)
    for path in updates:
        path_key = _normalize(str(path))
        if namespace_key in WALLET_AUTHORITY_NAMESPACES or _wallet_authority_path(path_key):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Gateway wallet authority config updates are disabled in Marlin "
                    "runtime; wallet authority comes only from MARLIN_MNEMONIC"
                ),
            )


def sanitize_account_credential_update(
    *,
    connector_name: str,
    credentials: Mapping[str, Any],
) -> dict[str, Any]:
    """Allow only Marlin-marked mnemonic-derived wallet credentials in runtime."""
    sanitized = dict(credentials)
    if not is_marlin_runtime():
        return sanitized
    namespace_key = _normalize(connector_name)
    expected_keys = MNEMONIC_DERIVED_CREDENTIAL_KEYS.get(namespace_key)
    marker = sanitized.pop(MARLIN_MNEMONIC_DERIVED_CREDENTIAL_FLAG, False)
    if marker is True and expected_keys is not None and expected_keys.issubset(sanitized):
        forbidden_keys = {
            str(key)
            for key in sanitized
            if _wallet_authority_path(_normalize(str(key))) and str(key) not in expected_keys
        }
        if not forbidden_keys:
            derived = _derive_marlin_credential_values(namespace_key)
            if derived is not None:
                for key, expected_value in derived.items():
                    supplied_value = sanitized.get(key)
                    if supplied_value is None:
                        raise HTTPException(
                            status_code=403,
                            detail=f"Credential {key} does not match MARLIN_MNEMONIC-derived value",
                        )
                    if key.endswith("_address") and str(supplied_value).startswith("0x"):
                        if not _addresses_equal(str(supplied_value), str(expected_value)):
                            raise HTTPException(
                                status_code=403,
                                detail=f"Credential {key} does not match MARLIN_MNEMONIC-derived value",
                            )
                    elif str(supplied_value) != str(expected_value):
                        raise HTTPException(
                            status_code=403,
                            detail=f"Credential {key} does not match MARLIN_MNEMONIC-derived value",
                        )
            return sanitized
    assert_gateway_config_update_allowed(namespace=connector_name, updates=sanitized)
    return sanitized


def assert_connector_credential_deletion_allowed(connector_name: str) -> None:
    """Keep mnemonic-derived provider credentials materialized in Marlin runtime."""
    if not is_marlin_runtime():
        return
    if is_mnemonic_credential_connector(connector_name):
        raise HTTPException(
            status_code=403,
            detail="Mnemonic-derived connector credentials cannot be deleted in Marlin runtime",
        )


def assert_marlin_default_wallet_identity(
    *,
    chain: str,
    network: str,
    address: str,
    wallet_ref: str,
) -> None:
    """Verify scoped default identity is derived from MARLIN_MNEMONIC."""
    canonical = _canonical_gateway_wallet_context(chain=chain, network=network)
    policy = GATEWAY_WALLET_POLICIES.get(canonical)
    if policy is None:
        raise HTTPException(status_code=400, detail=f"Unsupported Marlin wallet policy: {chain}/{network}")
    derivation_path, expected_wallet_ref, coin = policy
    if wallet_ref != expected_wallet_ref:
        raise HTTPException(status_code=400, detail="wallet_ref does not match Marlin wallet policy")
    expected_address = _derive_marlin_public_address(derivation_path=derivation_path, coin=coin)
    if not _addresses_equal(address, expected_address):
        raise HTTPException(
            status_code=403,
            detail="wallet address does not match MARLIN_MNEMONIC-derived policy address",
        )


def _wallet_authority_path(path: str) -> bool:
    return any(key in path for key in WALLET_AUTHORITY_CONFIG_KEYS)


def _normalize(value: str) -> str:
    return value.strip().lower().replace("-", "").replace(".", "").replace("/", "")


def _canonical_gateway_wallet_context(*, chain: str, network: str) -> tuple[str, str]:
    normalized = (chain.strip().lower().replace("_", "-"), network.strip().lower().replace("_", "-"))
    return GATEWAY_WALLET_ALIASES.get(normalized, normalized)


def _derive_marlin_public_address(*, derivation_path: str, coin: Bip44Coins) -> str:
    mnemonic = os.environ.get("MARLIN_MNEMONIC", "").strip()
    if not mnemonic:
        raise HTTPException(status_code=500, detail="MARLIN_MNEMONIC is required for wallet identity verification")
    if len(mnemonic) >= 2 and mnemonic[0] in {"'", '"'} and mnemonic[-1] == mnemonic[0]:
        mnemonic = mnemonic[1:-1].strip()
    try:
        seed_bytes = Bip39SeedGenerator(mnemonic).Generate()
    except (MnemonicChecksumError, ValueError):
        raise HTTPException(status_code=500, detail="MARLIN_MNEMONIC is invalid") from None
    wallet = Bip44.FromSeed(seed_bytes, coin).Purpose().Coin().Account(_account_index(derivation_path))
    if coin is Bip44Coins.SOLANA:
        return str(wallet.Change(Bip44Changes.CHAIN_EXT).PublicKey().ToAddress())
    return str(wallet.Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PublicKey().ToAddress())


def _account_index(derivation_path: str) -> int:
    match = re.fullmatch(r"m/44'/\d+'/(?P<account>\d+)'/0(?:'|/0)", derivation_path)
    if match is None:
        raise HTTPException(status_code=500, detail="unsupported Marlin wallet derivation path")
    return int(match.group("account"))


def _addresses_equal(left: str, right: str) -> bool:
    if left == right:
        return True
    return left.startswith("0x") and right.startswith("0x") and left.lower() == right.lower()


def _derive_marlin_credential_values(namespace_key: str) -> dict[str, str] | None:
    """Derive expected wallet credential values from MARLIN_MNEMONIC."""
    hyperliquid_prefix = HYPERLIQUID_CREDENTIAL_PREFIXES.get(namespace_key)
    if hyperliquid_prefix is not None:
        return _derive_hyperliquid_credentials(prefix=hyperliquid_prefix)
    if namespace_key in {"hyperliquidtestnet", "hyperliquid_testnet"}:
        return _derive_hyperliquid_credentials(prefix="hyperliquid_testnet")
    if namespace_key in {"xrpl", "xrpledger", "xrp-ledger"}:
        return _derive_xrpl_credentials()
    return None


def _derive_hyperliquid_credentials(*, prefix: str) -> dict[str, str]:
    seed_bytes = _marlin_seed_bytes()
    wallet = Bip44.FromSeed(seed_bytes, Bip44Coins.ETHEREUM).Purpose().Coin().Account(20)
    address = wallet.Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
    return {
        f"{prefix}_address": str(address.PublicKey().ToAddress()),
        f"{prefix}_secret_key": f"0x{address.PrivateKey().Raw().ToHex()}",
    }


def _derive_xrpl_credentials() -> dict[str, str]:
    seed_bytes = _marlin_seed_bytes()
    wallet = Bip44.FromSeed(seed_bytes, Bip44Coins.RIPPLE).Purpose().Coin().Account(0)
    secret_hex = wallet.Change(Bip44Changes.CHAIN_EXT).AddressIndex(0).PrivateKey().Raw().ToHex()
    return {"xrpl_secret_key": f"00{secret_hex}"}


def _marlin_seed_bytes() -> bytes:
    mnemonic = os.environ.get("MARLIN_MNEMONIC", "").strip()
    if not mnemonic:
        raise HTTPException(
            status_code=500,
            detail="MARLIN_MNEMONIC is required for credential verification",
        )
    if len(mnemonic) >= 2 and mnemonic[0] in {"'", '"'} and mnemonic[-1] == mnemonic[0]:
        mnemonic = mnemonic[1:-1].strip()
    try:
        return Bip39SeedGenerator(mnemonic).Generate()
    except (MnemonicChecksumError, ValueError):
        raise HTTPException(status_code=500, detail="MARLIN_MNEMONIC is invalid") from None
