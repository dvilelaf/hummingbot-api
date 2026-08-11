from pathlib import Path


HYPERLIQUID_PERP = Path(
    "/opt/conda/envs/hummingbot-api/lib/python3.12/site-packages/"
    "hummingbot/connector/derivative/hyperliquid_perpetual/"
    "hyperliquid_perpetual_derivative.py"
)
EXCHANGE_PY_BASE = Path(
    "/opt/conda/envs/hummingbot-api/lib/python3.12/site-packages/"
    "hummingbot/connector/exchange_py_base.py"
)


def replace_once(source: str, old: str, new: str) -> str:
    old_count = source.count(old)
    new_count = source.count(new)
    if old_count == 1 and new_count == 0:
        return source.replace(old, new, 1)
    if old_count == 0 and new_count == 1:
        return source
    if old_count == 0 and new_count == 0:
        raise RuntimeError(f"Expected patch target not found: {old!r}")
    raise RuntimeError(
        f"Expected exactly one original or patched target, found old={old_count}, new={new_count}: {old!r}"
    )


def main() -> None:
    source = HYPERLIQUID_PERP.read_text()
    source = replace_once(
        source,
        "enable_hip3_markets: bool = True,",
        "enable_hip3_markets: bool = False,",
    )
    source = replace_once(
        source,
        "class HyperliquidPerpetualDerivative(PerpetualDerivativePyBase):\n"
        "    web_utils = web_utils",
        "class HyperliquidPerpetualDerivative(PerpetualDerivativePyBase):\n"
        "    _marlin_defer_close_notional_to_provider = True\n"
        "    web_utils = web_utils",
    )
    source = replace_once(
        source,
        "deployer, base = full_symbol.split(':')",
        "deployer, base = full_symbol.split(':', 1)",
    )
    source = replace_once(
        source,
        'dex_name, coin = exchange_symbol.split(":")',
        'dex_name, coin = exchange_symbol.split(":", 1)',
    )
    source = replace_once(
        source,
        "    def _should_inject_builder(self) -> bool:\n"
        '        """Builder attribution applies only on mainnet, non-vault orders — the venue rejects the\n'
        "        builder field on vault and testnet orders.\"\"\"\n"
        "        if not CONSTANTS.BUILDER_SUPPORTED:\n"
        "            return False\n"
        "        if self._use_vault or self._is_testnet:\n"
        "            return False\n"
        "        return True",
        "    def _should_inject_builder(self) -> bool:\n"
        "        return False\n",
    )
    source = replace_once(
        source,
        "    def _build_builder_field(self) -> Optional[Dict[str, Any]]:\n"
        '        """The ``{"b": <address>, "f": <tenths_of_bps>}`` order field, or None when omitted. Address\n'
        "        is lowercased (the venue rejects mixed-case).\"\"\"\n"
        "        if not self._should_inject_builder():\n"
        "            return None\n"
        '        return {"b": self._builder_address.lower(), "f": self._builder_fee_tenths_bps}',
        "    def _build_builder_field(self) -> Optional[Dict[str, Any]]:\n"
        "        return None\n",
    )
    HYPERLIQUID_PERP.write_text(source)

    exchange_source = EXCHANGE_PY_BASE.read_text()
    exchange_source = replace_once(
        exchange_source,
        "from hummingbot.core.data_type.common import OrderType, TradeType\n",
        "from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType\n",
    )
    exchange_source = replace_once(
        exchange_source,
        "        elif notional_size < trading_rule.min_notional_size:",
        "        elif (\n"
        "            notional_size < trading_rule.min_notional_size\n"
        "            and not (\n"
        "                getattr(self, \"_marlin_defer_close_notional_to_provider\", False)\n"
        "                and kwargs.get(\"position_action\") == PositionAction.CLOSE\n"
        "            )\n"
        "        ):",
    )
    EXCHANGE_PY_BASE.write_text(exchange_source)


if __name__ == "__main__":
    main()
