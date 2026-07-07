from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from hummingbot.client.settings import AllConnectorSettings

from deps import get_accounts_service
from services.accounts_service import AccountsService
from services.cowswap_runtime import (
    COWSWAP_CONNECTOR_NAME,
    cowswap_connector_config_map,
    cowswap_connector_metadata,
    cowswap_order_submission_blocker,
    cowswap_supported_order_types,
)
from services.market_data_service import MarketDataService

router = APIRouter(tags=["Connectors"], prefix="/connectors")


def _gateway_connector_items(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict):
        raw = payload.get("connectors", payload.get("configs", ()))
        values = raw if isinstance(raw, list) else ()
    else:
        values = ()
    return [item for item in values if isinstance(item, dict)]


def _gateway_connector_name(payload: dict[str, Any]) -> str | None:
    value = payload.get("name", payload.get("connector", payload.get("connectorName")))
    return str(value) if value else None


def _gateway_connector_supports_swap(payload: dict[str, Any]) -> bool:
    raw_types = (
        payload.get("tradingTypes")
        or payload.get("trading_types")
        or payload.get("tradingType")
        or payload.get("trading_type")
        or payload.get("supportedActions")
        or payload.get("supported_actions")
        or ()
    )
    if isinstance(raw_types, str):
        values = (raw_types,)
    elif isinstance(raw_types, list):
        values = tuple(str(item) for item in raw_types)
    else:
        values = ()
    return bool({"amm", "clmm", "router", "swap"} & {value.lower() for value in values})


async def _gateway_connector_configs(accounts_service: AccountsService) -> tuple[dict[str, Any], ...]:
    if not await accounts_service.gateway_client.ping():
        return ()
    payload = await accounts_service.gateway_client._request("GET", "config/connectors")
    return tuple(_gateway_connector_items(payload))


async def _gateway_swap_connector(
    accounts_service: AccountsService,
    connector_name: str,
) -> bool | None:
    for payload in await _gateway_connector_configs(accounts_service):
        if _gateway_connector_name(payload) != connector_name:
            continue
        return _gateway_connector_supports_swap(payload)
    return None


@router.get("/", response_model=List[str])
async def available_connectors(accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Get a list of all available connectors.

    Returns:
        List of connector names supported by the system (excludes DEX providers which use Gateway networks)
    """
    all_connectors = AllConnectorSettings.get_connector_settings().keys()
    # Filter out DEX providers (contain '/') - these are accessed via Gateway networks
    connectors = [c for c in all_connectors if '/' not in c]
    if cowswap_connector_metadata() is not None and COWSWAP_CONNECTOR_NAME not in connectors:
        connectors.append(COWSWAP_CONNECTOR_NAME)
    try:
        for payload in await _gateway_connector_configs(accounts_service):
            name = _gateway_connector_name(payload)
            if name and name not in connectors:
                connectors.append(name)
    except Exception:
        pass
    return connectors


@router.get("/{connector_name}/config-map", response_model=Dict[str, dict])
async def get_connector_config_map(connector_name: str, accounts_service: AccountsService = Depends(get_accounts_service)):
    """
    Get configuration fields required for a specific connector with type information.

    Args:
        connector_name: Name of the connector to get config map for

    Returns:
        Dictionary mapping field names to their type information.
        Each field contains:
        - type: The expected data type (e.g., "str", "SecretStr", "int")
        - required: Whether the field is required
    """
    if connector_name == COWSWAP_CONNECTOR_NAME:
        config_map = cowswap_connector_config_map()
        if config_map is None:
            raise HTTPException(status_code=404, detail=f"Connector '{connector_name}' not found")
        return config_map

    return accounts_service.get_connector_config_map(connector_name)


@router.get("/{connector_name}/trading-rules")
async def get_trading_rules(
    request: Request, 
    connector_name: str,
    trading_pairs: Optional[List[str]] = Query(default=None, description="Filter by specific trading pairs")
):
    """
    Get trading rules for a connector, optionally filtered by trading pairs.
    
    This endpoint uses the MarketDataService to access non-trading connector instances,
    which means no authentication or account setup is required.

    Args:
        request: FastAPI request object
        connector_name: Name of the connector (e.g., 'binance', 'binance_perpetual')
        trading_pairs: Optional list of trading pairs to filter by (e.g., ['BTC-USDT', 'ETH-USDT'])

    Returns:
        Dictionary mapping trading pairs to their trading rules

    Raises:
        HTTPException: 404 if connector not found, 500 for other errors
    """
    try:
        if connector_name == COWSWAP_CONNECTOR_NAME:
            accounts_service: AccountsService = request.app.state.accounts_service
            dependencies = getattr(accounts_service, "_cowswap_runtime_dependencies", None)
            blocker = cowswap_order_submission_blocker(
                connector_name,
                runtime_dependencies=dependencies,
            )
            if blocker:
                raise HTTPException(status_code=503, detail=blocker)
            runtime = getattr(accounts_service, "_cowswap_runtime", None)
            if runtime is None:
                raise HTTPException(status_code=503, detail="CowSwap runtime bridge is not initialized")
            rules = getattr(runtime, "trading_rules", {})
            pairs = trading_pairs or list(rules.keys())
            return {
                pair: _cowswap_trading_rule_payload(rules[pair])
                if pair in rules
                else {"error": f"Trading pair {pair} not found"}
                for pair in pairs
            }

        accounts_service: AccountsService = request.app.state.accounts_service
        if await _gateway_swap_connector(accounts_service, connector_name) is True:
            pairs = trading_pairs or []
            return {pair: {"not_applicable": True} for pair in pairs}

        market_data_service: MarketDataService = request.app.state.market_data_service

        # Get trading rules (filtered by trading pairs if provided)
        rules = await market_data_service.get_trading_rules(connector_name, trading_pairs)
        
        if "error" in rules:
            raise HTTPException(status_code=404, detail=f"Connector '{connector_name}' not found or error: {rules['error']}")
        
        return rules
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving trading rules: {str(e)}")


def _cowswap_trading_rule_payload(rule) -> dict:
    return {
        "min_order_size": float(getattr(rule, "min_order_size", 0)),
        "max_order_size": None,
        "min_price_increment": float(getattr(rule, "min_price_increment", 0)),
        "min_base_amount_increment": float(getattr(rule, "min_base_amount_increment", 0)),
        "min_quote_amount_increment": float(getattr(rule, "min_quote_amount_increment", 0)),
        "min_notional_size": 0.0,
        "min_order_value": 0.0,
        "max_price_significant_digits": None,
        "supports_limit_orders": False,
        "supports_market_orders": True,
        "buy_order_collateral_token": None,
        "sell_order_collateral_token": None,
    }


@router.get("/{connector_name}/order-types")
async def get_supported_order_types(request: Request, connector_name: str):
    """
    Get order types supported by a specific connector.

    This endpoint uses the MarketDataService to access non-trading connector instances,
    which means no authentication or account setup is required.

    Args:
        request: FastAPI request object
        connector_name: Name of the connector (e.g., 'binance', 'binance_perpetual')

    Returns:
        List of supported order types (LIMIT, MARKET, LIMIT_MAKER)

    Raises:
        HTTPException: 404 if connector not found, 500 for other errors
    """
    try:
        if connector_name == COWSWAP_CONNECTOR_NAME:
            accounts_service: AccountsService = request.app.state.accounts_service
            dependencies = getattr(accounts_service, "_cowswap_runtime_dependencies", None)
            blocker = cowswap_order_submission_blocker(
                connector_name,
                runtime_dependencies=dependencies,
            )
            if blocker:
                raise HTTPException(status_code=503, detail=blocker)
            order_types = cowswap_supported_order_types()
            if order_types is None:
                raise HTTPException(status_code=404, detail=f"Connector '{connector_name}' not found")
            return {"connector": connector_name, "supported_order_types": order_types}

        accounts_service: AccountsService = request.app.state.accounts_service
        if await _gateway_swap_connector(accounts_service, connector_name) is True:
            return {
                "connector": connector_name,
                "supported_actions": ["swap"],
                "supported_order_types": [],
            }

        market_data_service: MarketDataService = request.app.state.market_data_service

        # Access connector through UnifiedConnectorService
        # This creates a data connector if it doesn't exist
        try:
            connector_instance = market_data_service.connector_service.get_data_connector(connector_name)
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=404, detail=f"Connector '{connector_name}' not found: {str(e)}")

        # Get supported order types
        if hasattr(connector_instance, 'supported_order_types'):
            order_types = [order_type.name for order_type in connector_instance.supported_order_types()]
            return {"connector": connector_name, "supported_order_types": order_types}
        else:
            raise HTTPException(status_code=404, detail=f"Connector '{connector_name}' does not support order types query")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving order types: {str(e)}")
