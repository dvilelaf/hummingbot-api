from pathlib import Path


HYPERLIQUID_PERP = Path(
    "/opt/conda/envs/hummingbot-api/lib/python3.12/site-packages/"
    "hummingbot/connector/derivative/hyperliquid_perpetual/"
    "hyperliquid_perpetual_derivative.py"
)


def replace_once(source: str, old: str, new: str) -> str:
    if old not in source:
        if new in source:
            return source
        raise RuntimeError(f"Expected patch target not found: {old!r}")
    return source.replace(old, new, 1)


def main() -> None:
    source = HYPERLIQUID_PERP.read_text()
    source = replace_once(
        source,
        "enable_hip3_markets: bool = True,",
        "enable_hip3_markets: bool = False,",
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


if __name__ == "__main__":
    main()
