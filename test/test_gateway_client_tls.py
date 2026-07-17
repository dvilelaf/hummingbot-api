import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import ANY, AsyncMock, Mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "gateway_client.py"
spec = importlib.util.spec_from_file_location("gateway_client_under_test", MODULE_PATH)
gateway_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway_client)
GatewayClient = gateway_client.GatewayClient


def test_gateway_client_http_session_keeps_default_aiohttp_behavior(monkeypatch):
    session = Mock(closed=False)
    client_session = Mock(return_value=session)
    tcp_connector = Mock()
    create_default_context = Mock()
    monkeypatch.setattr(gateway_client.aiohttp, "ClientSession", client_session)
    monkeypatch.setattr(gateway_client.aiohttp, "TCPConnector", tcp_connector)
    monkeypatch.setattr(gateway_client.ssl, "create_default_context", create_default_context)
    monkeypatch.setenv("GATEWAY_CA_CERT_FILE", "/certs/ca.pem")
    monkeypatch.setenv("GATEWAY_CLIENT_CERT_FILE", "/certs/client.pem")
    monkeypatch.setenv("GATEWAY_CLIENT_KEY_FILE", "/certs/client.key")

    result = asyncio.run(GatewayClient(base_url="http://gateway.local:15888")._get_session())

    assert result is session
    client_session.assert_called_once_with(timeout=ANY)
    assert client_session.call_args.kwargs["timeout"].total == 180.0
    tcp_connector.assert_not_called()
    create_default_context.assert_not_called()


def test_gateway_client_https_session_uses_client_certificate_ssl_context(monkeypatch):
    session = Mock(closed=False)
    ssl_context = Mock()
    connector = Mock()
    client_session = Mock(return_value=session)
    tcp_connector = Mock(return_value=connector)
    create_default_context = Mock(return_value=ssl_context)
    monkeypatch.setattr(gateway_client.aiohttp, "ClientSession", client_session)
    monkeypatch.setattr(gateway_client.aiohttp, "TCPConnector", tcp_connector)
    monkeypatch.setattr(gateway_client.ssl, "create_default_context", create_default_context)
    monkeypatch.setenv("GATEWAY_CA_CERT_FILE", "/certs/ca.pem")
    monkeypatch.setenv("GATEWAY_CLIENT_CERT_FILE", "/certs/client.pem")
    monkeypatch.setenv("GATEWAY_CLIENT_KEY_FILE", "/certs/client.key")

    result = asyncio.run(GatewayClient(base_url="https://gateway.local:15888")._get_session())

    assert result is session
    create_default_context.assert_called_once_with(cafile="/certs/ca.pem")
    ssl_context.load_cert_chain.assert_called_once_with(certfile="/certs/client.pem", keyfile="/certs/client.key")
    tcp_connector.assert_called_once_with(ssl=ssl_context)
    client_session.assert_called_once_with(connector=connector, timeout=ANY)
    assert client_session.call_args.kwargs["timeout"].total == 180.0


def test_gateway_client_https_session_can_skip_hostname_verification(monkeypatch):
    session = Mock(closed=False)
    ssl_context = Mock(check_hostname=True)
    connector = Mock()
    client_session = Mock(return_value=session)
    tcp_connector = Mock(return_value=connector)
    create_default_context = Mock(return_value=ssl_context)
    monkeypatch.setattr(gateway_client.aiohttp, "ClientSession", client_session)
    monkeypatch.setattr(gateway_client.aiohttp, "TCPConnector", tcp_connector)
    monkeypatch.setattr(gateway_client.ssl, "create_default_context", create_default_context)
    monkeypatch.setenv("GATEWAY_CA_CERT_FILE", "/certs/ca.pem")
    monkeypatch.setenv("GATEWAY_CLIENT_CERT_FILE", "/certs/client.pem")
    monkeypatch.setenv("GATEWAY_CLIENT_KEY_FILE", "/certs/client.key")
    monkeypatch.setenv("GATEWAY_TLS_SKIP_HOSTNAME_VERIFY", "true")

    result = asyncio.run(GatewayClient(base_url="https://gateway.local:15888")._get_session())

    assert result is session
    assert ssl_context.check_hostname is False
    tcp_connector.assert_called_once_with(ssl=ssl_context)
    client_session.assert_called_once_with(connector=connector, timeout=ANY)


def test_gateway_client_https_session_with_partial_cert_env_keeps_default_aiohttp_behavior(monkeypatch):
    session = Mock(closed=False)
    client_session = Mock(return_value=session)
    tcp_connector = Mock()
    create_default_context = Mock()
    monkeypatch.setattr(gateway_client.aiohttp, "ClientSession", client_session)
    monkeypatch.setattr(gateway_client.aiohttp, "TCPConnector", tcp_connector)
    monkeypatch.setattr(gateway_client.ssl, "create_default_context", create_default_context)
    monkeypatch.setenv("GATEWAY_CA_CERT_FILE", "/certs/ca.pem")
    monkeypatch.delenv("GATEWAY_CLIENT_CERT_FILE", raising=False)
    monkeypatch.setenv("GATEWAY_CLIENT_KEY_FILE", "/certs/client.key")

    result = asyncio.run(GatewayClient(base_url="https://gateway.local:15888")._get_session())

    assert result is session
    client_session.assert_called_once_with(timeout=ANY)
    tcp_connector.assert_not_called()
    create_default_context.assert_not_called()


def test_gateway_client_request_timeout_can_be_overridden(monkeypatch):
    monkeypatch.setenv("GATEWAY_REQUEST_TIMEOUT_SECONDS", "3.5")

    timeout = GatewayClient._request_timeout()

    assert timeout.total == 3.5


def test_gateway_client_request_timeout_can_be_disabled(monkeypatch):
    monkeypatch.setenv("GATEWAY_REQUEST_TIMEOUT_SECONDS", "0")

    timeout = GatewayClient._request_timeout()

    assert timeout.total is None


def test_parse_success_response_falls_back_to_canonical_date_header():
    body = {"price": "1"}
    response = Mock(
        headers={"Date": "Fri, 17 Jul 2026 08:30:00 GMT"},
        json=AsyncMock(return_value=body),
    )

    result = asyncio.run(GatewayClient("http://gateway.local")._parse_success_response(response, True))

    assert result == {"price": "1", "observedAt": "2026-07-17T08:30:00+00:00"}


def test_parse_success_response_falls_back_when_observed_fields_are_null():
    body = {"observedAt": None, "observed_at": None, "price": "1"}
    response = Mock(
        headers={"Date": "Fri, 17 Jul 2026 08:30:00 GMT"},
        json=AsyncMock(return_value=body),
    )

    result = asyncio.run(GatewayClient("http://gateway.local")._parse_success_response(response, True))

    assert result == {"observedAt": "2026-07-17T08:30:00+00:00", "observed_at": None, "price": "1"}


def test_parse_success_response_preserves_non_null_observed_value_and_body():
    body = {"observedAt": "provider-value", "nested": {"amount": "1"}}
    response = Mock(
        headers={"Date": "Fri, 17 Jul 2026 08:30:00 GMT"},
        json=AsyncMock(return_value=body),
    )

    result = asyncio.run(GatewayClient("http://gateway.local")._parse_success_response(response, True))

    assert result == body


def test_parse_success_response_ignores_non_canonical_date_header():
    body = {"price": "1"}
    expected = body.copy()
    response = Mock(
        headers={"Date": "Friday, 17 Jul 2026 08:30:00 GMT"},
        json=AsyncMock(return_value=body),
    )

    result = asyncio.run(GatewayClient("http://gateway.local")._parse_success_response(response, True))

    assert result == expected
