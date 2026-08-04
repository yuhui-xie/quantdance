"""策略插件约定：注册对象 StrategySpec + 公共回测参数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np
import pandas as pd
from pydantic import BaseModel

from app.backtest_engine import BacktestResult


@dataclass(frozen=True)
class BaseBacktestParams:
    """所有策略共享的资金、费率与风控参数。"""

    initial_cash: float
    commission: float
    stop_loss_pct: float | None = None


StrategyRun = Callable[[pd.DataFrame, BaseBacktestParams, BaseModel], BacktestResult]
StrategySignals = Callable[
    [pd.DataFrame, BaseBacktestParams, BaseModel],
    Sequence[int] | np.ndarray,
]
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
    signals: StrategySignals | None = None

    def compute_signals(
        self,
        df: pd.DataFrame,
        base: BaseBacktestParams,
        params: BaseModel,
    ) -> np.ndarray:
        """
        返回策略的原始事件信号。

        新策略可提供独立 signals 回调；旧策略则从通用回测结果读取信号，
        以保持现有插件完全兼容。
        """
        raw = self.signals(df, base, params) if self.signals else self.run(df, base, params).signal
        signal = np.asarray(raw, dtype=np.int8)
        if len(signal) != len(df):
            raise ValueError(f"策略 {self.id} 的 signal 长度必须与行情数据一致")
        if not np.isin(signal, (-1, 0, 1)).all():
            raise ValueError(f"策略 {self.id} 的 signal 只能包含 -1、0、1")
        return signal
