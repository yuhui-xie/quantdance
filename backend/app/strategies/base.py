"""策略插件约定：注册对象 StrategySpec + 公共回测参数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Callable, Literal, Mapping, Sequence

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


def month_end_trading_days(calendar: Sequence[str], months: int = 1) -> list[str]:
    """返回日历上每 N 个自然月最后一个交易日的决策日。

    决策日锚定在自然月/季/年末，与回测起始日无关：无论回测窗口从哪天开始，
    同一日历月/季度的调仓日都相同，回测结果不会随起始日相位漂移而改变。
    months=1 每月末调仓；months=3 每季度末调仓（3/6/9/12 月末）。
    日期的 month 部分按 1-12 月标号过滤，季度锚定落在自然季末而非相对起点。
    """
    if months < 1:
        raise ValueError("调仓月数必须大于等于 1")
    ordered = sorted(str(day)[:10] for day in calendar)
    month_ends: dict[str, str] = {}
    for day in ordered:
        month_ends[day[:7]] = day  # 同月最后一个交易日覆盖先前的日期
    return [day for day in month_ends.values() if int(day[5:7]) % months == 0]


def decision_dates_by_frequency(
    calendar: Sequence[str],
    *,
    frequency: Literal["daily", "weekly", "monthly"] = "monthly",
    every_n: int = 1,
    warmup: int = 20,
) -> list[str]:
    """按自然周期锚定生成决策日：daily 每个交易日 / weekly 每周末 / monthly 每自然月末。

    与回测起始日解耦：weekly 锚定 ISO 周年末、monthly 锚定自然月末，
    同一日历周/月的决策日固定，回测结果不随起始日相位漂移。
    - ``frequency="daily"``：每个交易日都是决策日，但跳过前 ``warmup`` 根
      用于指标预热的冷启动期；
    - ``frequency="weekly"``：每个 ISO 周最后一个交易日；``every_n=2`` 即双周；
    - ``frequency="monthly"``：每 ``every_n`` 个自然月最后一个交易日（= 季末等）。
    """
    if every_n < 1:
        raise ValueError("决策间隔必须大于等于 1")
    ordered = sorted(str(day)[:10] for day in calendar)
    if frequency == "daily":
        return ordered[warmup:]
    if frequency == "weekly":
        week_ends: dict[tuple[int, int], str] = {}
        for day in ordered:
            iso_year, iso_week, _ = _date.fromisoformat(day).isocalendar()
            week_ends[(iso_year, iso_week)] = day  # 同 ISO 周最后一个交易日覆盖先前日期
        return [
            day for (_, week), day in week_ends.items() if week % every_n == 0
        ]
    if frequency == "monthly":
        return month_end_trading_days(ordered, every_n)
    raise ValueError(f"未知决策频率: {frequency}")


def decision_daily(
    calendar: Sequence[str],
    ctx: CrossSectionContext,
    params: BaseModel,
) -> list[str]:
    """每天都是决策日，但跳过前 N 根用于指标预热的冷启动期。

    预热天数取 params.decision_warmup（默认 20），保证 MA/LLT 等指标
    有足够历史数据，避免冷启动期的空选股。现委托统一频率辅助函数实现。
    """
    del ctx
    warmup = int(getattr(params, "decision_warmup", 20) or 0)
    return decision_dates_by_frequency(
        calendar, frequency="daily", every_n=1, warmup=warmup
    )


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
    default_symbols: tuple[str, ...] = ()
    requires_symbols: bool = False
    needs_fundamentals: bool = True
    needs_dividend: bool = False
    needs_financials: bool = False
    default_top_n: int = 10
    warnings: tuple[str, ...] = ()
