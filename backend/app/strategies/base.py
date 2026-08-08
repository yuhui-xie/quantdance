"""策略插件约定：注册对象 StrategySpec + 公共回测参数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd
from pydantic import BaseModel

from app.backtest_engine import BacktestResult


@dataclass(frozen=True)
class BaseBacktestParams:
    """所有策略共享的资金、费率与风控参数。"""

    initial_cash: float
    commission: float
    stop_loss_pct: float | None = None


StrategyRun = Callable[[pd.DataFrame, BaseBacktestParams, BaseModel], BacktestResult]
StrategySignals = Callable[
    [pd.DataFrame, BaseBacktestParams, BaseModel],
    Sequence[int] | np.ndarray,
]
StrategyMinBars = Callable[[BaseModel], int]


@dataclass(frozen=True)
class StrategySpec:
    """
    每个策略模块导出 STRATEGY: StrategySpec。
    插件代码在服务端受信任环境运行，勿执行不可信来源的模块。
    """

    id: str
    name: str
    description: str
    params_model: type[BaseModel]
    min_bars: StrategyMinBars
    run: StrategyRun
    signals: StrategySignals | None = None

    def compute_signals(
        self,
        df: pd.DataFrame,
        base: BaseBacktestParams,
        params: BaseModel,
    ) -> np.ndarray:
        """
        返回策略的原始事件信号。

        新策略可提供独立 signals 回调；旧策略则从通用回测结果读取信号，
        以保持现有插件完全兼容。
        """
        raw = self.signals(df, base, params) if self.signals else self.run(df, base, params).signal
        signal = np.asarray(raw, dtype=np.int8)
        if len(signal) != len(df):
            raise ValueError(f"策略 {self.id} 的 signal 长度必须与行情数据一致")
        if not np.isin(signal, (-1, 0, 1)).all():
            raise ValueError(f"策略 {self.id} 的 signal 只能包含 -1、0、1")
        return signal


@dataclass(frozen=True)
class CrossSectionContext:
    """横截面决策上下文；缓存可由策略跨决策日复用。"""

    panel: Mapping[str, Mapping[str, pd.DataFrame]]
    names: Mapping[str, str]
    cache: dict[str, Any] = field(default_factory=dict)


CrossSectionSelect = Callable[
    [str, CrossSectionContext, BaseModel],
    tuple[list[str], list[dict[str, Any]]],
]
CrossSectionDecisionDates = Callable[
    [Sequence[str], CrossSectionContext, BaseModel],
    list[str],
]


def every_n_trading_days(calendar: Sequence[str], interval: int) -> list[str]:
    """返回从首个交易日开始、每隔 N 个交易日一次的决策日。"""

    if interval < 1:
        raise ValueError("交易日决策间隔必须大于等于 1")
    return [str(day)[:10] for day in calendar[::interval]]


@dataclass(frozen=True)
class CrossSectionStrategySpec:
    """横截面策略插件：策略同时拥有选股逻辑和决策日调度。"""

    id: str
    name: str
    description: str
    params_model: type[BaseModel]
    select: CrossSectionSelect
    decision_dates: CrossSectionDecisionDates
    default_universe: str = "zz500"
    requires_symbols: bool = False
    needs_fundamentals: bool = True
    needs_dividend: bool = False
    needs_financials: bool = False
    default_top_n: int = 10
    warnings: tuple[str, ...] = ()
