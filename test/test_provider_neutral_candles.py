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


def _history_service(service_module, **kwargs):
    service = service_module.MarketDataService(None, None)
    service.__dict__.update(kwargs)
    return service


def _history_feed(fetch_candles, interval_seconds):
    return SimpleNamespace(columns=["timestamp", "close"], fetch_candles=fetch_candles, interval_in_seconds=interval_seconds)


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
    factory, service_module, _ = _install_hummingbot_stubs(monkeypatch)
    from services.market_data_service import MarketDataService

    factory._candles_map = {name: object() for name in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")}
    service = MarketDataService.__new__(MarketDataService)
    service._candle_source_cache = {}
    calls = []
    active = 0
    max_active = 0

    monkeypatch.setattr(service_module, "CANDLE_SOURCE_BATCH_TIMEOUT", 0.01)

    async def validate(connector_name, trading_pair, interval):
        nonlocal active, max_active
        calls.append(connector_name)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05 if connector_name == "alpha" else 0)
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
    factory, _, router = _install_hummingbot_stubs(monkeypatch)
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
    )
    feed = SimpleNamespace(
        columns=["timestamp", "open", "high", "low", "close", "volume"],
        fetch_candles=AsyncMock(return_value=candles_df.rows),
        interval_in_seconds=60,
    )
    factory.get_candle = MagicMock(return_value=feed)

    result = asyncio.run(
        router.get_candle_history(
            _request(service),
            CandleHistoryRequest(trading_pair="BTC-USDT", interval="1m", max_records=3),
        )
    )

    assert [row["timestamp"] for row in result] == [1, 2, 3]
    assert result[1]["close"] == 20
    config = factory.get_candle.call_args.args[0]
    assert config.connector == "alpha"
    assert config.trading_pair == "BTC-USDT"
    feed.fetch_candles.assert_awaited_once()


def test_candle_history_hits_long_interval_cache_before_boundary(monkeypatch):
    factory, service_module, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    now = [86390]
    monkeypatch.setattr(router.time, "time", lambda: now[0])
    rows = [{"timestamp": 1, "close": 10}]
    service = _history_service(service_module, resolve_candle_source=AsyncMock(return_value="alpha"))
    feed = _history_feed(AsyncMock(return_value=rows), 86400)
    factory.get_candle = MagicMock(return_value=feed)
    config = CandleHistoryRequest(trading_pair="BTC-USDT", interval="1d", max_records=1)

    first = asyncio.run(router.get_candle_history(_request(service), config))
    first[0]["close"] = 99
    now[0] = 86399
    second = asyncio.run(router.get_candle_history(_request(service), config))
    second[0]["close"] = 98
    third = asyncio.run(router.get_candle_history(_request(service), config))

    assert third == rows
    assert feed.fetch_candles.await_count == 1


def test_candle_history_bypasses_cache_for_one_minute(monkeypatch):
    factory, service_module, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    service = _history_service(service_module, resolve_candle_source=AsyncMock(return_value="alpha"))
    feed = _history_feed(
        AsyncMock(side_effect=[[{"timestamp": 1, "close": 10}], [{"timestamp": 2, "close": 20}]]), 60
    )
    factory.get_candle = MagicMock(return_value=feed)
    config = CandleHistoryRequest(trading_pair="BTC-USDT", interval="1m", max_records=1)

    first = asyncio.run(router.get_candle_history(_request(service), config))
    second = asyncio.run(router.get_candle_history(_request(service), config))

    assert first[0]["close"] == 10
    assert second[0]["close"] == 20
    assert feed.fetch_candles.await_count == 2


def test_candle_history_refetches_when_long_interval_boundary_is_reached(monkeypatch):
    factory, service_module, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    now = [3590]
    monkeypatch.setattr(router.time, "time", lambda: now[0])
    service = _history_service(service_module, resolve_candle_source=AsyncMock(return_value="alpha"))
    async def fetch_candles(**kwargs):
        if fetch_candles.calls == 0:
            now[0] = 3600
        fetch_candles.calls += 1
        return [{"timestamp": 1, "close": 10 + fetch_candles.calls - 1}]

    fetch_candles.calls = 0
    feed = _history_feed(
        AsyncMock(side_effect=fetch_candles), 3600
    )
    factory.get_candle = MagicMock(return_value=feed)
    config = CandleHistoryRequest(trading_pair="BTC-USDT", interval="1h", max_records=1)

    result = asyncio.run(router.get_candle_history(_request(service), config))

    assert result[0]["close"] == 11
    assert feed.fetch_candles.await_count == 2


def test_candle_history_fails_closed_when_refetch_crosses_again(monkeypatch):
    factory, service_module, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    now = [3590]
    monkeypatch.setattr(router.time, "time", lambda: now[0])
    service = _history_service(service_module, resolve_candle_source=AsyncMock(return_value="alpha"))

    async def fetch_candles(**kwargs):
        now[0] += 3600
        return [{"timestamp": 1, "close": 10}]

    feed = _history_feed(AsyncMock(side_effect=fetch_candles), 3600)
    factory.get_candle = MagicMock(return_value=feed)
    config = CandleHistoryRequest(trading_pair="BTC-USDT", interval="1h", max_records=1)

    with pytest.raises(router.HTTPException) as raised:
        asyncio.run(router.get_candle_history(_request(service), config))

    assert raised.value.status_code == 503
    assert feed.fetch_candles.await_count == 2
    assert not service._candle_history_cache


def test_candle_history_error_does_not_populate_one_hour_cache(monkeypatch):
    factory, service_module, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    service = _history_service(service_module, resolve_candle_source=AsyncMock(return_value="alpha"))
    fetch_candles = AsyncMock(side_effect=RuntimeError("provider error"))
    factory.get_candle = MagicMock(
        return_value=_history_feed(fetch_candles, 3600)
    )
    config = CandleHistoryRequest(trading_pair="BTC-USDT", interval="1h", max_records=1)

    for _ in range(2):
        with pytest.raises(router.HTTPException) as raised:
            asyncio.run(router.get_candle_history(_request(service), config))
        assert raised.value.status_code == 503

    assert fetch_candles.await_count == 4
    assert not service._candle_history_cache


def test_market_data_stop_clears_candle_history_cache(monkeypatch):
    _, service_module, _ = _install_hummingbot_stubs(monkeypatch)
    service = _history_service(service_module)
    service.cache_candle_history(("alpha", "BTC-USDT", "1h", 1), [{"close": 1}], 1, 3600)

    service.stop()

    assert not service._candle_history_cache


def test_candle_history_cache_purges_expired_and_evicts_oldest(monkeypatch):
    _, service_module, _ = _install_hummingbot_stubs(monkeypatch)
    service = _history_service(service_module)
    service._candle_history_cache[("alpha", "BTC-USDT", "1h", 0)] = (1, [])

    for index in range(128):
        service.cache_candle_history(("alpha", str(index), "1h", 1), [{"close": index}], 100, 3600)
    service.cache_candle_history(("alpha", "new", "1h", 1), [{"close": 1}], 100, 3600)

    assert len(service._candle_history_cache) == 128
    assert ("alpha", "BTC-USDT", "1h", 0) not in service._candle_history_cache
    assert ("alpha", "0", "1h", 1) not in service._candle_history_cache


def test_candle_history_normalizes_pair_case_before_source_resolution(monkeypatch):
    factory, _, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    service = SimpleNamespace(resolve_candle_source=AsyncMock(return_value="alpha"))
    feed = SimpleNamespace(
        columns=["timestamp", "close"],
        fetch_candles=AsyncMock(return_value=[{"timestamp": 1, "close": 1}]),
        interval_in_seconds=60,
    )
    factory.get_candle = MagicMock(return_value=feed)

    asyncio.run(
        router.get_candle_history(
            _request(service),
            CandleHistoryRequest(trading_pair="cbBTC-USDC", interval="1h", max_records=1),
        )
    )

    service.resolve_candle_source.assert_awaited_once_with("CBBTC-USDC", "1h")
    assert factory.get_candle.call_args.args[0].trading_pair == "CBBTC-USDC"


@pytest.mark.parametrize(
    ("requested_pair", "normalized_pair"),
    [
        ("WETH-USDC", "ETH-USDC"),
        ("WSOL-USDC", "SOL-USDC"),
        ("WBNB-USDC", "BNB-USDC"),
        ("WPOL-USDC", "POL-USDC"),
        ("WAVAX-USDC", "AVAX-USDC"),
        ("USDC-WETH", "USDC-ETH"),
    ],
)
def test_candle_history_unwraps_native_pair_for_source_and_fetch(
    monkeypatch, requested_pair, normalized_pair
):
    factory, _, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    rows = [{"timestamp": 1, "close": 2500}]
    service = SimpleNamespace(resolve_candle_source=AsyncMock(return_value="alpha"))
    feed = SimpleNamespace(
        columns=["timestamp", "close"],
        fetch_candles=AsyncMock(return_value=rows),
    )
    factory.get_candle = MagicMock(return_value=feed)

    result = asyncio.run(
        router.get_candle_history(
            _request(service),
            CandleHistoryRequest(trading_pair=requested_pair, interval="1h", max_records=1),
        )
    )

    assert result == rows
    service.resolve_candle_source.assert_awaited_once_with(normalized_pair, "1h")
    assert factory.get_candle.call_args.args[0].trading_pair == normalized_pair


def test_candle_history_normalizes_one_current_close_timestamp(monkeypatch):
    factory, _, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    monkeypatch.setattr(router.time, "time", lambda: 1000)
    rows = [
        {"timestamp": 940, "close": 1},
        {"timestamp": 1000, "close": 2},
        {"timestamp": 1060, "close": 3},
    ]
    service = SimpleNamespace(resolve_candle_source=AsyncMock(return_value="alpha"))
    factory.get_candle = MagicMock(
        return_value=SimpleNamespace(
            columns=["timestamp", "close"],
            fetch_candles=AsyncMock(return_value=rows),
            interval_in_seconds=60,
        )
    )

    result = asyncio.run(
        router.get_candle_history(
            _request(service),
            CandleHistoryRequest(trading_pair="BTC-USDT", interval="1m", max_records=3),
        )
    )

    assert [row["timestamp"] for row in result] == [880, 940, 1000]


def test_candle_history_does_not_normalize_distant_future_timestamp(monkeypatch):
    factory, _, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    monkeypatch.setattr(router.time, "time", lambda: 1000)
    rows = [{"timestamp": 1120, "close": 1}]
    service = SimpleNamespace(resolve_candle_source=AsyncMock(return_value="alpha"))
    factory.get_candle = MagicMock(
        return_value=SimpleNamespace(
            columns=["timestamp", "close"],
            fetch_candles=AsyncMock(return_value=rows),
            interval_in_seconds=60,
        )
    )

    result = asyncio.run(
        router.get_candle_history(
            _request(service),
            CandleHistoryRequest(trading_pair="BTC-USDT", interval="1m", max_records=1),
        )
    )

    assert result == rows


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

    assert raised.value.status_code == 503
    assert raised.value.detail == "Candle history is temporarily unavailable."
    assert provider_error not in caplog.text


def test_candle_history_retries_one_transient_fetch_error(monkeypatch):
    factory, _, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    rows = [
        {"timestamp": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]
    fetch_candles = AsyncMock(side_effect=[RuntimeError("transient"), rows])
    service = SimpleNamespace(resolve_candle_source=AsyncMock(return_value="alpha"))
    factory.get_candle = MagicMock(
        return_value=SimpleNamespace(
            columns=["timestamp", "open", "high", "low", "close", "volume"],
            fetch_candles=fetch_candles,
        )
    )

    result = asyncio.run(
        router.get_candle_history(
            _request(service),
            CandleHistoryRequest(trading_pair="BTC-USDT", interval="1m", max_records=20),
        )
    )

    assert result == rows
    assert fetch_candles.await_count == 2


def test_candle_history_redacts_second_fetch_error(monkeypatch, caplog):
    factory, _, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    provider_error = "provider secret endpoint details"
    fetch_candles = AsyncMock(side_effect=RuntimeError(provider_error))
    service = SimpleNamespace(resolve_candle_source=AsyncMock(return_value="alpha"))
    factory.get_candle = MagicMock(
        return_value=SimpleNamespace(fetch_candles=fetch_candles)
    )

    with caplog.at_level("ERROR"):
        with pytest.raises(router.HTTPException) as raised:
            asyncio.run(
                router.get_candle_history(
                    _request(service),
                    CandleHistoryRequest(
                        trading_pair="BTC-USDT",
                        interval="1m",
                        max_records=20,
                    ),
                )
            )

    assert raised.value.status_code == 503
    assert raised.value.detail == "Candle history is temporarily unavailable."
    assert fetch_candles.await_count == 2
    assert provider_error not in caplog.text


def test_order_book_retries_one_transient_error(monkeypatch):
    _, _, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import OrderBookRequest

    service = SimpleNamespace(
        get_order_book_data=AsyncMock(
            side_effect=[
                {"error": "transient"},
                {
                    "trading_pair": "BTC-USDT",
                    "bids": [[100, 2]],
                    "asks": [[101, 3]],
                    "timestamp": 1,
                },
            ]
        )
    )

    result = asyncio.run(
        router.get_order_book(
            OrderBookRequest(connector_name="alpha", trading_pair="BTC-USDT", depth=5),
            service,
        )
    )

    assert result.bids[0].price == 100
    assert result.asks[0].price == 101
    assert service.get_order_book_data.await_count == 2


def test_order_book_redacts_second_transient_error(monkeypatch):
    _, _, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import OrderBookRequest

    service = SimpleNamespace(
        get_order_book_data=AsyncMock(return_value={"error": "provider secret details"})
    )

    with pytest.raises(router.HTTPException) as raised:
        asyncio.run(
            router.get_order_book(
                OrderBookRequest(
                    connector_name="alpha",
                    trading_pair="BTC-USDT",
                    depth=5,
                ),
                service,
            )
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == "Order book is temporarily unavailable."
    assert service.get_order_book_data.await_count == 2


def test_candle_history_maps_fetch_timeout_to_gateway_timeout(monkeypatch):
    factory, _, router = _install_hummingbot_stubs(monkeypatch)
    from models.market_data import CandleHistoryRequest

    service = SimpleNamespace(resolve_candle_source=AsyncMock(return_value="alpha"))
    factory.get_candle = MagicMock(
        return_value=SimpleNamespace(
            fetch_candles=AsyncMock(side_effect=asyncio.TimeoutError),
        )
    )

    with pytest.raises(router.HTTPException) as raised:
        asyncio.run(
            router.get_candle_history(
                _request(service),
                CandleHistoryRequest(trading_pair="BTC-USDT", interval="1m", max_records=20),
            )
        )

    assert raised.value.status_code == 504
    assert raised.value.detail == "Candle history request timed out."
