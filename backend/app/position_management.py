"""可复用的组合仓位管理策略。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


TakeProfitMode = Literal["none", "fixed", "trailing"]


@dataclass(frozen=True)
class PositionManagementPolicy:
    """固定资金槽位、分批建仓和个股风控参数。"""

    max_positions: int
    initial_allocation_pct: float = 0.5
    add_allocation_pct: float = 0.25
    add_trigger_pct: float = 0.10
    stop_loss_pct: float | None = 0.10
    take_profit_mode: TakeProfitMode = "none"
    take_profit_pct: float | None = None
    trailing_drawdown_pct: float | None = None

    def __post_init__(self) -> None:
        if self.max_positions < 1:
            raise ValueError("max_positions 至少为 1")
        for name, value in (
            ("initial_allocation_pct", self.initial_allocation_pct),
            ("add_allocation_pct", self.add_allocation_pct),
            ("add_trigger_pct", self.add_trigger_pct),
        ):
            if value <= 0:
                raise ValueError(f"{name} 须 > 0")
        if self.initial_allocation_pct > 1 or self.add_allocation_pct > 1:
            raise ValueError("initial_allocation_pct 与 add_allocation_pct 须 <= 1")
        if self.stop_loss_pct is not None and self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct 须 > 0")
        if self.take_profit_mode not in {"none", "fixed", "trailing"}:
            raise ValueError("take_profit_mode 必须为 none、fixed 或 trailing")
        if self.take_profit_mode != "none":
            if self.take_profit_pct is None or self.take_profit_pct <= 0:
                raise ValueError("启用止盈时 take_profit_pct 须 > 0")
        if self.take_profit_mode == "trailing":
            if self.trailing_drawdown_pct is None or not 0 < self.trailing_drawdown_pct < 1:
                raise ValueError("追踪止盈的 trailing_drawdown_pct 须在 0 与 1 之间")


@dataclass
class PositionManager:
    """保存跨日加仓档位和追踪止盈峰值。"""

    policy: PositionManagementPolicy
    initial_cash: float
    entry_price: dict[str, float] = field(default_factory=dict)
    next_add_price: dict[str, float] = field(default_factory=dict)
    peak_price: dict[str, float] = field(default_factory=dict)

    @property
    def max_position_value(self) -> float:
        return self.initial_cash / self.policy.max_positions

    @property
    def initial_budget(self) -> float:
        return self.max_position_value * self.policy.initial_allocation_pct

    @property
    def add_budget(self) -> float:
        return self.max_position_value * self.policy.add_allocation_pct

    def record_buy(self, symbol: str, price: float, *, is_add: bool) -> None:
        if not is_add or symbol not in self.entry_price:
            self.entry_price[symbol] = price
            self.next_add_price[symbol] = price * (1.0 + self.policy.add_trigger_pct)
            self.peak_price[symbol] = price
            return
        entry = self.entry_price[symbol]
        self.next_add_price[symbol] = (
            self.next_add_price[symbol] + entry * self.policy.add_trigger_pct
        )

    def record_sell(self, symbol: str) -> None:
        self.entry_price.pop(symbol, None)
        self.next_add_price.pop(symbol, None)
        self.peak_price.pop(symbol, None)

    def update_peak(self, symbol: str, price: float) -> None:
        self.peak_price[symbol] = max(self.peak_price.get(symbol, price), price)

    def exit_reason(self, symbol: str, price: float, avg_cost: float) -> str | None:
        """止损优先；追踪止盈按启动后的最高价回撤计算。"""
        stop = self.policy.stop_loss_pct
        if stop is not None and price <= avg_cost * (1.0 - stop):
            return "stop_loss"

        mode = self.policy.take_profit_mode
        take_profit = self.policy.take_profit_pct
        if mode == "none" or take_profit is None:
            return None
        if mode == "fixed":
            if price >= avg_cost * (1.0 + take_profit):
                return "take_profit"
            return None

        arm_price = avg_cost * (1.0 + take_profit)
        peak = self.peak_price.get(symbol, price)
        if peak < arm_price:
            return None
        drawdown = self.policy.trailing_drawdown_pct
        if drawdown is not None and price <= peak * (1.0 - drawdown):
            return "take_profit"
        return None

    def add_budget_for(
        self,
        symbol: str,
        *,
        price: float,
        shares: int,
        available_cash: float,
    ) -> float:
        trigger = self.next_add_price.get(symbol)
        if trigger is None or price < trigger:
            return 0.0
        remaining = self.max_position_value - shares * price
        return max(0.0, min(self.add_budget, remaining, available_cash))
