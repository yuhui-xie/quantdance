"""market_data 的 a_stock_data 日线入口测试。"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from app.data_sources.market_data import (
    fetch_a_share_daily,
    fetch_a_share_universe,
    fetch_etf_universe,
    fetch_gz2000_universe,
    fetch_hs300_universe,
    fetch_hs300_universe_at,
    fetch_star50_universe,
    fetch_star_board_universe,
    fetch_zz1000_universe,
    fetch_zz1000_universe_at,
    fetch_zz500_universe,
    fetch_zz500_universe_at,
)
from app.universe import resolve_universe_rows


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


def test_fetch_etf_universe_parses_akshare_spot(monkeypatch):
    fake_ak = SimpleNamespace(
        fund_etf_spot_em=lambda: pd.DataFrame(
            {
                "代码": ["510300", "159915", "518880", "not_a_code", "511880"],
                "名称": ["沪深300ETF", "创业板ETF", "黄金ETF", "忽略", "货币ETF"],
            }
        )
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    rows, note = fetch_etf_universe(2)

    # 非 6 位代码被过滤；按代码升序截取前 2 只
    assert rows == [
        {"symbol": "159915", "name": "创业板ETF"},
        {"symbol": "510300", "name": "沪深300ETF"},
    ]
    assert "场内 ETF 共 4 只" in note
    assert "截取前 2 只" in note


def test_fetch_etf_universe_sampling_with_seed(monkeypatch):
    fake_ak = SimpleNamespace(
        fund_etf_spot_em=lambda: pd.DataFrame(
            {
                "代码": [f"51{i:04d}" for i in range(10)],
                "名称": [f"ETF{i}" for i in range(10)],
            }
        )
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    rows, note = fetch_etf_universe(3, seed=7)

    assert len(rows) == 3
    assert all(row["symbol"].startswith("51") for row in rows)
    assert "随机种子 7 抽样 3 只" in note


def test_fetch_etf_universe_falls_back_to_ths_when_em_fails(monkeypatch):
    # 东财 fund_etf_spot_em 抛错（如被风控/限流）时，应回退到同花顺 fund_etf_spot_ths
    fake_ak = SimpleNamespace(
        fund_etf_spot_em=lambda: (_ for _ in ()).throw(RuntimeError("东财限流")),
        fund_etf_spot_ths=lambda: pd.DataFrame(
            {
                "基金代码": ["510300", "159915", "518880"],
                "基金简称": ["沪深300ETF", "创业板ETF", "黄金ETF"],
            }
        ),
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    rows, note = fetch_etf_universe(2)

    assert rows == [
        {"symbol": "159915", "name": "创业板ETF"},
        {"symbol": "510300", "name": "沪深300ETF"},
    ]
    assert "场内 ETF 共 3 只" in note


def test_fetch_etf_universe_falls_back_to_sina_with_prefix(monkeypatch):
    # 东财、同花顺都不可用时，回退到新浪 fund_etf_category_sina，
    # 其代码带 sh/sz 前缀，需剥成 6 位数字。
    fake_ak = SimpleNamespace(
        fund_etf_spot_em=lambda: (_ for _ in ()).throw(RuntimeError("东财限流")),
        fund_etf_spot_ths=lambda: (_ for _ in ()).throw(RuntimeError("同花顺失败")),
        fund_etf_category_sina=lambda symbol: pd.DataFrame(
            {
                "代码": ["sz159998", "sh510300", "sz159915"],
                "名称": ["芯片ETF", "沪深300ETF", "创业板ETF"],
            }
        ),
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    rows, note = fetch_etf_universe(3)

    assert rows == [
        {"symbol": "159998", "name": "芯片ETF"},
        {"symbol": "510300", "name": "沪深300ETF"},
        {"symbol": "159915", "name": "创业板ETF"},
    ]
    assert "场内 ETF 共 3 只" in note


def test_fetch_etf_universe_all_sources_fail(monkeypatch):
    # 全部数据源都失败时，抛 MarketDataError 且错误信息汇总各来源
    fake_ak = SimpleNamespace(
        fund_etf_spot_em=lambda: (_ for _ in ()).throw(RuntimeError("东财限流")),
        fund_etf_spot_ths=lambda: (_ for _ in ()).throw(RuntimeError("同花顺失败")),
        fund_etf_category_sina=lambda symbol: (_ for _ in ()).throw(RuntimeError("新浪失败")),
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    with pytest.raises(Exception) as exc_info:
        fetch_etf_universe(2)

    assert "东财限流" in str(exc_info.value)
    assert "同花顺失败" in str(exc_info.value)
    assert "新浪失败" in str(exc_info.value)


def test_resolve_universe_rows_etf_preset(monkeypatch):
    from app.universe import resolve_universe_rows

    monkeypatch.setattr(
        "app.universe.fetch_etf_universe",
        lambda max_universe, seed=None: (
            [{"symbol": "510300", "name": "沪深300ETF"}],
            "akshare 场内 ETF mock",
        ),
    )

    rows, note = resolve_universe_rows(
        symbols=None, universe="etf", max_universe=50, seed=None
    )

    assert rows == [{"symbol": "510300", "name": "沪深300ETF"}]
    assert note == "akshare 场内 ETF mock"


def _patch_index_constitution(monkeypatch, frame: pd.DataFrame) -> None:
    """把 index_constitution 注入 sys.modules，constituents_at 返回 frame。"""
    monkeypatch.setitem(
        sys.modules,
        "index_constitution",
        SimpleNamespace(constituents_at=lambda index, when: frame),
    )


def _patch_spot(monkeypatch) -> None:
    """akshare.stock_zh_a_spot_em 返回全市场代码->名称，供补名。"""
    fake_ak = SimpleNamespace(
        stock_zh_a_spot_em=lambda: pd.DataFrame(
            {
                "代码": ["600000", "000001", "600004", "600008", "301526"],
                "名称": ["浦发银行", "平安银行", "白云机场", "首创股份", "国际复材"],
            }
        )
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)


def test_fetch_zz500_universe_at_uses_csi500_asof(monkeypatch):
    _patch_index_constitution(
        monkeypatch,
        pd.DataFrame(
            {
                "symbol": ["SH600004", "SZ301526", "600008"],
                "name": ["乱码", "乱码", "乱码"],
                "opt-in": ["2023-12-08", "2025-06-13", "2007-01-15"],
                "opt-out": [None, None, None],
            }
        ),
    )
    _patch_spot(monkeypatch)

    rows, note = fetch_zz500_universe_at("2024-06-14")

    assert rows == [
        {"symbol": "600004", "name": "白云机场"},
        {"symbol": "301526", "name": "国际复材"},
        {"symbol": "600008", "name": "首创股份"},
    ]
    assert "中证500" in note and "2024-06-14 时点成分股" in note
    assert "幸存者偏差" not in note


def test_fetch_zz500_universe_at_samples_with_seed(monkeypatch):
    symbols = [f"SH{i:06d}" for i in range(1, 501)]
    _patch_index_constitution(
        monkeypatch,
        pd.DataFrame(
            {
                "symbol": symbols,
                "name": ["乱码"] * 500,
                "opt-in": ["2007-01-15"] * 500,
                "opt-out": [None] * 500,
            }
        ),
    )
    _patch_spot(monkeypatch)

    rows, note = fetch_zz500_universe_at("2024-06-14", 100, seed=42)

    assert len(rows) == 100
    assert len({r["symbol"] for r in rows}) == 100
    assert "随机种子 42 抽样 100 只" in note


def test_fetch_zz500_universe_at_ignores_index_constitution_bad_names(monkeypatch):
    # name 列乱码不影响成分池，symbol 归一仍正确
    _patch_index_constitution(
        monkeypatch,
        pd.DataFrame(
            {
                "symbol": ["SH600004"],
                "name": ["��"],
                "opt-in": ["2023-12-08"],
                "opt-out": [None],
            }
        ),
    )
    _patch_spot(monkeypatch)

    rows, note = fetch_zz500_universe_at("2024-06-14")

    assert rows == [{"symbol": "600004", "name": "白云机场"}]


def test_fetch_zz500_universe_at_falls_back_to_akshare_when_missing(monkeypatch):
    # index_constitution 不可用时回退 akshare 当前成分（含幸存者偏差 note）
    monkeypatch.setitem(sys.modules, "index_constitution", None)
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(
            index_stock_cons_csindex=lambda symbol: pd.DataFrame(
                {
                    "成分券代码": ["600000"],
                    "成分券名称": ["浦发银行"],
                }
            )
        ),
    )

    rows, note = fetch_zz500_universe_at("2024-06-14")

    assert rows == [{"symbol": "600000", "name": "浦发银行"}]
    assert "幸存者偏差" in note


def test_fetch_hs300_universe_at_maps_000300(monkeypatch):
    _patch_index_constitution(
        monkeypatch,
        pd.DataFrame(
            {
                "symbol": ["SH600000", "SZ000001"],
                "name": ["乱码", "乱码"],
                "opt-in": ["2007-01-15", "2007-01-15"],
                "opt-out": [None, None],
            }
        ),
    )
    _patch_spot(monkeypatch)

    rows, note = fetch_hs300_universe_at("2024-06-14")

    assert rows == [
        {"symbol": "600000", "name": "浦发银行"},
        {"symbol": "000001", "name": "平安银行"},
    ]
    assert "沪深300" in note


def test_resolve_universe_rows_asof_routes_to_historical(monkeypatch):
    _patch_index_constitution(
        monkeypatch,
        pd.DataFrame(
            {
                "symbol": ["SH600004"],
                "name": ["乱码"],
                "opt-in": ["2023-12-08"],
                "opt-out": [None],
            }
        ),
    )
    _patch_spot(monkeypatch)

    rows, note = resolve_universe_rows(
        symbols=None, universe="zz500", max_universe=10, seed=None, asof="2024-06-14"
    )

    assert rows == [{"symbol": "600004", "name": "白云机场"}]
    assert "时点成分股" in note


def test_resolve_universe_rows_asof_none_keeps_current(monkeypatch):
    # asof=None 时仍走 akshare 当前成分（向后兼容）
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(
            index_stock_cons_csindex=lambda symbol: pd.DataFrame(
                {"成分券代码": ["600000"], "成分券名称": ["浦发银行"]}
            )
        ),
    )

    rows, note = resolve_universe_rows(
        symbols=None, universe="zz500", max_universe=10, seed=None
    )

    assert rows == [{"symbol": "600000", "name": "浦发银行"}]
    assert "当前成分股" in note


def test_fetch_zz1000_universe_at_falls_back_to_current(monkeypatch):
    # zz1000（000852）不在 index_constitution，恒回退 akshare 当前成分
    monkeypatch.setitem(sys.modules, "index_constitution", None)
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(
            index_stock_cons_csindex=lambda symbol: pd.DataFrame(
                {"成分券代码": ["600000"], "成分券名称": ["浦发银行"]}
            )
        ),
    )

    rows, note = fetch_zz1000_universe_at("2024-06-14")

    assert rows == [{"symbol": "600000", "name": "浦发银行"}]
    assert "幸存者偏差" in note
