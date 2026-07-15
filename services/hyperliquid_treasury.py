"""Provider-owned Hyperliquid treasury withdrawal primitives."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

import aiohttp
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import is_address, to_checksum_address, to_hex

HYPERLIQUID_EXCHANGE_URL = "https://api.hyperliquid.xyz/exchange"
HYPERLIQUID_WITHDRAWAL_FEE_USDC = Decimal(1)
_MAX_UINT64 = (1 << 64) - 1


class HyperliquidTreasuryError(RuntimeError):
    """Base error for provider-owned Hyperliquid treasury mutations."""


class ProviderRejected(HyperliquidTreasuryError):
    """The provider definitively rejected the submitted withdrawal."""


class SubmissionAmbiguous(HyperliquidTreasuryError):
    """The provider may have accepted a withdrawal whose response was lost."""


@dataclass(frozen=True, slots=True)
class HyperliquidWithdrawalEnvelope:
    """Exact signed request persisted before a withdrawal side effect."""

    action: Mapping[str, object]
    nonce: int
    signature: Mapping[str, object]

    def payload(self) -> dict[str, object]:
        """Return the exact provider payload without exposing signing authority."""
        return {
            "action": dict(self.action),
            "expiresAfter": None,
            "nonce": self.nonce,
            "signature": dict(self.signature),
            "vaultAddress": None,
        }


def source_debit_for_destination(target: Decimal) -> Decimal:
    """Return the source debit needed for a destination amount and fixed fee."""
    normalized = _usdc_amount(target)
    return normalized + HYPERLIQUID_WITHDRAWAL_FEE_USDC


def build_withdrawal_envelope(
    *,
    source_address: str,
    private_key: str,
    destination_address: str,
    amount: Decimal,
    nonce_ms: int,
) -> HyperliquidWithdrawalEnvelope:
    """Build and sign one replay-stable Hyperliquid mainnet withdrawal."""
    if not isinstance(nonce_ms, int) or isinstance(nonce_ms, bool) or not 0 < nonce_ms <= _MAX_UINT64:
        raise ValueError("Hyperliquid withdrawal nonce must be a positive uint64")
    source = _evm_address(source_address, "source")
    # Hyperliquid signs the destination text exactly; canonical lowercase keeps
    # retries stable and matches the provider's reference implementation.
    destination = _evm_address(destination_address, "destination").lower()
    try:
        wallet = Account.from_key(private_key)
    except (TypeError, ValueError) as exc:
        raise ValueError("Hyperliquid withdrawal private key is invalid") from exc
    if wallet.address.lower() != source.lower():
        raise ValueError("Hyperliquid withdrawal signer does not match source address")

    action: dict[str, object] = {
        "destination": destination,
        "amount": _decimal_text(_usdc_amount(amount)),
        "time": nonce_ms,
        "type": "withdraw3",
        "signatureChainId": "0x66eee",
        "hyperliquidChain": "Mainnet",
    }
    typed_data = {
        "domain": {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": int("0x66eee", 16),
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "types": {
            "HyperliquidTransaction:Withdraw": [
                {"name": "hyperliquidChain", "type": "string"},
                {"name": "destination", "type": "string"},
                {"name": "amount", "type": "string"},
                {"name": "time", "type": "uint64"},
            ],
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
        },
        "primaryType": "HyperliquidTransaction:Withdraw",
        "message": action,
    }
    signed = wallet.sign_message(encode_typed_data(full_message=typed_data))
    signature: dict[str, object] = {
        "r": to_hex(signed.r),
        "s": to_hex(signed.s),
        "v": signed.v,
    }
    return HyperliquidWithdrawalEnvelope(
        action=MappingProxyType(action),
        nonce=nonce_ms,
        signature=MappingProxyType(signature),
    )


async def submit_withdrawal(
    envelope: HyperliquidWithdrawalEnvelope,
    session: aiohttp.ClientSession,
) -> dict[str, Any]:
    """Submit the exact persisted envelope without creating another signature."""
    try:
        async with session.post(HYPERLIQUID_EXCHANGE_URL, json=envelope.payload()) as response:
            try:
                body = await response.json()
            except (aiohttp.ContentTypeError, ValueError) as exc:
                if response.status >= 400:
                    raise ProviderRejected(f"Hyperliquid withdrawal rejected with HTTP {response.status}") from exc
                raise SubmissionAmbiguous("Hyperliquid withdrawal response was not valid JSON") from exc
            if response.status >= 400:
                raise ProviderRejected(f"Hyperliquid withdrawal rejected with HTTP {response.status}")
    except ProviderRejected:
        raise
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise SubmissionAmbiguous("Hyperliquid withdrawal submission outcome is ambiguous") from exc

    if not isinstance(body, dict) or body.get("status") != "ok":
        raise ProviderRejected("Hyperliquid withdrawal was rejected")
    provider_response = body.get("response")
    if not isinstance(provider_response, dict) or provider_response.get("type") != "default":
        raise ProviderRejected("Hyperliquid withdrawal acceptance response is invalid")
    return body


def _evm_address(value: str, label: str) -> str:
    text = str(value).strip()
    if not is_address(text):
        raise ValueError(f"Hyperliquid withdrawal {label} address is invalid")
    return to_checksum_address(text)


def _usdc_amount(value: Decimal) -> Decimal:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Hyperliquid withdrawal amount must be decimal") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Hyperliquid withdrawal amount must be positive and finite")
    if amount.as_tuple().exponent < -6:
        raise ValueError("Hyperliquid withdrawal amount exceeds USDC precision")
    return amount


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
