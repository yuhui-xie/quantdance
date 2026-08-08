"""统一策略注册与横截面调度测试。"""

from __future__ import annotations

from app.strategies.base import CrossSectionContext, CrossSectionStrategySpec
from app.strategies.registry import CROSS_SECTION_STRATEGIES, STRATEGIES


def test_registry_recursively_loads_both_strategy_kinds():
    assert "ma_crossover" in STRATEGIES
    assert {
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


def test_each_cross_section_strategy_owns_decision_schedule():
    calendar = [f"2024-01-{day:02d}" for day in range(1, 11)]
    ctx = CrossSectionContext(panel={}, names={})

    for spec in CROSS_SECTION_STRATEGIES.values():
        params = spec.params_model(decision_interval=3)
        assert spec.decision_dates(calendar, ctx, params) == [
            "2024-01-01",
            "2024-01-04",
            "2024-01-07",
            "2024-01-10",
        ]
