import importlib.util
from pathlib import Path

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


def _run_main_on(source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    connector_path = tmp_path / "hyperliquid_perpetual_derivative.py"
    connector_path.write_text(source)
    monkeypatch.setattr(PATCH, "HYPERLIQUID_PERP", connector_path)
    PATCH.main()
    return connector_path.read_text()


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


def test_patch_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    once = _run_main_on(_BASE_SOURCE, tmp_path, monkeypatch)
    twice = _run_main_on(once, tmp_path, monkeypatch)

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
