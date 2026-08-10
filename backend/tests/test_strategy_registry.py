"""统一策略注册与横截面调度测试。"""

from __future__ import annotations

import pytest

from app.strategies.base import (
    CrossSectionContext,
    CrossSectionStrategySpec,
    decision_dates_by_frequency,
    month_end_trading_days,
)
from app.strategies.registry import CROSS_SECTION_STRATEGIES, STRATEGIES


def test_registry_recursively_loads_both_strategy_kinds():
    assert "ma_crossover" in STRATEGIES
    assert {
        "cyclical_rotation",
        "etf_rotation",
        "limit_up_pullback",
        "market_auntie",
        "order_inflection",
        "small_cap_zz399101",
    }.issubset(CROSS_SECTION_STRATEGIES)
    assert all(
        isinstance(spec, CrossSectionStrategySpec)
        for spec in CROSS_SECTION_STRATEGIES.values()
    )


def test_month_end_trading_days_anchors_to_calendar_months():
    # 跨 4 个自然月的合成日历（2024 闰年含 2-29）
    calendar = [
        "2024-01-05", "2024-01-31",
        "2024-02-02", "2024-02-29",
        "2024-03-08", "2024-03-29",
        "2024-04-03", "2024-04-30",
    ]
    # 月频：每月最后一个交易日
    assert month_end_trading_days(calendar, 1) == [
        "2024-01-31",
        "2024-02-29",
        "2024-03-29",
        "2024-04-30",
    ]
    # 季度：仅 3/6/9/12 月末，与起始日无关
    assert month_end_trading_days(calendar, 3) == ["2024-03-29"]
    # 输入乱序不影响结果（内部排序）
    assert month_end_trading_days(list(reversed(calendar)), 1) == [
        "2024-01-31",
        "2024-02-29",
        "2024-03-29",
        "2024-04-30",
    ]
    # 同月最后一个交易日即月末
    assert month_end_trading_days(["2024-01-15", "2024-01-20"], 1) == ["2024-01-20"]
    with pytest.raises(ValueError):
        month_end_trading_days(calendar, 0)


def test_each_cross_section_strategy_owns_decision_schedule():
    calendar = [
        "2024-01-05", "2024-01-31",
        "2024-02-02", "2024-02-29",
        "2024-03-08", "2024-03-29",
        "2024-04-03", "2024-04-30",
    ]
    ctx = CrossSectionContext(panel={}, names={})

    for spec in CROSS_SECTION_STRATEGIES.values():
        if "decision_frequency" not in spec.params_model.model_fields:
            continue
        # 月频：每月最后一个交易日
        params = spec.params_model(decision_frequency="monthly", decision_every_n=1)
        assert spec.decision_dates(calendar, ctx, params) == [
            "2024-01-31",
            "2024-02-29",
            "2024-03-29",
            "2024-04-30",
        ]
        # 季度锚定仅落在 3/6/9/12 月末
        params_q = spec.params_model(decision_frequency="monthly", decision_every_n=3)
        assert spec.decision_dates(calendar, ctx, params_q) == ["2024-03-29"]


def test_decision_dates_by_frequency_daily_weekly_monthly():
    # 跨 2 个 ISO 周的合成日历：week1 = 01-01~01-05，week2 = 01-08~01-12
    week_calendar = [
        "2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05",
        "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12",
    ]
    # weekly：每周末（ISO 周最后一个交易日），与起始日无关
    assert decision_dates_by_frequency(
        week_calendar, frequency="weekly", every_n=1
    ) == ["2024-01-05", "2024-01-12"]
    # 双周：仅 ISO 周号为偶数的周末
    assert decision_dates_by_frequency(
        week_calendar, frequency="weekly", every_n=2
    ) == ["2024-01-12"]
    # daily：每个交易日，跳过前 warmup 根冷启动
    assert decision_dates_by_frequency(
        week_calendar, frequency="daily", every_n=1, warmup=2
    ) == week_calendar[2:]
    # monthly 委托 month_end_trading_days；输入乱序不影响
    assert decision_dates_by_frequency(
        list(reversed(week_calendar)), frequency="monthly", every_n=1
    ) == ["2024-01-12"]
    # 非法频率 / 非法步长抛错
    with pytest.raises(ValueError):
        decision_dates_by_frequency(week_calendar, frequency="yearly")
    with pytest.raises(ValueError):
        decision_dates_by_frequency(week_calendar, frequency="weekly", every_n=0)


def test_prosperity_resonance_checks_daily_with_warmup():
    spec = CROSS_SECTION_STRATEGIES["prosperity_resonance"]
    calendar = [f"2024-01-{day:02d}" for day in range(1, 11)]
    ctx = CrossSectionContext(panel={}, names={})
    # 默认日频：跳过前 N 根冷启动
    params = spec.params_model(decision_warmup=2)
    assert spec.decision_dates(calendar, ctx, params) == [
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
        "2024-01-06",
        "2024-01-07",
        "2024-01-08",
        "2024-01-09",
        "2024-01-10",
    ]
    # 同参数下切到月频：整个 1 月最后一个交易日
    params_m = spec.params_model(decision_frequency="monthly", decision_warmup=2)
    assert spec.decision_dates(calendar, ctx, params_m) == ["2024-01-10"]
