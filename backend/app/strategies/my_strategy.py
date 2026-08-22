"""my_strategy：LLT 斜率动量策略（初版）。

组合语义（滞回）：
- 进场：LLT 斜率上穿 ``+slope_threshold`` 时由空仓转持仓（动量向上）。
- 离场：LLT 斜率下穿 ``-slope_threshold`` 时由持仓转空仓（动量转弱）。
  介于正负阈值之间为观望死区，用于过滤拐点附近的噪音震荡；设
  ``slope_threshold=0`` 即退化为原始过零逻辑（上穿 0 买入、下穿 0 卖出）。

「方向/时机」由 LLT 斜率刻画，正负阈值构成滞回带，避免单一信号反复进出。
斜率默认用单点差分（变动快）；设 ``slope_fit_window>=2`` 可改用滚动最小二乘
拟合斜率（更平滑、对噪音更稳健）。此时还可设 ``min_fit_r2`` 做拟合质量过滤：
R² 过低（拟合方差大、斜率不可信）时放弃进场。

大环境过滤（可选）：用 Choppiness Index 判断行情是否处于震荡市。
当 CHOP > chop_threshold（默认 62）判定为震荡市，此时不开趋势单、
强制空仓，避免趋势策略在无趋势行情中反复进出。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from app.backtest_engine import BacktestResult, run_from_signals
from app.factors import adx, choppiness_index, llt, vpt
from app.factors.llt import llt_slope, llt_slope_fit
from app.strategies.base import BaseBacktestParams, StrategySpec


class MyStrategyParams(BaseModel):
    # --- LLT 斜率（动量方向）---
    llt_period: int = Field(20, ge=2, le=400, description="LLT 平滑周期")
    slope_lookback: int = Field(
        1,
        ge=1,
        le=60,
        description="判断 LLT 斜率时回看的交易日数（仅用于 slope_fit_window=1 时的单点差分）",
    )
    slope_fit_window: int = Field(
        1,
        ge=1,
        le=120,
        description=(
            "LLT 斜率拟合窗口：>=2 时对最近该根 K 线的 LLT 趋势线做最小二乘"
            "线性回归、取拟合斜率（对噪音更稳健、变化更平滑，窗口越大越平缓）；"
            "=1 时退化为单点差分（llt_slope，变动快）。"
        ),
    )
    min_fit_r2: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "回归拟合质量（R²）阈值，仅 slope_fit_window>=2 时生效：R² 越低说明"
            "窗口内涨跌越混乱、拟合方差越大、斜率越不可信。R² 低于此值时放弃进场"
            "（视为无可靠趋势）。设 0 表示不做拟合质量过滤。"
        ),
    )
    slope_threshold: float = Field(
        0.0,
        ge=0.0,
        description=(
            "斜率阈值（价格单位）：斜率上穿 +threshold 才买入，下穿 "
            "-threshold 才卖出；介于正负阈值之间为观望死区，用于过滤拐点附近"
            "的噪音震荡。设 0 即退化为原始过零逻辑。"
        ),
    )

    # --- 大环境过滤（震荡市不开趋势单）---
    use_chop_filter: bool = Field(
        True,
        description="是否启用 Choppiness 震荡市过滤；为 False 时按原逻辑交易",
    )
    chop_period: int = Field(14, ge=2, le=400, description="Choppiness Index 周期")
    chop_threshold: float = Field(
        62.0,
        ge=0,
        le=100,
        description="CHOP 高于此值判定为震荡市，不开趋势单（空仓）",
    )

    # --- ADX 趋势强度过滤（无趋势/震荡市不开趋势单）---
    use_adx_filter: bool = Field(
        True,
        description="是否启用 ADX 趋势强度过滤：ADX 低于 adx_threshold 视为无趋势/震荡，不开趋势单",
    )
    adx_period: int = Field(14, ge=2, le=400, description="ADX 周期")
    adx_threshold: float = Field(
        20.0,
        ge=0,
        le=100,
        description="ADX 低于此值判定为无趋势/震荡市，不开趋势单（空仓）",
    )

    # --- VPT 量价过滤（量价共振才做趋势）---
    use_vpt_filter: bool = Field(
        False,
        description="是否启用 VPT 量价过滤：要求 VPT 斜率非负（量价共振向上）才允许持仓，VPT 斜率转负则离场",
    )
    vpt_slope_lookback: int = Field(
        1,
        ge=1,
        le=60,
        description="判断 VPT 斜率时回看的交易日数",
    )


def _positions(
    slope: np.ndarray,
    ranging: np.ndarray,
    vpt_ok: np.ndarray,
    params: MyStrategyParams,
    slope_ok: np.ndarray | None = None,
) -> np.ndarray:
    """按「斜率上穿 +阈值」进场、「下穿 -阈值」离场，生成持仓状态。

    - 进场：``slope > +slope_threshold`` 才由空仓转持仓（动量向上）。
    - 离场：``slope < -slope_threshold`` 才由持仓转空仓（动量转弱）。
      介于正负阈值之间为观望死区，保持原状态，避免拐点附近反复进出。

    ``ranging`` 为布尔掩码，True 表示处于震荡市（CHOP > threshold），
    该区间强制空仓、不开趋势单。

    ``vpt_ok`` 为布尔掩码，True 表示量价共振向上（VPT 斜率非负）。
    启用 ``use_vpt_filter`` 时：进场还需 ``vpt_ok``；持仓期间 VPT 斜率
    转负（量价背离）也触发离场。未启用时该掩码恒为 True，不影响原逻辑。

    ``slope_ok`` 为可选布尔掩码，True 表示斜率可信（如回归拟合 R² 达阈值）。
    提供时：进场还需 ``slope_ok``（拟合差、斜率不可信则放弃进场）；缺省
    为 None 时视为恒 True，不影响原逻辑。
    """
    n = len(slope)
    positions = np.zeros(n, dtype=np.int8)
    active = 0
    for i in range(n):
        # 震荡市：不开趋势单，强制空仓
        if ranging[i]:
            active = 0
            positions[i] = active
            continue
        sl = slope[i]
        if not np.isfinite(sl):
            positions[i] = active
            continue
        if not active:
            if (
                sl > params.slope_threshold
                and (slope_ok is None or slope_ok[i])
                and (not params.use_vpt_filter or vpt_ok[i])
            ):
                active = 1
        else:
            if sl < -params.slope_threshold or (
                params.use_vpt_filter and not vpt_ok[i]
            ):
                active = 0
        positions[i] = active
    return positions


def _positions_to_events(positions: np.ndarray) -> np.ndarray:
    events = np.zeros(len(positions), dtype=np.int8)
    previous = 0
    for i, current in enumerate(positions):
        value = int(current)
        if value != previous:
            events[i] = 1 if value else -1
        previous = value
    return events


def _compute(
    df: pd.DataFrame,
    params: MyStrategyParams,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """计算持仓状态、事件信号与叠加曲线。"""
    close = df["close"].astype(float).to_numpy()
    trend = llt(close, period=params.llt_period)
    # 斜率：>=2 时用滚动最小二乘拟合（更平滑），否则用单点差分（变动快）
    if params.slope_fit_window >= 2:
        slope, fit_r2 = llt_slope_fit(trend, params.slope_fit_window)
        # 拟合质量过滤：R² 低于阈值视为斜率不可信，放弃进场
        slope_ok = np.where(
            np.isfinite(fit_r2), fit_r2 >= params.min_fit_r2, True
        )
    else:
        slope = llt_slope(trend, params.slope_lookback)
        fit_r2 = np.full(len(slope), np.nan, dtype=float)
        slope_ok = np.ones(len(slope), dtype=bool)

    # VPT 量价趋势：斜率非负视为量价共振向上。
    if "volume" in df.columns:
        volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(float).to_numpy()
    else:
        volume = np.zeros(len(close), dtype=float)
    vpt_arr = vpt(close, volume)
    vpt_slope = llt_slope(vpt_arr, params.vpt_slope_lookback)
    vpt_ok = np.where(np.isfinite(vpt_slope), vpt_slope >= 0, True)

    if params.use_chop_filter:
        chop = choppiness_index(
            df["high"].astype(float).to_numpy(),
            df["low"].astype(float).to_numpy(),
            close,
            period=params.chop_period,
        )
    else:
        chop = np.full(len(close), np.nan, dtype=float)
    ranging_chop = np.where(np.isfinite(chop), chop > params.chop_threshold, False)

    # ADX 趋势强度过滤：ADX 低于阈值视为无趋势/震荡，不开趋势单。
    if params.use_adx_filter:
        high = df["high"].astype(float).to_numpy()
        low = df["low"].astype(float).to_numpy()
        adx_arr = adx(high, low, close, period=params.adx_period)
    else:
        adx_arr = np.full(len(close), np.nan, dtype=float)
    adx_weak = np.where(np.isfinite(adx_arr), adx_arr < params.adx_threshold, False)

    # 组合判定无趋势/震荡 → 强制空仓；CHOP 与 ADX 任一路径成立即触发。
    ranging = ranging_chop | adx_weak

    positions = _positions(slope, ranging, vpt_ok, params, slope_ok)
    signal = _positions_to_events(positions)
    overlays: dict[str, np.ndarray] = {
        "llt": trend,
        "llt_dt": slope,
        "ranging": ranging.astype(np.int8),
        "position": positions,
    }
    # 仅在对应过滤启用时才输出叠加曲线，避免报告绘制无效/恒值的指标图
    if params.slope_fit_window >= 2:
        overlays["llt_r2"] = fit_r2
        overlays["llt_fit_ok"] = slope_ok.astype(np.int8)
    if params.use_chop_filter:
        overlays["chop"] = chop
        overlays["chop_ranging"] = ranging_chop.astype(np.int8)
    if params.use_adx_filter:
        overlays["adx"] = adx_arr
        overlays["adx_weak"] = adx_weak.astype(np.int8)
    if params.use_vpt_filter:
        overlays["vpt"] = vpt_arr
        overlays["vpt_slope"] = vpt_slope
        overlays["vpt_ok"] = vpt_ok.astype(np.int8)
    return signal, overlays


def _run(
    df: pd.DataFrame,
    base: BaseBacktestParams,
    params: MyStrategyParams,
) -> BacktestResult:
    signal, overlays = _compute(df, params)
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
    params: MyStrategyParams,
) -> np.ndarray:
    signal, _overlays = _compute(df, params)
    return signal


def _min_bars(params: MyStrategyParams) -> int:
    need = params.llt_period
    if params.use_chop_filter:
        need = max(need, params.chop_period)
    if params.use_adx_filter:
        need = max(need, params.adx_period)
    if params.use_vpt_filter:
        need = max(need, params.vpt_slope_lookback)
    # 斜率暖机：单点差分需 slope_lookback，回归拟合需 slope_fit_window-1
    slope_warmup = max(params.slope_lookback, params.slope_fit_window - 1)
    return int(need) + slope_warmup + 1


STRATEGY = StrategySpec(
    id="my_strategy",
    name="我的策略",
    description="LLT 斜率动量策略：斜率上穿 +阈值买入、下穿 -阈值卖出，正负阈值之间为观望死区（滞回），设 0 退化为过零逻辑；默认启用 ADX 趋势强度过滤（ADX<20 视为无趋势不开单）与 Choppiness（CHOP>62）震荡市过滤，震荡市强制空仓；可选启用 VPT 量价过滤，要求 VPT 斜率非负（量价共振）才持仓。",
    params_model=MyStrategyParams,
    min_bars=_min_bars,
    run=_run,
    signals=_signals,
)
