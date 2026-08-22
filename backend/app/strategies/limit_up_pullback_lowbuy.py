"""涨停回调低吸策略：近 N 日涨停 → 回调回踩 MA30 极度缩量十字星 → 次日放量收阳买点。

思路（短线回调低吸）：
1. 近 ``limit_lookback`` 日有过涨停（动量与炒作热度）；
2. 随后回调回踩重要均线 MA``ma_period``（默认 30）获得支撑：日内最低价触及
   均线附近且收盘仍站在均线上方，且处于近期回调低位而非高位；
3. 出现关键趋势逆转 K 线：**极度缩量十字星**回踩 MA30（实体极小、量能远低于
   近期均值，多空在该位置达成平衡，是潜在的止跌转折点）；
4. 次日若**放量收阳**（量能显著放大、收阳且高于昨日收盘）即为买点，按当日
   收盘价全仓买入，博 3~5 日反弹（目标 5%~25%）；
5. 离场：跌破成本 -``stop_loss_pct`` 止损，或**下一次放量日**收盘落袋
   （量能释放常伴随冲高），或以 ``max_hold_days`` 时间窗口兜底强制平仓。

本策略用自定义 run 循环（同 first_limit_up）：买点需按确认日收盘价成交、离场
需用 OHLC 判断，无法用「信号当日收盘成交」的向量化引擎表达。单票全仓、单持仓，
平仓后可对下一个符合条件的买点再进场。

注：涨停用日涨跌幅阈值近似，非交易所正式涨停状态；放量/缩量用成交量相对其
短期均线的倍数近似。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from app.backtest_engine import BacktestResult, metrics_from_equity
from app.strategies.base import BaseBacktestParams, StrategySpec


class LimitUpPullbackLowbuyParams(BaseModel):
    # --- 涨停判定 ---
    limit_pct: float = Field(
        9.5, ge=1.0, le=30.0, description="涨停近似阈值（%），主板 10% 用 9.5"
    )
    limit_lookback: int = Field(
        15, ge=3, le=60, description="近 N 交易日有涨停的观察窗口"
    )

    # --- 回踩均线 ---
    ma_period: int = Field(
        30, ge=5, le=250, description="回踩的重要均线周期（默认 MA30）"
    )
    support_tolerance: float = Field(
        0.02, ge=0.0, le=0.10, description="low 触及 MA 上方容差比例（回踩判定）"
    )

    # --- 缩量十字星（趋势逆转 K 线）---
    doji_body_ratio: float = Field(
        0.30, ge=0.0, le=1.0, description="十字星：|close-open| ≤ ratio×(high-low)"
    )
    low_vol_ratio: float = Field(
        0.65, ge=0.0, le=1.0, description="极度缩量：volume ≤ ratio×MAvol"
    )

    # --- 量能基准（买点放量与离场放量共用）---
    confirm_vol_ratio: float = Field(
        1.20, ge=1.0, le=10.0, description="放量：volume ≥ ratio×MAvol（买点与离场均用）"
    )
    vol_ma_period: int = Field(
        5, ge=2, le=60, description="量能基准 MA 周期"
    )

    # --- 离场 ---
    stop_loss_pct: float = Field(
        0.05, ge=0.0, le=1.0, description="止损：跌破成本该比例即离场（默认 -5%）"
    )
    max_hold_days: int = Field(
        5, ge=1, le=30, description="持仓时间上限（交易日），到点强制平仓兜底"
    )


def _qualifying_setup(
    close: np.ndarray,
    open_a: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    volume: np.ndarray,
    pct: np.ndarray,
    ma: np.ndarray,
    vol_ma: np.ndarray,
    params: LimitUpPullbackLowbuyParams,
) -> np.ndarray:
    """逐日判定是否为「涨停回调 + 缩量十字星回踩均线」设置日 T。

    返回 bool 数组，True 表示该日为设置日（其后一个交易日为放量收阳买点候选）。
    仅使用 ≤ 当日收盘的信息。
    """
    n = len(close)
    limit_thresh = params.limit_pct / 100.0
    limit_up = np.isfinite(pct) & (pct >= limit_thresh)

    qual = np.zeros(n, dtype=bool)
    for i in range(n):
        if i < params.ma_period or i < params.vol_ma_period:
            continue
        if i == 0:
            continue
        ma_i = ma[i]
        if not np.isfinite(ma_i) or ma_i <= 0:
            continue
        vol_ma_i = vol_ma[i]
        if not np.isfinite(vol_ma_i) or vol_ma_i <= 0:
            continue

        # 近 limit_lookback 日有涨停（含当日；十字星日通常非涨停，落在窗口内即可）
        start = max(0, i - params.limit_lookback)
        if not bool(limit_up[start : i + 1].any()):
            continue

        # 回调回踩均线获得支撑：low 触及 MA 附近且收盘仍在其上方
        if not (low[i] <= ma_i * (1.0 + params.support_tolerance) and close[i] >= ma_i):
            continue
        # 确认是回调而非高位：现价低于近 limit_lookback 日盘中峰值
        peak = float(np.nanmax(high[max(0, i - params.limit_lookback) : i]))
        if not np.isfinite(peak) or close[i] >= peak:
            continue

        # 缩量十字星
        rng = high[i] - low[i]
        if rng <= 0:
            continue
        body = abs(close[i] - open_a[i])
        if body > params.doji_body_ratio * rng:
            continue
        if volume[i] > params.low_vol_ratio * vol_ma_i:
            continue

        qual[i] = True
    return qual


def _is_volume_expansion(
    volume: np.ndarray,
    vol_ma: np.ndarray,
    i: int,
    params: LimitUpPullbackLowbuyParams,
) -> bool:
    if i < 0 or i >= len(volume):
        return False
    v = vol_ma[i]
    if not np.isfinite(v) or v <= 0:
        return False
    return bool(np.isfinite(volume[i]) and volume[i] >= params.confirm_vol_ratio * v)


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: LimitUpPullbackLowbuyParams,
) -> BacktestResult:
    close = df["close"].astype(float).to_numpy()
    open_a = df["open"].astype(float).to_numpy()
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(float).to_numpy()
    n = len(close)
    dates = [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in df.index]

    pct = np.full(n, np.nan, dtype=float)
    pct[1:] = close[1:] / close[:-1] - 1.0

    close_s = pd.Series(close)
    ma = close_s.rolling(params.ma_period).mean().to_numpy()
    vol_ma = pd.Series(volume).rolling(params.vol_ma_period).mean().to_numpy()
    qual = _qualifying_setup(close, open_a, high, low, volume, pct, ma, vol_ma, params)

    cash = float(base.initial_cash)
    shares = 0.0
    entry_price = 0.0
    entry_i = -1
    equity_curve: list[float] = []
    trades: list[dict[str, float]] = []
    signal = np.zeros(n, dtype=np.int8)
    position = np.zeros(n, dtype=np.int8)

    for i in range(n):
        was_holding = shares > 0
        if was_holding:
            # 离场（从进场次日起检查，避免买点当日立即触发「放量落袋」）
            exit_now = False
            if low[i] <= entry_price * (1.0 - params.stop_loss_pct):
                exit_now = True
            elif _is_volume_expansion(volume, vol_ma, i, params):
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
            # 买点：昨日为设置日且今日放量收阳 → 今日收盘价买入
            if (
                i >= 1
                and qual[i - 1]
                and _is_volume_expansion(volume, vol_ma, i, params)
                and close[i] > open_a[i]
                and close[i] > close[i - 1]
            ):
                cost = cash * (1.0 - base.commission)
                new_shares = cost / close[i]
                trades.append(
                    {
                        "date": dates[i],
                        "side": "buy",
                        "price": float(close[i]),
                        "shares": float(new_shares),
                        "cash_after": 0.0,
                    }
                )
                shares = new_shares
                cash = 0.0
                entry_price = float(close[i])
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
            "ma": float(ma[i]) if np.isfinite(ma[i]) else None,
            "vol_ma": float(vol_ma[i]) if np.isfinite(vol_ma[i]) else None,
            "setup": int(qual[i]),
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


def _min_bars(params: LimitUpPullbackLowbuyParams) -> int:
    return max(params.ma_period, params.limit_lookback, params.vol_ma_period) + 2


STRATEGY = StrategySpec(
    id="limit_up_pullback_lowbuy",
    name="涨停回调低吸",
    description="近15日有涨停后回调回踩MA30，出现极度缩量十字星（趋势逆转K线），次日放量收阳为买点按收盘价买入；跌破成本-5%止损，或下一次放量日收盘落袋，或持仓满max_hold_days天强制平仓，博3~5日反弹（目标5%~25%）。",
    params_model=LimitUpPullbackLowbuyParams,
    min_bars=_min_bars,
    run=_run,
)
