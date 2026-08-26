"""可复用的持仓风险控制组件。

把「止盈 / 止损 / 分批减仓」三类出场决策从共享引擎中抽取出来，封装成
独立的 `RiskControl` 状态机。任何按日逐标的喂入价格/成本的策略或引擎，
都可以复用这套逻辑而无需理解内部的峰值/档位状态如何维护。

使用方式::

    from app.backtest.risk_control import RiskControl

    risk = RiskControl(
        stop_loss_pct=0.1,
        take_profit_arm_pct=0.2,
        take_profit_exit_pct=0.1,
        stop_loss_tier_pct=0.1,
        stop_loss_tier_sell_fraction=1 / 3,
        stop_loss_tier_anchor="peak",
    )
    risk.on_entry("510300", shares, price)          # 新开仓
    action = risk.evaluate("510300", price, cost)   # 逐日调用
    if action is not None:                          # 出场动作
        risk.on_close("510300")                     # 仓位清零后释放状态
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional

__all__ = ["ExitAction", "RiskControl"]


@dataclass
class ExitAction:
    """一次出场动作。

    - ``kind="full"``: 全部清仓。
    - ``kind="fraction"``: 按 ``fraction``（建仓基准的比例）卖出一档仓位。
    """

    kind: Literal["full", "fraction"]
    reason: str
    fraction: float = 1.0


@dataclass
class _PosState:
    """单个持仓代码的出场状态。

    - ``entry_shares``: 建仓股数，作为分批减仓的档位基准。
    - ``peak``: peak 锚定下的持仓期间最高价（追踪回落基准）。
    - ``armed``: 移动止盈是否已触发"抵达 +arm"标记。
    - ``tier_level``: 分批减仓已触发的档位数。
    """

    entry_shares: float = 0.0
    peak: float = 0.0
    armed: bool = False
    tier_level: int = 0


class RiskControl:
    """共享账户/策略的可复用出场与风控决策组件。

    三类规则（按优先级从高到低）:

    - **分批减仓** ``stop_loss_tier_pct`` + ``stop_loss_tier_sell_fraction``：
      每再跌破一个步长，卖出建仓基准的 ``sell_fraction`` 比例。``anchor`` 决定
      触发基准是持仓期间最高价（``"peak"``，追踪回落、能保护涨高后的回吐）
      还是平均成本（``"cost"``）。启用时覆盖单一止损。
    - **单一止损** ``stop_loss_pct``：跌破平均成本的该比例即全部清仓。
    - **移动止盈** ``take_profit_arm_pct`` + ``take_profit_exit_pct``：涨到
      ``+arm`` 后，回落到 ``+exit``（``exit=0`` 表示到 ``+arm`` 即卖）时兑现。
      三者只能择一使用（分批减仓优先，其次单一止损，止盈可在前两者之外叠加）。
    """

    def __init__(
        self,
        *,
        take_profit_arm_pct: float | None = None,
        take_profit_exit_pct: float | None = None,
        stop_loss_pct: float | None = None,
        stop_loss_tier_pct: float | None = None,
        stop_loss_tier_sell_fraction: float | None = None,
        stop_loss_tier_anchor: Literal["cost", "peak"] = "peak",
    ) -> None:
        arm, exit_level = take_profit_arm_pct, take_profit_exit_pct
        if (arm is None) ^ (exit_level is None):
            raise ValueError(
                "take_profit_arm_pct 与 take_profit_exit_pct 须同时设置或同时为空"
            )
        if arm is not None and exit_level is not None:
            if arm <= 0 or exit_level < 0:
                raise ValueError("take_profit_arm_pct 须 > 0，take_profit_exit_pct 须 >= 0")
            if exit_level > 0 and exit_level >= arm:
                raise ValueError("take_profit_exit_pct 必须小于 take_profit_arm_pct")
        if stop_loss_pct is not None and stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct 须 > 0")
        if (stop_loss_tier_pct is None) ^ (stop_loss_tier_sell_fraction is None):
            raise ValueError(
                "stop_loss_tier_pct 与 stop_loss_tier_sell_fraction 须同时设置或同时为空"
            )
        if stop_loss_tier_pct is not None and stop_loss_tier_pct <= 0:
            raise ValueError("stop_loss_tier_pct 须 > 0")
        if stop_loss_tier_sell_fraction is not None and not (
            0 < stop_loss_tier_sell_fraction <= 1
        ):
            raise ValueError("stop_loss_tier_sell_fraction 须在 (0,1] 区间")

        self.take_profit_arm_pct = arm
        self.take_profit_exit_pct = exit_level
        self.stop_loss_pct = stop_loss_pct
        self.stop_loss_tier_pct = stop_loss_tier_pct
        self.stop_loss_tier_sell_fraction = stop_loss_tier_sell_fraction
        self.stop_loss_tier_anchor = stop_loss_tier_anchor
        self._states: dict[str, _PosState] = {}

    def active(self) -> bool:
        """是否配置了任何出场规则。"""
        return (
            self.stop_loss_pct is not None
            or self.take_profit_arm_pct is not None
            or self.stop_loss_tier_pct is not None
        )

    def on_entry(self, symbol: str, shares: float, price: float) -> None:
        """新开仓：记录建仓基准、峰值，并复位档位与止盈标记。"""
        self._states[symbol] = _PosState(
            entry_shares=float(shares),
            peak=float(price),
            armed=False,
            tier_level=0,
        )

    def reset_armed(self, symbol: str) -> None:
        """调仓/加仓后复位该代码的移动止盈已触发标记。"""
        state = self._states.get(symbol)
        if state is not None:
            state.armed = False

    def has(self, symbol: str) -> bool:
        return symbol in self._states

    def entry_shares(self, symbol: str) -> float:
        """分批减仓的档位基准（建仓股数）；未开仓或无记录返回 0。"""
        state = self._states.get(symbol)
        return state.entry_shares if state is not None else 0.0

    def on_close(self, symbol: str) -> None:
        """仓位清零：释放该代码的全部出场状态。"""
        self._states.pop(symbol, None)

    def evaluate(self, symbol: str, price: float, cost: float) -> Optional[ExitAction]:
        """给定当日价与平均成本，返回出场动作；不出场返回 ``None``。

        优先级与共享引擎原逻辑一致：分批减仓 > 单一止损 > 移动止盈。
        """
        state = self._states.get(symbol)
        if state is None or price <= 0 or cost <= 0 or not math.isfinite(price):
            return None
        # 分批减仓：每再跌破一个步长卖出一档（peak 锚定先更新追踪峰值）。
        if (
            self.stop_loss_tier_pct is not None
            and self.stop_loss_tier_sell_fraction is not None
            and state.entry_shares > 0
        ):
            if self.stop_loss_tier_anchor == "peak":
                state.peak = max(state.peak, price)
                base = state.peak
            else:
                base = cost
            if price <= base * (1.0 - self.stop_loss_tier_pct * (state.tier_level + 1)):
                state.tier_level += 1
                return ExitAction(
                    kind="fraction",
                    reason="tiered_stop",
                    fraction=self.stop_loss_tier_sell_fraction,
                )
        # 单一止损（分批减仓启用时被覆盖，不会触发）。
        elif self.stop_loss_pct is not None and price <= cost * (1.0 - self.stop_loss_pct):
            return ExitAction(kind="full", reason="stop_loss")
        # 移动止盈：涨到 +arm 后回落至 +exit 兑现；exit=0 表示到 +arm 即卖。
        if self.take_profit_arm_pct is not None and self.take_profit_exit_pct is not None:
            if self.take_profit_exit_pct == 0 and price >= cost * (1.0 + self.take_profit_arm_pct):
                return ExitAction(kind="full", reason="take_profit")
            if self.take_profit_exit_pct > 0:
                if not state.armed and price >= cost * (1.0 + self.take_profit_arm_pct):
                    state.armed = True
                if state.armed and price <= cost * (1.0 + self.take_profit_exit_pct):
                    return ExitAction(kind="full", reason="take_profit")
        return None
