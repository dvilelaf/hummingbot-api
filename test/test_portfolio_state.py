"""
Tests for Portfolio State refresh behavior.

Run with: pytest test/test_portfolio_state.py -v
"""
import inspect
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("hummingbot")


class TestPortfolioStateRefresh:
    """Tests for portfolio state refresh behavior."""

    @pytest.mark.asyncio
    async def test_refresh_true_calls_update_account_state(self):
        """refresh=True should call update_account_state."""
        from models.trading import PortfolioStateFilterRequest
        from routers.portfolio import get_portfolio_state

        mock_service = MagicMock()
        mock_service.update_account_state = AsyncMock()
        mock_service.get_accounts_state.return_value = {}

        request = PortfolioStateFilterRequest(refresh=True)
        await get_portfolio_state(request, mock_service)

        mock_service.update_account_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_false_does_not_call_update_account_state(self):
        """refresh=False should NOT call update_account_state."""
        from models.trading import PortfolioStateFilterRequest
        from routers.portfolio import get_portfolio_state

        mock_service = MagicMock()
        mock_service.update_account_state = AsyncMock()
        mock_service.get_accounts_state.return_value = {}

        request = PortfolioStateFilterRequest(refresh=False)
        await get_portfolio_state(request, mock_service)

        mock_service.update_account_state.assert_not_called()


class TestBalanceRefresh:
    """Tests for _get_connector_tokens_info balance refresh."""

    @pytest.fixture
    def accounts_service(self):
        """Create AccountsService with mocked dependencies."""
        from services.accounts_service import AccountsService

        service = AccountsService.__new__(AccountsService)
        service._market_data_service = MagicMock()
        service._market_data_service.get_rate.return_value = Decimal("1")
        return service

    @pytest.fixture
    def mock_connector(self):
        """Create a mock connector."""
        connector = MagicMock()
        connector._update_balances = AsyncMock()
        connector.get_all_balances.return_value = {"USDT": Decimal("1000")}
        connector.get_available_balance.return_value = Decimal("1000")
        return connector

    @pytest.mark.asyncio
    async def test_calls_update_balances(self, accounts_service, mock_connector):
        """_get_connector_tokens_info should call _update_balances."""
        await accounts_service._get_connector_tokens_info(mock_connector, "okx")

        mock_connector._update_balances.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_update_balances_when_requested(self, accounts_service, mock_connector):
        """skip_balance_refresh=True should skip _update_balances."""
        await accounts_service._get_connector_tokens_info(
            mock_connector, "okx", skip_balance_refresh=True
        )

        mock_connector._update_balances.assert_not_called()

    @pytest.mark.asyncio
    async def test_balance_failure_preserves_stale_data(self, accounts_service, mock_connector):
        """_update_balances failure should preserve stale cached data."""
        mock_connector._update_balances = AsyncMock(side_effect=Exception("API error"))
        mock_connector.get_all_balances.return_value = {"USDT": Decimal("500")}

        result = await accounts_service._get_connector_tokens_info(mock_connector, "okx")

        # Should still return data from get_all_balances (stale cache)
        assert len(result) == 1
        assert result[0]["token"] == "USDT"
        assert result[0]["units"] == 500.0

    @pytest.mark.asyncio
    async def test_hyperliquid_testnet_uses_public_collateral_when_balances_are_empty(
        self, accounts_service, mock_connector, monkeypatch
    ):
        """Hyperliquid testnet margin collateral is usable quote balance."""
        import services.accounts_service as accounts_module

        mock_connector.get_all_balances.return_value = {}
        mock_connector.get_available_balance.return_value = Decimal("0")
        mock_connector.hyperliquid_testnet_address = "0x043F9e880763576c15eBCB7d4f0D7453F2Db1708"
        monkeypatch.setattr(
            accounts_module,
            "_fetch_hyperliquid_testnet_clearinghouse_state",
            AsyncMock(
                return_value={
                    "withdrawable": "999.0",
                    "marginSummary": {"accountValue": "999.0"},
                }
            ),
        )

        result = await accounts_service._get_connector_tokens_info(
            mock_connector, "hyperliquid_testnet"
        )

        assert result == [
            {
                "token": "USDC",
                "units": 999.0,
                "price": 1.0,
                "value": 999.0,
                "available_units": 999.0,
            }
        ]


class TestGatewayRefreshSelection:
    """Tests for Gateway balance refresh selection."""

    def test_gateway_filter_keeps_gateway_chain_networks(self):
        """Gateway filters keep only chain-network connector names."""
        from services.accounts_service import _gateway_chain_network_filters

        assert _gateway_chain_network_filters(["binance_paper_trade", "ethereum-base"]) == (
            "ethereum-base",
        )

    def test_gateway_filter_skips_cex_only_connector_names(self):
        """CEX-only connector filters should not trigger Gateway refresh."""
        from services.accounts_service import _gateway_chain_network_filters

        assert _gateway_chain_network_filters(["binance_perpetual_testnet"]) == ()

    @pytest.mark.asyncio
    async def test_update_account_state_skips_gateway_for_cex_only_filter(self):
        """CEX-only portfolio refresh should not call Gateway balances."""
        from services.accounts_service import AccountsService

        service = AccountsService.__new__(AccountsService)
        service.accounts_state = {}
        service._connector_service = MagicMock()
        connector = MagicMock()
        service._connector_service.get_all_trading_connectors.return_value = {
            "master_account": {"binance_perpetual_testnet": connector}
        }
        service._get_connector_tokens_info = AsyncMock(return_value=[])
        service._update_gateway_balances = AsyncMock()

        await service.update_account_state(
            skip_gateway=False,
            account_names=["master_account"],
            connector_names=["binance_perpetual_testnet"],
        )

        service._get_connector_tokens_info.assert_awaited_once_with(
            connector,
            "binance_perpetual_testnet",
        )
        service._update_gateway_balances.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_account_state_refreshes_gateway_for_gateway_filter(self):
        """Gateway chain-network filters should call Gateway balances."""
        from services.accounts_service import AccountsService

        service = AccountsService.__new__(AccountsService)
        service.accounts_state = {}
        service._connector_service = MagicMock()
        service._connector_service.get_all_trading_connectors.return_value = {}
        service._update_gateway_balances = AsyncMock()

        await service.update_account_state(
            skip_gateway=False,
            connector_names=["ethereum-base"],
        )

        service._update_gateway_balances.assert_awaited_once_with(
            chain_networks=("ethereum-base",),
        )

    @pytest.mark.asyncio
    async def test_update_account_state_preserves_unfiltered_gateway_refresh(self):
        """Unfiltered portfolio refresh preserves existing Gateway behavior."""
        from services.accounts_service import AccountsService

        service = AccountsService.__new__(AccountsService)
        service.accounts_state = {}
        service._connector_service = MagicMock()
        service._connector_service.get_all_trading_connectors.return_value = {}
        service._update_gateway_balances = AsyncMock()

        await service.update_account_state(skip_gateway=False)

        service._update_gateway_balances.assert_awaited_once_with(chain_networks=None)


class TestConnectorStartup:
    """Tests for connector startup ordering."""

    def test_xrpl_requires_network_before_initial_queries(self):
        """XRPL must start its node pool before rules/balance queries."""
        from services.unified_connector_service import UnifiedConnectorService

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)

        assert service._requires_network_before_initial_queries("xrpl")
        assert not service._requires_network_before_initial_queries("binance_perpetual_testnet")

    def test_trading_connector_init_starts_early_network_before_balances(self):
        """Early-network connectors should not query balances before network start."""
        from services.unified_connector_service import UnifiedConnectorService

        source = inspect.getsource(UnifiedConnectorService._create_and_initialize_trading_connector)

        early_network_index = source.index("_requires_network_before_initial_queries")
        balance_index = source.index("await connector._update_balances()")
        final_start_index = source.rindex("_start_connector_network")

        assert early_network_index < balance_index
        assert balance_index < final_start_index

    @pytest.mark.asyncio
    async def test_startup_connector_allowlist_skips_unlisted_credentials(self, monkeypatch):
        """Startup should initialize only explicitly allowed connectors when scoped."""
        import services.unified_connector_service as module
        from services.unified_connector_service import UnifiedConnectorService

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)
        service.list_available_credentials = MagicMock(
            return_value=["binance_perpetual_testnet", "xrpl"]
        )
        service.get_trading_connector = AsyncMock()
        monkeypatch.setattr(module.fs_util, "list_folders", lambda path: ["master_account"])

        await service.initialize_all_trading_connectors(
            startup_connectors={"binance_perpetual_testnet"},
        )

        service.get_trading_connector.assert_awaited_once_with(
            "master_account",
            "binance_perpetual_testnet",
        )

    @pytest.mark.asyncio
    async def test_account_state_connector_allowlist_skips_unlisted_credentials(self):
        """Account-state background initialization should use the same connector scope."""
        from services.accounts_service import AccountsService

        service = AccountsService.__new__(AccountsService)
        service.startup_connectors = {"binance_perpetual_testnet"}
        service._connector_service = MagicMock()
        service._connector_service.list_available_credentials.return_value = [
            "binance_perpetual_testnet",
            "xrpl",
        ]
        service._connector_service.is_trading_connector_initialized.return_value = False
        service._connector_service.get_trading_connector = AsyncMock()

        await service._ensure_account_connectors_initialized("master_account")

        service._connector_service.get_trading_connector.assert_awaited_once_with(
            "master_account",
            "binance_perpetual_testnet",
        )


class TestXrplTradingSafety:
    """Tests for XRPL execution safety boundaries."""

    @pytest.mark.asyncio
    async def test_xrpl_market_orders_delegate_to_connector_when_supported(self, monkeypatch):
        """XRPL MARKET orders are handled by the Hummingbot connector, not blocked in API."""
        from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType
        import services.accounts_service as accounts_module
        from services.accounts_service import AccountsService

        class TradingRule:
            min_order_size = Decimal("0.000001")
            min_notional_size = Decimal("0.000001")

        service = AccountsService.__new__(AccountsService)
        service.list_accounts = MagicMock(return_value=["master_account"])
        service._connector_service = MagicMock()
        connector = MagicMock()
        connector.trading_rules = {"XRP-USD": TradingRule()}
        connector.supported_order_types.return_value = [OrderType.LIMIT, OrderType.MARKET]
        connector.quantize_order_amount.return_value = Decimal("1")
        connector.buy.return_value = "xrpl-market-order"
        service._connector_service.get_trading_connector = AsyncMock(return_value=connector)
        service._ensure_trading_pair_rules_loaded = AsyncMock()
        service._market_data_service = MagicMock()
        service._market_data_service.get_prices = AsyncMock(return_value={"XRP-USD": "0.5"})
        monkeypatch.setattr(accounts_module, "assert_live_order_submission_allowed", lambda **_: None)

        order_id = await service.place_trade(
            account_name="master_account",
            connector_name="xrpl",
            trading_pair="XRP-USD",
            trade_type=TradeType.BUY,
            amount=Decimal("1"),
            order_type=OrderType.MARKET,
        )

        assert order_id == "xrpl-market-order"
        service._connector_service.get_trading_connector.assert_awaited_once_with("master_account", "xrpl")
        connector.buy.assert_called_once_with(
            trading_pair="XRP-USD",
            amount=Decimal("1"),
            order_type=OrderType.MARKET,
            price=Decimal("0.5"),
            position_action=PositionAction.OPEN,
        )
