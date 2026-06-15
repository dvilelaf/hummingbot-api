import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "gateway_poll.py"
spec = importlib.util.spec_from_file_location("gateway_poll_under_test", MODULE_PATH)
gateway_poll = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway_poll)


def test_gateway_poll_error_none_is_not_failure():
    assert gateway_poll.gateway_poll_error_detail({"txStatus": 1, "error": None}) is None


def test_gateway_poll_truthy_error_fails_closed():
    assert (
        gateway_poll.gateway_poll_error_detail({"txStatus": -1, "error": "simulation failed"})
        == "simulation failed"
    )
