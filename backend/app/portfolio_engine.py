"""多标的等权组合回测：按调仓日目标持仓再平衡。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from app.backtest_engine import metrics_from_equity

Selector = Callable[[str, Mapping[str, Any]], list[str]]


@dataclass
class PortfolioBacktestResult:
    equity: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    holdings: list[dict[str, Any]]
    metrics: dict[str, float]
    rebalances: list[dict[str, Any]]


def every_n_trading_days(calendar: Sequence[str], n: int) -> list[str]:
    """
    从交易日序列中每隔 n 个交易日取一个调仓日（含首日）。

    例如 n=20 约等于月频；n=5 约等于周频。
    """
    if n < 1:
        raise ValueError("调仓间隔 n 至少为 1 个交易日")
    days = sorted({str(d)[:10] for d in calendar if str(d).strip()})
    if not days:
        return []
    return days[::n]


def _lot_shares(cash: float, price: float, *, lot_size: int = 100) -> int:
    if cash <= 0 or price <= 0 or lot_size <= 0:
        return 0
    raw = int(cash // (price * lot_size)) * lot_size
    return max(raw, 0)


def _trade_cost(
    notional: float,
    *,
    commission: float,
    min_commission: float,
    slippage: float,
    side: str,
) -> float:
    """返回含滑点与佣金后的现金变动绝对值方向由调用方处理。"""
    slip = abs(notional) * max(slippage, 0.0)
    fee = abs(notional) * max(commission, 0.0)
    if min_commission > 0:
        fee = max(fee, min_commission)
    return slip + fee


def build_close_panel(
    series_by_symbol: Mapping[str, pd.Series | pd.DataFrame],
) -> pd.DataFrame:
    """
    构建收盘价宽表：index=YYYY-MM-DD，columns=symbol。
    接受 Series(close) 或 DataFrame(含 close/date 列)。
    """
    frames: dict[str, pd.Series] = {}
    for symbol, obj in series_by_symbol.items():
        if isinstance(obj, pd.Series):
            s = obj.copy()
            s.index = pd.to_datetime(s.index, errors="coerce").strftime("%Y-%m-%d")
            frames[symbol] = pd.to_numeric(s, errors="coerce")
            continue
        if not isinstance(obj, pd.DataFrame) or obj.empty:
            continue
        df = obj
        if "close" not in df.columns:
            continue
        if "date" in df.columns:
            idx = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            frames[symbol] = pd.Series(
                pd.to_numeric(df["close"], errors="coerce").values,
                index=idx,
            )
        else:
            s = pd.to_numeric(df["close"], errors="coerce")
            s.index = pd.to_datetime(df.index, errors="coerce").strftime("%Y-%m-%d")
            frames[symbol] = s

    if not frames:
        raise ValueError("收盘价面板为空")
    panel = pd.DataFrame(frames).sort_index()
    panel = panel[~panel.index.isna()]
    return panel


def run_equal_weight_rebalance(
    close_panel: pd.DataFrame,
    *,
    rebalance_dates: Sequence[str],
    select_holdings: Selector,
    initial_cash: float,
    commission: float = 0.0003,
    min_commission: float = 5.0,
    slippage: float = 0.0,
    lot_size: int = 100,
    context: Mapping[str, Any] | None = None,
    take_profit_arm_pct: float | None = None,
    take_profit_exit_pct: float | None = None,
    stop_loss_pct: float | None = None,
) -> PortfolioBacktestResult:
    """
    等权再平衡组合回测。

    select_holdings(asof, context) -> 目标股票列表（等权）。
    成交价按调仓日收盘价，并叠加 slippage；卖出后再买入。

    通用风控（可选，仅非调仓日）：
    - 止损：相对成本浮亏达到 stop_loss_pct 则卖出；
    - 止盈：浮盈达到 take_profit_arm_pct(x) 后启动，回落到 take_profit_exit_pct(y) 再卖。
    """
    if close_panel.empty:
        raise ValueError("close_panel 为空")
    if initial_cash <= 0:
        raise ValueError("initial_cash 必须为正")

    arm = take_profit_arm_pct
    exit_lvl = take_profit_exit_pct
    if (arm is None) ^ (exit_lvl is None):
        raise ValueError("take_profit_arm_pct 与 take_profit_exit_pct 须同时设置或同时为空")
    if arm is not None and exit_lvl is not None:
        if arm <= 0 or exit_lvl < 0:
            raise ValueError("take_profit_arm_pct 须 > 0，take_profit_exit_pct 须 >= 0")
        if exit_lvl >= arm:
            raise ValueError("take_profit_exit_pct 必须小于 take_profit_arm_pct")
    if stop_loss_pct is not None and stop_loss_pct <= 0:
        raise ValueError("stop_loss_pct 须 > 0")

    ctx = dict(context or {})
    calendar = [str(d)[:10] for d in close_panel.index.tolist()]
    rebalance_set = {str(d)[:10] for d in rebalance_dates if str(d)[:10] in set(calendar)}
    if not rebalance_set:
        raise ValueError("调仓日与交易日历无交集")

    cash = float(initial_cash)
    positions: dict[str, int] = {}
    avg_cost: dict[str, float] = {}
    tp_armed: set[str] = set()
    trades: list[dict[str, Any]] = []
    holdings_log: list[dict[str, Any]] = []
    rebalances: list[dict[str, Any]] = []
    equity_curve: list[float] = []

    def mark_to_market(day: str) -> float:
        total = cash
        row = close_panel.loc[day]
        for sym, shares in positions.items():
            px = row.get(sym)
            if px is None or not np.isfinite(float(px)):
                continue
            total += shares * float(px)
        return float(total)

    def _sell_position(day: str, sym: str, *, reason: str) -> None:
        nonlocal cash
        shares = positions.get(sym, 0)
        if shares <= 0:
            return
        row = close_panel.loc[day]
        px = row.get(sym)
        if px is None or not np.isfinite(float(px)) or float(px) <= 0:
            return
        price = float(px)
        notional = shares * price
        cost = _trade_cost(
            notional,
            commission=commission,
            min_commission=min_commission,
            slippage=slippage,
            side="sell",
        )
        proceeds = notional - cost
        cash += proceeds
        trades.append(
            {
                "date": day,
                "symbol": sym,
                "side": "sell",
                "price": price,
                "shares": float(shares),
                "cash_after": float(cash),
                "cost": float(cost),
                "reason": reason,
            }
        )
        positions[sym] = 0
        avg_cost.pop(sym, None)
        tp_armed.discard(sym)

    def _buy_position(day: str, sym: str, budget: float) -> None:
        nonlocal cash
        row = close_panel.loc[day]
        px = row.get(sym)
        if px is None or not np.isfinite(float(px)) or float(px) <= 0:
            return
        price = float(px)
        exec_price = price * (1.0 + max(slippage, 0.0))
        shares = _lot_shares(budget, exec_price, lot_size=lot_size)
        if shares <= 0:
            return
        notional = shares * exec_price
        fee = notional * max(commission, 0.0)
        if min_commission > 0:
            fee = max(fee, min_commission)
        total_pay = notional + fee
        if total_pay > cash:
            shares = _lot_shares(cash - min_commission, exec_price, lot_size=lot_size)
            if shares <= 0:
                return
            notional = shares * exec_price
            fee = notional * max(commission, 0.0)
            if min_commission > 0:
                fee = max(fee, min_commission)
            total_pay = notional + fee
            if total_pay > cash:
                return
        cash -= total_pay
        prev_shares = positions.get(sym, 0)
        prev_cost = avg_cost.get(sym, exec_price)
        new_shares = prev_shares + shares
        if new_shares > 0:
            avg_cost[sym] = (prev_cost * prev_shares + exec_price * shares) / new_shares
        positions[sym] = new_shares
        tp_armed.discard(sym)
        trades.append(
            {
                "date": day,
                "symbol": sym,
                "side": "buy",
                "price": exec_price,
                "shares": float(shares),
                "cash_after": float(cash),
                "cost": float(fee + shares * exec_price * max(slippage, 0.0)),
                "reason": "rebalance",
            }
        )

    risk_enabled = stop_loss_pct is not None or (arm is not None and exit_lvl is not None)
    for day in calendar:
        # 非调仓日：止损 / 止盈
        if day not in rebalance_set and risk_enabled:
            row = close_panel.loc[day]
            for sym in list(positions.keys()):
                shares = positions.get(sym, 0)
                if shares <= 0:
                    continue
                cost_px = avg_cost.get(sym)
                px = row.get(sym)
                if (
                    cost_px is None
                    or cost_px <= 0
                    or px is None
                    or not np.isfinite(float(px))
                    or float(px) <= 0
                ):
                    continue
                px_f = float(px)
                if stop_loss_pct is not None and px_f <= cost_px * (1.0 - stop_loss_pct):
                    _sell_position(day, sym, reason="stop_loss")
                    continue
                if arm is not None and exit_lvl is not None:
                    ret = px_f / cost_px - 1.0
                    if sym not in tp_armed and ret >= arm:
                        tp_armed.add(sym)
                    if sym in tp_armed and px_f <= cost_px * (1.0 + exit_lvl):
                        _sell_position(day, sym, reason="take_profit")
            positions = {k: v for k, v in positions.items() if v > 0}

        if day in rebalance_set:
            target = list(select_holdings(day, ctx))
            target = [s for s in target if s in close_panel.columns]
            for sym in list(positions.keys()):
                _sell_position(day, sym, reason="rebalance")
            positions = {k: v for k, v in positions.items() if v > 0}

            if target:
                budget = cash / len(target)
                for sym in target:
                    _buy_position(day, sym, budget)

            rebalances.append(
                {
                    "date": day,
                    "targets": target,
                    "weights": {s: 1.0 / len(target) for s in target} if target else {},
                    "cash": float(cash),
                }
            )
            holdings_log.append(
                {
                    "date": day,
                    "positions": {k: int(v) for k, v in positions.items() if v > 0},
                    "cash": float(cash),
                }
            )

        equity_curve.append(mark_to_market(day))

    eq_arr = np.asarray(equity_curve, dtype=float)
    metrics = metrics_from_equity(eq_arr, initial_cash, trades, calendar)
    equity_out = [{"date": calendar[i], "equity": float(equity_curve[i])} for i in range(len(calendar))]
    return PortfolioBacktestResult(
        equity=equity_out,
        trades=trades,
        holdings=holdings_log,
        metrics=metrics,
        rebalances=rebalances,
    )
