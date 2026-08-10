"""筹码分布指标与含换手率日线数据源测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.data_sources.market_data import MarketDataError, fetch_a_share_daily_turnover
from app.data_sources.tencent_finance_sdk import TencentFinanceError
from app.indicators import chip_cost_distribution


def _make_df(
    *,
    highs: list[float],
    lows: list[float],
    closes: list[float],
    turnovers: list[float],
) -> pd.DataFrame:
    """构造用于筹码分布的日线 DataFrame（DatetimeIndex，时间升序）。"""
    n = len(closes)
    index = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000.0] * n,
            "turnover_rate": turnovers,
        },
        index=index,
    )


def test_chip_distribution_basic_invariants():
    rng = np.random.default_rng(42)
    n = 120
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    high = close + 0.3
    low = close - 0.3
    turnovers = np.clip(rng.uniform(0.01, 0.05, n), 0.01, None)

    result = chip_cost_distribution(
        _make_df(highs=high, lows=low, closes=close, turnovers=turnovers),
        include_distribution=True,
    )

    dist = np.asarray(result["distribution"])
    assert dist.sum() == pytest.approx(1.0, abs=1e-6)
    assert np.isfinite(dist).all()
    assert 0.0 <= result["profit_ratio"] <= 1.0
    assert np.min(low) <= result["average_cost"] <= np.max(high)
    assert np.min(low) <= result["peak_price"] <= np.max(high)
    assert 0.0 <= result["concentration_90"] < 1.0
    assert 0.0 <= result["concentration_70"] < 1.0
    assert result["interval_90_low"] <= result["interval_90_high"]
    assert result["interval_70_low"] <= result["interval_70_high"]
    assert result["concentration_70"] <= result["concentration_90"]


def test_chip_distribution_all_profit_when_close_at_high():
    # 最新收盘价等于全期最高价：所有筹码成本低于现价，获利盘应接近 1。
    df = _make_df(
        highs=[10.2, 10.3],
        lows=[10.0, 10.1],
        closes=[10.1, 10.3],
        turnovers=[0.05, 0.05],
    )
    result = chip_cost_distribution(df)
    assert result["profit_ratio"] > 0.99


def test_chip_distribution_all_loss_when_close_at_low():
    # 最新收盘价等于全期最低价：筹码成本均不低于现价，获利盘应接近 0。
    df = _make_df(
        highs=[10.2, 10.2],
        lows=[10.0, 10.0],
        closes=[10.1, 10.0],
        turnovers=[0.05, 0.05],
    )
    result = chip_cost_distribution(df)
    assert result["profit_ratio"] < 0.01


def test_chip_distribution_hand_calculated_triangle():
    # 两日均换手率 100%：首日筹码全部衰减，最终分布即第二日 [10,12] 的三角分布，
    # 峰顶在均价 11。对称三角的均值/峰位为 11，获利盘（成本 <=11）为 0.5。
    df = _make_df(
        highs=[12.0, 12.0],
        lows=[10.0, 10.0],
        closes=[11.0, 11.0],
        turnovers=[1.0, 1.0],
    )
    result = chip_cost_distribution(df, bins=2000)
    assert result["profit_ratio"] == pytest.approx(0.5, abs=0.02)
    assert result["average_cost"] == pytest.approx(11.0, abs=0.05)
    assert result["peak_price"] == pytest.approx(11.0, abs=0.05)


def test_chip_distribution_all_zero_turnover_warns():
    df = _make_df(
        highs=[10.2, 10.3],
        lows=[10.0, 10.1],
        closes=[10.1, 10.2],
        turnovers=[0.0, 0.0],
    )
    result = chip_cost_distribution(df)
    assert "warning" in result
    assert "换手率" in result["warning"]


def test_chip_distribution_flat_price_day_does_not_crash():
    # 一字板：low == high，日价格区间内没有格点，应回退到最近格点。
    df = _make_df(
        highs=[10.0, 10.0],
        lows=[10.0, 10.0],
        closes=[10.0, 10.0],
        turnovers=[0.5, 0.5],
    )
    result = chip_cost_distribution(df, include_distribution=True)
    assert result["profit_ratio"] == pytest.approx(1.0, abs=0.02)
    assert np.sum(result["distribution"]) == pytest.approx(1.0, abs=1e-6)


def test_chip_distribution_include_distribution_returns_arrays():
    df = _make_df(
        highs=[10.2, 10.3],
        lows=[10.0, 10.1],
        closes=[10.1, 10.2],
        turnovers=[0.05, 0.05],
    )
    result = chip_cost_distribution(df, bins=50, include_distribution=True)
    assert len(result["price_grid"]) == 50
    assert len(result["distribution"]) == 50
    assert all(np.isfinite(result["price_grid"]))


def test_chip_distribution_insufficient_data_raises():
    df = _make_df(highs=[10.0], lows=[10.0], closes=[10.0], turnovers=[0.05])
    with pytest.raises(ValueError, match="至少需要2条K线"):
        chip_cost_distribution(df)


def test_chip_distribution_missing_column_raises():
    df = _make_df(
        highs=[10.0, 10.1],
        lows=[9.8, 9.9],
        closes=[10.0, 10.1],
        turnovers=[0.05, 0.05],
    ).drop(columns=["turnover_rate"])
    with pytest.raises(ValueError, match="缺少列"):
        chip_cost_distribution(df)


def test_chip_distribution_nan_raises():
    df = _make_df(
        highs=[10.0, np.nan],
        lows=[9.8, 9.9],
        closes=[10.0, 10.1],
        turnovers=[0.05, 0.05],
    )
    with pytest.raises(ValueError, match="无效值"):
        chip_cost_distribution(df)


def test_chip_distribution_rejects_bad_bins():
    df = _make_df(
        highs=[10.0, 10.1],
        lows=[9.8, 9.9],
        closes=[10.0, 10.1],
        turnovers=[0.05, 0.05],
    )
    with pytest.raises(ValueError, match="bins"):
        chip_cost_distribution(df, bins=1)


def _fake_tencent_bars(n_rows: int = 60) -> list[dict[str, object]]:
    """构造腾讯 newfqkline bar 列表（升序），换手率为小数、成交额为元。"""
    dates = pd.date_range("2024-01-02", periods=n_rows, freq="B")
    return [
        {
            "date": ts.strftime("%Y-%m-%d"),
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "volume": 10000,
            "amount": 1_010_000.0,
            "turnover_rate": 0.01,
        }
        for ts in dates
    ]


class _FakeTencentSDK:
    """mock TencentFinanceSDK：get_kline_with_turnover 取 date<=end 的最近 count 条（升序）。"""

    def __init__(self, bars: list[dict[str, object]], page_size: int = 640) -> None:
        self._bars = bars
        self.calls: list[tuple[str | None, int]] = []

    def get_kline_with_turnover(self, symbol, *, count=640, end=None):  # noqa: ANN001
        self.calls.append((end, count))
        sub = [b for b in self._bars if end is None or b["date"] <= end]
        return sub[-count:]


def test_fetch_a_share_daily_turnover_uses_tencent_turnover(monkeypatch):
    fake = _FakeTencentSDK(_fake_tencent_bars(60))
    monkeypatch.setattr("app.data_sources.market_data.TencentFinanceSDK", lambda: fake)

    out = fetch_a_share_daily_turnover("000001.SZ", limit=50)

    assert list(out.columns) == [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turnover_rate",
    ]
    assert isinstance(out.index, pd.DatetimeIndex)
    assert len(out) == 50  # limit 截断
    # 腾讯返回的小数换手率与元成交额原样保留
    assert out["turnover_rate"].iloc[0] == pytest.approx(0.01)
    assert out["amount"].iloc[0] == pytest.approx(1_010_000.0)


def test_fetch_a_share_daily_turnover_pages_for_large_limit(monkeypatch):
    fake = _FakeTencentSDK(_fake_tencent_bars(700))
    monkeypatch.setattr("app.data_sources.market_data.TencentFinanceSDK", lambda: fake)

    out = fetch_a_share_daily_turnover("000001", limit=700)

    assert len(out) == 700
    assert len(fake.calls) == 2  # 超过单页 640 上限自动翻页
    # 两页按时间先后拼接，跨页边界日期去重后仍为连续升序
    assert out.index.is_monotonic_increasing


def test_fetch_a_share_daily_turnover_date_range(monkeypatch):
    fake = _FakeTencentSDK(_fake_tencent_bars(60))
    monkeypatch.setattr("app.data_sources.market_data.TencentFinanceSDK", lambda: fake)

    out = fetch_a_share_daily_turnover(
        "000001",
        start="2024-01-02",
        end="2024-01-10",
    )
    assert str(out.index[0])[:10] == "2024-01-02"
    assert len(out) <= 7


def test_fetch_a_share_daily_turnover_empty_raises(monkeypatch):
    fake = _FakeTencentSDK([])
    monkeypatch.setattr("app.data_sources.market_data.TencentFinanceSDK", lambda: fake)
    with pytest.raises(MarketDataError, match="未获取到"):
        fetch_a_share_daily_turnover("000001", limit=100)


def test_fetch_a_share_daily_turnover_tencent_failure_raises(monkeypatch):
    class _BrokenTencent:
        def get_kline_with_turnover(self, symbol, *, count=640, end=None):  # noqa: ANN001
            raise TencentFinanceError("http_error", "network down", symbol=symbol)

    monkeypatch.setattr("app.data_sources.market_data.TencentFinanceSDK", lambda: _BrokenTencent())
    with pytest.raises(MarketDataError, match="腾讯日线获取失败"):
        fetch_a_share_daily_turnover("000001", limit=100)


def test_fetch_a_share_daily_turnover_requires_range_or_limit():
    with pytest.raises(ValueError, match="请同时提供"):
        fetch_a_share_daily_turnover("000001")


def test_fetch_a_share_daily_turnover_rejects_small_limit():
    with pytest.raises(ValueError, match="limit"):
        fetch_a_share_daily_turnover("000001", limit=10)
