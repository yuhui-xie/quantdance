"""RSRS（阻力支撑相对强度）：high~low 回归斜率 beta 的时序标准分 × R² × beta。

原始 RSRS 用「最高价对最低价」的滚动 OLS 斜率刻画支撑/阻力的相对强度：一段时间内
high 相对 low抬升越快，beta 越大 = 上行动能越强。本模块按项目口径给出**带趋势方向**的
合成值：

    1. 取最近 ``regression_days``（默认 18）根 K 线，以 low 为自变量、high 为因变量做
       最小二乘回归，得斜率 beta 与拟合优度 R²（逐窗滚动，每个交易日一个窗口）；
    2. 取最近 ``zscore_days``（默认 120）个 beta，做**时序**标准分
       ``z = (beta_last − mean(betas)) / std(betas)``（ddof=0，与
       :func:`app.factors.cross_section.zscore` 口径一致）；
    3. ``rsrs = z × R² × beta``。

口径说明：经典 RSRS 为 ``z(beta) × R²``（只表达"相对自身历史有多强"）。本实现额外乘
``beta`` 生值，使 rsrs 的**符号**跟随斜率方向（beta<0 即区间内 high 随 low 下行 →
值为负），从而可直接用作"是否允许持有"的绝对门槛（见 etf_rotation 的 RSRS 门控），
而不必再叠加一次趋势方向判断。

数据要求：至少 ``regression_days + zscore_days − 1`` 根 K 线（默认 137），且
``history`` 需含 high/low 列；不足或窗口方差退化时返回 None（调用方自行决定放行与否）。
只使用传入 ``history`` 中的数据，不做任何截断/取未来值的处理——无前视由调用方保证
（策略侧传 :func:`app.factors.momentum.price_history` 的 asof 截断结果）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# 默认参数：18 日回归窗 / 120 个 beta 的时序标准分窗（RSRS 常用口径）。
DEFAULT_REGRESSION_DAYS = 18
DEFAULT_ZSCORE_DAYS = 120


@dataclass(frozen=True)
class RsrsValue:
    """单次 RSRS 计算结果：末窗斜率 beta、R²、beta 的时序标准分 z 与合成值 value。"""

    beta: float
    r_squared: float
    zscore: float
    value: float


def rolling_beta_r2(
    low: np.ndarray,
    high: np.ndarray,
    window: int,
) -> tuple[np.ndarray, np.ndarray]:
    """滚动 OLS：以 low 为自变量、high 为因变量，返回逐窗 ``(beta, R²)`` 两个数组。

    第 i 个元素对应 ``low/high`` 的第 ``[i, i+window)`` 段（长度 = ``len - window + 1``）。
    用累积量（Σx、Σy、Σxy、Σx²、Σy²）一次算完全部窗口，等价于逐窗
    :func:`app.factors.momentum.linreg`，但避免 Python 循环。低/高价方差退化为 0
    （窗口内横盘）的窗口返回 NaN。
    """
    low_s = pd.Series(np.asarray(low, dtype=float))
    high_s = pd.Series(np.asarray(high, dtype=float))
    n = float(window)
    sx = low_s.rolling(window).sum()
    sy = high_s.rolling(window).sum()
    sxy = (low_s * high_s).rolling(window).sum()
    sxx = (low_s * low_s).rolling(window).sum()
    syy = (high_s * high_s).rolling(window).sum()
    den_x = n * sxx - sx * sx          # n × Var(low)
    den_y = n * syy - sy * sy          # n × Var(high)
    num = n * sxy - sx * sy            # n × Cov(low, high)
    beta = num / den_x
    r_squared = (num * num) / (den_x * den_y)
    degenerate = ~((den_x > 0) & (den_y > 0))  # 任一方差退化 → 斜率/拟合度无定义
    beta = beta.mask(degenerate)
    r_squared = r_squared.mask(degenerate)
    # 前 window-1 个窗口样本不足（rolling 的 min_periods=window 已置 NaN），裁掉。
    return (
        beta.to_numpy(dtype=float)[window - 1 :],
        r_squared.to_numpy(dtype=float)[window - 1 :],
    )


def rsrs(
    history: pd.DataFrame,
    *,
    regression_days: int = DEFAULT_REGRESSION_DAYS,
    zscore_days: int = DEFAULT_ZSCORE_DAYS,
) -> RsrsValue | None:
    """计算末根 K 线的 RSRS = ``z(beta) × R² × beta``；数据不足/退化返回 None。

    ``history`` 为按日期升序、已去重的日线 DataFrame（需含 high/low 列，通常来自
    :func:`app.factors.momentum.price_history`），本函数只取**末尾**
    ``regression_days + zscore_days − 1`` 根参与计算。beta 序列里无效窗口（横盘致方差
    为 0 或非有限）被剔除后再做时序标准分；有效 beta 少于 2 个或标准差为 0（beta 恒定）
    时 z 记 0（此时 ``value = 0``）。
    """
    if not {"high", "low"}.issubset(history.columns):
        return None
    need = regression_days + zscore_days - 1
    if len(history) < need:
        return None
    high = pd.to_numeric(history["high"], errors="coerce").to_numpy(dtype=float)[-need:]
    low = pd.to_numeric(history["low"], errors="coerce").to_numpy(dtype=float)[-need:]
    if not (np.all(np.isfinite(high)) and np.all(np.isfinite(low))):
        return None

    betas, r_squareds = rolling_beta_r2(low, high, regression_days)
    valid = np.isfinite(betas) & np.isfinite(r_squareds)
    betas = betas[valid][-zscore_days:]
    r_squareds = r_squareds[valid][-zscore_days:]
    if betas.size < 2:
        return None

    std = float(betas.std())
    mean = float(betas.mean())
    z = 0.0 if std == 0.0 or not np.isfinite(std) else float((betas[-1] - mean) / std)
    beta_last = float(betas[-1])
    r_squared_last = float(r_squareds[-1])
    return RsrsValue(
        beta=beta_last,
        r_squared=r_squared_last,
        zscore=z,
        value=z * r_squared_last * beta_last,
    )
