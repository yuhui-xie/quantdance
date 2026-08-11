"""横截面策略的"按周期决策"专用模块：日历基元、共享参数与默认调度器。

横截面策略通常按自然周期（每日 / 每周 / 每月）生成决策日。为消除各策略
重复声明 params 字段与 ``decision_dates`` 包装函数的样板，本模块集中提供：

- ``DecisionFrequencyParams``：三个共享决策周期字段（Pydantic 基类，策略继承）；
- ``periodic_decision_dates``：按 params 中频率字段生成决策日的默认调度器；
- ``month_end_trading_days`` / ``decision_dates_by_frequency`` / ``decision_daily``：
  底层日历基元。

本模块自包含，仅依赖 stdlib 与 pydantic，不依赖 ``app.strategies.base``，
从而避免与 ``base.py`` 之间产生循环导入。
"""

from __future__ import annotations

from datetime import date as _date
from typing import Literal, Sequence

from pydantic import BaseModel, Field


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


class DecisionFrequencyParams(BaseModel):
    """按周期决策的共享参数；横截面策略的 Params 直接继承即可复用。"""

    decision_frequency: Literal["daily", "weekly", "monthly"] = Field(
        "monthly", description="决策频率：daily=每日（冷启动期后）| weekly=每周末 | monthly=每自然月末"
    )
    decision_every_n: int = Field(1, ge=1, description="决策步长：monthly+3=季末、weekly+2=双周；daily 忽略")
    decision_warmup: int = Field(20, ge=0, le=250, description="冷启动期（交易日），仅 daily 生效")


def periodic_decision_dates(
    calendar: Sequence[str],
    ctx: object,
    params: BaseModel,
) -> list[str]:
    """默认决策日调度器：按 ``params`` 中的频率字段生成决策日。

    - ``params.decision_frequency``：daily / weekly / monthly（默认 monthly）；
    - ``params.decision_every_n``：决策步长（默认 1）；
    - ``params.decision_warmup``：daily 冷启动期（默认 20）。

    用 ``getattr`` 兜底，使未继承 ``DecisionFrequencyParams`` 的 params 也可安全调用。
    """
    del ctx
    frequency = str(getattr(params, "decision_frequency", "monthly"))
    every_n = int(getattr(params, "decision_every_n", 1) or 1)
    warmup = int(getattr(params, "decision_warmup", 20) or 0)
    return decision_dates_by_frequency(
        calendar,
        frequency=frequency,  # type: ignore[arg-type]
        every_n=every_n,
        warmup=warmup,
    )


def decision_daily(
    calendar: Sequence[str],
    ctx: object,
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
