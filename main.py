import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import logfire
from dotenv import load_dotenv

# Apply the patch before importing hummingbot components
from hummingbot.client.config import config_helpers

# Load environment variables early
load_dotenv()

VERSION = "1.0.1"

# Monkey patch save_to_yml to prevent writes to library directory


def patched_save_to_yml(yml_path, cm):
    """Patched version of save_to_yml that prevents writes to library directory"""
    import logging
    logger = logging.getLogger(__name__)
    logger.debug(f"Skipping config write to {yml_path} (patched for API mode)")
    # Do nothing - this prevents the original function from trying to write to the library directory


config_helpers.save_to_yml = patched_save_to_yml

from fastapi import Depends, FastAPI, HTTPException, Request, status  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.security import HTTPBasic, HTTPBasicCredentials  # noqa: E402
from hummingbot.client.config.client_config_map import GatewayConfigMap  # noqa: E402
from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger  # noqa: E402
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient  # noqa: E402
from hummingbot.core.rate_oracle.rate_oracle import RATE_ORACLE_SOURCES, RateOracle  # noqa: E402

from config import settings  # noqa: E402
from database import AsyncDatabaseManager  # noqa: E402
from services.accounts_service import AccountsService  # noqa: E402
from services.cowswap_runtime import (  # noqa: E402
    CowSwapRuntimeDependencies,
    build_cowswap_runtime,
    cowswap_token_map_from_json,
    get_cowswap_runtime_status,
)
from services.market_data_service import MarketDataService  # noqa: E402
from services.trading_service import TradingService  # noqa: E402
from services.unified_connector_service import UnifiedConnectorService  # noqa: E402
from services.websocket_manager import WebSocketManager  # noqa: E402
from utils.security import BackendAPISecurity  # noqa: E402


def env_text(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def env_csv_set(name: str) -> set[str] | None:
    value = os.environ.get(name, "")
    values = {item.strip() for item in value.split(",") if item.strip()}
    return values or None


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def runtime_profile() -> str:
    return env_text("HUMMINGBOT_API_RUNTIME_PROFILE", "full").strip().lower()


def provider_runtime_enabled() -> bool:
    return runtime_profile() in {"provider", "marlin"}


def marlin_runtime_enabled() -> bool:
    return (
        runtime_profile() == "marlin"
        or os.environ.get("MARLIN_RUNTIME_PROFILE", "").strip().lower() == "marlin"
    )

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Enable info logging for MQTT manager
logging.getLogger('services.mqtt_manager').setLevel(logging.INFO)

# Get settings from Pydantic Settings
username = settings.security.username
password = settings.security.password
debug_mode = settings.security.debug_mode

# Security setup
security = HTTPBasic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for the FastAPI application.
    Handles startup and shutdown events.
    """
    # Ensure password verification file exists
    if BackendAPISecurity.new_password_required():
        # Create secrets manager with CONFIG_PASSWORD
        secrets_manager = ETHKeyFileSecretManger(password=settings.security.config_password)
        BackendAPISecurity.store_password_verification(secrets_manager)
        logging.info("Created password verification file for master_account")

    # =========================================================================
    # 1. Infrastructure Setup
    # =========================================================================

    # Initialize GatewayHttpClient singleton
    parsed_gateway_url = urlparse(settings.gateway.url)
    gateway_config = GatewayConfigMap(
        gateway_api_host=parsed_gateway_url.hostname or "localhost",
        gateway_api_port=str(parsed_gateway_url.port or 15888),
        gateway_use_ssl=parsed_gateway_url.scheme == "https"
    )
    GatewayHttpClient.get_instance(gateway_config)
    logging.info(f"Initialized GatewayHttpClient with URL: {settings.gateway.url}")

    # Initialize secrets manager and database
    secrets_manager = ETHKeyFileSecretManger(password=settings.security.config_password)
    db_manager = AsyncDatabaseManager(settings.database.url)
    await db_manager.create_tables()
    logging.info("Database initialized")

    # Read rate oracle configuration from conf_client.yml
    from utils.file_system import FileSystemUtil
    fs_util = FileSystemUtil()

    try:
        conf_client_path = "credentials/master_account/conf_client.yml"
        config_data = fs_util.read_yaml_file(conf_client_path)

        # Get rate_oracle_source configuration
        rate_oracle_source_data = config_data.get("rate_oracle_source", {})
        source_name = rate_oracle_source_data.get("name", "binance")

        # Get global_token configuration
        global_token_data = config_data.get("global_token", {})
        quote_token = global_token_data.get("global_token_name", "USDT")

        # Create rate source instance
        from routers.rate_oracle import create_rate_source
        if source_name in RATE_ORACLE_SOURCES:
            rate_source = create_rate_source(source_name)
            logging.info(f"Configured RateOracle with source: {source_name}, quote_token: {quote_token}")
        else:
            logging.warning(f"Unknown rate oracle source '{source_name}', defaulting to binance")
            rate_source = create_rate_source("binance")
            source_name = "binance"

        # Initialize RateOracle with configured source and quote token
        rate_oracle = RateOracle.get_instance()
        rate_oracle.source = rate_source
        rate_oracle.quote_token = quote_token

    except FileNotFoundError:
        logging.warning("conf_client.yml not found, using default RateOracle configuration (binance, USDT)")
        rate_oracle = RateOracle.get_instance()
    except Exception as e:
        logging.warning(f"Error reading conf_client.yml: {e}, using default RateOracle configuration")
        rate_oracle = RateOracle.get_instance()

    # =========================================================================
    # 2. UnifiedConnectorService - Single source of truth for all connectors
    # =========================================================================

    connector_service = UnifiedConnectorService(
        secrets_manager=secrets_manager,
        db_manager=db_manager
    )
    logging.info("UnifiedConnectorService initialized")

    # =========================================================================
    # 3. Services that depend on connector_service
    # =========================================================================

    # MarketDataService - candles, order books, prices
    market_data_service = MarketDataService(
        connector_service=connector_service,
        rate_oracle=rate_oracle,
        cleanup_interval=settings.market_data.cleanup_interval,
        feed_timeout=settings.market_data.feed_timeout
    )
    logging.info("MarketDataService initialized")

    # TradingService - order placement, positions, trading interfaces
    trading_service = TradingService(
        connector_service=connector_service,
        market_data_service=market_data_service
    )
    logging.info("TradingService initialized")

    startup_connectors = env_csv_set("HUMMINGBOT_STARTUP_CONNECTORS")

    # AccountsService - account management, balances, portfolio (simplified)
    accounts_service = AccountsService(
        account_update_interval=settings.app.account_update_interval,
        gateway_url=settings.gateway.url,
        startup_connectors=startup_connectors,
    )
    # Inject services into AccountsService
    accounts_service._connector_service = connector_service
    accounts_service._market_data_service = market_data_service
    accounts_service._trading_service = trading_service
    market_data_service.configure_accounts_service(accounts_service)
    cowswap_status = get_cowswap_runtime_status()
    if cowswap_status.registration_available:
        cowswap_owner = None
        if marlin_runtime_enabled():
            cowswap_owner = accounts_service._marlin_gateway_default_wallet_address(
                chain="ethereum",
                network="base",
            )
            if cowswap_owner:
                await accounts_service.gateway_client.set_marlin_default_wallet(
                    address=cowswap_owner,
                    chain="ethereum",
                    network="base",
                    wallet_ref="base:mainnet:evm_gateway",
                )
        else:
            cowswap_owner = os.environ.get("COWSWAP_OWNER_ADDRESS")
        if not cowswap_owner and not marlin_runtime_enabled():
            try:
                cowswap_owner = await accounts_service.gateway_client.get_wallet_address_or_default("ethereum")
            except Exception as exc:
                logging.warning(f"CowSwap owner address not available from Gateway: {exc}")

        if cowswap_owner:
            try:
                cowswap_runtime, cowswap_dependencies = build_cowswap_runtime(
                    gateway_url=settings.gateway.url,
                    owner_address=cowswap_owner,
                    receiver_address=(
                        cowswap_owner
                        if marlin_runtime_enabled()
                        else os.environ.get("COWSWAP_RECEIVER_ADDRESS") or cowswap_owner
                    ),
                    data_dir=Path(os.environ.get("BOTS_PATH", "/hummingbot-api/bots")) / "data",
                    chain_id=env_int("COWSWAP_CHAIN_ID", 8453),
                    chain_name=env_text("COWSWAP_CHAIN_NAME", "base"),
                    network=env_text("COWSWAP_NETWORK", "base"),
                    env=env_text("COWSWAP_ENV", "staging"),
                    app_data=env_text("COWSWAP_APP_DATA", "0x" + "00" * 32),
                    slippage_bps=env_int("COWSWAP_SLIPPAGE_BPS", 50),
                    token_map=cowswap_token_map_from_json(os.environ.get("COWSWAP_TOKEN_MAP_JSON")),
                )
                accounts_service.configure_cowswap_runtime(
                    runtime=cowswap_runtime,
                    runtime_dependencies=cowswap_dependencies,
                )
                logging.info("CowSwap runtime initialized with Gateway-managed signer")
            except Exception as exc:
                logging.warning(f"CowSwap runtime build failed: {exc}")
                accounts_service.configure_cowswap_runtime(
                    runtime_dependencies=CowSwapRuntimeDependencies(),
                )
        else:
            logging.warning(
                "CowSwap owner address not configured; "
                "set MARLIN_MNEMONIC in Marlin runtime or configure an Ethereum wallet in Gateway"
            )
            accounts_service.configure_cowswap_runtime(
                runtime_dependencies=CowSwapRuntimeDependencies(),
            )
    else:
        logging.info(
            "CowSwap package not installed or blocked; runtime unavailable"
            + (f": {'; '.join(cowswap_status.blockers)}" if cowswap_status.blockers else ""),
        )
    logging.info("AccountsService initialized")

    executor_service = None
    executor_ws_manager = None
    backtesting_service = None
    bot_archiver = None
    bots_orchestrator = None
    docker_service = None
    gateway_service = None

    if provider_runtime_enabled():
        logging.info("Provider runtime profile enabled; bot orchestration/admin services disabled")
    else:
        from services.backtesting_service import BacktestingService
        from services.bots_orchestrator import BotsOrchestrator
        from services.docker_service import DockerService
        from services.executor_service import ExecutorService
        from services.gateway_service import GatewayService
        from utils.bot_archiver import BotArchiver

        executor_service = ExecutorService(
            trading_service=trading_service,
            db_manager=db_manager,
            default_account="master_account",
            update_interval=1.0,
            max_retries=10
        )
        logging.info("ExecutorService initialized")

        docker_control_disabled = env_bool("HUMMINGBOT_API_DISABLE_DOCKER_CONTROL")
        if docker_control_disabled:
            logging.info("Docker control services disabled by HUMMINGBOT_API_DISABLE_DOCKER_CONTROL")
        else:
            bots_orchestrator = BotsOrchestrator(
                broker_host=settings.broker.host,
                broker_port=settings.broker.port,
                broker_username=settings.broker.username,
                broker_password=settings.broker.password,
                performance_dump_interval=settings.broker.performance_dump_interval
            )
            docker_service = DockerService()
            gateway_service = GatewayService()

        backtesting_service = BacktestingService()
        bot_archiver = BotArchiver(
            settings.aws.api_key,
            settings.aws.secret_key,
            settings.aws.s3_default_bucket_name
        )

    # =========================================================================
    # 6. Start services
    # =========================================================================

    # Initialize all trading connectors FIRST (before any service that might use them)
    # This ensures OrdersRecorder is properly attached before any concurrent access
    if startup_connectors is None:
        logging.info("Initializing all trading connectors...")
    else:
        logging.info("Initializing startup connector allowlist: %s", sorted(startup_connectors))
    await connector_service.initialize_all_trading_connectors(
        startup_connectors=startup_connectors,
    )

    # Reconcile persisted active orders against the exchange (e.g. after an API
    # restart/crash that lost in-memory references). Confirmed-closed orders are
    # marked terminal; still-open orders are re-tracked so they stay cancelable.
    # Runs after connectors reload their persisted in-flight orders.
    await connector_service.reconcile_active_orders()

    if bots_orchestrator is not None:
        bots_orchestrator.start()
    market_data_service.start()
    await market_data_service.warmup_rate_oracle()
    if executor_service is not None:
        executor_service.start()
        await executor_service.cleanup_orphaned_executors()
        await executor_service.recover_positions_from_db()
    accounts_service.start()

    # =========================================================================
    # 7. Store services in app state
    # =========================================================================

    app.state.db_manager = db_manager
    app.state.connector_service = connector_service
    app.state.market_data_service = market_data_service
    app.state.trading_service = trading_service
    app.state.accounts_service = accounts_service
    app.state.executor_service = executor_service
    websocket_manager = WebSocketManager(market_data_service)
    app.state.websocket_manager = websocket_manager

    app.state.backtesting_service = backtesting_service
    app.state.bots_orchestrator = bots_orchestrator
    app.state.docker_service = docker_service
    app.state.gateway_service = gateway_service
    app.state.bot_archiver = bot_archiver

    if executor_service is not None:
        from services.executor_ws_manager import ExecutorWebSocketManager

        executor_ws_manager = ExecutorWebSocketManager(executor_service, market_data_service, bots_orchestrator)
    app.state.executor_ws_manager = executor_ws_manager

    logging.info("All services started successfully")

    yield

    # =========================================================================
    # Shutdown services
    # =========================================================================

    logging.info("Shutting down services...")

    websocket_manager.shutdown()
    if executor_ws_manager is not None:
        await executor_ws_manager.shutdown()
    if bots_orchestrator is not None:
        bots_orchestrator.stop()
    await accounts_service.stop()
    if executor_service is not None:
        await executor_service.stop()
    market_data_service.stop()
    await connector_service.stop_all()
    if docker_service is not None:
        docker_service.cleanup()
    await db_manager.close()

    logging.info("All services stopped")

# Initialize FastAPI with metadata and lifespan
app = FastAPI(
    title="Hummingbot API",
    description="API for managing Hummingbot trading instances",
    version=VERSION,
    lifespan=lifespan,
    redirect_slashes=False,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Modify in production to specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Custom handler for validation errors to log detailed error messages.
    """
    # Build a readable error message from validation errors
    error_messages = []
    for error in exc.errors():
        loc = " -> ".join(str(part) for part in error.get("loc", []))
        msg = error.get("msg", "Validation error")
        error_messages.append(f"{loc}: {msg}")

    # Log the validation error with details
    logging.warning(
        f"Validation error on {request.method} {request.url.path}: {'; '.join(error_messages)}"
    )

    # Return standard FastAPI validation error response
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors()},
    )

logfire.configure(send_to_logfire="if-token-present", environment=settings.app.logfire_environment,
                  service_name="hummingbot-api")
logfire.instrument_fastapi(app)


def auth_user(
        credentials: Annotated[HTTPBasicCredentials, Depends(security)],
):
    """Authenticate user using HTTP Basic Auth"""
    current_username_bytes = credentials.username.encode("utf8")
    correct_username_bytes = f"{username}".encode("utf8")
    is_correct_username = secrets.compare_digest(
        current_username_bytes, correct_username_bytes
    )
    current_password_bytes = credentials.password.encode("utf8")
    correct_password_bytes = f"{password}".encode("utf8")
    is_correct_password = secrets.compare_digest(
        current_password_bytes, correct_password_bytes
    )
    if not (is_correct_username and is_correct_password) and not debug_mode:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


def _include_provider_routers() -> None:
    from routers import (
        accounts,
        connectors,
        gateway_bridge,
        gateway_swap,
        market_data,
        portfolio,
        provider_boundary,
        provider_treasury,
        rate_oracle,
        trading,
    )

    app.include_router(connectors.router, dependencies=[Depends(auth_user)])
    app.include_router(
        accounts.credential_router,
        prefix="/accounts",
        dependencies=[Depends(auth_user)],
    )
    app.include_router(portfolio.router, dependencies=[Depends(auth_user)])
    app.include_router(trading.router, dependencies=[Depends(auth_user)])
    app.include_router(provider_boundary.router, dependencies=[Depends(auth_user)])
    app.include_router(provider_treasury.router, dependencies=[Depends(auth_user)])
    app.include_router(gateway_bridge.router, dependencies=[Depends(auth_user)])
    app.include_router(gateway_swap.router, dependencies=[Depends(auth_user)])
    app.include_router(market_data.router, dependencies=[Depends(auth_user)])
    app.include_router(rate_oracle.router, dependencies=[Depends(auth_user)])


def _include_full_routers() -> None:
    from routers import (
        accounts,
        archived_bots,
        backtesting,
        bot_orchestration,
        controllers,
        docker,
        executors,
        gateway,
        gateway_bridge,
        gateway_clmm,
        gateway_lp,
        scripts,
        storage,
        websocket,
    )

    app.include_router(docker.router, dependencies=[Depends(auth_user)])
    app.include_router(gateway.router, dependencies=[Depends(auth_user)])
    app.include_router(accounts.router, dependencies=[Depends(auth_user)])
    _include_provider_routers()
    app.include_router(gateway_bridge.router, dependencies=[Depends(auth_user)])
    app.include_router(gateway_clmm.router, dependencies=[Depends(auth_user)])
    app.include_router(gateway_lp.router, dependencies=[Depends(auth_user)])
    app.include_router(bot_orchestration.router, dependencies=[Depends(auth_user)])
    app.include_router(controllers.router, dependencies=[Depends(auth_user)])
    app.include_router(scripts.router, dependencies=[Depends(auth_user)])
    app.include_router(backtesting.router, dependencies=[Depends(auth_user)])
    app.include_router(archived_bots.router, dependencies=[Depends(auth_user)])
    app.include_router(storage.router, dependencies=[Depends(auth_user)])
    app.include_router(executors.router, dependencies=[Depends(auth_user)])
    app.include_router(websocket.router)


if provider_runtime_enabled():
    _include_provider_routers()
else:
    _include_full_routers()


@app.get("/")
async def root():
    """API root endpoint returning basic information."""
    return {
        "name": "Hummingbot API",
        "version": VERSION,
        "status": "running",
    }
