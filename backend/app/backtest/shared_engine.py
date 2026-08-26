"""共享现金、多标的目标持仓回测引擎。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

import numpy as np
import pandas as pd

from app.backtest_engine import metrics_from_equity
from app.backtest.risk_control import ExitAction, RiskControl
from app.position_management import PositionManagementPolicy, PositionManager


@dataclass
class SharedBacktestResult:
    equity: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    holdings: list[dict[str, Any]]
    metrics: dict[str, float]
    # 暂留此名称，兼容现有报告数据结构。
    rebalances: list[dict[str, Any]]


def build_close_panel(
    series_by_symbol: Mapping[str, pd.Series | pd.DataFrame],
) -> pd.DataFrame:
    """构建 index=交易日、columns=标的的收盘价宽表。"""
    frames: dict[str, pd.Series] = {}
    for symbol, obj in series_by_symbol.items():
        if isinstance(obj, pd.Series):
            series = obj.copy()
            series.index = pd.to_datetime(series.index, errors="coerce").strftime("%Y-%m-%d")
            frames[symbol] = pd.to_numeric(series, errors="coerce")
        elif isinstance(obj, pd.DataFrame) and not obj.empty and "close" in obj:
            if "date" in obj:
                index = pd.to_datetime(obj["date"], errors="coerce").dt.strftime("%Y-%m-%d")
                frames[symbol] = pd.Series(
                    pd.to_numeric(obj["close"], errors="coerce").values,
                    index=index,
                )
            else:
                series = pd.to_numeric(obj["close"], errors="coerce")
                series.index = pd.to_datetime(obj.index, errors="coerce").strftime("%Y-%m-%d")
                frames[symbol] = series
    if not frames:
        raise ValueError("收盘价面板为空")
    panel = pd.DataFrame(frames).sort_index()
    # 交易日历取所有标的的并集。某些标在日历尾部（如部分数据源的当日/停牌日）
    # 没有对应 K 线，需沿时间向前填充最后收盘价，否则该日持仓会被按 NaN→0 估值，
    # 造成组合净值在最后一个未完全覆盖的交易日被错误清零。
    panel = panel.ffill()
    return panel[~panel.index.isna()]


def _lot_shares(cash: float, price: float, lot_size: int) -> int:
    if cash <= 0 or price <= 0 or lot_size <= 0:
        return 0
    return max(int(cash // (price * lot_size)) * lot_size, 0)


def run_shared_backtest(
    close_panel: pd.DataFrame,
    *,
    targets_by_date: Mapping[str, Sequence[str]],
    initial_cash: float,
    commission: float = 0.0003,
    min_commission: float = 5.0,
    slippage: float = 0.0,
    lot_size: int = 100,
    take_profit_arm_pct: float | None = None,
    take_profit_exit_pct: float | None = None,
    stop_loss_pct: float | None = None,
    stop_loss_tier_pct: float | None = None,
    stop_loss_tier_sell_fraction: float | None = None,
    stop_loss_tier_anchor: Literal["cost", "peak"] = "peak",
    position_policy: PositionManagementPolicy | None = None,
    rebalance_mode: str = "full",
) -> SharedBacktestResult:
    """按日期目标映射执行共享账户回测；仅映射中出现的交易日进行换仓。"""
    if close_panel.empty:
        raise ValueError("close_panel 为空")
    if initial_cash <= 0:
        raise ValueError("initial_cash 必须为正")
    risk = RiskControl(
        take_profit_arm_pct=take_profit_arm_pct,
        take_profit_exit_pct=take_profit_exit_pct,
        stop_loss_pct=stop_loss_pct,
        stop_loss_tier_pct=stop_loss_tier_pct,
        stop_loss_tier_sell_fraction=stop_loss_tier_sell_fraction,
        stop_loss_tier_anchor=stop_loss_tier_anchor,
    )
    if position_policy is not None and risk.active():
        raise ValueError("position_policy 不能与旧版止损止盈参数同时使用")

    calendar = [str(day)[:10] for day in close_panel.index]
    calendar_set = set(calendar)
    targets = {
        str(day)[:10]: list(symbols)
        for day, symbols in targets_by_date.items()
        if str(day)[:10] in calendar_set
    }
    if not targets:
        raise ValueError("目标日期与交易日历无交集")

    cash = float(initial_cash)
    positions: dict[str, int] = {}
    average_cost: dict[str, float] = {}
    manager = PositionManager(position_policy, initial_cash) if position_policy else None
    trades: list[dict[str, Any]] = []
    holdings: list[dict[str, Any]] = []
    rebalances: list[dict[str, Any]] = []
    realized_pnls: list[float] = []
    equity_values: list[float] = []
    previous_target: tuple[str, ...] | None = None

    def sell(day: str, symbol: str, reason: str) -> None:
        nonlocal cash
        shares = positions.get(symbol, 0)
        price = close_panel.loc[day].get(symbol)
        if shares <= 0 or price is None or not np.isfinite(float(price)) or float(price) <= 0:
            return
        price = float(price)
        notional = shares * price
        cost = abs(notional) * max(slippage, 0.0)
        fee = abs(notional) * max(commission, 0.0)
        if min_commission > 0:
            fee = max(fee, min_commission)
        cost += fee
        cash += notional - cost
        # 已实现盈亏：按平均成本（已含买入滑点）卖出，再扣卖出滑点与佣金。
        basis = average_cost.get(symbol, price)
        realized = shares * (price - basis) - cost
        realized_pnls.append(float(realized))
        trades.append({
            "date": day, "symbol": symbol, "side": "sell", "price": price,
            "shares": float(shares), "cash_after": float(cash), "cost": float(cost),
            "pnl": float(realized), "reason": reason,
        })
        positions[symbol] = 0
        average_cost.pop(symbol, None)
        risk.on_close(symbol)
        if manager:
            manager.record_sell(symbol)

    def sell_fraction(day: str, symbol: str, fraction: float, reason: str) -> None:
        """分批止损：按建仓基准的 fraction 比例卖出一档仓位，不足一手则清仓。"""
        nonlocal cash
        shares = positions.get(symbol, 0)
        price = close_panel.loc[day].get(symbol)
        if shares <= 0 or price is None or not np.isfinite(float(price)) or float(price) <= 0:
            return
        price = float(price)
        entry = risk.entry_shares(symbol) or float(shares)
        target = int(entry * fraction)
        if lot_size > 0:
            target = (target // lot_size) * lot_size
        shares_to_sell = max(target, 0)
        if shares_to_sell <= 0:
            shares_to_sell = shares  # 不足一手 -> 清仓
        shares_to_sell = min(shares_to_sell, shares)
        notional = shares_to_sell * price
        cost = abs(notional) * max(slippage, 0.0)
        fee = abs(notional) * max(commission, 0.0)
        if min_commission > 0:
            fee = max(fee, min_commission)
        cost += fee
        cash += notional - cost
        basis = average_cost.get(symbol, price)
        realized = shares_to_sell * (price - basis) - cost
        realized_pnls.append(float(realized))
        trades.append({
            "date": day, "symbol": symbol, "side": "sell", "price": price,
            "shares": float(shares_to_sell), "cash_after": float(cash),
            "cost": float(cost), "pnl": float(realized), "reason": reason,
        })
        positions[symbol] = shares - shares_to_sell
        if positions[symbol] <= 0:
            average_cost.pop(symbol, None)
            risk.on_close(symbol)
            if manager:
                manager.record_sell(symbol)

    def buy(day: str, symbol: str, budget: float, reason: str = "rebalance") -> bool:
        nonlocal cash
        price = close_panel.loc[day].get(symbol)
        if price is None or not np.isfinite(float(price)) or float(price) <= 0:
            return False
        execution_price = float(price) * (1.0 + max(slippage, 0.0))
        shares = _lot_shares(min(budget, cash), execution_price, lot_size)
        while shares > 0:
            notional = shares * execution_price
            fee = notional * max(commission, 0.0)
            if min_commission > 0:
                fee = max(fee, min_commission)
            if notional + fee <= cash:
                break
            shares -= lot_size
        if shares <= 0:
            return False
        total = shares * execution_price + fee
        cash -= total
        previous_shares = positions.get(symbol, 0)
        previous_cost = average_cost.get(symbol, execution_price)
        new_shares = previous_shares + shares
        average_cost[symbol] = (
            previous_cost * previous_shares + execution_price * shares
        ) / new_shares
        positions[symbol] = new_shares
        if previous_shares == 0:
            # 新开仓：记录分批减仓的基准、追踪峰值并复位档位/止盈标记
            risk.on_entry(symbol, float(new_shares), execution_price)
        else:
            # 加仓：仅复位移动止盈的已触发标记
            risk.reset_armed(symbol)
        if manager:
            manager.record_buy(symbol, execution_price, is_add=reason == "pyramid_add")
        trades.append({
            "date": day, "symbol": symbol, "side": "buy", "price": execution_price,
            "shares": float(shares), "cash_after": float(cash),
            "cost": float(fee + shares * execution_price * max(slippage, 0.0)),
            "reason": reason,
        })
        return True

    for day in calendar:
        row = close_panel.loc[day]
        target: list[str] | None = None
        sold_today: set[str] = set()
        opened_today: set[str] = set()
        target_changed = False

        if day in targets:
            target = list(
                dict.fromkeys(
                    symbol
                    for symbol in targets[day]
                    if symbol in close_panel.columns
                )
            )
            normalized_target = tuple(target)
            target_changed = normalized_target != previous_target
            previous_target = normalized_target

        if manager:
            if target is not None:
                target = target[: manager.policy.max_positions]
                for symbol in list(positions):
                    if symbol not in target:
                        sell(day, symbol, "rebalance")
                        sold_today.add(symbol)
                positions = {s: n for s, n in positions.items() if n > 0}

            for symbol in list(positions):
                price = row.get(symbol)
                cost = average_cost.get(symbol)
                if price is None or cost is None or not np.isfinite(float(price)):
                    continue
                manager.update_peak(symbol, float(price))
                reason = manager.exit_reason(symbol, float(price), cost)
                if reason:
                    sell(day, symbol, reason)
                    sold_today.add(symbol)
            positions = {s: n for s, n in positions.items() if n > 0}

            if target is not None:
                for symbol in target:
                    if positions.get(symbol, 0) <= 0 and symbol not in sold_today:
                        if buy(day, symbol, min(manager.initial_budget, cash), "initial_entry"):
                            opened_today.add(symbol)
            for symbol in list(positions):
                if symbol in opened_today:
                    continue
                price = row.get(symbol)
                if price is None or not np.isfinite(float(price)) or float(price) <= 0:
                    continue
                budget = manager.add_budget_for(
                    symbol, price=float(price), shares=positions[symbol], available_cash=cash
                )
                if budget > 0:
                    buy(day, symbol, budget, "pyramid_add")
        else:
            if day not in targets:
                for symbol in list(positions):
                    price, cost = row.get(symbol), average_cost.get(symbol)
                    if (
                        price is None or cost is None or cost <= 0
                        or not np.isfinite(float(price)) or float(price) <= 0
                    ):
                        continue
                    # 出场决策统一交由 RiskControl：分批减仓 > 单一止损 > 移动止盈。
                    action: ExitAction | None = risk.evaluate(
                        symbol, float(price), float(cost)
                    )
                    if action is None:
                        continue
                    if action.kind == "fraction":
                        sell_fraction(day, symbol, action.fraction, action.reason)
                    else:
                        sell(day, symbol, action.reason)
                positions = {s: n for s, n in positions.items() if n > 0}
            else:
                if target_changed:
                    if rebalance_mode == "incremental":
                        # 增量换仓：只卖掉落出名单的、买进新加入的，保留共同持仓。
                        # 新标的用卖出释放的现金等权建仓，存量持仓维持不动（权重自然漂移）。
                        target_set = set(target or [])
                        for symbol in list(positions):
                            if symbol not in target_set:
                                sell(day, symbol, "rebalance")
                        positions = {s: n for s, n in positions.items() if n > 0}
                        new_symbols = [
                            s for s in (target or []) if positions.get(s, 0) <= 0
                        ]
                        if new_symbols:
                            budget = cash / len(new_symbols)
                            for symbol in new_symbols:
                                buy(day, symbol, budget)
                    else:
                        for symbol in list(positions):
                            sell(day, symbol, "rebalance")
                        positions = {s: n for s, n in positions.items() if n > 0}
                        if target:
                            budget = cash / len(target)
                            for symbol in target:
                                buy(day, symbol, budget)

        if target is not None:
            rebalances.append({
                "date": day,
                "targets": target,
                "weights": {s: 1.0 / len(target) for s in target} if target else {},
                "cash": float(cash),
            })
            holdings.append({
                "date": day,
                "positions": {s: int(n) for s, n in positions.items() if n > 0},
                "cash": float(cash),
            })

        value = cash
        for symbol, shares in positions.items():
            price = row.get(symbol)
            if price is not None and np.isfinite(float(price)):
                value += shares * float(price)
        equity_values.append(float(value))

    metrics = metrics_from_equity(
        np.asarray(equity_values, dtype=float), initial_cash, trades, calendar,
        pnls=realized_pnls,
    )
    return SharedBacktestResult(
        equity=[
            {"date": day, "equity": equity_values[index]}
            for index, day in enumerate(calendar)
        ],
        trades=trades,
        holdings=holdings,
        metrics=metrics,
        rebalances=rebalances,
    )
