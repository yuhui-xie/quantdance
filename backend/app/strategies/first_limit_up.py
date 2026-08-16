"""首板超短线策略：平台回调后的首板 → 次日开盘进场 → 1~5 日博反弹。

思路（据知乎"首板战法"）：
1. 首板：当日收盘涨停，且此前一段窗口内无涨停（非连板）；
2. 平台回调：首板前一段时间处于低振幅平台整理（回调蓄势）；
3. 一次性上穿几根均线：首板当根 K 线从均线下方一次性站上数根均线；
4. 均线平行或上行：短/中/长均线呈平行或向上；
5. 相对低位：股价在较长窗口内处于相对低位（前期没有大幅炒作）；
6. 次日开盘进场：不高开很多（gap up 受限）时按次日开盘价买入；
7. 离场：跌破首板日低点（起涨点）止损；日内峰值 trailing 回撤（近似分时
   调头/上涨乏力）；或 1~5 日持仓窗口结束强制平仓（5 天内不上涨即止损）。

本策略用**自定义 run 循环**而非 run_from_signals：进场需按次日开盘价成交、
离场需用 OHLC 判断，无法用"信号当日收盘成交"的向量化引擎表达。单票全仓，
一个持仓，平仓后可对下一个符合条件的首板再进场。

注：涨停用日涨跌幅阈值近似，非交易所正式涨停状态；"分时图调头"用日内
峰值 trailing 止损近似；无板块/概念数据，暂不支持热点叠加（可改为横截面
策略实现）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from app.backtest_engine import BacktestResult, metrics_from_equity
from app.strategies.base import BaseBacktestParams, StrategySpec


class FirstLimitUpParams(BaseModel):
    # --- 首板判定 ---
    limit_pct: float = Field(
        9.5, ge=1.0, le=30.0, description="涨停近似阈值（%），主板 10% 用 9.5"
    )
    board_lookback: int = Field(
        60, ge=10, le=250, description="首板判定：此前 N 交易日无涨停（排除连板/连涨）"
    )

    # --- 平台回调 ---
    platform_days: int = Field(20, ge=5, le=60, description="回调平台观察窗口（交易日）")
    platform_max_range: float = Field(
        0.15, ge=0.02, le=1.0, description="平台振幅上限 (high-low)/mean"
    )

    # --- 一次性上穿均线 + 均线平行/上行 ---
    ma_fast: int = Field(5, ge=2, le=60)
    ma_mid: int = Field(10, ge=3, le=120)
    ma_slow: int = Field(20, ge=5, le=250)
    min_crossed: int = Field(
        2, ge=1, le=3, description="首板当根 K 线一次性上穿的均线数下限"
    )
    ma_slope_lookback: int = Field(
        3, ge=1, le=20, description="均线平行/上行判断时回看的交易日数"
    )

    # --- 相对低位 / 前期未大幅炒作 ---
    position_lookback: int = Field(
        250, ge=40, le=500, description="相对低位观察窗口（交易日，近似年线位置）"
    )
    max_price_position: float = Field(
        0.5, ge=0.05, le=1.0, description="首板前股价在窗口高低点中的最高分位"
    )
    spec_lookback: int = Field(
        60, ge=20, le=250, description="前期炒作观察窗口（交易日）"
    )
    max_spec_ret: float = Field(
        0.30, ge=0.0, le=5.0, description="首板前窗口累计涨幅上限（前期未大幅炒作）"
    )

    # --- 次日进场 ---
    max_gap_up: float = Field(
        0.05, ge=0.0, le=0.20, description="次日开盘相对首板收盘的最大高开幅度（超过不追）"
    )

    # --- 离场 ---
    max_hold_days: int = Field(5, ge=1, le=20, description="持仓窗口上限（交易日）")
    min_profit: float = Field(
        0.0, ge=-1.0, le=2.0, description="时间止损：窗口内涨幅低于此值即按止损离场"
    )
    trailing_pct: float = Field(
        0.04, ge=0.0, le=1.0, description="上涨衰竭回撤阈值：收盘跌破日内峰值该幅度即离场"
    )

    @model_validator(mode="after")
    def check_periods(self) -> FirstLimitUpParams:
        if not (self.ma_fast < self.ma_mid < self.ma_slow):
            raise ValueError("须满足 ma_fast < ma_mid < ma_slow")
        return self


def _is_platform(closes: np.ndarray, days: int, max_range: float) -> bool:
    """近 ``days`` 日是否为低振幅平台整理。"""
    if len(closes) < days:
        return False
    chunk = closes[-days:]
    vals = chunk[np.isfinite(chunk)]
    if len(vals) < max(5, days // 2):
        return False
    mean = float(vals.mean())
    if mean <= 0:
        return False
    amplitude = (float(vals.max()) - float(vals.min())) / mean
    return amplitude <= max_range


def _price_position(closes: np.ndarray) -> float | None:
    """现价在窗口高低点中的分位 (last-lo)/(hi-lo)，∈[0,1]。"""
    vals = closes[np.isfinite(closes)]
    if len(vals) < 2:
        return None
    lo = float(vals.min())
    hi = float(vals.max())
    last = float(closes[-1])
    if not np.isfinite(last):
        return None
    if hi <= lo:
        return 0.5
    return (last - lo) / (hi - lo)


def _qualifying_boards(
    close: np.ndarray,
    pct: np.ndarray,
    params: FirstLimitUpParams,
) -> np.ndarray:
    """逐日判定是否为符合条件的首板日（仅用 ≤ 当日收盘的信息）。

    返回 bool 数组，True 表示该日为首板触发日（其后一个交易日为进场候选）。
    """
    n = len(close)
    limit_thresh = params.limit_pct / 100.0
    limit_up = np.isfinite(pct) & (pct >= limit_thresh)

    # 首板：此前 board_lookback 日无涨停
    board = limit_up.copy()
    for i in range(n):
        if not limit_up[i]:
            board[i] = False
            continue
        if i == 0:
            board[i] = False
            continue
        start = max(0, i - params.board_lookback)
        if bool(limit_up[start:i].any()):
            board[i] = False

    close_s = pd.Series(close)
    ma_f = close_s.rolling(params.ma_fast).mean().to_numpy()
    ma_m = close_s.rolling(params.ma_mid).mean().to_numpy()
    ma_s = close_s.rolling(params.ma_slow).mean().to_numpy()
    s = params.ma_slope_lookback
    need = max(
        params.ma_slow,
        params.platform_days,
        params.spec_lookback,
        params.position_lookback,
    )

    qual = np.zeros(n, dtype=bool)
    for i in range(n):
        if not board[i]:
            continue
        if i < need:
            continue

        # 平台回调：首板之前 platform_days 日低振幅
        if not _is_platform(
            close[i - params.platform_days : i],
            params.platform_days,
            params.platform_max_range,
        ):
            continue

        # 一次性上穿均线（用首板当根收盘，已知于当日收盘）
        if not (
            np.isfinite(ma_f[i])
            and np.isfinite(ma_m[i])
            and np.isfinite(ma_s[i])
        ):
            continue
        above = (
            close[i] > ma_f[i]
            and close[i] > ma_m[i]
            and close[i] > ma_s[i]
        )
        if not above:
            continue
        crossed = 0
        for ma in (ma_f, ma_m, ma_s):
            if close[i - 1] <= ma[i] < close[i]:
                crossed += 1
        if crossed < params.min_crossed:
            continue

        # 均线平行或上行
        if not (
            ma_f[i] >= ma_f[i - s]
            and ma_m[i] >= ma_m[i - s]
            and ma_s[i] >= ma_s[i - s]
        ):
            continue

        # 相对低位（首板前，不含首板当根）
        pos_closes = close[i - params.position_lookback : i]
        pp = _price_position(pos_closes)
        if pp is None or pp > params.max_price_position:
            continue

        # 前期未大幅炒作（首板前窗口累计涨幅）
        c0 = close[i - params.spec_lookback]
        c1 = close[i - 1]
        if not np.isfinite(c0) or not np.isfinite(c1) or c0 <= 0:
            continue
        spec_ret = c1 / c0 - 1.0
        if spec_ret > params.max_spec_ret:
            continue

        qual[i] = True
    return qual


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: FirstLimitUpParams,
) -> BacktestResult:
    close = df["close"].astype(float).to_numpy()
    open_a = df["open"].astype(float).to_numpy()
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    n = len(close)
    dates = [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in df.index]

    pct = np.full(n, np.nan, dtype=float)
    pct[1:] = close[1:] / close[:-1] - 1.0
    qual = _qualifying_boards(close, pct, params)

    close_s = pd.Series(close)
    ma_f = close_s.rolling(params.ma_fast).mean().to_numpy()
    ma_m = close_s.rolling(params.ma_mid).mean().to_numpy()
    ma_s = close_s.rolling(params.ma_slow).mean().to_numpy()

    cash = float(base.initial_cash)
    shares = 0.0
    entry_price = 0.0
    stop_price = 0.0
    peak = 0.0
    entry_i = -1
    equity_curve: list[float] = []
    trades: list[dict[str, float]] = []
    signal = np.zeros(n, dtype=np.int8)
    position = np.zeros(n, dtype=np.int8)

    for i in range(n):
        was_holding = shares > 0
        if was_holding:
            # 更新持仓期日内峰值，用于 trailing 止盈
            peak = max(peak, high[i])
            # 离场判定（优先级：止损 > 动量 trailing > 时间窗口）
            exit_now = False
            if low[i] <= stop_price:
                exit_now = True
            elif close[i] <= peak * (1.0 - params.trailing_pct):
                exit_now = True
            elif (i - entry_i) >= params.max_hold_days:
                exit_now = True
            if exit_now:
                proceeds = shares * close[i] * (1.0 - base.commission)
                trades.append(
                    {
                        "date": dates[i],
                        "side": "sell",
                        "price": float(close[i]),
                        "shares": float(shares),
                        "cash_after": float(proceeds),
                    }
                )
                cash = proceeds
                shares = 0.0
                signal[i] = -1

        if not was_holding:
            # 次日开盘进场：前一交易日为合格首板且未过度高开
            if i >= 1 and qual[i - 1] and open_a[i] <= close[i - 1] * (1.0 + params.max_gap_up):
                cost = cash * (1.0 - base.commission)
                new_shares = cost / open_a[i]
                trades.append(
                    {
                        "date": dates[i],
                        "side": "buy",
                        "price": float(open_a[i]),
                        "shares": float(new_shares),
                        "cash_after": 0.0,
                    }
                )
                shares = new_shares
                cash = 0.0
                entry_price = float(open_a[i])
                stop_price = float(low[i - 1])  # 首板日低点 = 起涨点
                peak = float(high[i])
                entry_i = i
                signal[i] = 1

        position[i] = 1 if shares > 0 else 0
        equity_curve.append(float(cash + shares * close[i]))

    eq_arr = np.array(equity_curve, dtype=float)
    metrics = metrics_from_equity(eq_arr, base.initial_cash, trades, dates)

    equity_out = [
        {"date": dates[i], "equity": float(equity_curve[i])} for i in range(n)
    ]
    price_out = [
        {
            "date": dates[i],
            "open": float(open_a[i]),
            "high": float(high[i]),
            "low": float(low[i]),
            "close": float(close[i]),
            "ma_fast": float(ma_f[i]) if np.isfinite(ma_f[i]) else None,
            "ma_mid": float(ma_m[i]) if np.isfinite(ma_m[i]) else None,
            "ma_slow": float(ma_s[i]) if np.isfinite(ma_s[i]) else None,
            "limit_up": int(qual[i]),
            "position": int(position[i]),
        }
        for i in range(n)
    ]

    return BacktestResult(
        equity=equity_out,
        trades=trades,
        metrics=metrics,
        price=price_out,
        signal=signal.tolist(),
    )


def _min_bars(params: FirstLimitUpParams) -> int:
    return (
        max(
            params.ma_slow,
            params.platform_days,
            params.spec_lookback,
            params.position_lookback,
            params.ma_slope_lookback,
        )
        + 1
    )


STRATEGY = StrategySpec(
    id="first_limit_up",
    name="首板超短线（次日开盘进）",
    description="平台回调后的首板：涨停首板 + 低振幅平台整理 + 一次上穿多均线 + 均线平行/上行 + 相对低位且前期未大涨；次日不高开很多时按开盘价进场，博 1~5 日反弹；跌破首板起涨点止损、日内峰值回撤或窗口到期离场。",
    params_model=FirstLimitUpParams,
    min_bars=_min_bars,
    run=_run,
)
