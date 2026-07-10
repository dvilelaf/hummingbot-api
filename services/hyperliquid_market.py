from typing import Any


HYPERLIQUID_PERPETUAL_CONNECTOR = "hyperliquid_perpetual"
HYPERLIQUID_PERPETUAL_CONNECTOR_PAIR = "HYPE-USD"
HYPERLIQUID_PERPETUAL_LOGICAL_PAIR = "HYPE-USDC"


def connector_trading_pair(connector_name: str, trading_pair: str) -> str:
    if (
        connector_name == HYPERLIQUID_PERPETUAL_CONNECTOR
        and trading_pair == HYPERLIQUID_PERPETUAL_LOGICAL_PAIR
    ):
        return HYPERLIQUID_PERPETUAL_CONNECTOR_PAIR
    return trading_pair


def logical_trading_pair(connector_name: str, trading_pair: str) -> str:
    if (
        connector_name == HYPERLIQUID_PERPETUAL_CONNECTOR
        and trading_pair == HYPERLIQUID_PERPETUAL_CONNECTOR_PAIR
    ):
        return HYPERLIQUID_PERPETUAL_LOGICAL_PAIR
    return trading_pair


def logical_collateral(connector_name: str, asset: Any) -> Any:
    if connector_name == HYPERLIQUID_PERPETUAL_CONNECTOR and asset == "USD":
        return "USDC"
    return asset


def logical_balance_rows(connector_name: str, rows: Any) -> Any:
    if connector_name != HYPERLIQUID_PERPETUAL_CONNECTOR or not isinstance(rows, list):
        return rows
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            normalized.append(row)
            continue
        item = dict(row)
        for key in ("asset", "symbol", "token"):
            if key in item:
                item[key] = logical_collateral(connector_name, item[key])
        normalized.append(item)
    return normalized


def logical_trading_rule(connector_name: str, rule: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(rule)
    for key in ("buy_order_collateral_token", "sell_order_collateral_token"):
        if key in normalized:
            normalized[key] = logical_collateral(connector_name, normalized[key])
    return normalized


def logical_observation(record: dict[str, Any]) -> dict[str, Any]:
    connector_name = str(record.get("connector_name") or "")
    normalized = dict(record)
    if "trading_pair" in normalized:
        normalized["trading_pair"] = logical_trading_pair(
            connector_name,
            normalized["trading_pair"],
        )
    if "fee_currency" in normalized:
        normalized["fee_currency"] = logical_collateral(
            connector_name,
            normalized["fee_currency"],
        )
    return normalized
