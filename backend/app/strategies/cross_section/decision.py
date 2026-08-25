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


def _monthly_trading_days(
    calendar: Sequence[str], months: int, anchor: Literal["start", "end"]
) -> list[str]:
    """返回日历上每 N 个自然月的锚定交易日（首个或末个）的决策日。

    决策日锚定在自然月/季/年的首日或末日，与回测起始日无关：无论回测窗口从哪天
    开始，同一日历月/季度的调仓日都相同，回测结果不会随起始日相位漂移而改变。
    months=1 每月调仓；months=3 每季度调仓（3/6/9/12 月）。
    anchor="end" 取每 N 月最后一个交易日，anchor="start" 取第一个交易日。
    日期的 month 部分按 1-12 月标号过滤，季度锚定落在自然季界而非相对起点。
    """
    if months < 1:
        raise ValueError("调仓月数必须大于等于 1")
    ordered = sorted(str(day)[:10] for day in calendar)
    month_days: dict[str, str] = {}
    for day in ordered:
        if anchor == "start":
            month_days.setdefault(day[:7], day)  # 同月第一个交易日
        else:
            month_days[day[:7]] = day  # 同月最后一个交易日覆盖先前的日期
    return [day for day in month_days.values() if int(day[5:7]) % months == 0]


def _monthly_weekday_trading_days(
    calendar: Sequence[str],
    *,
    months: int = 1,
    weekday: int = 0,
    weeks: Sequence[int] = (1, 3),
) -> list[str]:
    """返回每 N 个自然月内第 ``weeks`` 个指定星期几的交易日的决策日。

    ``weekday`` 用 ``date.weekday()`` 口径（0=周一 … 6=周日）。``weeks`` 取
    该星期几在当月交易日历中出现的第 N 次（1=首个），例如 (1,3) 且 weekday=0
    即"每月第一、第三个周一"。若某月符合条件的交易日不足（长假等），只取
    存在的前若干个，不补齐。日期按天排序去重，与回测起始日无关。
    """
    ordered = sorted(str(day)[:10] for day in calendar)
    by_month: dict[str, list[str]] = {}
    for day in ordered:
        by_month.setdefault(day[:7], []).append(day)
    picks: list[str] = []
    for month in sorted(by_month):
        if int(month[5:7]) % months != 0:
            continue
        wd_days = [
            d for d in by_month[month]
            if _date.fromisoformat(d).weekday() == weekday
        ]
        for w in weeks:
            if len(wd_days) >= w:
                picks.append(wd_days[w - 1])
    return sorted(picks)


def month_end_trading_days(calendar: Sequence[str], months: int = 1) -> list[str]:
    """返回日历上每 N 个自然月最后一个交易日的决策日（月末锚定）。"""
    return _monthly_trading_days(calendar, months, "end")


def month_start_trading_days(calendar: Sequence[str], months: int = 1) -> list[str]:
    """返回日历上每 N 个自然月第一个交易日的决策日（月初锚定）。"""
    return _monthly_trading_days(calendar, months, "start")


def _weekly_trading_days(calendar: Sequence[str], every_n: int) -> list[str]:
    """返回日历上每 N 个 ISO 周最后一个交易日的决策日（周/双周锚定）。"""
    ordered = sorted(str(day)[:10] for day in calendar)
    week_ends: dict[tuple[int, int], str] = {}
    for day in ordered:
        iso_year, iso_week, _ = _date.fromisoformat(day).isocalendar()
        week_ends[(iso_year, iso_week)] = day  # 同 ISO 周最后一个交易日覆盖先前日期
    return [
        day for (_, week), day in week_ends.items() if week % every_n == 0
    ]


def decision_dates_by_frequency(
    calendar: Sequence[str],
    *,
    frequency: Literal["daily", "weekly", "biweekly", "monthly", "monthly_2x"] = "monthly",
    every_n: int = 1,
    warmup: int = 20,
    anchor: Literal["start", "end"] = "end",
) -> list[str]:
    """按自然周期锚定生成决策日：daily 每个交易日 / weekly 每周末 / biweekly 每双周 / monthly 每月。

    与回测起始日解耦：weekly/biweekly 锚定 ISO 周年末、monthly 锚定自然月初/月末，
    同一日历周/月/双周的决策日固定，回测结果不随起始日相位漂移。
    - ``frequency="daily"``：每个交易日都是决策日，但跳过前 ``warmup`` 根
      用于指标预热的冷启动期；
    - ``frequency="weekly"``：每个 ISO 周最后一个交易日；``every_n=2`` 即双周；
    - ``frequency="biweekly"``：每 2 个 ISO 周最后一个交易日（固定双周，忽略 every_n）；
    - ``frequency="monthly_2x"``：每自然月第一、第三个周一的交易日（忽略 every_n/anchor），
      落在自然月内的半月节奏，比 ``biweekly``（偶数 ISO 周）更贴近日历月；
    - ``frequency="monthly"``：每 ``every_n`` 个自然月锚定交易日；``anchor="end"``
      取月末、``anchor="start"`` 取月初（= 季末/季初等）。
    """
    if every_n < 1:
        raise ValueError("决策间隔必须大于等于 1")
    ordered = sorted(str(day)[:10] for day in calendar)
    if frequency == "daily":
        return ordered[warmup:]
    if frequency == "weekly":
        return _weekly_trading_days(ordered, every_n)
    if frequency == "biweekly":
        return _weekly_trading_days(ordered, 2)
    if frequency == "monthly_2x":
        return _monthly_weekday_trading_days(ordered, months=1, weekday=0, weeks=(1, 3))
    if frequency == "monthly":
        return _monthly_trading_days(ordered, every_n, anchor)
    raise ValueError(f"未知决策频率: {frequency}")


class DecisionFrequencyParams(BaseModel):
    """按周期决策的共享参数；横截面策略的 Params 直接继承即可复用。"""

    decision_frequency: Literal["daily", "weekly", "biweekly", "monthly", "monthly_2x"] = Field(
        "monthly",
        description=(
            "决策频率：daily=每日（冷启动期后）| weekly=每周末 | biweekly=每双周 | "
            "monthly_2x=每月第一、第三个周一 | monthly=每月"
        ),
    )
    decision_every_n: int = Field(1, ge=1, description="决策步长：monthly+3=季、weekly+2=双周；daily/biweekly/monthly_2x 忽略")
    decision_anchor: Literal["start", "end"] = Field(
        "end",
        description="月度决策锚点：end=每自然月末（默认）| start=每自然月首个交易日；仅 monthly 生效",
    )
    decision_warmup: int = Field(20, ge=0, le=250, description="冷启动期（交易日），仅 daily 生效")


def periodic_decision_dates(
    calendar: Sequence[str],
    ctx: object,
    params: BaseModel,
) -> list[str]:
    """默认决策日调度器：按 ``params`` 中的频率字段生成决策日。

    - ``params.decision_frequency``：daily / weekly / biweekly / monthly（默认 monthly）；
    - ``params.decision_every_n``：决策步长（默认 1）；
    - ``params.decision_warmup``：daily 冷启动期（默认 20）。

    用 ``getattr`` 兜底，使未继承 ``DecisionFrequencyParams`` 的 params 也可安全调用。
    """
    del ctx
    frequency = str(getattr(params, "decision_frequency", "monthly"))
    every_n = int(getattr(params, "decision_every_n", 1) or 1)
    warmup = int(getattr(params, "decision_warmup", 20) or 0)
    anchor = str(getattr(params, "decision_anchor", "end") or "end")
    return decision_dates_by_frequency(
        calendar,
        frequency=frequency,  # type: ignore[arg-type]
        every_n=every_n,
        warmup=warmup,
        anchor=anchor,  # type: ignore[arg-type]
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
