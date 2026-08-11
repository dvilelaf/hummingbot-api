import importlib.util
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = ROOT / "docker" / "hyperliquid-runtime-patch.py"
SPEC = importlib.util.spec_from_file_location("hyperliquid_runtime_patch", PATCH_PATH)
assert SPEC is not None and SPEC.loader is not None
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)

# Representative source snippets modeled on the upstream Hummingbot
# hyperliquid_perpetual_derivative.py as of commit 9d048b34.

_BASE_SOURCE = """\
class HyperliquidPerpetualDerivative(PerpetualDerivativePyBase):
    web_utils = web_utils

    def __init__(
            self,
            ...
            enable_hip3_markets: bool = True,
    ):
        ...
        self._enable_hip3_markets = enable_hip3_markets
        ...

    def _place_order(self, ...):
        ...
        builder_field = self._build_builder_field()
        if builder_field is not None:
            api_params["builder"] = builder_field

    # === Builder code support (HGP-87) ===

    @property
    def _is_testnet(self) -> bool:
        return self._domain == CONSTANTS.TESTNET_DOMAIN

    def _should_inject_builder(self) -> bool:
        \"\"\"Builder attribution applies only on mainnet, non-vault orders — the venue rejects the
        builder field on vault and testnet orders.\"\"\"
        if not CONSTANTS.BUILDER_SUPPORTED:
            return False
        if self._use_vault or self._is_testnet:
            return False
        return True

    def _build_builder_field(self) -> Optional[Dict[str, Any]]:
        \"\"\"The ``{\"b\": <address>, \"f\": <tenths_of_bps>}`` order field, or None when omitted. Address
        is lowercased (the venue rejects mixed-case).\"\"\"
        if not self._should_inject_builder():
            return None
        return {"b": self._builder_address.lower(), "f": self._builder_fee_tenths_bps}

    async def _initialize_builder_fee(self) -> None:
        if not self._should_inject_builder():
            return
        ...

    def _exchange_info_processing(self):
        deployer, base = full_symbol.split(':')
        ...

    def _exchange_symbol_from_token_id(self):
        dex_name, coin = exchange_symbol.split(":")
        ...
"""

_EXCHANGE_BASE_SOURCE = """\
from hummingbot.core.data_type.common import OrderType, TradeType

class ExchangePyBase:
    async def _create_order(self, trading_rule, notional_size, **kwargs):
        if False:
            return
        elif notional_size < trading_rule.min_notional_size:
            return "blocked"
        return "submitted", kwargs.get("position_action")
"""


def _run_patch(
    connector_source: str,
    exchange_source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    connector_path = tmp_path / "hyperliquid_perpetual_derivative.py"
    exchange_path = tmp_path / "exchange_py_base.py"
    connector_path.write_text(connector_source)
    exchange_path.write_text(exchange_source)
    monkeypatch.setattr(PATCH, "HYPERLIQUID_PERP", connector_path)
    monkeypatch.setattr(PATCH, "EXCHANGE_PY_BASE", exchange_path)
    PATCH.main()
    return connector_path.read_text(), exchange_path.read_text()


def _run_main_on(source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    connector_source, _ = _run_patch(
        source,
        _EXCHANGE_BASE_SOURCE,
        tmp_path,
        monkeypatch,
    )
    return connector_source


def test_builder_attribution_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched = _run_main_on(_BASE_SOURCE, tmp_path, monkeypatch)

    # The method must unconditionally return False.
    assert "    def _should_inject_builder(self) -> bool:\n        return False\n" in patched

    # The old body must be gone.
    assert "if not CONSTANTS.BUILDER_SUPPORTED" not in patched
    assert "if self._use_vault or self._is_testnet" not in patched


def test_build_builder_field_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched = _run_main_on(_BASE_SOURCE, tmp_path, monkeypatch)

    # _build_builder_field() must now unconditionally return None.
    assert "    def _build_builder_field(self) -> Optional[Dict[str, Any]]:\n        return None\n" in patched
    # The old dict-returning body must be gone.
    assert 'self._builder_address.lower()' not in patched
    assert 'self._builder_fee_tenths_bps' not in patched


def test_initialize_builder_fee_short_circuits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched = _run_main_on(_BASE_SOURCE, tmp_path, monkeypatch)

    # _initialize_builder_fee also checks _should_inject_builder(), so it
    # will return early without querying the network.
    assert 'if not self._should_inject_builder():' in patched


def test_hip3_still_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patched = _run_main_on(_BASE_SOURCE, tmp_path, monkeypatch)

    assert "enable_hip3_markets: bool = False," in patched
    assert "enable_hip3_markets: bool = True," not in patched


def test_symbol_split_still_fixed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patched = _run_main_on(_BASE_SOURCE, tmp_path, monkeypatch)

    assert "full_symbol.split(':', 1)" in patched
    assert "full_symbol.split(':')" not in patched
    assert 'exchange_symbol.split(":", 1)' in patched
    assert 'exchange_symbol.split(":")' not in patched


def test_close_reductions_defer_minimum_notional_to_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector, patched = _run_patch(
        _BASE_SOURCE,
        _EXCHANGE_BASE_SOURCE,
        tmp_path,
        monkeypatch,
    )

    assert "_marlin_defer_close_notional_to_provider = True" in connector
    assert "OrderType, PositionAction, TradeType" in patched
    assert 'kwargs.get("position_action") == PositionAction.CLOSE' in patched

    executable = patched.replace(
        "from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType",
        "from enum import Enum\n"
        "class PositionAction(Enum):\n"
        "    OPEN = 'OPEN'\n"
        "    CLOSE = 'CLOSE'\n"
        "class OrderType: pass\n"
        "class TradeType: pass",
    )
    namespace: dict[str, object] = {}
    exec(executable, namespace)
    base_type = namespace["ExchangePyBase"]
    position_action = namespace["PositionAction"]
    rule = SimpleNamespace(min_notional_size=10)

    class Hyperliquid(base_type):
        _marlin_defer_close_notional_to_provider = True

    assert asyncio.run(
        Hyperliquid()._create_order(rule, 9, position_action=position_action.CLOSE)
    ) == ("submitted", position_action.CLOSE)
    assert asyncio.run(
        Hyperliquid()._create_order(rule, 9, position_action=position_action.OPEN)
    ) == "blocked"
    assert asyncio.run(
        base_type()._create_order(rule, 9, position_action=position_action.CLOSE)
    ) == "blocked"
    assert asyncio.run(base_type()._create_order(rule, 10)) == ("submitted", None)


def test_patch_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    once = _run_patch(
        _BASE_SOURCE,
        _EXCHANGE_BASE_SOURCE,
        tmp_path,
        monkeypatch,
    )
    twice = _run_patch(once[0], once[1], tmp_path, monkeypatch)

    assert once == twice


def test_drift_detection_raises_on_unexpected_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifted = _BASE_SOURCE.replace(
        "    def _build_builder_field(self) -> Optional[Dict[str, Any]]:\n"
        '        """The ``{"b": <address>, "f": <tenths_of_bps>}`` order field, or None when omitted. Address\n'
        "        is lowercased (the venue rejects mixed-case).\"\"\"\n"
        "        if not self._should_inject_builder():\n"
        "            return None\n"
        '        return {"b": self._builder_address.lower(), "f": self._builder_fee_tenths_bps}',
        "    def _build_builder_field(self) -> Optional[Dict[str, Any]]:\n"
        "        return {'b': self._builder_address.lower(), 'f': 0}\n",
    )
    with pytest.raises(RuntimeError, match="Expected patch target not found"):
        _run_main_on(drifted, tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "exchange_source",
    [
        _EXCHANGE_BASE_SOURCE.replace(
            "from hummingbot.core.data_type.common import OrderType, TradeType",
            "from hummingbot.core.data_type.common import OrderType, TradeType, X",
        ),
        _EXCHANGE_BASE_SOURCE.replace(
            "        elif notional_size < trading_rule.min_notional_size:",
            "        elif notional_size <= trading_rule.min_notional_size:",
        ),
        _EXCHANGE_BASE_SOURCE
        + "\n        elif notional_size < trading_rule.min_notional_size:\n            pass\n",
        _EXCHANGE_BASE_SOURCE.replace(
            "\n\nclass ExchangePyBase:",
            "\nfrom hummingbot.core.data_type.common import "
            "OrderType, PositionAction, TradeType\n\nclass ExchangePyBase:",
        ),
    ],
)
def test_exchange_base_drift_is_rejected(
    exchange_source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="Expected (patch target not found|exactly one)"):
        _run_patch(_BASE_SOURCE, exchange_source, tmp_path, monkeypatch)
