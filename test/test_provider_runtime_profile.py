import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _main_source() -> str:
    return (ROOT / "main.py").read_text()


def _provider_router_section() -> str:
    source = _main_source()
    return source[source.index("def _include_provider_routers(") : source.index("def _include_full_routers()")]


def _openapi_paths(profile: str) -> dict[str, list[str]]:
    if importlib.util.find_spec("hummingbot") is None:
        pytest.skip("Hummingbot runtime dependencies are not installed")

    command = (
        "import json, main; "
        "print('OPENAPI_PATHS=' + json.dumps({path: sorted(methods) "
        "for path, methods in main.app.openapi()['paths'].items()}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=ROOT,
        env={**os.environ, "HUMMINGBOT_API_RUNTIME_PROFILE": profile},
        check=True,
        capture_output=True,
        text=True,
    )
    payload = next(
        line.removeprefix("OPENAPI_PATHS=")
        for line in result.stdout.splitlines()
        if line.startswith("OPENAPI_PATHS=")
    )
    return json.loads(payload)


def test_provider_runtime_profile_is_configured_for_compose():
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "HUMMINGBOT_API_RUNTIME_PROFILE=provider" in compose
    assert "MARLIN_RUNTIME_PROFILE=marlin" in compose
    assert "/var/run/docker.sock" not in compose
    assert "profiles:\n      - full" in compose


def test_provider_runtime_router_surface_is_provider_boundary_only():
    provider_section = _provider_router_section()

    kept = (
        "connectors",
        "portfolio",
        "trading",
        "provider_boundary",
        "provider_treasury",
        "gateway_swap",
        "market_data",
        "rate_oracle",
        "accounts.credential_router",
    )
    removed = (
        "docker",
        "gateway_clmm",
        "gateway_lp",
        "bot_orchestration",
        "controllers",
        "scripts",
        "backtesting",
        "archived_bots",
        "storage",
        "executors",
        "websocket",
    )

    for router_name in kept:
        assert router_name in provider_section
    for router_name in removed:
        assert router_name not in provider_section


def test_provider_runtime_does_not_eager_import_orchestration_services():
    source = _main_source()
    top_level = source[: source.index("def env_text")]

    assert "services.accounts_service import AccountsService" in top_level
    assert "services.docker_service" not in top_level
    assert "services.bots_orchestrator" not in top_level
    assert "services.executor_service" not in top_level


def test_provider_runtime_initializes_connectors_on_explicit_request_only():
    source = _main_source()
    startup = source[source.index('startup_connectors = env_csv_set("HUMMINGBOT_STARTUP_CONNECTORS")') :]
    startup = startup[: startup.index("# AccountsService")]

    assert 'startup_connectors = env_csv_set("HUMMINGBOT_STARTUP_CONNECTORS")' in startup
    assert "if startup_connectors is None and provider_runtime_enabled():" in startup
    assert "startup_connectors = set()" in startup


def test_provider_runtime_excludes_raw_gateway_mutations():
    provider = _provider_router_section()

    assert "include_gateway_mutations: bool = False" in provider
    assert 'excluded_paths={"/gateway/swap/execute"}' in provider


def test_provider_runtime_openapi_exposes_only_provider_mutation_boundaries():
    paths = _openapi_paths("provider")

    assert "/gateway/bridge/execute" not in paths
    assert "/gateway/swap/execute" not in paths
    assert "/provider/intents" in paths
    assert "/provider/treasury/rebalances/{rebalance_id}/execute" in paths
    assert "/gateway/swap/quote" in paths
    assert "/gateway/swaps/{transaction_hash}/status" in paths


def test_full_runtime_keeps_raw_gateway_mutations():
    source = _main_source()
    full = source[source.index("def _include_full_routers()") :]

    assert "_include_provider_routers(include_gateway_mutations=True)" in full
    assert "app.include_router(gateway_bridge.router" in full


def test_full_runtime_openapi_keeps_raw_gateway_mutations():
    paths = _openapi_paths("full")

    assert "/gateway/bridge/execute" in paths
    assert "/gateway/swap/execute" in paths


def test_marlin_cowswap_startup_reconciles_gateway_wallet_before_runtime_build():
    source = _main_source()
    section = source[source.index("if marlin_runtime_enabled():") : source.index("else:", source.index("if marlin_runtime_enabled():"))]

    assert "_marlin_gateway_default_wallet_address(" in section
    assert "set_marlin_default_wallet(" in section
    assert 'wallet_ref="base:mainnet:evm_gateway"' in section


def test_services_package_does_not_eager_import_orchestration_modules():
    for module_name in (
        "services",
        "services.docker_service",
        "services.bots_orchestrator",
        "services.executor_service",
    ):
        sys.modules.pop(module_name, None)

    importlib.import_module("services")

    assert "services.docker_service" not in sys.modules
    assert "services.bots_orchestrator" not in sys.modules
    assert "services.executor_service" not in sys.modules
