"""
UnifiedConnectorService - Single source of truth for all connector instances.

This service consolidates connector management from:
- ConnectorManager (trading connectors)
- MarketDataProvider._non_trading_connectors (data-only connectors)

Key features:
- Trading connectors: authenticated, per-account, with order tracking
- Data connectors: non-authenticated, shared, for public market data
- get_best_connector_for_market(): prefers trading connector (has order book tracker)
"""
import asyncio
import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger
from hummingbot.client.config.config_helpers import ClientConfigAdapter, api_keys_from_connector_config_map, get_connector_class
from hummingbot.client.settings import AllConnectorSettings
from hummingbot.client.config.security import Security
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.connector.connector_metrics_collector import TradeVolumeMetricCollector
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.gateway.gateway import Gateway
from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
from hummingbot.core.rate_oracle.rate_oracle import RateOracle
from hummingbot.core.utils.async_utils import safe_ensure_future

from services.cowswap_runtime import (
    COWSWAP_CONNECTOR_NAME,
    UNWIRED_RUNTIME_BLOCKER,
    CowSwapRuntimeUnavailableError,
    cowswap_order_submission_blocker,
)
from services.marlin_runtime import (
    _derive_marlin_credential_values,
    is_marlin_hyperliquid_bootstrap_scope,
)
from utils.file_system import fs_util
from utils.hummingbot_api_config_adapter import HummingbotAPIConfigAdapter
from utils.security import BackendAPISecurity

logger = logging.getLogger(__name__)
EARLY_NETWORK_CONNECTORS = frozenset({"xrpl"})


class UnifiedConnectorService:
    """
    Single source of truth for ALL connector instances.

    Manages two types of connectors:
    1. Trading connectors: authenticated, per-account, with full trading capabilities
    2. Data connectors: non-authenticated, shared, for public market data only

    The key method `get_best_connector_for_market()` ensures that order book
    operations use the trading connector when available (which already has
    order_book_tracker running), falling back to data connector otherwise.
    """

    METRICS_ACTIVATION_INTERVAL = Decimal("900")  # 15 minutes
    METRICS_VALUATION_TOKEN = "USDT"

    def __init__(self, secrets_manager: ETHKeyFileSecretManger, db_manager=None):
        self.secrets_manager = secrets_manager
        self.db_manager = db_manager

        # Trading connectors: account_name -> connector_name -> ConnectorBase
        self._trading_connectors: Dict[str, Dict[str, ConnectorBase]] = {}

        # Data-only connectors: connector_name -> ConnectorBase (shared, non-authenticated)
        self._data_connectors: Dict[str, ConnectorBase] = {}
        self._data_connectors_started: Dict[str, bool] = {}

        # Order and funding recorders (for trading connectors)
        self._orders_recorders: Dict[str, any] = {}
        self._funding_recorders: Dict[str, any] = {}
        self._metrics_collectors: Dict[str, TradeVolumeMetricCollector] = {}

        # Locks to prevent race conditions in connector creation
        self._connector_locks: Dict[str, asyncio.Lock] = {}

        # Connector settings cache
        self._conn_settings = AllConnectorSettings.get_connector_settings()

    def _is_perpetual_connector(self, connector: ConnectorBase) -> bool:
        """Check if connector is a perpetual derivative connector.

        Args:
            connector: The connector instance to check

        Returns:
            True if perpetual connector, False otherwise
        """
        return isinstance(connector, PerpetualDerivativePyBase)

    # =========================================================================
    # Trading Connector Management (authenticated, per-account)
    # =========================================================================

    async def get_trading_connector(
        self,
        account_name: str,
        connector_name: str
    ) -> ConnectorBase:
        """
        Get or create an authenticated trading connector for a specific account.

        Trading connectors have:
        - API key authentication
        - Order tracking (OrdersRecorder)
        - Funding tracking for perpetuals (FundingRecorder)
        - Metrics collection
        - Full trading capabilities

        Args:
            account_name: The account name
            connector_name: The connector name (e.g., "binance", "binance_perpetual")

        Returns:
            Initialized trading connector
        """
        cache_key = f"{account_name}:{connector_name}"

        # Create lock for this cache key if it doesn't exist
        if cache_key not in self._connector_locks:
            self._connector_locks[cache_key] = asyncio.Lock()

        # Use lock to prevent race conditions during connector creation
        async with self._connector_locks[cache_key]:
            if account_name not in self._trading_connectors:
                self._trading_connectors[account_name] = {}

            if connector_name not in self._trading_connectors[account_name]:
                connector = await self._create_and_initialize_trading_connector(
                    account_name, connector_name
                )
                self._trading_connectors[account_name][connector_name] = connector

            return self._trading_connectors[account_name][connector_name]

    def get_all_trading_connectors(self) -> Dict[str, Dict[str, ConnectorBase]]:
        """
        Get all trading connectors organized by account.

        Returns:
            Dict mapping account_name -> connector_name -> ConnectorBase
        """
        return self._trading_connectors

    def get_account_connectors(self, account_name: str) -> Dict[str, ConnectorBase]:
        """
        Get all connectors for a specific account.

        Args:
            account_name: Account name

        Returns:
            Dict mapping connector_name -> ConnectorBase for this account
        """
        return self._trading_connectors.get(account_name, {})

    def is_trading_connector_initialized(
        self,
        account_name: str,
        connector_name: str
    ) -> bool:
        """Check if a trading connector is already initialized."""
        return (
            account_name in self._trading_connectors and
            connector_name in self._trading_connectors[account_name]
        )

    # =========================================================================
    # Data Connector Management (non-authenticated, shared)
    # =========================================================================

    def get_data_connector(self, connector_name: str) -> ConnectorBase:
        """
        Get or create a non-authenticated data connector for public market data.

        Data connectors:
        - No API keys required (public endpoints only)
        - Shared across accounts
        - Used for: trading rules, prices, order books, candles
        - NOT used for: trading, balance queries

        Args:
            connector_name: The connector name

        Returns:
            Non-authenticated connector instance
        """
        if connector_name not in self._data_connectors:
            self._data_connectors[connector_name] = self._create_data_connector(
                connector_name
            )
        return self._data_connectors[connector_name]

    async def ensure_data_connector_started(
        self,
        connector_name: str,
        trading_pair: str
    ) -> bool:
        """
        Ensure a data connector's network is started with at least one trading pair.

        This is needed because exchanges close WebSocket connections without subscriptions.

        Args:
            connector_name: The connector name
            trading_pair: Initial trading pair to subscribe to

        Returns:
            True if started successfully
        """
        if self._data_connectors_started.get(connector_name, False):
            return True

        connector = self.get_data_connector(connector_name)

        try:
            # Add trading pair before starting network
            if trading_pair not in connector._trading_pairs:
                connector._trading_pairs.append(trading_pair)

            # Start network
            await connector.start_network()
            self._data_connectors_started[connector_name] = True
            logger.info(f"Started data connector: {connector_name} with pair {trading_pair}")

            # Wait for order book tracker to be ready
            max_wait = 30
            waited = 0
            tracker = connector.order_book_tracker
            while waited < max_wait:
                if tracker._order_book_stream_listener_task is not None:
                    await asyncio.sleep(2.0)
                    break
                await asyncio.sleep(0.5)
                waited += 0.5

            return True

        except Exception as e:
            logger.error(f"Error starting data connector {connector_name}: {e}")
            return False

    # =========================================================================
    # Best Connector Selection (THE KEY FIX)
    # =========================================================================

    def get_best_connector_for_market(
        self,
        connector_name: str,
        account_name: Optional[str] = None
    ) -> Optional[ConnectorBase]:
        """
        Get the best available connector for market operations (order books, prices).

        CRITICAL: This method ensures order book initialization uses the correct
        connector. It prefers trading connectors because they already have
        order_book_tracker running with WebSocket connections.

        Priority:
        1. Specific account's trading connector (if account_name provided)
        2. Any trading connector for this connector_name
        3. Data connector (creates new if needed)

        Args:
            connector_name: The connector name
            account_name: Optional account to prefer

        Returns:
            Best available connector for market operations
        """
        # 1. Try specific account's trading connector
        if account_name:
            trading = self._trading_connectors.get(account_name, {}).get(connector_name)
            if trading:
                logger.debug(
                    f"Using trading connector for {connector_name} "
                    f"(account: {account_name})"
                )
                return trading

        # 2. Try ANY trading connector for this connector_name
        for acc_name, acc_connectors in self._trading_connectors.items():
            if connector_name in acc_connectors:
                logger.debug(
                    f"Using trading connector for {connector_name} "
                    f"(found in account: {acc_name})"
                )
                return acc_connectors[connector_name]

        # 3. Fall back to data connector
        logger.debug(f"Using data connector for {connector_name} (no trading connector)")
        return self.get_data_connector(connector_name)

    # =========================================================================
    # Order Book Initialization
    # =========================================================================

    async def initialize_order_book(
        self,
        connector_name: str,
        trading_pair: str,
        account_name: Optional[str] = None,
        timeout: float = 30.0
    ) -> bool:
        """
        Initialize order book for a trading pair using the best available connector.

        This method:
        1. Gets the best connector (prefers trading over data)
        2. Adds trading pair to order book tracker
        3. Waits for order book to have valid data

        Args:
            connector_name: The connector name
            trading_pair: The trading pair
            account_name: Optional account to prefer
            timeout: Timeout in seconds

        Returns:
            True if order book initialized successfully
        """
        connector = self.get_best_connector_for_market(connector_name, account_name)

        if not connector:
            logger.error(f"No connector available for {connector_name}")
            return False

        # Gateway/AMM connectors don't have order book trackers - skip initialization
        if not hasattr(connector, 'order_book_tracker') or connector.order_book_tracker is None:
            logger.info(f"Connector {connector_name} doesn't have order book tracker (AMM/Gateway) - skipping")
            return True

        tracker = connector.order_book_tracker

        # Check if already initialized
        if trading_pair in tracker.order_books:
            ob = tracker.order_books[trading_pair]
            try:
                bids, asks = ob.snapshot
                if len(bids) > 0 and len(asks) > 0:
                    logger.info(f"Order book for {trading_pair} already initialized")
                    return True
            except Exception:
                pass

        # For data connectors, ensure network is started
        if connector_name in self._data_connectors:
            if not self._data_connectors_started.get(connector_name, False):
                success = await self.ensure_data_connector_started(
                    connector_name, trading_pair
                )
                if not success:
                    return False
                # Wait for order book after starting
                return await self._wait_for_order_book(tracker, trading_pair, timeout)
            else:
                # Connector started, dynamically add trading pair
                success = await self._add_trading_pair_to_tracker(
                    connector, trading_pair, timeout
                )
                if not success:
                    return False

        # For trading connectors, dynamically add trading pair
        else:
            success = await self._add_trading_pair_to_tracker(connector, trading_pair, timeout)
            if not success:
                return False

        # Wait for order book to have data
        return await self._wait_for_order_book(tracker, trading_pair, timeout)

    def _is_tracker_running(self, tracker) -> bool:
        """Check if the order book tracker is running."""
        if not tracker:
            return False
        task = tracker._order_book_stream_listener_task
        if task and not task.done():
            return True
        task = tracker._init_order_books_task
        if task and not task.done():
            return True
        return False

    async def _add_trading_pair_to_tracker(
        self,
        connector: ExchangePyBase,
        trading_pair: str,
        timeout: float = 30.0,
    ) -> bool:
        """Add a trading pair to connector's order book tracker.

        ExchangePyBase connectors have:
        - order_book_tracker with _trading_pairs, start(), _orderbook_ds
        - add_trading_pair() for dynamic addition

        Approach:
        1. If tracker is running, use connector.add_trading_pair()
        2. Otherwise, register the pair and start the tracker
        """
        try:
            # Safety check - gateway/AMM connectors don't have order book trackers
            if not hasattr(connector, 'order_book_tracker') or connector.order_book_tracker is None:
                logger.debug(f"Connector {type(connector).__name__} doesn't have order book tracker")
                return True

            tracker = connector.order_book_tracker

            # Case 1: Tracker is already running and ready
            if self._is_tracker_running(tracker) and tracker.ready:
                if trading_pair in tracker.order_books:
                    logger.debug(f"Order book for {trading_pair} already exists")
                    return True

                logger.info(f"Adding {trading_pair} to running tracker")
                try:
                    result = await asyncio.wait_for(
                        connector.add_trading_pair(trading_pair), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"Timeout ({timeout}s) adding {trading_pair} to tracker, "
                        "falling back to REST snapshot"
                    )
                    result = None
                if result is True:
                    logger.info(f"Successfully added {trading_pair}")
                    return True
                if result is False:
                    logger.warning(f"add_trading_pair() returned False for {trading_pair}")

            # Case 2: Tracker not running - start it with this trading pair
            else:
                logger.info(f"Starting order book tracker for {type(connector).__name__} with {trading_pair}")

                # Register the trading pair before starting tracker
                if trading_pair not in tracker._trading_pairs:
                    tracker._trading_pairs.append(trading_pair)

                tracker.start()
                try:
                    await asyncio.wait_for(tracker.wait_ready(), timeout=30.0)
                    logger.info(f"Order book tracker ready for {type(connector).__name__}")
                except asyncio.TimeoutError:
                    logger.warning("Timeout waiting for tracker to be ready")

                if trading_pair in tracker.order_books:
                    logger.info(f"Order book for {trading_pair} initialized")
                    return True

            # Fallback: Get order book snapshot directly via REST
            logger.info(f"Fallback order book initialization for {trading_pair}")
            try:
                order_book = await connector._orderbook_ds.get_new_order_book(trading_pair)
                tracker.order_books[trading_pair] = order_book
                if trading_pair not in tracker._trading_pairs:
                    tracker._trading_pairs.append(trading_pair)
                logger.info(f"Initialized order book for {trading_pair} via REST fallback")
                return True
            except Exception as e:
                logger.error(f"Fallback order book initialization failed: {e}")

            logger.error(f"Failed to add {trading_pair} to order book tracker")
            return False

        except Exception as e:
            logger.error(f"Error adding trading pair {trading_pair}: {e}", exc_info=True)
            return False

    async def remove_trading_pair(
        self,
        connector_name: str,
        trading_pair: str,
        account_name: Optional[str] = None
    ) -> bool:
        """
        Remove a trading pair from a connector's order book tracker.

        This method cleans up order book resources for a trading pair that is
        no longer needed. Useful for:
        - Executor cleanup when stopping
        - Memory management for unused pairs
        - Account cleanup operations

        Args:
            connector_name: The connector name
            trading_pair: The trading pair to remove
            account_name: Optional account to target specific trading connector

        Returns:
            True if successfully removed, False otherwise
        """
        connector = self.get_best_connector_for_market(connector_name, account_name)

        if not connector:
            logger.warning(f"No connector available for {connector_name} to remove {trading_pair}")
            return False

        return await self._remove_trading_pair_from_tracker(connector, trading_pair)

    async def _remove_trading_pair_from_tracker(
        self,
        connector: ExchangePyBase,
        trading_pair: str
    ) -> bool:
        """Remove a trading pair from connector's order book tracker.

        ExchangePyBase.remove_trading_pair() handles:
        - Order book cleanup via order_book_tracker
        - Funding info cleanup for perpetual connectors
        """
        try:
            result = await connector.remove_trading_pair(trading_pair)
            if result:
                logger.info(f"Removed trading pair {trading_pair}")
                return True

            # Fallback: Manual removal from tracker (if connector has one)
            if not hasattr(connector, 'order_book_tracker') or connector.order_book_tracker is None:
                return True  # No tracker to clean up for AMM/Gateway connectors
            tracker = connector.order_book_tracker
            if trading_pair in tracker.order_books:
                del tracker.order_books[trading_pair]
                if trading_pair in tracker._trading_pairs:
                    tracker._trading_pairs.remove(trading_pair)
                logger.info(f"Removed trading pair {trading_pair} via manual fallback")
                return True

            logger.warning(f"Trading pair {trading_pair} not found")
            return False

        except Exception as e:
            logger.error(f"Error removing trading pair {trading_pair}: {e}")
            return False

    async def _wait_for_websocket_ready(
        self,
        connector: ExchangePyBase,
        timeout: float = 10.0
    ) -> bool:
        """Wait for the order book data source WebSocket to be connected."""
        data_source = connector._orderbook_ds
        waited = 0
        interval = 0.2

        while waited < timeout:
            if data_source._ws_assistant is not None:
                logger.debug(f"WebSocket ready for {type(connector).__name__}")
                return True
            await asyncio.sleep(interval)
            waited += interval

        logger.warning(f"Timeout waiting for WebSocket connection on {type(connector).__name__}")
        return False

    async def _wait_for_order_book(
        self,
        tracker,
        trading_pair: str,
        timeout: float
    ) -> bool:
        """Wait for order book to have valid bid/ask data."""
        waited = 0
        interval = 0.5

        while waited < timeout:
            if trading_pair in tracker.order_books:
                ob = tracker.order_books[trading_pair]
                try:
                    bids, asks = ob.snapshot
                    if len(bids) > 0 and len(asks) > 0:
                        logger.info(
                            f"Order book for {trading_pair} ready with "
                            f"{len(bids)} bids and {len(asks)} asks"
                        )
                        return True
                except Exception:
                    pass
            await asyncio.sleep(interval)
            waited += interval

        logger.warning(f"Timeout waiting for {trading_pair} order book")
        return False

    # =========================================================================
    # Trading Connector Creation (internal)
    # =========================================================================

    async def _create_and_initialize_trading_connector(
        self,
        account_name: str,
        connector_name: str
    ) -> ConnectorBase:
        """Create and fully initialize a trading connector."""
        # Authenticate and create connector
        connector = self._create_trading_connector(account_name, connector_name)
        network_started = False

        if self._requires_network_before_initial_queries(connector_name):
            await self._start_connector_network(connector, connector_name=connector_name)
            network_started = True

        # Initialize symbol map and trading rules
        await connector._initialize_trading_pair_symbol_map()
        await connector._update_trading_rules()
        await connector._update_balances()

        # Perpetual-specific setup
        if self._is_perpetual_connector(connector):
            if PositionMode.HEDGE in connector.supported_position_modes():
                connector.set_position_mode(PositionMode.HEDGE)
            await connector._update_positions()

        # Load existing orders from database
        if self.db_manager:
            await self._load_existing_orders(connector, account_name, connector_name)

        # Setup order and funding recorders
        cache_key = f"{account_name}:{connector_name}"
        if self.db_manager and cache_key not in self._orders_recorders:
            from services.orders_recorder import OrdersRecorder
            orders_recorder = OrdersRecorder(self.db_manager, account_name, connector_name)
            orders_recorder.start(connector)
            self._orders_recorders[cache_key] = orders_recorder

            if self._is_perpetual_connector(connector):
                from services.funding_recorder import FundingRecorder
                funding_recorder = FundingRecorder(self.db_manager, account_name, connector_name)
                funding_recorder.start(connector)
                self._funding_recorders[cache_key] = funding_recorder

        # Initialize metrics
        self._initialize_metrics(connector, account_name, connector_name, cache_key)

        # Start network tasks
        if not network_started:
            await self._start_connector_network(connector, connector_name=connector_name)

        # Only update order status for orders loaded from DB (balances, rules, positions
        # were already fetched above — no need to repeat via _update_connector_state)
        if connector.in_flight_orders:
            try:
                connector._set_current_timestamp(time.time())
                await connector._update_order_status()
            except Exception as e:
                logger.error(f"Error updating initial order status for {connector_name}: {e}")

        logger.info(f"Initialized trading connector {connector_name} for {account_name}")
        return connector

    def _create_trading_connector(
        self,
        account_name: str,
        connector_name: str
    ) -> ConnectorBase:
        """Create a trading connector with API keys.

        For Gateway network connectors (e.g., 'solana-mainnet-beta'), creates a unified
        Gateway connector which auto-detects chain/network and uses the default wallet.
        The dex_name and trading_type are passed to methods, not to the connector.
        """
        if connector_name == COWSWAP_CONNECTOR_NAME:
            blocker = cowswap_order_submission_blocker(connector_name) or UNWIRED_RUNTIME_BLOCKER
            raise CowSwapRuntimeUnavailableError(blocker)

        marlin_hyperliquid_bootstrap = is_marlin_hyperliquid_bootstrap_scope(account_name, connector_name)
        if marlin_hyperliquid_bootstrap:
            authenticated = BackendAPISecurity.validate_password(self.secrets_manager)
            if authenticated:
                BackendAPISecurity.secrets_manager = self.secrets_manager
                Security.secrets_manager = self.secrets_manager
                BackendAPISecurity._secure_configs.clear()
                BackendAPISecurity._decryption_done.clear()
                BackendAPISecurity._decryption_done.set()
        else:
            authenticated = BackendAPISecurity.login_account(
                account_name=account_name,
                secrets_manager=self.secrets_manager,
            )
        if marlin_hyperliquid_bootstrap and authenticated is not True:
            raise PermissionError("Marlin Hyperliquid account authentication failed")

        # Check if this is a Gateway network connector
        # Gateway connectors are NOT in AllConnectorSettings (those are exchange connectors)
        # Network format: "chain-network" (e.g., "solana-mainnet-beta", "ethereum-mainnet")
        if connector_name not in self._conn_settings:
            logger.info(f"Creating Gateway connector for network: {connector_name}")
            return Gateway(
                connector_name=connector_name,
                trading_pairs=[],
                trading_required=True,
            )

        conn_setting = self._conn_settings[connector_name]

        if marlin_hyperliquid_bootstrap:
            keys = _derive_marlin_credential_values(connector_name)
            if keys is None:
                raise RuntimeError("MARLIN_MNEMONIC-derived Hyperliquid credentials unavailable")
        else:
            keys = BackendAPISecurity.api_keys(connector_name)

        init_params = conn_setting.conn_init_parameters(
            trading_pairs=[],
            trading_required=True,
            api_keys=keys,
        )

        connector_class = get_connector_class(connector_name)
        return connector_class(**init_params)

    def _create_data_connector(self, connector_name: str) -> ConnectorBase:
        """Create a non-authenticated data connector."""
        if connector_name == COWSWAP_CONNECTOR_NAME:
            blocker = cowswap_order_submission_blocker(connector_name) or UNWIRED_RUNTIME_BLOCKER
            raise ValueError(blocker)

        conn_setting = self._conn_settings.get(connector_name)
        if not conn_setting:
            raise ValueError(f"Connector {connector_name} not found")

        # Get config keys but don't use real API keys
        connector_config = AllConnectorSettings.get_connector_config_keys(connector_name)
        if getattr(connector_config, "use_auth_for_public_endpoints", False):
            api_keys = api_keys_from_connector_config_map(
                ClientConfigAdapter(connector_config)
            )
        elif connector_config is not None:
            api_keys = {
                key: ""
                for key in connector_config.__class__.model_fields.keys()
                if key != "connector"
            }
        else:
            api_keys = {}

        init_params = conn_setting.conn_init_parameters(
            trading_pairs=[],
            trading_required=False,
            api_keys=api_keys,
        )

        connector_class = get_connector_class(connector_name)
        connector = connector_class(**init_params)

        logger.info(f"Created data connector: {connector_name}")
        return connector

    # =========================================================================
    # Network and State Management
    # =========================================================================

    def _requires_network_before_initial_queries(self, connector_name: str) -> bool:
        """Return true for connectors whose initial queries require a started network."""
        return connector_name in EARLY_NETWORK_CONNECTORS

    async def _start_connector_network(self, connector: ConnectorBase, connector_name: str | None = None):
        """Start connector network tasks."""
        try:
            await self._stop_connector_network(connector)

            if connector_name in EARLY_NETWORK_CONNECTORS and hasattr(connector, 'start_network'):
                await connector.start_network()
                logger.debug("Started connector-owned network for %s", connector_name)
                return

            # Gateway/AMM connectors use start_network() instead of individual polling tasks
            if hasattr(connector, '_trading_rules_polling_loop'):
                connector._trading_rules_polling_task = safe_ensure_future(
                    connector._trading_rules_polling_loop()
                )
            if hasattr(connector, '_trading_fees_polling_loop'):
                connector._trading_fees_polling_task = safe_ensure_future(
                    connector._trading_fees_polling_loop()
                )
            if hasattr(connector, '_create_user_stream_tracker_task'):
                connector._user_stream_tracker_task = connector._create_user_stream_tracker_task()
            if hasattr(connector, '_user_stream_event_listener'):
                connector._user_stream_event_listener_task = safe_ensure_future(
                    connector._user_stream_event_listener()
                )
            if hasattr(connector, '_lost_orders_update_polling_loop'):
                connector._lost_orders_update_task = safe_ensure_future(
                    connector._lost_orders_update_polling_loop()
                )

            # For gateway connectors, call start_network() which handles chain/network detection
            if hasattr(connector, 'start_network') and not hasattr(connector, '_trading_rules_polling_loop'):
                await connector.start_network()

            # NOTE: Order book tracker is started lazily when first trading pair is added
            # (in _add_trading_pair_to_tracker). Starting it here with no subscriptions
            # causes exchanges like Binance to immediately disconnect (close code 1008).

            logger.debug("Started network tasks for connector")

        except Exception as e:
            logger.error(f"Error starting connector network: {e}")
            raise

    async def _stop_connector_network(self, connector: ConnectorBase):
        """Stop connector network tasks."""
        tasks = [
            '_trading_rules_polling_task',
            '_trading_fees_polling_task',
            '_status_polling_task',
            '_user_stream_tracker_task',
            '_user_stream_event_listener_task',
            '_lost_orders_update_task',
        ]

        for task_name in tasks:
            task = getattr(connector, task_name, None)
            if task:
                task.cancel()
                setattr(connector, task_name, None)

        # Stop the order book tracker (if connector has one - AMM/Gateway connectors don't)
        if hasattr(connector, 'order_book_tracker') and connector.order_book_tracker:
            connector.order_book_tracker.stop()

        # For gateway connectors, call stop_network()
        if hasattr(connector, 'stop_network'):
            await connector.stop_network()

    async def _update_connector_state(
        self,
        connector: ConnectorBase,
        connector_name: str,
        account_name: str = None
    ):
        """Update connector state (balances, positions, orders).

        Note: Trading rules are NOT refreshed here — the background
        _trading_rules_polling_loop() (started in _start_connector_network)
        already handles that.
        """
        try:
            connector._set_current_timestamp(time.time())
            await connector._update_balances()

            if self._is_perpetual_connector(connector):
                await connector._update_positions()

            if connector.in_flight_orders:
                await connector._update_order_status()
                if account_name:
                    await self._sync_orders_to_database(
                        connector, account_name, connector_name
                    )

        except Exception as e:
            logger.error(f"Error updating connector state: {e}")

    async def update_all_trading_connector_states(self):
        """Update state for all trading connectors in parallel."""
        tasks = []
        task_keys = []
        for account_name, connectors in self._trading_connectors.items():
            for connector_name, connector in connectors.items():
                tasks.append(self._update_connector_state(connector, connector_name, account_name))
                task_keys.append(f"{account_name}/{connector_name}")
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for key, result in zip(task_keys, results):
                if isinstance(result, Exception):
                    logger.error(f"Error updating {key}: {result}")

    async def initialize_all_trading_connectors(
        self,
        startup_connectors: Optional[set[str]] = None,
    ):
        """
        Initialize all trading connectors for all accounts at startup.

        This ensures that:
        1. All connectors are ready to use immediately
        2. Existing orders from database are loaded into in_flight_orders
        3. Order tracking and cancellation work without needing manual initialization
        """
        # Get list of all accounts
        marlin_bootstrap = is_marlin_hyperliquid_bootstrap_scope(
            "master_account", "hyperliquid_perpetual"
        )
        try:
            accounts = fs_util.list_folders('credentials')
        except FileNotFoundError:
            if not marlin_bootstrap:
                raise
            accounts = []
        if marlin_bootstrap and "master_account" not in accounts:
            accounts.append("master_account")

        total_initialized = 0
        for account_name in accounts:
            # Get all connector credentials for this account
            connector_names = self.list_available_credentials(account_name)

            for connector_name in connector_names:
                if startup_connectors is not None and connector_name not in startup_connectors:
                    logger.info(
                        "Skipping startup initialization for %s/%s; not in startup connector allowlist",
                        account_name,
                        connector_name,
                    )
                    continue
                try:
                    logger.info(f"Initializing connector: {account_name}/{connector_name}")
                    await self.get_trading_connector(account_name, connector_name)
                    total_initialized += 1
                except Exception as e:
                    logger.error(f"Failed to initialize {account_name}/{connector_name}: {e}")
                    # Continue with other connectors even if one fails
                    continue

        logger.info(f"Initialized {total_initialized} trading connectors across {len(accounts)} accounts")

    # =========================================================================
    # Order Management
    # =========================================================================

    async def _load_existing_orders(
        self,
        connector: ConnectorBase,
        account_name: str,
        connector_name: str
    ):
        """Load existing orders from database into connector."""
        try:
            from database import OrderRepository

            async with self.db_manager.get_session_context() as session:
                order_repo = OrderRepository(session)
                active_orders = await order_repo.get_active_orders(
                    account_name=account_name,
                    connector_name=connector_name
                )

                for order_record in active_orders:
                    try:
                        in_flight_order = self._convert_db_order_to_in_flight(order_record)
                        connector.in_flight_orders[in_flight_order.client_order_id] = in_flight_order
                    except Exception as e:
                        logger.error(f"Error loading order {order_record.client_order_id}: {e}")

                logger.info(
                    f"Loaded {len(connector.in_flight_orders)} orders for "
                    f"{account_name}/{connector_name}"
                )

        except Exception as e:
            logger.error(f"Error loading orders from database: {e}")

    async def _sync_orders_to_database(
        self,
        connector: ConnectorBase,
        account_name: str,
        connector_name: str
    ):
        """Sync connector's in_flight_orders state to database."""
        if not self.db_manager:
            return

        terminal_states = [
            OrderState.FILLED, OrderState.CANCELED,
            OrderState.FAILED, OrderState.COMPLETED
        ]
        orders_to_remove = []

        for client_order_id, order in list(connector.in_flight_orders.items()):
            try:
                from database import OrderRepository

                async with self.db_manager.get_session_context() as session:
                    order_repo = OrderRepository(session)
                    db_order = await order_repo.get_order_by_client_id(client_order_id)

                    if db_order:
                        new_status = self._map_order_state_to_status(order.current_state)
                        if db_order.status != new_status:
                            await order_repo.update_order_status(client_order_id, new_status)

                    if order.current_state in terminal_states:
                        orders_to_remove.append(client_order_id)

            except Exception as e:
                logger.error(f"Error syncing order {client_order_id}: {e}")

        for order_id in orders_to_remove:
            connector.in_flight_orders.pop(order_id, None)

    @staticmethod
    def _supports_order_status_query(connector: ConnectorBase) -> bool:
        """Whether a connector can be asked the real state of a single order."""
        return (
            hasattr(connector, "_request_order_status")
            and hasattr(connector, "_is_order_not_found_during_status_update_error")
            and hasattr(connector, "in_flight_orders")
        )

    async def reconcile_active_orders(self) -> Dict[str, int]:
        """Reconcile persisted active orders against the exchange on startup.

        Must be called AFTER ``initialize_all_trading_connectors`` so that each
        connector's persisted active orders have been reloaded into
        ``in_flight_orders``. For every tracked order we ask the exchange for its
        real state (``_request_order_status``) and:

        - **Confirmed terminal** (filled/cancelled/failed) -> update the DB to the
          real status and drop it from tracking.
        - **Confirmed not found** on the exchange -> mark it CANCELLED in the DB
          (it no longer exists) and drop it from tracking.
        - **Still open** -> sync the DB to the live state and KEEP it tracked, so
          it remains visible and cancelable via the trading endpoints.
        - **Unverifiable** (transient error, or the connector isn't available)
          -> leave the order untouched. We never mark an order terminal unless
          the exchange confirms it, so a connector that failed to start can never
          cause a live order to be falsely reported as cancelled.

        Returns a summary dict with counts for logging/telemetry.
        """
        summary = {"reconciled_terminal": 0, "still_open": 0, "unverified": 0, "skipped_connectors": 0}
        if not self.db_manager:
            return summary

        terminal_states = {
            OrderState.FILLED, OrderState.CANCELED,
            OrderState.FAILED, OrderState.COMPLETED,
        }

        from database import OrderRepository
        from database.repositories.trade_repository import TradeRepository

        for account_name, connectors in self._trading_connectors.items():
            for connector_name, connector in connectors.items():
                if not self._supports_order_status_query(connector) or not connector.in_flight_orders:
                    continue

                # Snapshot tracked orders (the set was loaded from the DB at init).
                tracked_orders = list(connector.in_flight_orders.values())
                for order in tracked_orders:
                    client_order_id = order.client_order_id
                    note = None
                    external_fills: List[Dict[str, Any]] = []
                    try:
                        order_update = await connector._request_order_status(order)
                        new_state = order_update.new_state
                        if new_state in terminal_states:
                            await self._refresh_tracked_order_fills(connector, order)
                            external_fills = await self._fetch_hyperliquid_order_fills(connector, order)
                    except Exception as exc:
                        if connector._is_order_not_found_during_status_update_error(exc):
                            # The exchange does not know this order -> it is gone.
                            new_state = OrderState.CANCELED
                            note = "Reconciled on startup: order not found on exchange"
                        else:
                            # Transient/unknown error - do not touch the order.
                            logger.warning(
                                f"Could not verify order {client_order_id} on "
                                f"{account_name}/{connector_name}: {exc}"
                            )
                            summary["unverified"] += 1
                            continue

                    db_status = self._map_order_state_to_status(new_state)
                    try:
                        async with self.db_manager.get_session_context() as session:
                            order_repo = OrderRepository(session)
                            trade_repo = TradeRepository(session)
                            await self._persist_tracked_order_fill(order_repo, order)
                            await self._persist_external_order_fills(
                                order_repo=order_repo,
                                trade_repo=trade_repo,
                                order=order,
                                fills=external_fills,
                            )
                            await order_repo.update_order_status(
                                client_order_id=client_order_id,
                                status=db_status,
                                error_message=note,
                            )
                    except Exception as exc:
                        logger.error(f"Failed to persist reconciled order {client_order_id}: {exc}")
                        summary["unverified"] += 1
                        continue

                    if new_state in terminal_states:
                        connector.in_flight_orders.pop(client_order_id, None)
                        summary["reconciled_terminal"] += 1
                    else:
                        # Keep tracking so it stays cancelable via the trading endpoints.
                        summary["still_open"] += 1

        logger.info(
            "Order reconciliation complete: "
            f"{summary['reconciled_terminal']} closed, "
            f"{summary['still_open']} still open (re-tracked), "
            f"{summary['unverified']} unverified"
        )
        return summary

    async def _refresh_tracked_order_fills(self, connector: ConnectorBase, order: InFlightOrder) -> None:
        """Ask a connector to hydrate fills for a tracked order before terminal reconciliation."""
        try:
            if hasattr(connector, "_all_trade_updates_for_order"):
                trade_updates = await connector._all_trade_updates_for_order(order)
                for trade_update in trade_updates or []:
                    connector._order_tracker.process_trade_update(trade_update)
            if hasattr(connector, "_update_trade_history"):
                await connector._update_trade_history()
        except Exception as exc:
            logger.warning(
                f"Could not refresh fills for order {order.client_order_id}: {exc}",
            )

    @staticmethod
    async def _fetch_hyperliquid_order_fills(
        connector: ConnectorBase,
        order: InFlightOrder,
    ) -> List[Dict[str, Any]]:
        """Fetch Hyperliquid fills for one order when websocket fill events were missed."""
        if not connector.__class__.__module__.startswith("hummingbot.connector.") or "hyperliquid" not in connector.__class__.__module__:
            return []

        user = (
            getattr(connector, "hyperliquid_perpetual_address", None)
            or getattr(connector, "hyperliquid_address", None)
        )
        if not user:
            return []

        url = await connector._api_request_url(path_url="/info")
        fills = await asyncio.to_thread(
            UnifiedConnectorService._post_json,
            url,
            {"type": "userFills", "user": user},
        )
        exchange_order_id = str(order.exchange_order_id or "")
        client_order_id = str(order.client_order_id)
        return [
            fill for fill in fills or []
            if str(fill.get("oid", "")) == exchange_order_id
            or str(fill.get("cloid", "")) == client_order_id
        ]

    @staticmethod
    def _post_json(url: str, payload: Dict[str, Any]) -> Any:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())

    @staticmethod
    async def _persist_tracked_order_fill(order_repo, order: InFlightOrder) -> None:
        """Persist fill totals already hydrated on an InFlightOrder without double-counting."""
        executed_amount = Decimal(str(getattr(order, "executed_amount_base", 0) or 0))
        if executed_amount <= 0:
            return

        db_order = await order_repo.get_order_by_client_id(order.client_order_id)
        if db_order is None:
            return

        previous_filled = Decimal(str(db_order.filled_amount or 0))
        fill_delta = executed_amount - previous_filled
        if fill_delta <= 0:
            return

        average_price = (
            getattr(order, "average_executed_price", None)
            or getattr(order, "last_executed_price", None)
            or getattr(order, "price", None)
            or Decimal("0")
        )
        fee_paid = getattr(order, "cumulative_fee_paid_quote", None)
        fee_currency = getattr(order, "quote_asset", None)
        await order_repo.update_order_fill(
            client_order_id=order.client_order_id,
            filled_amount=fill_delta,
            average_fill_price=Decimal(str(average_price)),
            fee_paid=Decimal(str(fee_paid)) if fee_paid else None,
            fee_currency=fee_currency,
            exchange_order_id=order.exchange_order_id,
        )

    @staticmethod
    async def _persist_external_order_fills(
        *,
        order_repo,
        trade_repo,
        order: InFlightOrder,
        fills: List[Dict[str, Any]],
    ) -> None:
        """Persist exchange fills idempotently for terminal orders missing websocket fill events."""
        if not fills:
            return

        db_order = await order_repo.get_order_by_client_id(order.client_order_id)
        if db_order is None:
            return

        for fill in sorted(fills, key=lambda item: int(item.get("time", 0) or 0)):
            trade_id = str(fill.get("tid") or "")
            if not trade_id or await trade_repo.get_trade_by_id(trade_id):
                continue

            filled_amount = Decimal(str(fill["sz"]))
            fill_price = Decimal(str(fill["px"]))
            fee_paid = Decimal(str(fill.get("fee") or 0))
            fee_currency = fill.get("feeToken")
            timestamp_ms = int(fill.get("time") or 0)
            trade_type = order.trade_type.name
            created_trade = await trade_repo.create_trade(
                {
                    "order_id": db_order.id,
                    "trade_id": trade_id,
                    "timestamp": datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc),
                    "trading_pair": order.trading_pair,
                    "trade_type": trade_type,
                    "amount": float(filled_amount),
                    "price": float(fill_price),
                    "fee_paid": float(fee_paid),
                    "fee_currency": fee_currency,
                }
            )
            if created_trade is None:
                continue

            await order_repo.update_order_fill(
                client_order_id=order.client_order_id,
                filled_amount=filled_amount,
                average_fill_price=fill_price,
                fee_paid=fee_paid,
                fee_currency=fee_currency,
                exchange_order_id=str(fill.get("oid") or order.exchange_order_id or ""),
            )

    async def sync_all_orders_to_database(self):
        """
        Sync connector's in_flight_orders state to database for all trading connectors.

        The connector's built-in polling already updates in_flight_orders from the exchange.
        This method syncs that state to our database and cleans up closed orders.
        """
        for account_name, connectors in self._trading_connectors.items():
            for connector_name, connector in connectors.items():
                try:
                    if not connector.in_flight_orders:
                        continue
                    await self._sync_orders_to_database(connector, account_name, connector_name)
                    logger.debug(f"Synced order state to DB for {account_name}/{connector_name}")
                except Exception as e:
                    logger.error(f"Error syncing order state for {account_name}/{connector_name}: {e}")

    def _convert_db_order_to_in_flight(self, order_record) -> InFlightOrder:
        """Convert database order to InFlightOrder."""
        status_mapping = {
            "SUBMITTED": OrderState.PENDING_CREATE,
            "OPEN": OrderState.OPEN,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
            "FILLED": OrderState.FILLED,
            "CANCELLED": OrderState.CANCELED,
            "FAILED": OrderState.FAILED,
        }

        order_state = status_mapping.get(order_record.status, OrderState.PENDING_CREATE)

        try:
            order_type = OrderType[order_record.order_type]
        except (KeyError, ValueError):
            order_type = OrderType.LIMIT

        try:
            trade_type = TradeType[order_record.trade_type]
        except (KeyError, ValueError):
            trade_type = TradeType.BUY

        creation_timestamp = (
            order_record.created_at.timestamp()
            if order_record.created_at else time.time()
        )

        in_flight_order = InFlightOrder(
            client_order_id=order_record.client_order_id,
            trading_pair=order_record.trading_pair,
            order_type=order_type,
            trade_type=trade_type,
            amount=Decimal(str(order_record.amount)),
            creation_timestamp=creation_timestamp,
            price=Decimal(str(order_record.price)) if order_record.price else None,
            exchange_order_id=order_record.exchange_order_id,
            initial_state=order_state,
            leverage=1,
            position=PositionAction.NIL,
        )

        in_flight_order.current_state = order_state
        if order_record.filled_amount:
            in_flight_order.executed_amount_base = Decimal(str(order_record.filled_amount))

        return in_flight_order

    def _map_order_state_to_status(self, order_state: OrderState) -> str:
        """Map OrderState to database status string."""
        mapping = {
            OrderState.PENDING_CREATE: "SUBMITTED",
            OrderState.OPEN: "OPEN",
            OrderState.PENDING_CANCEL: "PENDING_CANCEL",
            OrderState.CANCELED: "CANCELLED",
            OrderState.PARTIALLY_FILLED: "PARTIALLY_FILLED",
            OrderState.FILLED: "FILLED",
            OrderState.FAILED: "FAILED",
            OrderState.PENDING_APPROVAL: "SUBMITTED",
            OrderState.APPROVED: "SUBMITTED",
            OrderState.CREATED: "SUBMITTED",
            OrderState.COMPLETED: "FILLED",
        }
        return mapping.get(order_state, "SUBMITTED")

    # =========================================================================
    # Metrics
    # =========================================================================

    def _initialize_metrics(
        self,
        connector: ConnectorBase,
        account_name: str,
        connector_name: str,
        cache_key: str
    ):
        """Initialize trade volume metrics collector."""
        if cache_key in self._metrics_collectors:
            return

        if "_paper_trade" in connector_name:
            return

        try:
            instance_id = f"{account_name}_hbotapi"
            rate_provider = RateOracle.get_instance()

            metrics_collector = TradeVolumeMetricCollector(
                connector=connector,
                activation_interval=self.METRICS_ACTIVATION_INTERVAL,
                rate_provider=rate_provider,
                instance_id=instance_id,
                valuation_token=self.METRICS_VALUATION_TOKEN
            )
            metrics_collector.start()
            self._metrics_collectors[cache_key] = metrics_collector

        except Exception as e:
            logger.warning(f"Failed to init metrics for {connector_name}: {e}")

    # =========================================================================
    # Credentials and Configuration
    # =========================================================================

    async def update_connector_keys(
        self,
        account_name: str,
        connector_name: str,
        keys: dict
    ) -> ConnectorBase:
        """Update API keys and recreate connector."""
        if not BackendAPISecurity.login_account(
            account_name=account_name,
            secrets_manager=self.secrets_manager
        ):
            raise ValueError(f"Failed to authenticate for {account_name}")

        connector_config = HummingbotAPIConfigAdapter(
            AllConnectorSettings.get_connector_config_keys(connector_name)
        )

        for key, value in keys.items():
            setattr(connector_config, key, value)

        BackendAPISecurity.update_connector_keys(account_name, connector_config)
        BackendAPISecurity.decrypt_all(account_name=account_name)

        # Properly stop old connector (stops recorders, network tasks, cleans up caches)
        await self.stop_trading_connector(account_name, connector_name)

        # Create new connector with fresh recorders
        return await self.get_trading_connector(account_name, connector_name)

    def clear_trading_connector(
        self,
        account_name: Optional[str] = None,
        connector_name: Optional[str] = None
    ):
        """Clear trading connector from cache."""
        if account_name and connector_name:
            if account_name in self._trading_connectors:
                self._trading_connectors[account_name].pop(connector_name, None)
        elif account_name:
            self._trading_connectors.pop(account_name, None)
        else:
            self._trading_connectors.clear()

    def list_account_connectors(self, account_name: str) -> List[str]:
        """List initialized connectors for an account."""
        return list(self._trading_connectors.get(account_name, {}).keys())

    def list_available_credentials(self, account_name: str) -> List[str]:
        """List connector credentials available for an account."""
        try:
            files = fs_util.list_files(f"credentials/{account_name}/connectors")
            result = [f.replace(".yml", "") for f in files if f.endswith(".yml")]
        except FileNotFoundError:
            result = []

        if is_marlin_hyperliquid_bootstrap_scope(account_name, "hyperliquid_perpetual"):
            marlin_only = "hyperliquid_perpetual"
            if marlin_only not in result:
                result.append(marlin_only)

        return result

    @staticmethod
    def get_connector_config_map(connector_name: str):
        """Get connector config field info."""
        from typing import Literal, get_args, get_origin

        connector_config = HummingbotAPIConfigAdapter(
            AllConnectorSettings.get_connector_config_keys(connector_name)
        )
        fields_info = {}

        for key, field in connector_config.hb_config.model_fields.items():
            if key == "connector":
                continue

            field_type = field.annotation
            type_name = getattr(field_type, "__name__", str(field_type))
            allowed_values = None

            origin = get_origin(field_type)
            args = get_args(field_type)

            if origin is Literal:
                type_name = "Literal"
                allowed_values = list(args)
            elif origin is not None:
                if type(None) in args:
                    actual_types = [arg for arg in args if arg is not type(None)]
                    if actual_types:
                        inner_type = actual_types[0]
                        inner_origin = get_origin(inner_type)
                        inner_args = get_args(inner_type)
                        if inner_origin is Literal:
                            type_name = "Literal"
                            allowed_values = list(inner_args)
                        else:
                            type_name = getattr(inner_type, "__name__", str(inner_type))
                else:
                    type_name = str(field_type)

            field_info = {"type": type_name, "required": field.is_required()}
            if allowed_values is not None:
                field_info["allowed_values"] = allowed_values
            fields_info[key] = field_info

        return fields_info

    # =========================================================================
    # Cleanup
    # =========================================================================

    async def stop_trading_connector(self, account_name: str, connector_name: str):
        """Stop a trading connector and its services."""
        cache_key = f"{account_name}:{connector_name}"

        # Stop recorders
        if cache_key in self._orders_recorders:
            try:
                await self._orders_recorders[cache_key].stop()
                del self._orders_recorders[cache_key]
            except Exception as e:
                logger.error(f"Error stopping orders recorder: {e}")

        if cache_key in self._funding_recorders:
            try:
                await self._funding_recorders[cache_key].stop()
                del self._funding_recorders[cache_key]
            except Exception as e:
                logger.error(f"Error stopping funding recorder: {e}")

        if cache_key in self._metrics_collectors:
            try:
                self._metrics_collectors[cache_key].stop()
                del self._metrics_collectors[cache_key]
            except Exception as e:
                logger.error(f"Error stopping metrics: {e}")

        # Stop connector network
        if account_name in self._trading_connectors:
            connector = self._trading_connectors[account_name].get(connector_name)
            if connector:
                await self._stop_connector_network(connector)
                del self._trading_connectors[account_name][connector_name]

        logger.info(f"Stopped trading connector {account_name}/{connector_name}")

    async def stop_all(self):
        """Stop all connectors and services."""
        # Stop all trading connectors
        for account_name, connectors in list(self._trading_connectors.items()):
            for connector_name in list(connectors.keys()):
                await self.stop_trading_connector(account_name, connector_name)

        # Stop data connectors
        for connector_name, connector in self._data_connectors.items():
            try:
                await connector.stop_network()
            except Exception as e:
                logger.error(f"Error stopping data connector {connector_name}: {e}")

        self._data_connectors.clear()
        self._data_connectors_started.clear()

        logger.info("Stopped all connectors")

    # =========================================================================
    # Order Book Tracker Diagnostics & Restart
    # =========================================================================

    def get_order_book_tracker_diagnostics(
        self,
        connector_name: str,
        account_name: Optional[str] = None
    ) -> Dict:
        """Get diagnostics for a connector's order book tracker.

        Returns information about:
        - Whether the tracker is running
        - Task status (alive/crashed)
        - Metrics (diffs processed, last update, etc.)
        - WebSocket status

        Args:
            connector_name: The connector to diagnose
            account_name: Optional account for trading connector

        Returns:
            Dictionary with diagnostic information
        """
        connector = self.get_best_connector_for_market(connector_name, account_name)

        if not connector:
            return {"error": f"No connector found for {connector_name}"}

        diagnostics = {
            "connector_type": type(connector).__name__,
            "connector_name": connector_name,
            "has_order_book_tracker": False,
            "tracker_ready": False,
            "tasks": {},
            "trading_pairs": [],
            "order_books": {},
            "metrics": None,
            "websocket_status": "unknown",
        }

        if not hasattr(connector, 'order_book_tracker') or not connector.order_book_tracker:
            return diagnostics

        tracker = connector.order_book_tracker
        diagnostics["has_order_book_tracker"] = True
        diagnostics["tracker_ready"] = tracker.ready if hasattr(tracker, 'ready') else False

        # Get trading pairs
        if hasattr(tracker, '_trading_pairs'):
            diagnostics["trading_pairs"] = list(tracker._trading_pairs) if isinstance(tracker._trading_pairs, (list, set)) else []

        # Check task status
        task_names = [
            '_order_book_stream_listener_task',
            '_order_book_diff_listener_task',
            '_order_book_trade_listener_task',
            '_order_book_snapshot_listener_task',
            '_order_book_diff_router_task',
            '_order_book_snapshot_router_task',
            '_init_order_books_task',
            '_emit_trade_event_task',
        ]

        for task_name in task_names:
            task = getattr(tracker, task_name, None)
            if task is not None:
                diagnostics["tasks"][task_name] = {
                    "exists": True,
                    "done": task.done(),
                    "cancelled": task.cancelled(),
                    "exception": str(task.exception()) if task.done() and not task.cancelled() and task.exception() else None,
                }
            else:
                diagnostics["tasks"][task_name] = {"exists": False}

        # Check order books
        if hasattr(tracker, 'order_books'):
            for trading_pair, order_book in tracker.order_books.items():
                try:
                    bids, asks = order_book.snapshot
                    best_bid = float(bids.iloc[0]['price']) if len(bids) > 0 else None
                    best_ask = float(asks.iloc[0]['price']) if len(asks) > 0 else None
                    diagnostics["order_books"][trading_pair] = {
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "bid_count": len(bids),
                        "ask_count": len(asks),
                        "snapshot_uid": order_book.snapshot_uid if hasattr(order_book, 'snapshot_uid') else None,
                        "last_diff_uid": order_book.last_diff_uid if hasattr(order_book, 'last_diff_uid') else None,
                    }
                except Exception as e:
                    diagnostics["order_books"][trading_pair] = {"error": str(e)}

        # Get metrics if available
        if hasattr(tracker, 'metrics'):
            try:
                diagnostics["metrics"] = tracker.metrics.to_dict()
            except Exception as e:
                diagnostics["metrics"] = {"error": str(e)}

        # Check WebSocket status
        if hasattr(connector, '_orderbook_ds') and connector._orderbook_ds:
            data_source = connector._orderbook_ds
            if hasattr(data_source, '_ws_assistant') and data_source._ws_assistant is not None:
                diagnostics["websocket_status"] = "connected"
            else:
                diagnostics["websocket_status"] = "not_connected"

        return diagnostics

    async def restart_order_book_tracker(
        self,
        connector_name: str,
        account_name: Optional[str] = None
    ) -> Dict:
        """Restart the order book tracker for a connector.

        This method:
        1. Stops the existing order book tracker
        2. Restarts it with the same trading pairs

        Args:
            connector_name: The connector to restart
            account_name: Optional account for trading connector

        Returns:
            Dictionary with restart status
        """
        connector = self.get_best_connector_for_market(connector_name, account_name)

        if not connector:
            return {"success": False, "error": f"No connector found for {connector_name}"}

        # Gateway/AMM connectors don't have order book trackers
        if not hasattr(connector, 'order_book_tracker') or connector.order_book_tracker is None:
            return {"success": False, "error": f"Connector {connector_name} doesn't have order book tracker (AMM/Gateway)"}

        tracker = connector.order_book_tracker
        trading_pairs = list(tracker._trading_pairs)

        if not trading_pairs:
            return {"success": False, "error": "No trading pairs to restart"}

        try:
            # Stop the tracker
            logger.info(f"Stopping order book tracker for {connector_name}...")
            tracker.stop()

            # Wait a moment for cleanup
            await asyncio.sleep(0.5)

            # Re-add trading pairs to tracker before restarting
            tracker._trading_pairs.clear()
            for tp in trading_pairs:
                tracker._trading_pairs.append(tp)

            # Restart the tracker
            logger.info(f"Restarting order book tracker for {connector_name} with pairs: {trading_pairs}")
            tracker.start()

            # Wait for initialization
            try:
                await asyncio.wait_for(tracker.wait_ready(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for tracker to be ready, continuing anyway...")

            # Wait for WebSocket to be ready
            await self._wait_for_websocket_ready(connector, timeout=10.0)

            return {
                "success": True,
                "message": f"Order book tracker restarted for {connector_name}",
                "trading_pairs": trading_pairs,
            }

        except Exception as e:
            logger.error(f"Error restarting order book tracker: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
