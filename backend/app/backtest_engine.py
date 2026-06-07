"""简易向量化回测：全仓买卖；策略只需提供信号与图表指标。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


@dataclass
class BacktestResult:
    equity: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    metrics: dict[str, float]
    price: list[dict[str, Any]]


def _max_drawdown(equity: np.ndarray) -> float:
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / np.where(peak > 0, peak, np.nan)
    return float(np.nanmax(dd))


def _sharpe(daily_returns: np.ndarray, risk_free: float = 0.0) -> float:
    excess = daily_returns - risk_free / 252
    std = float(np.std(excess, ddof=1))
    if std < 1e-12:
        return 0.0
    return float(np.sqrt(252) * np.mean(excess) / std)


def _nf(x: Any) -> float | None:
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(xf):
        return None
    return xf


def _price_row(
    date: str,
    close: float,
    *,
    open_: Any = None,
    high: Any = None,
    low: Any = None,
    overlays: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """通用 price 行结构；策略指标通过 overlays 扩展。"""
    row = {
        "date": date,
        "open": _nf(open_),
        "high": _nf(high),
        "low": _nf(low),
        "close": float(close),
    }
    if overlays:
        row.update({key: _nf(value) for key, value in overlays.items()})
    return row


def _run_full_position_backtest(
    close: np.ndarray,
    dates: list[str],
    signal: np.ndarray,
    initial_cash: float,
    commission: float,
) -> tuple[list[float], list[dict[str, Any]], np.ndarray]:
    cash = float(initial_cash)
    shares = 0.0
    equity_curve: list[float] = []
    trades: list[dict[str, Any]] = []

    for i in range(len(close)):
        price = close[i]
        if signal[i] == 1 and cash > 0:
            cost = cash * (1 - commission)
            new_shares = cost / price
            trades.append(
                {
                    "date": dates[i],
                    "side": "buy",
                    "price": float(price),
                    "shares": float(new_shares),
                    "cash_after": 0.0,
                }
            )
            shares = new_shares
            cash = 0.0
        elif signal[i] == -1 and shares > 0:
            proceeds = shares * price * (1 - commission)
            trades.append(
                {
                    "date": dates[i],
                    "side": "sell",
                    "price": float(price),
                    "shares": float(shares),
                    "cash_after": float(proceeds),
                }
            )
            cash = proceeds
            shares = 0.0

        eq = cash + shares * price
        equity_curve.append(eq)

    eq_arr = np.array(equity_curve, dtype=float)
    return equity_curve, trades, eq_arr


def _round_trip_pnls(trades: list[dict[str, Any]]) -> list[float]:
    pnls: list[float] = []
    open_buy: dict[str, Any] | None = None
    for trade in trades:
        side = str(trade.get("side", "")).lower()
        if side == "buy":
            open_buy = trade
            continue
        if side != "sell" or open_buy is None:
            continue

        shares = _nf(open_buy.get("shares")) or 0.0
        buy_price = _nf(open_buy.get("price")) or 0.0
        sell_cash = _nf(trade.get("cash_after"))
        if shares <= 0 or buy_price <= 0 or sell_cash is None:
            open_buy = None
            continue
        pnls.append(float(sell_cash - shares * buy_price))
        open_buy = None
    return pnls


def _annualized_return(eq_arr: np.ndarray, initial_cash: float, dates: Sequence[str]) -> float:
    if initial_cash <= 0 or len(eq_arr) == 0:
        return 0.0
    total = float(eq_arr[-1] / initial_cash)
    if total <= 0:
        return -1.0

    years = 0.0
    if len(dates) >= 2:
        parsed = pd.to_datetime(list(dates), errors="coerce")
        if not parsed.isna().any():
            days = max((parsed[-1] - parsed[0]).days, 1)
            years = days / 365.25
    if years <= 0:
        years = max((len(eq_arr) - 1) / 252.0, 1 / 252.0)
    return float(total ** (1.0 / years) - 1.0)


def _metrics_from_equity(
    eq_arr: np.ndarray,
    initial_cash: float,
    trades: list[dict[str, Any]],
    dates: Sequence[str],
) -> dict[str, float]:
    ret = np.diff(eq_arr) / np.where(eq_arr[:-1] > 0, eq_arr[:-1], np.nan)
    ret = ret[np.isfinite(ret)]
    total_return = (eq_arr[-1] - initial_cash) / initial_cash if initial_cash > 0 else 0.0
    max_drawdown = _max_drawdown(eq_arr)
    pnls = _round_trip_pnls(trades)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = float(sum(wins))
    gross_loss = float(abs(sum(losses)))
    closed_trades = len(pnls)
    profit_factor = gross_profit / gross_loss if gross_loss > 1e-12 else (gross_profit if gross_profit > 0 else 0.0)
    return {
        "initial_cash": float(initial_cash),
        "final_equity": float(eq_arr[-1]),
        "total_return": float(total_return),
        "annualized_return": _annualized_return(eq_arr, initial_cash, dates),
        "max_drawdown": max_drawdown,
        "sharpe": _sharpe(ret) if len(ret) > 1 else 0.0,
        "num_trades": float(len(trades)),
        "closed_trades": float(closed_trades),
        "win_rate": float(len(wins) / closed_trades) if closed_trades else 0.0,
        "profit_factor": float(profit_factor),
        "avg_win": float(np.mean(wins)) if wins else 0.0,
        "avg_loss": float(np.mean(losses)) if losses else 0.0,
        "return_drawdown_ratio": float(total_return / max_drawdown) if max_drawdown > 1e-12 else 0.0,
    }


def run_from_signals(
    df: pd.DataFrame,
    signal: Sequence[int] | np.ndarray,
    initial_cash: float,
    commission: float = 0.0003,
    overlays: Mapping[str, Sequence[Any] | np.ndarray] | None = None,
) -> BacktestResult:
    """用策略生成的买卖信号执行通用全仓回测。"""
    if not {"open", "high", "low", "close", "volume"}.issubset(df.columns):
        raise ValueError("OHLCV 列不完整")

    close = df["close"].astype(float).values
    open_a = df["open"].astype(float).values
    high_a = df["high"].astype(float).values
    low_a = df["low"].astype(float).values
    dates = [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in df.index]

    sig = np.asarray(signal, dtype=np.int8)
    if len(sig) != len(close):
        raise ValueError("signal 长度必须与行情数据一致")

    overlay_arrays: dict[str, np.ndarray] = {}
    for key, values in (overlays or {}).items():
        arr = np.asarray(values)
        if len(arr) != len(close):
            raise ValueError(f"overlay {key} 长度必须与行情数据一致")
        overlay_arrays[key] = arr

    equity_curve, trades, eq_arr = _run_full_position_backtest(
        close, dates, sig, initial_cash, commission
    )
    metrics = _metrics_from_equity(eq_arr, initial_cash, trades, dates)

    equity_out = [
        {"date": dates[i], "equity": float(equity_curve[i])} for i in range(len(dates))
    ]
    price_out = [
        _price_row(
            dates[i],
            float(close[i]),
            open_=open_a[i],
            high=high_a[i],
            low=low_a[i],
            overlays={key: values[i] for key, values in overlay_arrays.items()},
        )
        for i in range(len(dates))
    ]

    return BacktestResult(
        equity=equity_out,
        trades=trades,
        metrics=metrics,
        price=price_out,
    )
