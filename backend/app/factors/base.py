"""因子 handler 与注册条目的公共类型。

独立成叶模块（不 import 任何 ``app.factors`` 子模块），供各因子模块与
``app/factors/registry.py`` 共用，避免循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

# 单只股票 DataFrame（DatetimeIndex，含 open/high/low/close/volume[/amount]）
# → 因子时间序列 Series（索引对齐 df，含 NaN 暖机）。
FactorFn = Callable[[pd.DataFrame], pd.Series]


@dataclass(frozen=True)
class FactorSpec:
    """单只股票的因子注册条目。

    - ``name``：因子 id（CLI / JSON 用）。
    - ``fn``：DataFrame → 因子时间序列（索引对齐 df，含 NaN 暖机）。
    - ``min_bars``：暖机所需最少 K 线，用于裁剪与统计有效性。

    来源标签（screening / momentum / strategy）不由本条目携带，由
    ``app/factors/registry.py`` 按所属模块自动推断（见其 ``_MODULE_SOURCE``）。
    各因子模块导出本模块自己的 ``FACTORS: list[FactorSpec]``，由 registry 自动汇总；
    注册表 / 最小 K 线 / 来源分组均由汇总结果派生，保证各处不脱节。
    """

    name: str
    fn: FactorFn
    min_bars: int = 1
