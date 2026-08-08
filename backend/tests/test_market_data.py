"""market_data 的 a_stock_data 日线入口测试。"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from app.data_sources.market_data import (
    fetch_a_share_daily,
    fetch_a_share_universe,
    fetch_gz2000_universe,
    fetch_hs300_universe,
    fetch_star50_universe,
    fetch_star_board_universe,
    fetch_zz1000_universe,
    fetch_zz500_universe,
)


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


def test_fetch_zz500_universe_uses_csi_000905(monkeypatch):
    requested: list[str] = []

    def constituents(symbol: str) -> pd.DataFrame:
        requested.append(symbol)
        return pd.DataFrame(
            {
                "成分券代码": ["600000"],
                "成分券名称": ["浦发银行"],
            }
        )

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(index_stock_cons_csindex=constituents),
    )

    rows, note = fetch_zz500_universe()

    assert requested == ["000905"]
    assert rows == [{"symbol": "600000", "name": "浦发银行"}]
    assert "中证500当前成分股共 1 只" in note


def test_fetch_zz1000_universe_uses_csi_000852(monkeypatch):
    requested: list[str] = []

    def constituents(symbol: str) -> pd.DataFrame:
        requested.append(symbol)
        return pd.DataFrame(
            {"成分券代码": ["600000"], "成分券名称": ["浦发银行"]}
        )

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(index_stock_cons_csindex=constituents),
    )

    rows, note = fetch_zz1000_universe()

    assert requested == ["000852"]
    assert rows == [{"symbol": "600000", "name": "浦发银行"}]
    assert "中证1000当前成分股共 1 只" in note


def test_fetch_star50_universe_uses_csi_000688(monkeypatch):
    requested: list[str] = []

    def constituents(symbol: str) -> pd.DataFrame:
        requested.append(symbol)
        return pd.DataFrame(
            {"成分券代码": ["688981"], "成分券名称": ["中芯国际"]}
        )

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(index_stock_cons_csindex=constituents),
    )

    rows, note = fetch_star50_universe()

    assert requested == ["000688"]
    assert rows == [{"symbol": "688981", "name": "中芯国际"}]
    assert "科创50当前成分股共 1 只" in note


def test_fetch_gz2000_universe_falls_back_to_sina(monkeypatch):
    called: list[str] = []

    def csindex(symbol: str) -> pd.DataFrame:
        called.append(f"csindex:{symbol}")
        raise RuntimeError("国证指数不在中证指数库")

    def cons(symbol: str) -> pd.DataFrame:
        called.append(f"cons:{symbol}")
        raise RuntimeError("新浪成分接口不存在")

    def sina(symbol: str) -> pd.DataFrame:
        called.append(f"sina:{symbol}")
        return pd.DataFrame({"证券代码": ["300001"], "证券简称": ["特锐德"]})

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(
            index_stock_cons_csindex=csindex,
            index_stock_cons=cons,
            index_stock_cons_sina=sina,
        ),
    )

    rows, note = fetch_gz2000_universe()

    assert called == ["csindex:399303", "cons:399303", "sina:sz399303"]
    assert rows == [{"symbol": "300001", "name": "特锐德"}]
    assert "国证2000当前成分股共 1 只" in note


def test_fetch_star_board_universe_filters_by_prefix(monkeypatch):
    class _FakeAStockDataSDK:
        def get_universe(self):
            return [
                {"symbol": "600000", "name": "浦发银行"},
                {"symbol": "688981", "name": "中芯国际"},
                {"symbol": "689009", "name": "九号公司"},
                {"symbol": "300750", "name": "宁德时代"},
            ]

    monkeypatch.setattr(
        "app.data_sources.market_data.AStockDataSDK",
        lambda: _FakeAStockDataSDK(),
    )

    rows, note = fetch_star_board_universe(10)

    assert rows == [
        {"symbol": "688981", "name": "中芯国际"},
        {"symbol": "689009", "name": "九号公司"},
    ]
    assert "科创板股票池共 2 只" in note


def test_fetch_star_board_universe_sampling_with_seed(monkeypatch):
    class _FakeAStockDataSDK:
        def get_universe(self):
            return [{"symbol": f"688{i:03d}", "name": f"股{i}"} for i in range(10)]

    monkeypatch.setattr(
        "app.data_sources.market_data.AStockDataSDK",
        lambda: _FakeAStockDataSDK(),
    )

    rows, note = fetch_star_board_universe(3, seed=42)

    assert len(rows) == 3
    assert all(row["symbol"].startswith("688") for row in rows)
    assert "随机种子 42 抽样 3 只" in note
