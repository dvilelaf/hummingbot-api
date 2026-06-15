from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("hummingbot")


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

        await service.update_account_state(
            skip_gateway=True,
            account_names=["master_account"],
            connector_names=[COWSWAP_CONNECTOR_NAME],
        )

        assert service.get_accounts_state() == {"master_account": {"cowswap": []}}
        service._connector_service.get_all_trading_connectors.assert_called_once()
        service._update_gateway_balances.assert_not_awaited()
    finally:
        fs_util.base_path = old_base_path
