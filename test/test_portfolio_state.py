"""
Tests for Portfolio State refresh behavior.

Run with: pytest test/test_portfolio_state.py -v
"""
import inspect
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call, patch

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
    @pytest.mark.parametrize("connector_name", ["hyperliquid", "hyperliquid_testnet"])
    async def test_hyperliquid_spot_does_not_expose_perpetual_margin_as_usdc(
        self, accounts_service, mock_connector, connector_name
    ):
        """An empty spot account must not inherit clearinghouse margin collateral."""
        mock_connector.get_all_balances.return_value = {}
        mock_connector.get_available_balance.return_value = Decimal("0")
        mock_connector.hyperliquid_address = "0x6394Bd277f792E6A6CDE7B6f9D075f7cfAbB5ff4"

        result = await accounts_service._get_connector_tokens_info(
            mock_connector, connector_name
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_hyperliquid_perpetual_exposes_connector_margin_balance(
        self, accounts_service, mock_connector
    ):
        """Perpetual collateral is provider-neutral USDC at the account boundary."""
        mock_connector.get_all_balances.return_value = {"USD": Decimal("15")}
        mock_connector.get_available_balance.return_value = Decimal("15")

        result = await accounts_service._get_connector_tokens_info(
            mock_connector, "hyperliquid_perpetual"
        )

        assert result == [
            {
                "token": "USDC",
                "units": 15.0,
                "price": 1.0,
                "value": 15.0,
                "available_units": 15.0,
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

    @pytest.mark.asyncio
    async def test_fresh_connector_state_is_account_scoped_and_propagates_refresh_failure(self):
        from services.accounts_service import AccountsService

        service = AccountsService.__new__(AccountsService)
        service.accounts_state = {}
        service._connector_service = MagicMock()
        connector = MagicMock()
        connector._update_balances = AsyncMock(side_effect=RuntimeError("refresh failed"))
        service._connector_service.get_all_trading_connectors.return_value = {
            "master_account": {"hyperliquid_perpetual": connector},
            "other_account": {"hyperliquid_perpetual": MagicMock()},
        }
        service._get_connector_tokens_info = AsyncMock()

        with pytest.raises(RuntimeError, match="refresh failed"):
            await service.get_fresh_available_balance("master_account", "hyperliquid_perpetual", "USDC")

        connector._update_balances.assert_awaited_once()
        service._get_connector_tokens_info.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_marlin_gateway_balance_refresh_ensures_derived_wallet(self, monkeypatch):
        """Marlin runtime must rehydrate Gateway's mnemonic-derived wallet before balances."""
        from services.marlin_runtime import MARLIN_RUNTIME_PROFILE, MARLIN_RUNTIME_PROFILE_ENV
        from services.accounts_service import AccountsService

        class FakeGatewayClient:
            def __init__(self):
                self.default_wallet_calls = []
                self.balance_calls = []

            async def ping(self):
                return True

            async def set_marlin_default_wallet(self, **kwargs):
                self.default_wallet_calls.append(kwargs)
                return {"status": "ok"}

            async def get_balances(self, chain, network, address, tokens=None):
                self.balance_calls.append(
                    {
                        "chain": chain,
                        "network": network,
                        "address": address,
                        "tokens": tokens,
                    },
                )
                return {"balances": {}}

        monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, MARLIN_RUNTIME_PROFILE)
        monkeypatch.setenv(
            "MARLIN_MNEMONIC",
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        )
        service = AccountsService.__new__(AccountsService)
        service.gateway_client = FakeGatewayClient()

        await service.get_gateway_balances(
            "ethereum",
            "0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
            network="ethereum-base",
            tokens=["USDC"],
        )
        await service.get_gateway_balances(
            "ethereum",
            "0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
            network="ethereum-base",
            tokens=["USDC"],
        )

        assert service.gateway_client.default_wallet_calls == [
            {
                "chain": "ethereum",
                "network": "ethereum-base",
                "address": "0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
                "wallet_ref": "base:mainnet:evm_gateway",
            },
            {
                "chain": "ethereum",
                "network": "ethereum-base",
                "address": "0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
                "wallet_ref": "base:mainnet:evm_gateway",
            },
        ]
        assert len(service.gateway_client.balance_calls) == 2

    @pytest.mark.asyncio
    async def test_marlin_unfiltered_gateway_refresh_discovers_materialized_network_wallets(self, monkeypatch):
        """Unfiltered Marlin refresh includes only materialized mnemonic-derived wallets."""
        from services.marlin_runtime import MARLIN_RUNTIME_PROFILE, MARLIN_RUNTIME_PROFILE_ENV
        from services.accounts_service import AccountsService

        monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, MARLIN_RUNTIME_PROFILE)
        monkeypatch.setenv(
            "MARLIN_MNEMONIC",
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        )
        expected_base = AccountsService._marlin_gateway_default_wallet_address(
            chain="ethereum", network="base"
        )
        expected_arbitrum = AccountsService._marlin_gateway_default_wallet_address(
            chain="ethereum", network="arbitrum"
        )
        assert expected_base and expected_arbitrum
        assert expected_base != expected_arbitrum

        class FakeGatewayClient:
            async def ping(self):
                return True

            async def get_chains(self):
                return {
                    "chains": [{"chain": "ethereum", "networks": ["base", "arbitrum", "mainnet"]}],
                }

            async def get_config(self, namespace):
                assert namespace == "ethereum-base"
                return {
                    "defaultWallet": expected_base,
                    "defaultNetworks": ["base"],
                    "defaultNetwork": "base",
                }

            async def get_wallets(self):
                return [
                    {
                        "chain": "ethereum",
                        "walletAddresses": [
                            expected_base,
                            expected_arbitrum,
                            "0x0000000000000000000000000000000000000001",
                        ],
                    },
                ]

        service = AccountsService.__new__(AccountsService)
        service.accounts_state = {}
        service.gateway_client = FakeGatewayClient()
        service.get_gateway_balances = AsyncMock(return_value=[])

        assert await service._update_gateway_balances() is True
        assert service.get_gateway_balances.await_args_list == [
            call("ethereum", expected_base, network="base", tokens=None),
            call("ethereum", expected_arbitrum, network="arbitrum", tokens=None),
        ]

    @pytest.mark.asyncio
    async def test_marlin_unfiltered_gateway_refresh_uses_persisted_treasury_source_context(self, monkeypatch):
        """Durable treasury source metadata restores a mnemonic wallet after Gateway restart."""
        from contextlib import asynccontextmanager
        from types import SimpleNamespace

        import services.accounts_service as accounts_service_module
        from services.marlin_runtime import MARLIN_RUNTIME_PROFILE, MARLIN_RUNTIME_PROFILE_ENV
        from services.accounts_service import AccountsService

        monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, MARLIN_RUNTIME_PROFILE)
        monkeypatch.setenv(
            "MARLIN_MNEMONIC",
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        )
        expected_base = AccountsService._marlin_gateway_default_wallet_address(
            chain="ethereum", network="base"
        )
        expected_arbitrum = AccountsService._marlin_gateway_default_wallet_address(
            chain="ethereum", network="arbitrum"
        )
        assert expected_base and expected_arbitrum and expected_base != expected_arbitrum

        class FakeGatewayClient:
            async def ping(self):
                return True

            async def get_chains(self):
                return {"chains": [{"chain": "ethereum", "networks": ["base", "arbitrum"]}]}

            async def get_config(self, namespace):
                assert namespace == "ethereum-base"
                return {
                    "defaultWallet": expected_base,
                    "defaultNetworks": ["base"],
                    "defaultNetwork": "base",
                }

            async def get_wallets(self):
                return [{"chain": "ethereum", "walletAddresses": [expected_base]}]

        class FakeDatabaseManager:
            @asynccontextmanager
            async def get_session_context(self):
                yield object()

        class FakeTreasuryRepository:
            def __init__(self, session):
                self.session = session

            async def list_rebalances(self):
                return [
                    SimpleNamespace(
                        response_payload={
                            "metadata": {
                                "source_chain": "ethereum",
                                "source_network": "arbitrum",
                            },
                        },
                    ),
                ]

        monkeypatch.setattr(
            accounts_service_module,
            "ProviderTreasuryRebalanceRepository",
            FakeTreasuryRepository,
        )
        service = AccountsService.__new__(AccountsService)
        service.accounts_state = {}
        service.db_manager = FakeDatabaseManager()
        service.gateway_client = FakeGatewayClient()
        service.get_gateway_balances = AsyncMock(return_value=[])

        assert await service._update_gateway_balances() is True
        assert service.get_gateway_balances.await_args_list == [
            call("ethereum", expected_base, network="base", tokens=None),
            call("ethereum", expected_arbitrum, network="arbitrum", tokens=None),
        ]

    @pytest.mark.asyncio
    async def test_marlin_unfiltered_gateway_refresh_ignores_invalid_treasury_source_contexts(self, monkeypatch):
        """Malformed, foreign, and unsupported durable contexts do not widen refresh scope."""
        from contextlib import asynccontextmanager
        from types import SimpleNamespace

        import services.accounts_service as accounts_service_module
        from services.marlin_runtime import MARLIN_RUNTIME_PROFILE, MARLIN_RUNTIME_PROFILE_ENV
        from services.accounts_service import AccountsService

        monkeypatch.setenv(MARLIN_RUNTIME_PROFILE_ENV, MARLIN_RUNTIME_PROFILE)
        monkeypatch.setenv(
            "MARLIN_MNEMONIC",
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        )
        expected_base = AccountsService._marlin_gateway_default_wallet_address(
            chain="ethereum", network="base"
        )
        assert expected_base

        class FakeGatewayClient:
            async def ping(self):
                return True

            async def get_chains(self):
                return {"chains": [{"chain": "ethereum", "networks": ["base", "arbitrum"]}]}

            async def get_config(self, namespace):
                assert namespace == "ethereum-base"
                return {
                    "defaultWallet": expected_base,
                    "defaultNetworks": ["base"],
                    "defaultNetwork": "base",
                }

            async def get_wallets(self):
                return [{"chain": "ethereum", "walletAddresses": [expected_base]}]

        class FakeDatabaseManager:
            @asynccontextmanager
            async def get_session_context(self):
                yield object()

        class FakeTreasuryRepository:
            def __init__(self, session):
                self.session = session

            async def list_rebalances(self):
                return [
                    SimpleNamespace(response_payload={"metadata": {"source_chain": "ethereum"}}),
                    SimpleNamespace(
                        response_payload={
                            "metadata": {"source_chain": "ethereum", "source_network": " "},
                        },
                    ),
                    SimpleNamespace(
                        response_payload={
                            "metadata": {"source_chain": 123, "source_network": "arbitrum"},
                        },
                    ),
                    SimpleNamespace(
                        response_payload={
                            "metadata": {"source_chain": "ethereum", "source_network": "mainnet"},
                        },
                    ),
                    SimpleNamespace(
                        response_payload={
                            "metadata": {"source_chain": "foreign", "source_network": "base"},
                        },
                    ),
                ]

        monkeypatch.setattr(
            accounts_service_module,
            "ProviderTreasuryRebalanceRepository",
            FakeTreasuryRepository,
        )
        service = AccountsService.__new__(AccountsService)
        service.accounts_state = {}
        service.db_manager = FakeDatabaseManager()
        service.gateway_client = FakeGatewayClient()
        service.get_gateway_balances = AsyncMock(return_value=[])

        assert await service._update_gateway_balances() is True
        assert service.get_gateway_balances.await_args_list == [
            call("ethereum", expected_base, network="base", tokens=None),
        ]


class TestGatewayBalances:
    """Tests for Gateway balance response formatting."""

    @pytest.fixture
    def accounts_service(self):
        """Create an AccountsService with isolated Gateway dependencies."""
        from services.accounts_service import AccountsService

        service = AccountsService.__new__(AccountsService)
        service.gateway_client = MagicMock()
        service.gateway_client.ping = AsyncMock(return_value=True)
        service.gateway_client.get_balances = AsyncMock()
        service._ensure_marlin_gateway_wallet = AsyncMock()
        service._fetch_gateway_prices_immediate = AsyncMock()
        return service

    @pytest.mark.asyncio
    async def test_requested_zero_balances_are_returned_and_priced(
        self, accounts_service
    ):
        """Explicitly requested zero balances remain observable and priceable."""
        accounts_service.gateway_client.get_balances.return_value = {
            "balances": {
                "WETH": "0",
                "USDC": "0",
                "ETH": "2",
                "UNREQUESTED": "0",
            }
        }
        accounts_service._fetch_gateway_prices_immediate.return_value = {
            "WETH": Decimal("3000"),
            "ETH": Decimal("2000"),
        }

        result = await accounts_service.get_gateway_balances(
            "ethereum",
            "0xwallet",
            network="ethereum-base",
            tokens=["WETH", "USDC", "ETH", "MISSING"],
        )

        assert result == [
            {
                "token": "WETH",
                "units": 0.0,
                "price": 3000.0,
                "value": 0.0,
                "available_units": 0.0,
            },
            {
                "token": "USDC",
                "units": 0.0,
                "price": 1.0,
                "value": 0.0,
                "available_units": 0.0,
            },
            {
                "token": "ETH",
                "units": 2.0,
                "price": 2000.0,
                "value": 4000.0,
                "available_units": 2.0,
            },
        ]
        accounts_service._fetch_gateway_prices_immediate.assert_awaited_once_with(
            "ethereum", "ethereum-base", ["WETH", "USDC", "ETH"]
        )

    @pytest.mark.asyncio
    async def test_unfiltered_gateway_balances_still_omit_zero_rows(self, accounts_service):
        """Unfiltered Gateway reads preserve the existing non-zero-only result."""
        accounts_service.gateway_client.get_balances.return_value = {
            "balances": {"USDC": "0", "ETH": "2"}
        }
        accounts_service._fetch_gateway_prices_immediate.return_value = {
            "ETH": Decimal("2000")
        }

        result = await accounts_service.get_gateway_balances(
            "ethereum", "0xwallet", network="ethereum-base"
        )

        assert result == [
            {
                "token": "ETH",
                "units": 2.0,
                "price": 2000.0,
                "value": 4000.0,
                "available_units": 2.0,
            }
        ]
        accounts_service._fetch_gateway_prices_immediate.assert_awaited_once_with(
            "ethereum", "ethereum-base", ["ETH"]
        )

    @pytest.mark.asyncio
    async def test_cached_eth_price_values_balance_without_gateway_quote(self, accounts_service):
        """A cached ETH-USDC rate prices an unsupported Gateway network locally."""
        from hummingbot.core.rate_oracle.rate_oracle import RateOracle
        from services.accounts_service import AccountsService

        accounts_service._fetch_gateway_prices_immediate = (
            AccountsService._fetch_gateway_prices_immediate.__get__(accounts_service)
        )
        accounts_service.gateway_client.quote_swap = AsyncMock()
        accounts_service.gateway_client.get_balances.return_value = {
            "balances": {"ETH": "0.000500329"}
        }
        rate_oracle = MagicMock()
        rate_oracle.get_pair_rate.side_effect = lambda pair: (
            Decimal("1919") if pair == "ETH-USDC" else None
        )

        with patch.object(RateOracle, "get_instance", return_value=rate_oracle):
            result = await accounts_service.get_gateway_balances(
                "ethereum", "0xwallet", network="arbitrum"
            )

        assert result == [
            {
                "token": "ETH",
                "units": 0.000500329,
                "price": 1919.0,
                "value": 0.960131351,
                "available_units": 0.000500329,
            }
        ]
        accounts_service.gateway_client.quote_swap.assert_not_called()

    @pytest.mark.asyncio
    async def test_unpriced_positive_balance_is_incomplete_not_zero(self, accounts_service):
        """An unknown positive token price must not become a zero valuation."""
        from hummingbot.core.rate_oracle.rate_oracle import RateOracle
        from services.accounts_service import AccountsService

        accounts_service._fetch_gateway_prices_immediate = (
            AccountsService._fetch_gateway_prices_immediate.__get__(accounts_service)
        )
        accounts_service.gateway_client.quote_swap = AsyncMock()
        accounts_service.gateway_client.get_balances.return_value = {
            "balances": {"ETH": "0.000500329"}
        }
        rate_oracle = MagicMock()
        rate_oracle.get_pair_rate.return_value = None

        with patch.object(RateOracle, "get_instance", return_value=rate_oracle):
            result = await accounts_service.get_gateway_balances(
                "ethereum", "0xwallet", network="arbitrum"
            )

        assert result == [
            {
                "token": "ETH",
                "units": 0.000500329,
                "price": None,
                "value": None,
                "available_units": 0.000500329,
            }
        ]
        accounts_service.gateway_client.quote_swap.assert_not_called()


class TestConnectorStartup:
    """Tests for connector startup ordering."""

    def test_xrpl_requires_network_before_initial_queries(self):
        """XRPL must start its node pool before rules/balance queries."""
        from services.unified_connector_service import UnifiedConnectorService

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)

        assert service._requires_network_before_initial_queries("xrpl")
        assert not service._requires_network_before_initial_queries("binance_perpetual_testnet")

    def test_marlin_discovers_hyperliquid_only_for_master_account(self, monkeypatch):
        from services.unified_connector_service import UnifiedConnectorService

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)
        monkeypatch.setenv("MARLIN_RUNTIME_PROFILE", "marlin")
        monkeypatch.setattr("services.unified_connector_service.fs_util.list_files", lambda path: [])

        assert service.list_available_credentials("master_account") == ["hyperliquid_perpetual"]
        assert service.list_available_credentials("secondary") == []

    def test_marlin_hyperliquid_uses_ephemeral_derived_keys_after_login(self, monkeypatch):
        import services.unified_connector_service as module
        from services.unified_connector_service import UnifiedConnectorService

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)
        service.secrets_manager = MagicMock()
        setting = MagicMock()
        setting.conn_init_parameters.return_value = {"credential": "derived"}
        service._conn_settings = {"hyperliquid_perpetual": setting}
        monkeypatch.setenv("MARLIN_RUNTIME_PROFILE", "marlin")
        derived = {
            "hyperliquid_perpetual_address": "0xderived",
            "hyperliquid_perpetual_secret_key": "secret",
        }

        with (
            patch.object(module, "_derive_marlin_credential_values", return_value=derived) as derive,
            patch.object(module.BackendAPISecurity, "validate_password", return_value=True) as validate,
            patch.object(module.BackendAPISecurity, "login_account") as file_login,
            patch.object(module.BackendAPISecurity, "api_keys") as file_keys,
            patch.object(module, "get_connector_class", return_value=lambda **kwargs: kwargs),
        ):
            connector = service._create_trading_connector(
                account_name="master_account",
                connector_name="hyperliquid_perpetual",
            )

        validate.assert_called_once_with(service.secrets_manager)
        file_login.assert_not_called()
        derive.assert_called_once_with("hyperliquid_perpetual")
        file_keys.assert_not_called()
        setting.conn_init_parameters.assert_called_once_with(
            trading_pairs=[], trading_required=True, api_keys=derived,
        )
        assert connector == {"credential": "derived"}

    def test_marlin_hyperliquid_failed_login_does_not_derive(self, monkeypatch):
        import services.unified_connector_service as module
        from services.unified_connector_service import UnifiedConnectorService

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)
        service.secrets_manager = MagicMock()
        service._conn_settings = {"hyperliquid_perpetual": MagicMock()}
        monkeypatch.setenv("MARLIN_RUNTIME_PROFILE", "marlin")

        with (
            patch.object(module, "_derive_marlin_credential_values") as derive,
            patch.object(module.BackendAPISecurity, "validate_password", return_value=False),
            pytest.raises(PermissionError, match="authentication failed"),
        ):
            service._create_trading_connector("master_account", "hyperliquid_perpetual")

        derive.assert_not_called()

    def test_marlin_hyperliquid_never_falls_back_to_file_credentials(self, monkeypatch):
        import services.unified_connector_service as module
        from services.unified_connector_service import UnifiedConnectorService

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)
        service.secrets_manager = MagicMock()
        service._conn_settings = {"hyperliquid_perpetual": MagicMock()}
        monkeypatch.setenv("MARLIN_RUNTIME_PROFILE", "marlin")

        with (
            patch.object(module, "_derive_marlin_credential_values", return_value=None),
            patch.object(module.BackendAPISecurity, "validate_password", return_value=True),
            patch.object(module.BackendAPISecurity, "login_account") as file_login,
            patch.object(module.BackendAPISecurity, "api_keys") as file_keys,
            pytest.raises(RuntimeError, match="credentials unavailable"),
        ):
            service._create_trading_connector("master_account", "hyperliquid_perpetual")

        file_login.assert_not_called()
        file_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_cached_marlin_hyperliquid_connector_is_idempotent(self, monkeypatch):
        import services.unified_connector_service as module
        from services.unified_connector_service import UnifiedConnectorService

        cached = object()
        service = UnifiedConnectorService.__new__(UnifiedConnectorService)
        service._connector_locks = {}
        service._trading_connectors = {
            "master_account": {"hyperliquid_perpetual": cached},
        }
        monkeypatch.setenv("MARLIN_RUNTIME_PROFILE", "marlin")

        with (
            patch.object(module, "_derive_marlin_credential_values") as derive,
            patch.object(module.BackendAPISecurity, "login_account") as login,
        ):
            result = await service.get_trading_connector(
                "master_account", "hyperliquid_perpetual",
            )

        assert result is cached
        derive.assert_not_called()
        login.assert_not_called()

    def test_non_marlin_hyperliquid_uses_file_backed_keys(self, monkeypatch):
        import services.unified_connector_service as module
        from services.unified_connector_service import UnifiedConnectorService

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)
        service.secrets_manager = MagicMock()
        setting = MagicMock()
        setting.conn_init_parameters.return_value = {}
        service._conn_settings = {"hyperliquid_perpetual": setting}
        monkeypatch.setenv("MARLIN_RUNTIME_PROFILE", "provider")
        persisted = {"hyperliquid_perpetual_address": "persisted"}

        with (
            patch.object(module, "_derive_marlin_credential_values") as derive,
            patch.object(module.BackendAPISecurity, "login_account"),
            patch.object(module.BackendAPISecurity, "api_keys", return_value=persisted) as file_keys,
            patch.object(module, "get_connector_class", return_value=lambda **kwargs: kwargs),
        ):
            service._create_trading_connector("master_account", "hyperliquid_perpetual")

        derive.assert_not_called()
        file_keys.assert_called_once_with("hyperliquid_perpetual")
        setting.conn_init_parameters.assert_called_once_with(
            trading_pairs=[], trading_required=True, api_keys=persisted,
        )

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
    async def test_marlin_clean_container_starts_master_account_hyperliquid(self, monkeypatch):
        """Without credentials folder, Marlin runtime still starts master_account hyperliquid_perpetual."""
        import services.unified_connector_service as module
        from services.unified_connector_service import UnifiedConnectorService

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)
        service.get_trading_connector = AsyncMock()
        monkeypatch.setenv("MARLIN_RUNTIME_PROFILE", "marlin")

        def missing_credentials(path):
            raise FileNotFoundError(f"Directory '{path}' not found")

        monkeypatch.setattr(module.fs_util, "list_folders", missing_credentials)

        await service.initialize_all_trading_connectors()

        service.get_trading_connector.assert_awaited_once_with(
            "master_account",
            "hyperliquid_perpetual",
        )

    @pytest.mark.asyncio
    async def test_non_marlin_clean_container_still_raises_missing_credentials(self, monkeypatch):
        """non-Marlin must propagate the missing credentials-directory error."""
        import services.unified_connector_service as module
        from services.unified_connector_service import UnifiedConnectorService

        monkeypatch.setenv("MARLIN_RUNTIME_PROFILE", "provider")

        def missing_credentials(path):
            raise FileNotFoundError(f"Directory '{path}' not found")

        monkeypatch.setattr(module.fs_util, "list_folders", missing_credentials)

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)
        with pytest.raises(FileNotFoundError, match="credentials"):
            await service.initialize_all_trading_connectors()

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
