"""扫描 app.portfolio 包内策略模块并收集导出的 STRATEGY。"""

from __future__ import annotations

import importlib
import pkgutil

from app.portfolio.base import PortfolioStrategySpec


def _plugin_module_names() -> list[str]:
    import app.portfolio as portfolio_pkg

    skip = frozenset({"base", "registry", "runner", "common", "universe"})
    out: list[str] = []
    for mod in pkgutil.iter_modules(portfolio_pkg.__path__):
        if mod.name.startswith("_") or mod.name in skip:
            continue
        out.append(mod.name)
    return sorted(out)


def _load_strategies() -> dict[str, PortfolioStrategySpec]:
    registry: dict[str, PortfolioStrategySpec] = {}
    for name in _plugin_module_names():
        m = importlib.import_module(f"app.portfolio.{name}")
        if not hasattr(m, "STRATEGY"):
            continue
        spec = m.STRATEGY
        if not isinstance(spec, PortfolioStrategySpec):
            raise TypeError(f"app.portfolio.{name}: STRATEGY 必须是 PortfolioStrategySpec")
        if spec.id in registry:
            raise RuntimeError(f"重复的组合策略 id: {spec.id}")
        registry[spec.id] = spec
    return registry


PORTFOLIO_STRATEGIES: dict[str, PortfolioStrategySpec] = _load_strategies()


def get_portfolio_strategy(strategy_id: str) -> PortfolioStrategySpec | None:
    return PORTFOLIO_STRATEGIES.get(strategy_id)


def list_portfolio_strategies() -> list[PortfolioStrategySpec]:
    return [PORTFOLIO_STRATEGIES[k] for k in sorted(PORTFOLIO_STRATEGIES)]
