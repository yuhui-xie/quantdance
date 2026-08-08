"""递归扫描 app.strategies，统一注册时序与横截面策略。"""

from __future__ import annotations

import importlib
import pkgutil

from app.strategies.base import CrossSectionStrategySpec, StrategySpec


def _plugin_module_names() -> list[str]:
    import app.strategies as strategies_pkg

    skip = frozenset({"base", "registry"})
    out: list[str] = []
    prefix = f"{strategies_pkg.__name__}."
    for mod in pkgutil.walk_packages(strategies_pkg.__path__, prefix=prefix):
        leaf = mod.name.rsplit(".", 1)[-1]
        if leaf.startswith("_") or leaf in skip or mod.ispkg:
            continue
        out.append(mod.name)
    return sorted(out)


def _load_strategies() -> tuple[
    dict[str, StrategySpec],
    dict[str, CrossSectionStrategySpec],
]:
    timing: dict[str, StrategySpec] = {}
    cross_section: dict[str, CrossSectionStrategySpec] = {}
    seen: dict[str, str] = {}
    for module_name in _plugin_module_names():
        m = importlib.import_module(module_name)
        if not hasattr(m, "STRATEGY"):
            continue
        spec = m.STRATEGY
        if not isinstance(spec, (StrategySpec, CrossSectionStrategySpec)):
            raise TypeError(
                f"{module_name}: STRATEGY 必须是 StrategySpec 或 CrossSectionStrategySpec"
            )
        if spec.id in seen:
            raise RuntimeError(
                f"重复的策略 id: {spec.id}（{seen[spec.id]} 与 {module_name}）"
            )
        seen[spec.id] = module_name
        if isinstance(spec, StrategySpec):
            timing[spec.id] = spec
        else:
            cross_section[spec.id] = spec
    return timing, cross_section


STRATEGIES, CROSS_SECTION_STRATEGIES = _load_strategies()
ALL_STRATEGIES: dict[str, StrategySpec | CrossSectionStrategySpec] = {
    **STRATEGIES,
    **CROSS_SECTION_STRATEGIES,
}


def get_strategy(strategy_id: str) -> StrategySpec | None:
    return STRATEGIES.get(strategy_id)


def get_cross_section_strategy(
    strategy_id: str,
) -> CrossSectionStrategySpec | None:
    return CROSS_SECTION_STRATEGIES.get(strategy_id)


def get_registered_strategy(
    strategy_id: str,
) -> StrategySpec | CrossSectionStrategySpec | None:
    """从统一注册表获取任一类型的策略。"""
    return ALL_STRATEGIES.get(strategy_id)


def list_cross_section_strategies() -> list[CrossSectionStrategySpec]:
    return [
        CROSS_SECTION_STRATEGIES[key]
        for key in sorted(CROSS_SECTION_STRATEGIES)
    ]
