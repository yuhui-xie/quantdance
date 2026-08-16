"""first_limit_up 首板超短线策略测试（内存构造行情，不访问网络）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategies.first_limit_up import (
    FirstLimitUpParams,
    STRATEGY,
    _is_platform,
    _price_position,
    _qualifying_boards,
)


def _params(**overrides) -> FirstLimitUpParams:
    defaults = dict(
        limit_pct=9.5,
        board_lookback=20,
        platform_days=10,
        platform_max_range=0.15,
        ma_fast=5,
        ma_mid=10,
        ma_slow=20,
        min_crossed=2,
        ma_slope_lookback=3,
        position_lookback=40,
        max_price_position=0.5,
        spec_lookback=20,
        max_spec_ret=0.30,
        max_gap_up=0.05,
        max_hold_days=5,
        min_profit=0.0,
        trailing_pct=0.04,
    )
    defaults.update(overrides)
    return FirstLimitUpParams(**defaults)


def _df(open_, high, low, close) -> pd.DataFrame:
    n = len(close)
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": np.ones(n)},
        index=idx,
    )


def _base_arrays() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    """构造「前期高位 → 回调 → 平台整理 → 首板涨停」序列，T=41 为合格首板日。

    返回 (open, high, low, close, T)。
    """
    closes = []
    # 0..15 从 12.0 缓跌到 10.5（前期高位后的回调）
    for k in range(16):
        closes.append(12.0 - k * 0.1)
    # 16..40 平台整理（振幅小）
    for j in range(25):
        closes.append(10.0 + 0.1 * np.sin(j))
    # 41 首板涨停（相对前收 +10%）
    closes.append(closes[-1] * 1.10)
    close = np.array(closes, dtype=float)
    n = len(close)
    open_ = close.copy()
    high = close + 0.4
    low = close - 0.4
    return open_, high, low, close, 41


def _append(df_arrays: tuple, ohlc_list: list[tuple[float, float, float, float]]):
    """在基础序列后追加若干 (open, high, low, close) 交易日，返回合并数组。"""
    o, h, l, c, T = df_arrays
    o2 = np.concatenate([o, [t[0] for t in ohlc_list]])
    h2 = np.concatenate([h, [t[1] for t in ohlc_list]])
    l2 = np.concatenate([l, [t[2] for t in ohlc_list]])
    c2 = np.concatenate([c, [t[3] for t in ohlc_list]])
    return o2, h2, l2, c2


def test_base_series_qualifies_as_first_board():
    _o, _h, _l, close, T = _base_arrays()
    pct = np.full(len(close), np.nan)
    pct[1:] = close[1:] / close[:-1] - 1.0
    qual = _qualifying_boards(close, pct, _params())
    assert bool(qual[T])
    assert int(qual[:T].sum()) == 0  # 首板之前没有其它合格触发


def test_profitable_trailing_exit():
    # 次日开盘(11.1)不高开进场，随后冲高，收盘跌破日内峰值回撤 → 止盈离场
    base = _base_arrays()
    o, h, l, c = _append(
        base,
        [
            (11.1, 11.5, 11.0, 11.4),  # 42 进场日（开盘价买入）
            (11.4, 12.2, 11.3, 12.0),  # 43 冲高，peak=12.2
            (12.0, 12.1, 11.2, 11.4),  # 44 收盘 11.4 <= 12.2*0.96 → 止盈
        ],
    )
    df = _df(o, h, l, c)
    result = STRATEGY.run(df, _base_cash(), _params())

    buys = [t for t in result.trades if t["side"] == "buy"]
    sells = [t for t in result.trades if t["side"] == "sell"]
    assert len(buys) == 1 and len(sells) == 1
    assert buys[0]["price"] == 11.1  # 按次日开盘价买入
    assert sells[0]["price"] == 11.4
    assert sells[0]["date"] == df.index[44].isoformat()
    assert result.metrics["final_equity"] > result.metrics["initial_cash"]


def test_stop_loss_below_start_point():
    # 首板日 low[41] 为起涨点（10.6），次日回跌破该位 → 止损
    _o, _h, _l, close, T = _base_arrays()
    base = (_o, _h, _l, close, T)
    o, h, l, c = _append(
        base,
        [
            (11.1, 11.5, 11.0, 11.4),  # 42 进场
            (11.4, 11.5, 10.5, 11.0),  # 43 low=10.5 <= 起涨点 10.6 → 止损
        ],
    )
    # 确保 base 首板日 low 就是起涨点
    l = l.copy()
    l[41] = 10.6
    df = _df(o, h, l, c)
    result = STRATEGY.run(df, _base_cash(), _params())

    sells = [t for t in result.trades if t["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["date"] == df.index[43].isoformat()
    assert result.metrics["final_equity"] < result.metrics["initial_cash"]


def test_time_stop_exit_at_hold_days():
    # 进场后小幅波动不触发 trailing，max_hold_days=5 → 第 5 个交易日强制平仓
    base = _base_arrays()
    o, h, l, c = _append(
        base,
        [
            (11.1, 11.5, 11.0, 11.5),  # 42 进场
            (11.5, 11.7, 11.4, 11.6),  # 43
            (11.6, 11.7, 11.5, 11.55),  # 44
            (11.55, 11.7, 11.5, 11.6),  # 45
            (11.6, 11.7, 11.5, 11.55),  # 46
            (11.55, 11.6, 11.4, 11.5),  # 47 = 42+5 → 时间止损离场
        ],
    )
    df = _df(o, h, l, c)
    result = STRATEGY.run(df, _base_cash(), _params())

    sells = [t for t in result.trades if t["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["date"] == df.index[47].isoformat()


def test_no_qualifying_event_is_flat():
    # 全程无 9.5% 涨停日 → 无交易，净值走平
    close = 10.0 + 0.3 * np.sin(np.linspace(0, 12, 80))
    open_ = close.copy()
    high = close + 0.3
    low = close - 0.3
    df = _df(open_, high, low, close)
    result = STRATEGY.run(df, _base_cash(), _params())

    assert result.metrics["num_trades"] == 0
    assert result.metrics["final_equity"] == result.metrics["initial_cash"]


def test_over_gap_up_skips_entry():
    # 次日开盘高开超过 max_gap_up → 不进场（无前视、不追高）
    base = _base_arrays()
    o, h, l, c = _append(
        base,
        [
            (11.6, 11.8, 11.5, 11.7),  # 高开 5.5% > 5% → 不进场
            (11.7, 11.9, 11.6, 11.8),
        ],
    )
    df = _df(o, h, l, c)
    result = STRATEGY.run(df, _base_cash(), _params())
    assert result.metrics["num_trades"] == 0


def test_min_bars():
    assert STRATEGY.min_bars(_params()) == 41  # max(20,10,20,40,3)+1
    assert STRATEGY.min_bars(_params(position_lookback=250)) == 251


def test_helpers():
    assert _price_position(np.array([10.0, 11.0, 12.0])) == 1.0
    assert _price_position(np.array([12.0, 11.0, 10.0])) == 0.0
    # 平台：近 10 日振幅小
    flat = 10.0 + 0.05 * np.sin(np.arange(10))
    assert _is_platform(flat, 10, 0.15)
    assert not _is_platform(np.linspace(10, 13, 10), 10, 0.15)


def _base_cash() -> "object":
    from app.strategies.base import BaseBacktestParams

    return BaseBacktestParams(initial_cash=100000, commission=0.0003)
