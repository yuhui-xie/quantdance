"""递归扫描 app.factors，按各模块导出的 ``FACTORS`` 自动汇总因子注册表。

与 ``app/strategies/registry.py`` 的插件式自动注册同思路：因子模块只需导出
``FACTORS: list[FactorSpec]`` 即被自动收集，无需在 ic_analysis 里手工登记。
"""

from __future__ import annotations

import importlib
import pkgutil

from app.factors.base import FactorFn, FactorSpec


def _plugin_module_names() -> list[str]:
    import app.factors as factors_pkg

    skip = frozenset({"base", "registry"})
    out: list[str] = []
    prefix = f"{factors_pkg.__name__}."
    for mod in pkgutil.walk_packages(factors_pkg.__path__, prefix=prefix):
        leaf = mod.name.rsplit(".", 1)[-1]
        if leaf.startswith("_") or leaf in skip or mod.ispkg:
            continue
        out.append(mod.name)
    return sorted(out)


# 来源按所属模块推断：screen_factors → screening，momentum → momentum，其余默认 strategy
_MODULE_SOURCE: dict[str, str] = {
    "screen_factors": "screening",
    "momentum": "momentum",
}


def _load_factors() -> tuple[list[FactorSpec], dict[str, str]]:
    """返回 (FACTORS, 因子名 → 来源标签)。"""
    specs: list[FactorSpec] = []
    name_source: dict[str, str] = {}
    seen: dict[str, str] = {}
    for module_name in _plugin_module_names():
        m = importlib.import_module(module_name)
        if not hasattr(m, "FACTORS"):
            continue
        leaf = module_name.rsplit(".", 1)[-1]
        source = _MODULE_SOURCE.get(leaf, "strategy")
        for spec in m.FACTORS:
            if not isinstance(spec, FactorSpec):
                raise TypeError(
                    f"{module_name}: FACTORS 元素必须是 FactorSpec（got {type(spec).__name__}）"
                )
            if spec.name in seen:
                raise RuntimeError(
                    f"重复的因子: {spec.name}（{seen[spec.name]} 与 {module_name}）"
                )
            seen[spec.name] = module_name
            specs.append(spec)
            name_source[spec.name] = source
    return specs, name_source


FACTORS, _FACTOR_SOURCE = _load_factors()

# 由 FACTORS 派生的查找结构
FACTOR_REGISTRY: dict[str, FactorFn] = {s.name: s.fn for s in FACTORS}
FACTOR_MIN_BARS: dict[str, int] = {s.name: s.min_bars for s in FACTORS}

_SOURCE_GROUPS: dict[str, frozenset[str]] = {
    src: frozenset(n for n, s in _FACTOR_SOURCE.items() if s == src)
    for src in ("screening", "momentum", "strategy")
}
SCREENING_FACTORS: frozenset[str] = _SOURCE_GROUPS["screening"]
MOMENTUM_FACTORS: frozenset[str] = _SOURCE_GROUPS["momentum"]
STRATEGY_FACTORS: frozenset[str] = _SOURCE_GROUPS["strategy"]


def factor_source(name: str) -> str:
    """因子来源标签：screening（选股技术） / momentum（动量趋势） / strategy（策略信号）。"""
    return _FACTOR_SOURCE.get(name, "screening")
