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
    HYPERLIQUID_PERP.write_text(source)


if __name__ == "__main__":
    main()
