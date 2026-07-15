import asyncio
from decimal import Decimal

import aiohttp
import pytest
from eth_account import Account

from services.hyperliquid_treasury import (
    HYPERLIQUID_EXCHANGE_URL,
    HYPERLIQUID_INFO_URL,
    HyperliquidTreasuryError,
    ProviderRejected,
    SubmissionAmbiguous,
    build_withdrawal_envelope,
    fetch_withdrawable_balance,
    source_debit_for_destination,
    submit_withdrawal,
)

PRIVATE_KEY = bytes.fromhex("0123456789" * 6 + "0123")
SOURCE_ADDRESS = Account.from_key(PRIVATE_KEY).address
DESTINATION_ADDRESS = "0x5e9ee1089755c3435139848e47e6635505d5a13a"
NONCE = 1687816341423


class _Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return self.body


class _Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, *, json):
        self.calls.append((url, json))
        if self.error is not None:
            raise self.error
        return self.response


def _envelope():
    return build_withdrawal_envelope(
        source_address=SOURCE_ADDRESS,
        private_key=PRIVATE_KEY,
        destination_address=DESTINATION_ADDRESS,
        amount=Decimal("1"),
        nonce_ms=NONCE,
    )


def test_build_withdrawal_matches_official_sdk_vector():
    envelope = _envelope()

    assert envelope.signature == {
        "r": "0xa155eccb6deecc343d5ce1d69ca20a6b8959cc3f21ffff6b82790e2e9f7fe888",
        "s": "0x6e78708de0806beceab552e1a97378fa80090d902bfffa8b6ee6b35d713f58c4",
        "v": 28,
    }
    assert envelope.action["type"] == "withdraw3"
    assert envelope.action["signatureChainId"] == "0x66eee"
    assert envelope.action["hyperliquidChain"] == "Mainnet"
    assert "private_key" not in envelope.payload()


def test_submit_reuses_exact_envelope_payload():
    envelope = _envelope()
    session = _Session(_Response({"status": "ok", "response": {"type": "default"}}))

    first = asyncio.run(submit_withdrawal(envelope, session))
    second = asyncio.run(submit_withdrawal(envelope, session))

    assert first == second
    assert session.calls == [
        (HYPERLIQUID_EXCHANGE_URL, envelope.payload()),
        (HYPERLIQUID_EXCHANGE_URL, envelope.payload()),
    ]


@pytest.mark.parametrize(
    "body,status",
    [
        ({"status": "err", "response": "rejected"}, 200),
        ({"status": "ok", "response": {"type": "unexpected"}}, 200),
        ({"error": "bad request"}, 400),
    ],
)
def test_submit_classifies_explicit_rejection(body, status):
    with pytest.raises(ProviderRejected):
        asyncio.run(submit_withdrawal(_envelope(), _Session(_Response(body, status))))


@pytest.mark.parametrize("error", [aiohttp.ClientConnectionError(), TimeoutError()])
def test_submit_classifies_ambiguous_transport(error):
    with pytest.raises(SubmissionAmbiguous):
        asyncio.run(submit_withdrawal(_envelope(), _Session(error=error)))


def test_build_rejects_signer_mismatch():
    with pytest.raises(ValueError, match="signer does not match"):
        build_withdrawal_envelope(
            source_address="0x0000000000000000000000000000000000000001",
            private_key=PRIVATE_KEY,
            destination_address=DESTINATION_ADDRESS,
            amount=Decimal("1"),
            nonce_ms=NONCE,
        )


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("NaN"), Decimal("1.0000001")])
def test_build_rejects_invalid_usdc_amount(amount):
    with pytest.raises(ValueError):
        build_withdrawal_envelope(
            source_address=SOURCE_ADDRESS,
            private_key=PRIVATE_KEY,
            destination_address=DESTINATION_ADDRESS,
            amount=amount,
            nonce_ms=NONCE,
        )


def test_source_debit_includes_current_provider_fee():
    assert source_debit_for_destination(Decimal("13.86191")) == Decimal("14.86191")


def test_fetch_withdrawable_balance_uses_clearinghouse_state():
    session = _Session(_Response({"withdrawable": "14.86191"}))

    balance = asyncio.run(fetch_withdrawable_balance(SOURCE_ADDRESS, session))

    assert balance == Decimal("14.86191")
    assert session.calls == [
        (
            HYPERLIQUID_INFO_URL,
            {"type": "clearinghouseState", "user": SOURCE_ADDRESS},
        )
    ]


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"withdrawable": "not-a-number"},
        {"withdrawable": "NaN"},
        {"withdrawable": "Infinity"},
        {"withdrawable": "-1"},
    ],
)
def test_fetch_withdrawable_balance_rejects_invalid_provider_state(body):
    with pytest.raises(HyperliquidTreasuryError):
        asyncio.run(fetch_withdrawable_balance(SOURCE_ADDRESS, _Session(_Response(body))))


def test_fetch_withdrawable_balance_rejects_provider_http_error():
    with pytest.raises(HyperliquidTreasuryError, match="HTTP 500"):
        asyncio.run(
            fetch_withdrawable_balance(
                SOURCE_ADDRESS,
                _Session(_Response({"withdrawable": "14.86191"}, status=500)),
            )
        )


def test_fetch_withdrawable_balance_rejects_transport_error():
    with pytest.raises(HyperliquidTreasuryError, match="balance read failed"):
        asyncio.run(
            fetch_withdrawable_balance(
                SOURCE_ADDRESS,
                _Session(error=aiohttp.ClientConnectionError()),
            )
        )
