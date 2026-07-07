import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _main_source() -> str:
    return (ROOT / "main.py").read_text()


def _provider_router_section() -> str:
    source = _main_source()
    return source[source.index("def _include_provider_routers()") : source.index("def _include_full_routers()")]


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
        "gateway_bridge",
        "gateway_swap",
        "market_data",
        "rate_oracle",
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
        "accounts",
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


def test_provider_runtime_registered_routes_include_bridge_without_admin_surfaces():
    source = _main_source()
    provider = source[source.index("def _include_provider_routers()") : source.index("def _include_full_routers()")]
    assert "accounts.router" not in provider
    assert "gateway.router" not in provider
    assert "gateway_bridge.router" in provider
    assert "gateway_lp.router" not in provider
    assert "docker.router" not in provider


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
