"""煤炭 ETF 日线 MFI(7) 20/80 策略：超卖买入、超买卖出。

纯日线频率，不叠加任何周线或趋势过滤。信号语义由 ``signal_mode`` 控制：

- ``level``（默认）——状态型：MFI **低于/等于** ``mfi_oversold``（20）即处于超卖区，
  持有多头（空仓时买入一次、持仓时引擎忽略）；MFI **高于/等于** ``mfi_overbought``
  （80）即处于超买区，卖出。
- ``cross``——交叉型：MFI 自下而上**上穿** 20 才买入，自上而下**下穿** 80 才卖出。

两者都不改变 MFI(7) 指标与 20/80 阈值；``level`` 更贴近「超卖区持有、超买区离场」
的经典读法，三段样本外检验均优于 ``cross``（见 docs/coal-etf-mfi-strategy.md）。

MFI（资金流量指数）把成交量加权后的典型价格方向累积成 0~100 的"带量 RSI"，
见 :mod:`app.factors.money_flow`。信号在当日收盘价成交；数据不足 ``mfi_period``
个交易日时 MFI 为 NaN，不产生信号。

示例：

```bash
python -m app.script backtest --strategy coal_etf_mfi --symbol 515220 \\
    --start-date 2021-01-01 --end-date 2026-09-18
```
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from app.backtest_engine import BacktestResult, run_from_signals
from app.factors.money_flow import money_flow_index
from app.strategies.base import BaseBacktestParams, StrategySpec


class CoalEtfMfiParams(BaseModel):
    mfi_period: int = Field(7, ge=2, le=100, description="日线 MFI 窗口（交易日数）")
    mfi_oversold: float = Field(
        20.0, ge=0.0, le=100.0, description="MFI 超卖阈值：level 模式下 MFI≤该值买入，cross 模式下上穿该值买入"
    )
    mfi_overbought: float = Field(
        80.0, ge=0.0, le=100.0, description="MFI 超买阈值：level 模式下 MFI≥该值卖出，cross 模式下下穿该值卖出"
    )
    signal_mode: Literal["level", "cross"] = Field(
        "level",
        description=(
            "信号语义：level=超卖区（MFI≤20）持有、超买区（MFI≥80）离场（默认）；"
            "cross=仅在 MFI 上穿 20 时买入、下穿 80 时卖出。两者都不改变 20/80 阈值。"
        ),
    )


def _compute_signals(
    df: pd.DataFrame,
    params: CoalEtfMfiParams,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """返回 (信号数组, 指标 overlay)；信号 1=买入、-1=卖出、0=观望。"""
    mfi = money_flow_index(
        df["high"].astype(float).to_numpy(),
        df["low"].astype(float).to_numpy(),
        df["close"].astype(float).to_numpy(),
        df["volume"].astype(float).to_numpy(),
        period=params.mfi_period,
    )
    if params.signal_mode == "cross":
        prev = np.concatenate(([np.nan], mfi[:-1]))
        buy = (prev < params.mfi_oversold) & (mfi >= params.mfi_oversold)
        sell = (prev > params.mfi_overbought) & (mfi <= params.mfi_overbought)
    else:
        # 状态型：超卖区每天输出买入（空仓时买一次），超买区每天输出卖出。
        buy = mfi <= params.mfi_oversold
        sell = mfi >= params.mfi_overbought

    signal = np.zeros(len(df), dtype=np.int8)
    signal[buy] = 1
    signal[sell] = -1
    return signal, {"mfi": mfi}


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: CoalEtfMfiParams,
) -> BacktestResult:
    signal, overlays = _compute_signals(df, params)
    return run_from_signals(
        df,
        signal,
        base.initial_cash,
        commission=base.commission,
        stop_loss_pct=base.stop_loss_pct,
        overlays=overlays,
    )


def _signals(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: CoalEtfMfiParams,
) -> np.ndarray:
    signal, _ = _compute_signals(df, params)
    return signal


def _min_bars(params: CoalEtfMfiParams) -> int:
    return params.mfi_period + 2


STRATEGY = StrategySpec(
    id="coal_etf_mfi",
    name="煤炭ETF 日线MFI",
    description=(
        "日线 MFI(7)：上穿 20（超卖回升）买入、下穿 80（超买回落）卖出；"
        "默认标的 515220 煤炭ETF。"
    ),
    params_model=CoalEtfMfiParams,
    min_bars=_min_bars,
    run=_run,
    signals=_signals,
)
