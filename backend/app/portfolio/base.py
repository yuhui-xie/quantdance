"""低频组合策略插件约定：截面选股 + 周期调仓。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

import pandas as pd
from pydantic import BaseModel


@dataclass(frozen=True)
class PortfolioSelectContext:
    """调仓日选股上下文。"""

    panel: Mapping[str, Mapping[str, pd.DataFrame]]
    names: Mapping[str, str]


PortfolioSelect = Callable[
    [str, PortfolioSelectContext, BaseModel],
    tuple[list[str], list[dict[str, Any]]],
]


@dataclass(frozen=True)
class PortfolioStrategySpec:
    """
    每个组合策略模块导出 STRATEGY: PortfolioStrategySpec。

    与单票 StrategySpec 平行：负责“怎么选股”；资金/调仓/成交由统一 runner 执行。
    """

    id: str
    name: str
    description: str
    params_model: type[BaseModel]
    select: PortfolioSelect
    default_universe: str = "all_a"
    needs_dividend: bool = False
    default_top_n: int = 10
    warnings: tuple[str, ...] = ()
