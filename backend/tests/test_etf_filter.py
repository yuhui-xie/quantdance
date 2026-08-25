"""ETF 动态池筛选策略测试（不访问外网）。"""

from __future__ import annotations

import pandas as pd

from app.strategies.base import CrossSectionContext
from app.strategies.cross_section.etf_filter import (
    EtfFilterParams,
    infer_industry,
    select_etf_filter,
)
from app.strategies.registry import get_cross_section_strategy


def _value_df(
    dates: list[str],
    closes: list[float],
    volumes: list[float] | None = None,
    amounts: list[float] | None = None,
) -> pd.DataFrame:
    close = pd.Series(closes, dtype=float)
    d = {
        "date": dates,
        "close": close,
        "pct_change": close.pct_change().fillna(0.0) * 100.0,
    }
    if volumes is not None:
        d["volume"] = pd.Series(volumes, dtype=float)
    if amounts is not None:
        d["amount"] = pd.Series(amounts, dtype=float)
    return pd.DataFrame(d)


def test_registered():
    spec = get_cross_section_strategy("etf_filter")
    assert spec is not None
    assert spec.requires_symbols is False
    assert spec.default_universe == "etf"


def test_infer_industry():
    assert infer_industry("半导体ETF") == "半导体"
    assert infer_industry("医疗ETF") == "医药"
    assert infer_industry("银行ETF") == "金融"
    assert infer_industry("黄金ETF") == "贵金属"
    assert infer_industry("10年国债ETF") == "债券"
    assert infer_industry("纳指100ETF") == "海外"
    assert infer_industry("红利低波ETF") == "红利"
    assert infer_industry("沪深300ETF") == "宽基"
    assert infer_industry("创业板50ETF") == "宽基"
    assert infer_industry("随便什么") == "其他"
    assert infer_industry(None) == "其他"


def test_excludes_etf_not_listed_at_asof():
    """asof 之前无 K 线的标的（未上市）被剔除。"""
    dates = pd.bdate_range("2024-01-02", periods=10).strftime("%Y-%m-%d").tolist()
    late_dates = pd.bdate_range("2025-06-02", periods=10).strftime("%Y-%m-%d").tolist()
    panel = {
        # 全程有数据，asof=2024-01-15 时已上市
        "510300": {"value": _value_df(dates, [10 + i * 0.1 for i in range(10)])},
        # 所有 K 线都在 asof 之后，视为当时未上市
        "588000": {"value": _value_df(late_dates, [10 + i * 0.1 for i in range(10)])},
    }
    symbols, details = select_etf_filter(
        dates[-1],
        CrossSectionContext(panel=panel, names={"510300": "沪深300ETF", "588000": "科创50ETF"}),
        EtfFilterParams(momentum_days=5, min_momentum=0.0),
    )
    assert symbols == ["510300"]
    assert len(details) == 1


def test_min_amount_filters_low_amount_etf():
    """min_amount 按日均成交额过滤，成交额过低的被剔除。"""
    dates = pd.bdate_range("2024-01-02", periods=10).strftime("%Y-%m-%d").tolist()
    panel = {
        # 高成交额，动量正
        "510300": {
            "value": _value_df(
                dates, [10 + i * 0.1 for i in range(10)],
                amounts=[100_000_000] * 10,
            )
        },
        # 低成交额，动量更高——但应被 min_amount 过滤
        "512480": {
            "value": _value_df(
                dates, [10 + i * 0.2 for i in range(10)],
                amounts=[10_000] * 10,
            )
        },
    }
    symbols, _ = select_etf_filter(
        dates[-1],
        CrossSectionContext(panel=panel, names={"510300": "沪深300ETF", "512480": "半导体ETF"}),
        EtfFilterParams(momentum_days=5, min_amount=50_000_000),
    )
    assert symbols == ["510300"]


def test_min_volume_filters_low_volume_etf():
    """min_volume 按日均成交量过滤；amount 列缺省不影响 volume 过滤。"""
    dates = pd.bdate_range("2024-01-02", periods=10).strftime("%Y-%m-%d").tolist()
    panel = {
        "510300": {
            "value": _value_df(
                dates, [10 + i * 0.1 for i in range(10)], volumes=[1_000_000] * 10
            )
        },
        "512480": {
            "value": _value_df(
                dates, [10 + i * 0.2 for i in range(10)], volumes=[100] * 10
            )
        },
    }
    symbols, _ = select_etf_filter(
        dates[-1],
        CrossSectionContext(panel=panel, names={"510300": "沪深300ETF", "512480": "半导体ETF"}),
        EtfFilterParams(momentum_days=5, min_volume=500_000),
    )
    assert symbols == ["510300"]


def test_per_industry_cap_picks_one_each():
    """per_industry_top_k=1 时每行业最多选一只，按动量排序。"""
    dates = pd.bdate_range("2024-01-02", periods=10).strftime("%Y-%m-%d").tolist()
    panel = {
        "512480": {"value": _value_df(dates, [10 + i * 0.4 for i in range(10)])},  # 半导体 高动量
        "512800": {"value": _value_df(dates, [10 + i * 0.3 for i in range(10)])},  # 金融   高动量
        "512170": {"value": _value_df(dates, [10 + i * 0.2 for i in range(10)])},  # 医药   中动量
        "512880": {"value": _value_df(dates, [10 + i * 0.1 for i in range(10)])},  # 金融   低动量
    }
    names = {
        "512480": "半导体ETF",
        "512800": "银行ETF",
        "512170": "医疗ETF",
        "512880": "证券ETF",
    }
    symbols, details = select_etf_filter(
        dates[-1],
        CrossSectionContext(panel=panel, names=names),
        EtfFilterParams(momentum_days=5, top_n=3, per_industry_top_k=1),
    )
    # 金融行业只保留动量更高的银行ETF，证券ETF 被均衡挤掉
    assert symbols == ["512480", "512800", "512170"]
    industries = [d["industry"] for d in details]
    assert len(set(industries)) == 3


def test_without_industry_cap_is_global_top_n():
    """per_industry_top_k=0 时不均衡，纯全局动量排序取 top_n。"""
    dates = pd.bdate_range("2024-01-02", periods=10).strftime("%Y-%m-%d").tolist()
    panel = {
        "512480": {"value": _value_df(dates, [10 + i * 0.4 for i in range(10)])},  # 半导体
        "512800": {"value": _value_df(dates, [10 + i * 0.3 for i in range(10)])},  # 金融
        "512170": {"value": _value_df(dates, [10 + i * 0.2 for i in range(10)])},  # 医药
    }
    names = {"512480": "半导体ETF", "512800": "银行ETF", "512170": "医疗ETF"}
    symbols, _ = select_etf_filter(
        dates[-1],
        CrossSectionContext(panel=panel, names=names),
        EtfFilterParams(momentum_days=5, top_n=3, per_industry_top_k=0),
    )
    assert symbols == ["512480", "512800", "512170"]


def test_fetch_etf_universe_at(monkeypatch):
    """按 asof 过滤：K 线首根晚于 asof 的 ETF 被排除。"""
    import app.data_sources.market_data as md

    monkeypatch.setattr(
        md,
        "fetch_etf_universe",
        lambda _max_universe=500, seed=None: (
            [{"symbol": "510300", "name": "沪深300ETF"}, {"symbol": "588000", "name": "科创50ETF"}],
            "mock",
        ),
    )

    def fake_daily(symbol: str, **kwargs) -> pd.DataFrame:
        if symbol == "510300":
            idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
        else:  # 588000 首根 K 线在 asof 之后
            idx = pd.to_datetime(["2025-06-02", "2025-06-03"])
        return pd.DataFrame({"close": [10.0, 10.1]}, index=idx)

    monkeypatch.setattr(md, "fetch_a_share_daily", fake_daily)

    rows, note = md.fetch_etf_universe_at("2024-01-10", max_universe=500)
    assert [r["symbol"] for r in rows] == ["510300"]
    assert "已存在" in note


def test_config_pool_asof_filter(monkeypatch):
    """config ETF 池在带 asof 时按 K 线覆盖过滤出当时已存在的子集。"""
    import app.universe as uni

    rows = [{"symbol": "510300", "name": "沪深300ETF"}, {"symbol": "588000", "name": "科创50ETF"}]

    # mock filter_etf_symbols_at：588000 首根 K 线晚于 asof 被剔除
    monkeypatch.setattr(
        uni,
        "filter_etf_symbols_at",
        lambda _rows, _asof: ([{"symbol": "510300", "name": "沪深300ETF"}], ["588000"]),
    )

    existing, note = uni._maybe_asof_filter_config(rows, "使用 config 股票池 etf_core（2 只）。", "2024-01-01")
    assert [r["symbol"] for r in existing] == ["510300"]
    assert "已存在 1 只" in note
    assert "1 只 K 线首根晚于" in note

    # 无 asof 时原样返回，不做过滤
    unchanged, note2 = uni._maybe_asof_filter_config(rows, "note", None)
    assert unchanged == rows
    assert note2 == "note"


def test_config_pool_asof_empty_raises(monkeypatch):
    """asof 时点 config 池无可用标的时报 ValueError。"""
    import app.universe as uni

    rows = [{"symbol": "588000", "name": "科创50ETF"}]
    monkeypatch.setattr(uni, "filter_etf_symbols_at", lambda _rows, _asof: ([], ["588000"]))
    try:
        uni._maybe_asof_filter_config(rows, "note", "2024-01-01")
        assert False, "应当抛出 ValueError"
    except ValueError as e:
        assert "无可用标的" in str(e)
