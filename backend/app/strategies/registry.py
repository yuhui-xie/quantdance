"""扫描 app.strategies 包内模块并收集导出的 STRATEGY。"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

from app.strategies.base import StrategySpec

if TYPE_CHECKING:
    pass


def _plugin_module_names() -> list[str]:
    import app.strategies as strategies_pkg

    skip = frozenset({"base", "registry"})
    out: list[str] = []
    for mod in pkgutil.iter_modules(strategies_pkg.__path__):
        if mod.name.startswith("_") or mod.name in skip:
            continue
        out.append(mod.name)
    return sorted(out)


def _load_strategies() -> dict[str, StrategySpec]:
    registry: dict[str, StrategySpec] = {}
    for name in _plugin_module_names():
        m = importlib.import_module(f"app.strategies.{name}")
        if not hasattr(m, "STRATEGY"):
            continue
        spec = m.STRATEGY
        if not isinstance(spec, StrategySpec):
            raise TypeError(f"app.strategies.{name}: STRATEGY 必须是 StrategySpec")
        if spec.id in registry:
            raise RuntimeError(f"重复的策略 id: {spec.id}")
        registry[spec.id] = spec
    return registry


STRATEGIES: dict[str, StrategySpec] = _load_strategies()


def get_strategy(strategy_id: str) -> StrategySpec | None:
    return STRATEGIES.get(strategy_id)
