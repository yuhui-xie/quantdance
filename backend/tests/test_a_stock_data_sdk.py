"""a_stock_data 与 mootdx 适配层测试（全 mock，无外网依赖）。"""

from __future__ import annotations

import json

import pytest

from app.data_sources.a_stock_data import AStockDataError, AStockDataSDK
from app.data_sources.mootdx_market_sdk import MootdxMarketError, MootdxMarketSDK
from app.data_sources.tencent_finance_sdk import TencentFinanceError


class _FakeMootdxClient:
    def quotes(self, _codes):  # noqa: ANN001
        return [
            {
                "name": "贵州茅台",
                "price": 1888.88,
                "last_close": 1870.0,
                "open": 1875.0,
                "high": 1899.0,
                "low": 1860.0,
                "vol": 123456,
                "amount": 99999999.0,
                "servertime": "20260524150001",
                "bid1": 1888.8,
                "bid_vol1": 12,
                "ask1": 1888.9,
                "ask_vol1": 10,
            }
        ]

    def bars(self, code, frequency=9, start=0, offset=800, adjust=""):  # noqa: ANN001
        return [
            {"datetime": "2026-05-20", "open": 1800, "high": 1820, "low": 1790, "close": 1812, "vol": 10000},
            {"datetime": "2026-05-21", "open": 1812, "high": 1838, "low": 1808, "close": 1825, "vol": 12000},
        ]

    def get_transaction_data(self, _market, _code, _start, _count):  # noqa: ANN001
        return [
            {"time": "09:30:01", "price": 1888.8, "vol": 5, "side": "B"},
            {"time": "09:30:03", "price": 1888.7, "vol": 3, "side": "S"},
        ]

    def stocks(self, market):  # noqa: ANN001
        if market == 1:
            return [
                {"code": "600519", "name": "贵州茅台"},
                {"code": "510300", "name": "沪深300ETF"},
            ]
        return [
            {"code": "000858", "name": "五粮液"},
            {"code": "300750", "name": "宁德时代"},
        ]


class _FakeTencent:
    def get_valuation(self, symbol: str):  # noqa: ANN001
        return {
            "symbol": symbol,
            "pe_ttm": 20.1,
            "pb": 5.2,
            "market_cap": 2400000000000.0,
            "float_market_cap": 1900000000000.0,
            "turnover_rate": 0.8,
            "limit_up": 2050.0,
            "limit_down": 1670.0,
            "raw_fields": [],
        }

    def get_valuations(self, symbols: list[str]):  # noqa: ANN001
        return {s: self.get_valuation(s) for s in symbols}


class _FakeTencentFail:
    def get_valuation(self, _symbol: str):  # noqa: ANN001
        raise TencentFinanceError("http_error", "network fail")

    def get_valuations(self, _symbols: list[str]):  # noqa: ANN001
        raise TencentFinanceError("http_error", "network fail")


class _BrokenKlineMootdx(MootdxMarketSDK):
    def __init__(self) -> None:
        pass

    def get_klines(self, symbol: str, period="day", *, count=200):  # noqa: ANN001
        raise MootdxMarketError("source_error", "boom", symbol=symbol)


def test_mootdx_adapter_normalize_symbol_and_period():
    sdk = MootdxMarketSDK(client=_FakeMootdxClient())
    assert sdk.normalize_symbol("600519") == "sh600519"
    assert sdk.normalize_symbol("000858.SZ") == "sz000858"
    assert sdk.map_period("day") == 9
    assert sdk.map_period("1m") == 8


def test_mootdx_adapter_maps_quote_kline_and_trades():
    sdk = MootdxMarketSDK(client=_FakeMootdxClient())

    quote = sdk.get_quote("600519")
    bars = sdk.get_klines("600519", period="day", count=2)
    trades = sdk.get_trades("600519", count=2)

    assert quote["symbol"] == "sh600519"
    assert quote["name"] == "贵州茅台"
    assert quote["price"] == pytest.approx(1888.88)
    assert bars[1]["close"] == pytest.approx(1825.0)
    assert trades[0]["time"] == "09:30:01"
    assert trades[1]["side"] == "S"


def test_mootdx_adapter_klines_fetches_raw_then_forward_adjusts(monkeypatch):
    """拉取原始价（不传 adjust），再由本地除权记录自行前复权，保证跨除权日连续。"""
    class _RecordingClient:
        def __init__(self) -> None:
            self.bars_kwargs: list[dict] = []

        def bars(self, code, frequency=9, start=0, offset=800, adjust=""):  # noqa: ANN001
            self.bars_kwargs.append(dict(code=code, frequency=frequency, start=start, offset=offset, adjust=adjust))
            return [{"datetime": "2026-05-21", "open": 1812, "high": 1838, "low": 1808, "close": 1825, "vol": 12000}]

    client = _RecordingClient()
    sdk = MootdxMarketSDK(client=client)  # type: ignore[arg-type]
    # 无除权记录时原样返回，验证原始路径不依赖网络。
    monkeypatch.setattr(sdk, "_get_xdxr_info", lambda symbol: None)

    bars = sdk.get_klines("600519", period="day", count=2)

    assert bars[-1]["close"] == pytest.approx(1825.0)
    # 前复权由本地折算完成，不应把复权参数丢给 mootdx（其自带实现有除权断层 bug）。
    assert client.bars_kwargs
    assert all(kw.get("adjust") in ("", None) for kw in client.bars_kwargs)


def test_forward_adjust_smoothes_ex_right_gap(monkeypatch):
    """除权日前复权后应连续：除权日收盘与前一交易日收盘基本相等。"""
    import pandas as pd

    raw = [
        {"datetime": "2024-06-11 15:00", "open": 41.5, "high": 42.0, "low": 41.0, "close": 41.82, "volume": 100, "amount": 1.0},
        {"datetime": "2024-06-12 15:00", "open": 34.3, "high": 34.3, "low": 33.6, "close": 34.0, "volume": 200, "amount": 1.0},
        {"datetime": "2024-06-13 15:00", "open": 33.9, "high": 34.05, "low": 33.57, "close": 34.02, "volume": 150, "amount": 1.0},
    ]
    xdxr = pd.DataFrame(
        {"fenhong": [0.0, 10.0, 0.0], "peigu": [0.0, 0.0, 0.0], "peigujia": [0.0, 0.0, 0.0], "songzhuangu": [0.0, 2.0, 0.0]},
        index=pd.to_datetime(["2024-06-11", "2024-06-12", "2024-06-13"]),
    )
    sdk = MootdxMarketSDK(client=_FakeMootdxClient())  # type: ignore[arg-type]
    monkeypatch.setattr(sdk, "_get_xdxr_info", lambda symbol: xdxr)

    adjusted = sdk._forward_adjust_bars("600519", raw)  # type: ignore[arg-type]

    closes = [b["close"] for b in adjusted]
    # 除权日（06-12）收盘应与 06-11 收盘连续（差值远小于原始 -18.7% 断层）。
    assert closes[1] == pytest.approx(closes[0], rel=1e-3)
    assert closes[2] == pytest.approx(closes[1], rel=1e-2)


def test_a_stock_data_rebuilds_cache_without_adjust_marker(tmp_path):
    """旧的不复权缓存（无 adjust 标记）必须作废重拉，避免复权/不复权混合。"""
    class _CountingMootdx(MootdxMarketSDK):
        def __init__(self) -> None:
            self.calls = 0

        def get_klines(self, symbol: str, period="day", *, count=200):  # noqa: ANN001
            self.calls += 1
            return [
                {"datetime": "2026-05-21", "open": 1812, "high": 1838, "low": 1808, "close": 1825.0, "volume": 12000, "amount": None}
            ]

    # 先写入一份"旧格式"缓存：带 rows 但没有 adjust 标记。
    cache_file = tmp_path / "klines" / "day" / "sh600519.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        '{"type":"klines","source":"a_stock_data","symbol":"sh600519","period":"day",'
        '"requested_count":200,"cached_at":"2099-01-01","rows":['
        '{"datetime":"2026-05-21","open":1812,"high":1838,"low":1808,"close":1825,"volume":12000}]}\n',
        encoding="utf-8",
    )

    mootdx = _CountingMootdx()
    sdk = AStockDataSDK(mootdx_sdk=mootdx, tencent_sdk=_FakeTencent(), cache_dir=tmp_path)  # type: ignore[arg-type]
    rows = sdk.get_klines("600519", count=2)

    # 旧缓存版本不符，触发重新拉取，并写入带版本号的新缓存。
    assert mootdx.calls == 1
    assert rows[-1]["close"] == pytest.approx(1825.0)
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    assert payload.get("version") == 3
    assert payload.get("adjust") == "qfq"


def test_mootdx_adapter_get_stocks_filters_a_shares():
    sdk = MootdxMarketSDK(client=_FakeMootdxClient())

    stocks = sdk.get_stocks()

    assert [item["symbol"] for item in stocks] == ["sh600519", "sz000858", "sz300750"]
    assert stocks[0]["name"] == "贵州茅台"


def test_a_stock_snapshot_aggregates_quote_and_valuation():
    sdk = AStockDataSDK(
        mootdx_sdk=MootdxMarketSDK(client=_FakeMootdxClient()),
        tencent_sdk=_FakeTencent(),  # type: ignore[arg-type]
        cache_enabled=False,
    )

    snapshot = sdk.get_snapshot("600519")
    valuations = sdk.get_valuation(["600519", "000858"])
    universe = sdk.get_universe()

    assert snapshot["symbol"] == "sh600519"
    assert snapshot["quote"]["price"] == pytest.approx(1888.88)
    assert snapshot["valuation"] is not None
    assert snapshot["valuation"]["pb"] == pytest.approx(5.2)
    assert valuations["sh600519"]["pe_ttm"] == pytest.approx(20.1)
    assert valuations["sz000858"]["market_cap"] == pytest.approx(2400000000000.0)
    assert universe == [
        {"symbol": "600519", "name": "贵州茅台"},
        {"symbol": "000858", "name": "五粮液"},
        {"symbol": "300750", "name": "宁德时代"},
    ]


def test_a_stock_snapshot_tencent_fail_returns_quote_only():
    sdk = AStockDataSDK(
        mootdx_sdk=MootdxMarketSDK(client=_FakeMootdxClient()),
        tencent_sdk=_FakeTencentFail(),  # type: ignore[arg-type]
        cache_enabled=False,
    )
    snapshot = sdk.get_snapshot("600519")
    assert snapshot["quote"]["symbol"] == "sh600519"
    assert snapshot["valuation"] is None


def test_a_stock_wraps_mootdx_errors_with_source():
    class _BrokenMootdx(MootdxMarketSDK):
        def __init__(self) -> None:
            pass

        def get_quote(self, symbol: str):  # noqa: ANN001
            raise MootdxMarketError("source_error", "boom", symbol=symbol)

    sdk = AStockDataSDK(
        mootdx_sdk=_BrokenMootdx(),
        tencent_sdk=_FakeTencent(),  # type: ignore[arg-type]
        cache_enabled=False,
    )
    with pytest.raises(AStockDataError, match="source=mootdx"):
        sdk.get_snapshot("600519")


def test_a_stock_data_saves_and_reuses_kline_cache(tmp_path):
    class _CountingMootdx(MootdxMarketSDK):
        def __init__(self) -> None:
            self.calls = 0

        def get_klines(self, symbol: str, period="day", *, count=200):  # noqa: ANN001
            self.calls += 1
            return [
                {
                    "datetime": "2026-05-20",
                    "open": 1800.0,
                    "high": 1820.0,
                    "low": 1790.0,
                    "close": 1812.0,
                    "volume": 10000,
                    "amount": None,
                },
                {
                    "datetime": "2026-05-21",
                    "open": 1812.0,
                    "high": 1838.0,
                    "low": 1808.0,
                    "close": 1825.0,
                    "volume": 12000,
                    "amount": None,
                },
            ]

    mootdx = _CountingMootdx()
    sdk = AStockDataSDK(mootdx_sdk=mootdx, tencent_sdk=_FakeTencent(), cache_dir=tmp_path)  # type: ignore[arg-type]

    first = sdk.get_klines("600519", count=2)
    second = sdk.get_klines("600519", count=2)

    assert mootdx.calls == 1
    assert first == second
    assert (tmp_path / "klines" / "day" / "sh600519.json").exists()


def test_a_stock_data_uses_cached_klines_when_source_fails(tmp_path):
    writer = AStockDataSDK(
        mootdx_sdk=MootdxMarketSDK(client=_FakeMootdxClient()),
        tencent_sdk=_FakeTencent(),  # type: ignore[arg-type]
        cache_dir=tmp_path,
    )
    writer.get_klines("600519", count=2)
    reader = AStockDataSDK(mootdx_sdk=_BrokenKlineMootdx(), tencent_sdk=_FakeTencent(), cache_dir=tmp_path)  # type: ignore[arg-type]

    rows = reader.get_klines("600519", count=2)

    assert rows[-1]["close"] == pytest.approx(1825.0)


def test_a_stock_data_saves_and_reuses_universe_cache(tmp_path):
    class _CountingMootdx(MootdxMarketSDK):
        def __init__(self) -> None:
            self.calls = 0

        def get_stocks(self):  # noqa: ANN001
            self.calls += 1
            return [
                {"symbol": "sh600519", "code": "600519", "name": "贵州茅台"},
                {"symbol": "sz000858", "code": "000858", "name": "五粮液"},
            ]

    mootdx = _CountingMootdx()
    sdk = AStockDataSDK(mootdx_sdk=mootdx, tencent_sdk=_FakeTencent(), cache_dir=tmp_path)  # type: ignore[arg-type]

    first = sdk.get_universe()
    second = sdk.get_universe()

    assert mootdx.calls == 1
    assert first == second
    assert (tmp_path / "universe" / "all_a.json").exists()
