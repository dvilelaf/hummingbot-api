import asyncio
import sys
import types
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel


def _install_hummingbot_stubs(monkeypatch):
    class CandlesConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class HistoricalCandlesConfig(BaseModel):
        connector_name: str = ""
        trading_pair: str = ""
        interval: str = "1m"
        start_time: int = 0
        end_time: int = 0

    class UnsupportedConnectorException(Exception):
        pass

    class CandlesFactory:
        _candles_map = {}

    modules = {
        "hummingbot": types.ModuleType("hummingbot"),
        "hummingbot.core": types.ModuleType("hummingbot.core"),
        "hummingbot.core.data_type": types.ModuleType("hummingbot.core.data_type"),
        "hummingbot.core.data_type.common": types.ModuleType("hummingbot.core.data_type.common"),
        "hummingbot.core.rate_oracle": types.ModuleType("hummingbot.core.rate_oracle"),
        "hummingbot.core.rate_oracle.rate_oracle": types.ModuleType("hummingbot.core.rate_oracle.rate_oracle"),
        "hummingbot.data_feed": types.ModuleType("hummingbot.data_feed"),
        "hummingbot.data_feed.candles_feed": types.ModuleType("hummingbot.data_feed.candles_feed"),
        "hummingbot.data_feed.candles_feed.candles_factory": types.ModuleType(
            "hummingbot.data_feed.candles_feed.candles_factory"
        ),
        "hummingbot.data_feed.candles_feed.data_types": types.ModuleType(
            "hummingbot.data_feed.candles_feed.data_types"
        ),
    }
    common_module = modules["hummingbot.core.data_type.common"]
    common_module.OrderType = Enum("OrderType", "LIMIT MARKET LIMIT_MAKER")
    common_module.PositionAction = Enum("PositionAction", "OPEN CLOSE")
    common_module.TradeType = Enum("TradeType", "BUY SELL")
    modules["hummingbot.core.rate_oracle.rate_oracle"].RateOracle = object
    factory_module = modules["hummingbot.data_feed.candles_feed.candles_factory"]
    factory_module.CandlesFactory = CandlesFactory
    factory_module.UnsupportedConnectorException = UnsupportedConnectorException
    data_types_module = modules["hummingbot.data_feed.candles_feed.data_types"]
    data_types_module.CandlesConfig = CandlesConfig
    data_types_module.HistoricalCandlesConfig = HistoricalCandlesConfig
    for name, module in modules.items():
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.delitem(sys.modules, "services.market_data_service", raising=False)
    monkeypatch.delitem(sys.modules, "routers.market_data", raising=False)
    import services.market_data_service as service_module
    import routers.market_data as router_module

    return CandlesFactory, service_module, router_module


def _request(service):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(market_data_service=service)))


class _FakeDataFrame:
    def __init__(self, rows):
        self.rows = rows

    @property
    def empty(self):
        return not self.rows

    def drop_duplicates(self, subset, keep):
        assert subset == ["timestamp"]
        assert keep == "last"
        rows = {}
        for row in self.rows:
            rows[row["timestamp"]] = row
        return _FakeDataFrame(list(rows.values()))

    def sort_values(self, column):
        assert column == "timestamp"
        return _FakeDataFrame(sorted(self.rows, key=lambda row: row[column]))

    def tail(self, max_records):
        return _FakeDataFrame(self.rows[-max_records:])

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


def test_candle_history_request_has_no_provider_field(monkeypatch):
    _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    request = CandleHistoryRequest(trading_pair="BTC-USDT", interval="1m", max_records=20)

    assert request.trading_pair == "BTC-USDT"
    assert request.interval == "1m"
    assert request.max_records == 20
    assert not hasattr(request, "connector_name")


def test_resolve_candle_source_uses_sorted_candidates_and_caches_success(monkeypatch):
    factory, _, _ = _install_hummingbot_stubs(monkeypatch)
    from services.market_data_service import MarketDataService

    factory._candles_map = {"zulu": object(), "alpha": object()}
    service = MarketDataService.__new__(MarketDataService)
    service._candle_source_cache = {}
    calls = []

    async def validate(connector_name, trading_pair, interval):
        calls.append((connector_name, trading_pair, interval))
        if connector_name == "alpha":
            await asyncio.sleep(0.01)

    service.validate_trading_pair = validate

    assert asyncio.run(service.resolve_candle_source("BTC-USDT", "1m")) == "alpha"
    assert asyncio.run(service.resolve_candle_source("BTC-USDT", "1m")) == "alpha"
    assert sorted(calls) == [
        ("alpha", "BTC-USDT", "1m"),
        ("zulu", "BTC-USDT", "1m"),
    ]


def test_resolve_candle_source_limits_sorted_probe_batches(monkeypatch):
    factory, _, _ = _install_hummingbot_stubs(monkeypatch)
    from services.market_data_service import MarketDataService

    factory._candles_map = {name: object() for name in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")}
    service = MarketDataService.__new__(MarketDataService)
    service._candle_source_cache = {}
    calls = []
    active = 0
    max_active = 0

    async def validate(connector_name, trading_pair, interval):
        nonlocal active, max_active
        calls.append(connector_name)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        if connector_name != "foxtrot":
            raise ValueError("pair unavailable")

    service.validate_trading_pair = validate

    assert asyncio.run(service.resolve_candle_source("BTC-USDT", "1m")) == "foxtrot"
    assert calls == ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    assert max_active <= 5


def test_resolve_candle_source_negative_cache_expires(monkeypatch):
    factory, service_module, _ = _install_hummingbot_stubs(monkeypatch)
    from services.market_data_service import MarketDataService

    factory._candles_map = {"alpha": object(), "beta": object()}
    service = MarketDataService.__new__(MarketDataService)
    service._candle_source_cache = {}
    service.validate_trading_pair = AsyncMock(side_effect=ValueError("pair unavailable"))
    now = [100.0]
    monkeypatch.setattr(service_module.time, "monotonic", lambda: now[0])

    with pytest.raises(ValueError, match="No candle source supports BTC-USDT at 1m"):
        asyncio.run(service.resolve_candle_source("BTC-USDT", "1m"))
    with pytest.raises(ValueError, match="No candle source supports BTC-USDT at 1m"):
        asyncio.run(service.resolve_candle_source("BTC-USDT", "1m"))
    assert service.validate_trading_pair.await_count == 2

    now[0] += service_module.CANDLE_SOURCE_NEGATIVE_CACHE_TTL + 1
    with pytest.raises(ValueError, match="No candle source supports BTC-USDT at 1m"):
        asyncio.run(service.resolve_candle_source("BTC-USDT", "1m"))

    assert service.validate_trading_pair.await_count == 4


def test_resolve_candle_source_cache_is_separate_per_interval(monkeypatch):
    factory, _, _ = _install_hummingbot_stubs(monkeypatch)
    from services.market_data_service import MarketDataService

    factory._candles_map = {"alpha": object()}
    service = MarketDataService.__new__(MarketDataService)
    service._candle_source_cache = {}
    service.validate_trading_pair = AsyncMock()

    assert asyncio.run(service.resolve_candle_source("BTC-USDT", "1h")) == "alpha"
    assert asyncio.run(service.resolve_candle_source("BTC-USDT", "1d")) == "alpha"
    assert service.validate_trading_pair.await_count == 2
    service.validate_trading_pair.assert_any_await("alpha", "BTC-USDT", "1h")
    service.validate_trading_pair.assert_any_await("alpha", "BTC-USDT", "1d")


def test_candle_history_reuses_candle_normalization_and_skips_probe_after_resolution(monkeypatch):
    _, _, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    candles_df = _FakeDataFrame(
        [
            {"timestamp": 3, "open": 3, "high": 3, "low": 3, "close": 3, "volume": 3},
            {"timestamp": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            {"timestamp": 2, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
            {"timestamp": 2, "open": 20, "high": 20, "low": 20, "close": 20, "volume": 20},
        ]
    )
    service = SimpleNamespace(
        resolve_candle_source=AsyncMock(return_value="alpha"),
        validate_trading_pair=AsyncMock(),
        get_candles_feed=MagicMock(return_value=SimpleNamespace(ready=True, candles_df=candles_df)),
        stop_candle_feed=MagicMock(),
    )

    result = asyncio.run(
        router.get_candle_history(
            _request(service),
            CandleHistoryRequest(trading_pair="BTC-USDT", interval="1m", max_records=3),
        )
    )

    assert [row["timestamp"] for row in result] == [1, 2, 3]
    assert result[1]["close"] == 20
    service.validate_trading_pair.assert_not_awaited()
    config = service.get_candles_feed.call_args.args[0]
    assert config.connector == "alpha"
    assert config.trading_pair == "BTC-USDT"


def test_candle_history_redacts_provider_errors(monkeypatch, caplog):
    _, _, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    provider_error = "provider secret endpoint details"
    service = SimpleNamespace(
        resolve_candle_source=AsyncMock(side_effect=RuntimeError(provider_error)),
    )

    with caplog.at_level("ERROR"):
        with pytest.raises(router.HTTPException) as raised:
            asyncio.run(
                router.get_candle_history(
                    _request(service),
                    CandleHistoryRequest(trading_pair="BTC-USDT", interval="1m", max_records=20),
                )
            )

    assert raised.value.status_code == 500
    assert raised.value.detail == "Unable to fetch candle history."
    assert provider_error not in caplog.text
