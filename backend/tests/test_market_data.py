"""market_data 的 a_stock_data 日线入口测试。"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from app.data_sources.market_data import fetch_a_share_daily, fetch_a_share_universe, fetch_hs300_universe


def _a_stock_data_raw_bars():
    return [
        {
            "datetime": "2024-01-02",
            "open": 10.0,
            "high": 10.5,
            "low": 9.9,
            "close": 10.3,
            "volume": 100000,
            "amount": 1030000.0,
        },
        {
            "datetime": "2024-01-03",
            "open": 10.2,
            "high": 10.6,
            "low": 10.1,
            "close": 10.4,
            "volume": 120000,
            "amount": 1248000.0,
        },
    ]


def test_fetch_a_share_daily_a_stock_data_path(monkeypatch):
    class _FakeAStockDataSDK:
        def get_klines(self, symbol, period="day", *, count=200):  # noqa: ANN001
            assert symbol == "600000"
            assert period == "day"
            assert count == 50
            return _a_stock_data_raw_bars()

        def get_universe(self):
            return []

    monkeypatch.setattr(
        "app.data_sources.market_data.AStockDataSDK",
        lambda: _FakeAStockDataSDK(),
    )
    df = fetch_a_share_daily("600000", limit=50, data_source="a_stock_data")
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "amount"]
    assert len(df) == 2
    assert float(df.iloc[-1]["close"]) == 10.4
    assert float(df.iloc[-1]["amount"]) == 1248000.0


def test_fetch_a_share_universe_uses_a_stock_data_and_seed(monkeypatch):
    class _FakeAStockDataSDK:
        def get_universe(self):
            return [
                {"symbol": "600000", "name": "浦发银行"},
                {"symbol": "000001", "name": "平安银行"},
                {"symbol": "300750", "name": "宁德时代"},
            ]

    monkeypatch.setattr(
        "app.data_sources.market_data.AStockDataSDK",
        lambda: _FakeAStockDataSDK(),
    )

    rows, note = fetch_a_share_universe(2, seed=7)

    assert len(rows) == 2
    assert {row["symbol"] for row in rows}.issubset({"600000", "000001", "300750"})
    assert "a_stock_data 股票池共 3 只" in note
    assert "随机种子 7" in note


def test_fetch_a_share_universe_sorts_without_seed(monkeypatch):
    class _FakeAStockDataSDK:
        def get_universe(self):
            return [
                {"symbol": "600000", "name": "浦发银行"},
                {"symbol": "000001", "name": "平安银行"},
            ]

    monkeypatch.setattr(
        "app.data_sources.market_data.AStockDataSDK",
        lambda: _FakeAStockDataSDK(),
    )

    rows, note = fetch_a_share_universe(1)

    assert rows == [{"symbol": "000001", "name": "平安银行"}]
    assert "按代码升序截取前 1 只" in note


def test_fetch_a_share_daily_rejects_legacy_data_source():
    with pytest.raises(ValueError, match="不支持的数据源"):
        fetch_a_share_daily(
            "600000",
            start="2024-01-01",
            end="2024-01-10",
            data_source="tickflow",
        )


def test_fetch_hs300_universe_parses_akshare_constituents(monkeypatch):
    fake_ak = SimpleNamespace(
        index_stock_cons_csindex=lambda symbol: pd.DataFrame(
            {
                "成分券代码": ["600000", "000001", "300750"],
                "成分券名称": ["浦发银行", "平安银行", "宁德时代"],
            }
        )
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    rows, note = fetch_hs300_universe(2)

    assert rows == [
        {"symbol": "000001", "name": "平安银行"},
        {"symbol": "300750", "name": "宁德时代"},
    ]
    assert "沪深300当前成分股共 3 只" in note
    assert "截取前 2 只" in note
