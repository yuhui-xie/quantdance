"""策略插件约定：注册对象 StrategySpec + 公共回测参数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd
from pydantic import BaseModel

from app.backtest_engine import BacktestResult


@dataclass(frozen=True)
class BaseBacktestParams:
    """所有策略共享的资金与费率参数。"""

    initial_cash: float
    commission: float


StrategyRun = Callable[[pd.DataFrame, BaseBacktestParams, BaseModel], BacktestResult]
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
