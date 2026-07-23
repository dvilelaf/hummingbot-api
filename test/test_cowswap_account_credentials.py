import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("hummingbot")


@pytest.mark.asyncio
async def test_cowswap_get_trades_normalizes_blocking_chain_data_off_event_loop(monkeypatch):
    from services import accounts_service as module
    from services.accounts_service import AccountsService

    event_loop_thread = threading.get_ident()

    async def refreshed_orders(**_kwargs):
        return [{"client_order_id": "cow-1"}]

    def normalized_trades(*_args, **_kwargs):
        assert threading.get_ident() != event_loop_thread
        return [{"trade_type": "BUY"}]

    monkeypatch.setattr(module, "refreshed_cowswap_order_records", refreshed_orders)
    monkeypatch.setattr(module, "cowswap_trade_records", normalized_trades)
    service = AccountsService.__new__(AccountsService)
    service._cowswap_runtime = object()
    service._cowswap_runtime_dependencies = SimpleNamespace(evm_reader=object())

    trades = await service.get_trades(connector_name="cowswap")

    assert trades == [{"trade_type": "BUY"}]


@pytest.mark.asyncio
async def test_cowswap_add_credentials_bypasses_hummingbot_core_config(tmp_path):
    from services.accounts_service import AccountsService
    from services.cowswap_runtime import COWSWAP_CONNECTOR_NAME
    from utils.file_system import fs_util

    old_base_path = fs_util.base_path
    fs_util.base_path = str(tmp_path)
    try:
        service = AccountsService.__new__(AccountsService)
        service.accounts_state = {}
        service._connector_service = MagicMock()
        service._connector_service.update_connector_keys = AsyncMock()

        with patch(
            "services.accounts_service.cowswap_connector_config_map",
            return_value={"owner_address": {"type": "str"}},
        ):
            await service.add_credentials(
                "master_account",
                COWSWAP_CONNECTOR_NAME,
                {"owner_address": "0x0000000000000000000000000000000000000001"},
            )

        service._connector_service.update_connector_keys.assert_not_awaited()
        assert (tmp_path / "credentials" / "master_account" / "connectors" / "cowswap.yml").exists()
        assert service.accounts_state == {"master_account": {"cowswap": []}}
    finally:
        fs_util.base_path = old_base_path


@pytest.mark.asyncio
async def test_global_refresh_skips_unconfigured_cowswap():
    from services.accounts_service import AccountsService

    service = AccountsService.__new__(AccountsService)
    service.accounts_state = {
        "master_account": {"cowswap": [{"token": "USDC", "units": 999.0}]},
    }
    service._connector_service = MagicMock()
    connector = object()
    service._connector_service.get_all_trading_connectors.return_value = {
        "master_account": {"binance": connector},
    }
    service._update_gateway_balances = AsyncMock()
    service._connector_balance_refresh_errors = {}
    service._get_connector_tokens_info = AsyncMock(
        return_value=[{"token": "USDC", "units": 2.0}],
    )
    service._get_cowswap_tokens_info = AsyncMock(side_effect=AssertionError("unexpected CowSwap refresh"))
    service._has_cowswap_credentials = MagicMock(return_value=False)
    service._cowswap_runtime_dependencies = object()
    service._cowswap_runtime = None
    service.list_accounts = MagicMock(return_value=["master_account"])

    with patch("services.accounts_service.cowswap_order_submission_blocker", return_value=None):
        refresh_succeeded = await service.update_account_state(skip_gateway=True)

    assert refresh_succeeded is True
    assert service.get_accounts_state() == {
        "master_account": {"binance": [{"token": "USDC", "units": 2.0}]},
    }
    service._get_connector_tokens_info.assert_awaited_once_with(connector, "binance")


@pytest.mark.asyncio
async def test_global_refresh_does_not_create_missing_master_account():
    from services.accounts_service import AccountsService

    service = AccountsService.__new__(AccountsService)
    service.accounts_state = {"other_account": {}}
    service._connector_service = MagicMock()
    connector = object()
    service._connector_service.get_all_trading_connectors.return_value = {
        "other_account": {"binance": connector},
    }
    service._update_gateway_balances = AsyncMock()
    service._connector_balance_refresh_errors = {}
    service._get_connector_tokens_info = AsyncMock(return_value=[])
    service._get_cowswap_tokens_info = AsyncMock(side_effect=AssertionError("unexpected CowSwap refresh"))
    service._has_cowswap_credentials = MagicMock(return_value=False)
    service._cowswap_runtime_dependencies = object()
    service._cowswap_runtime = None
    service.list_accounts = MagicMock(return_value=["other_account"])

    assert await service.update_account_state(skip_gateway=True) is True
    assert service.accounts_state == {"other_account": {"binance": []}}


def test_cowswap_credentials_are_skipped_by_native_connector_decryption(tmp_path):
    from utils.file_system import fs_util
    from utils.security import BackendAPISecurity

    old_base_path = fs_util.base_path
    fs_util.base_path = str(tmp_path)
    try:
        credentials_dir = tmp_path / "credentials" / "master_account" / "connectors"
        credentials_dir.mkdir(parents=True)
        (credentials_dir / "cowswap.yml").write_text(
            "connector: cowswap\n",
            encoding="utf-8",
        )

        with patch.object(BackendAPISecurity, "decrypt_connector_config") as decrypt:
            BackendAPISecurity.decrypt_all("master_account")

        decrypt.assert_not_called()
    finally:
        fs_util.base_path = old_base_path


@pytest.mark.asyncio
async def test_cowswap_portfolio_state_sees_persisted_credential_after_restart(tmp_path):
    from services.accounts_service import AccountsService
    from services.cowswap_runtime import COWSWAP_CONNECTOR_NAME
    from utils.file_system import fs_util

    old_base_path = fs_util.base_path
    fs_util.base_path = str(tmp_path)
    try:
        credentials_dir = tmp_path / "credentials" / "master_account" / "connectors"
        credentials_dir.mkdir(parents=True)
        (credentials_dir / "cowswap.yml").write_text(
            "connector: cowswap\n",
            encoding="utf-8",
        )

        service = AccountsService.__new__(AccountsService)
        service.accounts_state = {}
        service._connector_service = MagicMock()
        service._connector_service.get_all_trading_connectors.return_value = {}
        service._update_gateway_balances = AsyncMock()

        refresh_succeeded = await service.update_account_state(
            skip_gateway=True,
            account_names=["master_account"],
            connector_names=[COWSWAP_CONNECTOR_NAME],
        )

        assert refresh_succeeded is False
        assert service.get_accounts_state() == {"master_account": {"cowswap": []}}
        service._connector_service.get_all_trading_connectors.assert_called_once()
        service._update_gateway_balances.assert_not_awaited()
    finally:
        fs_util.base_path = old_base_path


@pytest.mark.asyncio
async def test_cowswap_runtime_refresh_returns_real_deduplicated_rows(tmp_path):
    from services.accounts_service import AccountsService
    from services.cowswap_runtime import COWSWAP_CONNECTOR_NAME, CowSwapRuntimeDependencies
    from models.trading import PortfolioStateFilterRequest
    from routers.portfolio import get_portfolio_state
    from utils.file_system import fs_util

    old_base_path = fs_util.base_path
    fs_util.base_path = str(tmp_path)
    try:
        (tmp_path / "credentials" / "master_account").mkdir(parents=True)

        service = AccountsService.__new__(AccountsService)
        service.accounts_state = {}
        service._connector_service = MagicMock()
        service._connector_service.get_all_trading_connectors.return_value = {}
        service._update_gateway_balances = AsyncMock()
        service._connector_balance_refresh_errors = {}
        service._cowswap_runtime = object()
        weth = SimpleNamespace(symbol="WETH", decimals=18)
        usdc = SimpleNamespace(symbol="USDC", decimals=6)

        class EvmReader:
            def __init__(self):
                self.calls = []

            def balance_of(self, token, owner):
                self.calls.append((token, owner))
                return {
                    "WETH": "1500000000000000000",
                    "USDC": "1234567",
                }[token.symbol]

        reader = EvmReader()
        owner = "0x00000000000000000000000000000000000000aa"
        service._cowswap_runtime_dependencies = CowSwapRuntimeDependencies(
            signer_provider=SimpleNamespace(owner_address=owner),
            evm_reader=reader,
            token_map={
                "WETH-USDC": (weth, usdc),
                "USDC-WETH": (usdc, weth),
            },
            order_store=object(),
            owner_address=owner,
        )

        result = await get_portfolio_state(
            PortfolioStateFilterRequest(
                refresh=True,
                skip_gateway=True,
                account_names=["master_account"],
                connector_names=[COWSWAP_CONNECTOR_NAME],
            ),
            service,
        )

        assert result == {
            "master_account": {
                "cowswap": [
                    {"token": "WETH", "units": 1.5, "available_units": 1.5, "value": 0.0},
                    {"token": "USDC", "units": 1.234567, "available_units": 1.234567, "value": 0.0},
                ],
            },
        }
        assert reader.calls == [(weth, owner), (usdc, owner)]
    finally:
        fs_util.base_path = old_base_path


@pytest.mark.asyncio
async def test_cowswap_runtime_refresh_failure_returns_false_and_clears_rows(tmp_path):
    from services.accounts_service import AccountsService
    from services.cowswap_runtime import COWSWAP_CONNECTOR_NAME, CowSwapRuntimeDependencies
    from utils.file_system import fs_util

    old_base_path = fs_util.base_path
    fs_util.base_path = str(tmp_path)
    try:
        service = AccountsService.__new__(AccountsService)
        service.accounts_state = {}
        service._connector_service = MagicMock()
        service._connector_service.get_all_trading_connectors.return_value = {}
        service._update_gateway_balances = AsyncMock()
        service._connector_balance_refresh_errors = {}
        service._cowswap_runtime = object()

        weth = SimpleNamespace(symbol="WETH", decimals=18)
        usdc = SimpleNamespace(symbol="USDC", decimals=6)

        class FailingReader:
            def balance_of(self, token, owner):
                if token.symbol == "USDC":
                    raise RuntimeError("Gateway balance read failed")
                return "1500000000000000000"

        service._cowswap_runtime_dependencies = CowSwapRuntimeDependencies(
            signer_provider=SimpleNamespace(
                owner_address="0x00000000000000000000000000000000000000aa",
            ),
            evm_reader=FailingReader(),
            token_map={"WETH-USDC": (weth, usdc)},
            order_store=object(),
            owner_address="0x00000000000000000000000000000000000000aa",
        )

        refresh_succeeded = await service.update_account_state(
            skip_gateway=True,
            account_names=["master_account"],
            connector_names=[COWSWAP_CONNECTOR_NAME],
        )

        assert refresh_succeeded is False
        assert service.get_accounts_state() == {"master_account": {"cowswap": []}}
    finally:
        fs_util.base_path = old_base_path


@pytest.mark.asyncio
async def test_cowswap_refresh_rejects_non_master_account():
    from services.accounts_service import AccountsService
    from services.cowswap_runtime import COWSWAP_CONNECTOR_NAME

    service = AccountsService.__new__(AccountsService)
    service.accounts_state = {
        "other_account": {"cowswap": [{"token": "USDC", "units": 999.0}]},
    }
    service._connector_service = MagicMock()
    service._connector_service.get_all_trading_connectors.return_value = {}
    service._update_gateway_balances = AsyncMock()

    assert await service.update_account_state(
        skip_gateway=True,
        account_names=["other_account"],
        connector_names=[COWSWAP_CONNECTOR_NAME],
    ) is False
    assert service.accounts_state == {"other_account": {}}


def test_gateway_balance_reader_rejects_missing_token():
    from services.cowswap_runtime import CowSwapRuntimeUnavailableError, GatewayEvmReader

    token = SimpleNamespace(symbol="USDC", decimals=6)
    reader = GatewayEvmReader(gateway_url="http://gateway", network="base")
    with patch("services.cowswap_runtime._gateway_post", return_value={"balances": {}}):
        with pytest.raises(CowSwapRuntimeUnavailableError, match="missing USDC"):
            reader.balance_of(token, "0x00000000000000000000000000000000000000aa")


def test_gateway_balance_reader_preserves_large_exact_amount():
    from services.cowswap_runtime import GatewayEvmReader

    token = SimpleNamespace(symbol="WETH", decimals=18)
    reader = GatewayEvmReader(gateway_url="http://gateway", network="base")
    with patch(
        "services.cowswap_runtime._gateway_post",
        return_value={"balances": {"WETH": "12345678901.123456789012345678"}},
    ):
        assert reader.balance_of(
            token,
            "0x00000000000000000000000000000000000000aa",
        ) == "12345678901123456789012345678"


@pytest.mark.parametrize(
    "amount",
    ["-1", "NaN", "Infinity", "0.0000001", "1e1000000000", "1e-1000000000"],
)
def test_gateway_balance_reader_rejects_malformed_amount(amount):
    from services.cowswap_runtime import CowSwapRuntimeUnavailableError, GatewayEvmReader

    token = SimpleNamespace(symbol="USDC", decimals=6)
    reader = GatewayEvmReader(gateway_url="http://gateway", network="base")
    with patch(
        "services.cowswap_runtime._gateway_post",
        return_value={"balances": {"USDC": amount}},
    ):
        with pytest.raises(CowSwapRuntimeUnavailableError):
            reader.balance_of(token, "0x00000000000000000000000000000000000000aa")
