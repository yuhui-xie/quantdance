"""A 股技术面因子选股：预设打分、遍历标的、聚合结果（演示用途）。"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd

from app.data_sources.market_data import (
    MarketDataError,
    fetch_a_share_daily,
    fetch_a_share_universe,
    fetch_a_share_valuation_snapshot,
)
from app.schemas import ScreenFactorConfig, ScreenItem, ScreenRequest, ScreenResponse

PresetId = Literal[
    "momentum",
    "volume_pulse",
    "ma_alignment",
    "low_volatility",
    "short_reversal",
    "liquidity",
    "value_tilt",
    "low_pe",
    "low_pb",
    "low_ps",
    "quality_value",
    "dividend_tilt",
]

# 因子窗口：与 fetch 最少 K 线对齐
MIN_BARS = 65

FUNDAMENTAL_PRESETS: frozenset[str] = frozenset(
    {
        "value_tilt",
        "low_pe",
        "low_pb",
        "low_ps",
        "quality_value",
        "dividend_tilt",
    }
)

FUNDAMENTAL_FACTOR_FIELDS: frozenset[str] = frozenset(
    {
        "pe_ttm",
        "pb",
        "ps_ttm",
        "roe",
        "dividend_yield",
        "market_cap",
        "float_market_cap",
        "turnover_rate",
    }
)

TECHNICAL_FACTOR_MIN_BARS: dict[str, int] = {
    "ret_20": 61,
    "ret_60": 61,
    "vol_20": 61,
    "vol_60": 61,
    "vol_ratio": 25,
    "price_vs_ma5": 25,
    "ma_spread": 25,
    "sma5_slope": 25,
    "ret_5": 12,
    "ret_10": 12,
    "rel_vol": 21,
    "amihud_illiq_20": 21,
}

SUPPORTED_FACTOR_FIELDS: frozenset[str] = frozenset(
    set(TECHNICAL_FACTOR_MIN_BARS) | set(FUNDAMENTAL_FACTOR_FIELDS)
)

PRESET_MIN_BARS: dict[str, int] = {
    "low_volatility": 65,
    # 因子仅需约 12 根 K 线，略留余量（不与全局限定 MIN_BARS 混用）
    "short_reversal": 15,
    "liquidity": 65,
    "value_tilt": MIN_BARS,
    "low_pe": MIN_BARS,
    "low_pb": MIN_BARS,
    "low_ps": MIN_BARS,
    "quality_value": MIN_BARS,
    "dividend_tilt": MIN_BARS,
}

DISCLAIMER = "演示用途，技术面筛选不构成投资建议。"


def _min_bars_for_preset(preset: PresetId) -> int:
    if preset in PRESET_MIN_BARS:
        return PRESET_MIN_BARS[preset]
    return MIN_BARS


def _configured_factor_fields(config: ScreenFactorConfig | None) -> set[str]:
    if config is None:
        return set()
    return {rule.field for rule in config.factors}


def _min_bars_for_request(req: ScreenRequest) -> int:
    fields = _configured_factor_fields(req.factor_config)
    if not fields:
        return _min_bars_for_preset(req.preset)
    unknown = sorted(fields - SUPPORTED_FACTOR_FIELDS)
    if unknown:
        raise ValueError(f"不支持的自定义选股因子字段: {', '.join(unknown)}")
    technical_mins = [TECHNICAL_FACTOR_MIN_BARS[field] for field in fields if field in TECHNICAL_FACTOR_MIN_BARS]
    return max(technical_mins, default=1)


def _needs_fundamental(req: ScreenRequest) -> bool:
    fields = _configured_factor_fields(req.factor_config)
    if fields:
        return bool(fields & FUNDAMENTAL_FACTOR_FIELDS)
    return req.preset in FUNDAMENTAL_PRESETS


def _minmax_norm(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    span = hi - lo
    if span < 1e-15:
        return [0.5 for _ in values]
    return [(v - lo) / span for v in values]


def _fundamental_entry_ok(preset: PresetId, fund: dict[str, float]) -> bool:
    """按预设检查估值快照中是否具备入池所需字段（均为已清洗的正数）。"""
    if preset == "value_tilt":
        return "pe_ttm" in fund and "pb" in fund
    if preset == "low_pe":
        return "pe_ttm" in fund
    if preset == "low_pb":
        return "pb" in fund
    if preset == "low_ps":
        return "ps_ttm" in fund
    if preset == "quality_value":
        return "pe_ttm" in fund and "pb" in fund and "roe" in fund
    if preset == "dividend_tilt":
        return "dividend_yield" in fund
    return False


def compute_momentum_breakdown(df: pd.DataFrame) -> dict[str, float] | None:
    """20/60 日收益与 20 日收益波动率（惩罚项）。"""
    if len(df) < 61:
        return None
    close = df["close"].astype(float)
    ret_20 = float(close.iloc[-1] / close.iloc[-21] - 1.0)
    ret_60 = float(close.iloc[-1] / close.iloc[-61] - 1.0)
    daily = close.pct_change().dropna()
    if len(daily) < 20:
        return None
    vol_20 = float(daily.iloc[-20:].std())
    if not np.isfinite(vol_20):
        vol_20 = 0.0
    return {"ret_20": ret_20, "ret_60": ret_60, "vol_20": vol_20}


def compute_volume_pulse_breakdown(df: pd.DataFrame) -> dict[str, float] | None:
    """量比（相对过去 20 日均量，不含当日）+ 相对短期均线位置。优先用成交额。"""
    if len(df) < 25:
        return None
    close = df["close"].astype(float)
    if "amount" in df.columns:
        amount = pd.to_numeric(df["amount"], errors="coerce")
        if amount.notna().any() and float(amount.fillna(0).abs().sum()) > 0:
            activity = amount.astype(float)
        else:
            activity = df["volume"].astype(float)
    else:
        activity = df["volume"].astype(float)
    # 过去 20 日均量，不含当日
    vol_ma = float(activity.iloc[-21:-1].mean())
    if vol_ma < 1e-12:
        return None
    vol_ratio = float(activity.iloc[-1] / vol_ma)
    sma5 = float(close.iloc[-5:].mean())
    if sma5 < 1e-12:
        return None
    price_vs_ma5 = float(close.iloc[-1] / sma5 - 1.0)
    return {"vol_ratio": vol_ratio, "price_vs_ma5": price_vs_ma5}


def compute_ma_alignment_breakdown(df: pd.DataFrame) -> dict[str, float] | None:
    """短均线高于长均线 + 短期均线近端斜率为正。"""
    if len(df) < 25:
        return None
    close = df["close"].astype(float)
    sma5 = close.rolling(5).mean()
    sma20 = close.rolling(20).mean()
    s5 = float(sma5.iloc[-1])
    s20 = float(sma20.iloc[-1])
    if s20 < 1e-12:
        return None
    spread = (s5 - s20) / s20
    s5_prev = float(sma5.iloc[-4]) if len(sma5) >= 4 else s5
    denom = abs(s5_prev) if abs(s5_prev) > 1e-12 else 1.0
    slope_sma5 = (s5 - s5_prev) / denom
    return {"ma_spread": float(spread), "sma5_slope": float(slope_sma5)}


def compute_low_volatility_breakdown(df: pd.DataFrame) -> dict[str, float] | None:
    """20/60 日已实现波动率（日收益标准差）：数值越低表示波动越低。"""
    if len(df) < 61:
        return None
    close = df["close"].astype(float)
    daily = close.pct_change().dropna()
    if len(daily) < 60:
        return None
    vol_20 = float(daily.iloc[-20:].std())
    vol_60 = float(daily.iloc[-60:].std())
    if not np.isfinite(vol_20):
        vol_20 = 0.0
    if not np.isfinite(vol_60):
        vol_60 = 0.0
    return {"vol_20": vol_20, "vol_60": vol_60}


def compute_short_reversal_breakdown(df: pd.DataFrame) -> dict[str, float] | None:
    """近 5/10 日收益：偏好更低近期收益（与动量预设相反，偏短期反转/超跌）。"""
    if len(df) < 12:
        return None
    close = df["close"].astype(float)
    ret_5 = float(close.iloc[-1] / close.iloc[-6] - 1.0)
    ret_10 = float(close.iloc[-1] / close.iloc[-11] - 1.0)
    return {"ret_5": ret_5, "ret_10": ret_10}


def compute_liquidity_breakdown(df: pd.DataFrame) -> dict[str, float] | None:
    """相对成交量（今日量/20 日均量）与 Amihud 风格非流动性（|r|/成交量，越低越易成交）。"""
    if len(df) < 21:
        return None
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    vm = float(vol.iloc[-20:].mean())
    if vm < 1e-12:
        return None
    rel_vol = float(vol.iloc[-1] / vm)
    ret = close.pct_change()
    r20 = ret.iloc[-20:]
    v20 = vol.iloc[-20:]
    illiq = float((r20.abs() / (v20 + 1e-6)).mean())
    if not np.isfinite(illiq):
        illiq = 0.0
    return {"rel_vol": rel_vol, "amihud_illiq_20": illiq}


def _breakdown_for_preset(df: pd.DataFrame, preset: PresetId) -> dict[str, float] | None:
    if preset == "momentum":
        return compute_momentum_breakdown(df)
    if preset == "volume_pulse":
        return compute_volume_pulse_breakdown(df)
    if preset == "ma_alignment":
        return compute_ma_alignment_breakdown(df)
    if preset == "low_volatility":
        return compute_low_volatility_breakdown(df)
    if preset == "short_reversal":
        return compute_short_reversal_breakdown(df)
    if preset == "liquidity":
        return compute_liquidity_breakdown(df)
    if preset in FUNDAMENTAL_PRESETS:
        return None
    return None


def _technical_breakdown_for_fields(df: pd.DataFrame, fields: set[str]) -> dict[str, float] | None:
    breakdown: dict[str, float] = {}
    groups: list[tuple[set[str], Any]] = [
        ({"ret_20", "ret_60", "vol_20"}, compute_momentum_breakdown),
        ({"vol_ratio", "price_vs_ma5"}, compute_volume_pulse_breakdown),
        ({"ma_spread", "sma5_slope"}, compute_ma_alignment_breakdown),
        ({"vol_20", "vol_60"}, compute_low_volatility_breakdown),
        ({"ret_5", "ret_10"}, compute_short_reversal_breakdown),
        ({"rel_vol", "amihud_illiq_20"}, compute_liquidity_breakdown),
    ]
    for group_fields, compute in groups:
        if not (fields & group_fields):
            continue
        group = compute(df)
        if group is None:
            return None
        breakdown.update(group)

    missing = fields - set(breakdown)
    if missing:
        return None
    return {field: breakdown[field] for field in fields}


def _entry_ok_for_factor_config(config: ScreenFactorConfig, entry: dict[str, float]) -> bool:
    for rule in config.factors:
        value = entry.get(rule.field)
        if value is None or not np.isfinite(float(value)):
            return False
    return True


def _score_from_factor_config(
    config: ScreenFactorConfig,
    entries: list[tuple[str, str | None, dict[str, float]]],
) -> list[ScreenItem]:
    """按请求中的自定义因子方向与权重做横截面打分。"""
    if not entries:
        return []

    weight_sum = sum(rule.weight for rule in config.factors)
    scores = [0.0 for _ in entries]
    for rule in config.factors:
        values = [e[2][rule.field] for e in entries]
        normed = _minmax_norm(values)
        if rule.direction == "lower":
            normed = [1.0 - x for x in normed]
        weight = rule.weight / weight_sum
        for idx, value in enumerate(normed):
            scores[idx] += weight * value

    items: list[ScreenItem] = []
    for i, (sym, name, bd) in enumerate(entries):
        items.append(
            ScreenItem(
                symbol=sym,
                name=name,
                score=float(scores[i]),
                breakdown={k: float(v) for k, v in bd.items()},
            )
        )
    items.sort(key=lambda x: x.score, reverse=True)
    return items


def _score_from_breakdowns(
    preset: PresetId,
    entries: list[tuple[str, str | None, dict[str, float]]],
) -> list[ScreenItem]:
    """横截面 min-max 归一化后按预设权重加权求和。"""
    if not entries:
        return []

    if preset == "momentum":
        r20 = [e[2]["ret_20"] for e in entries]
        r60 = [e[2]["ret_60"] for e in entries]
        v20 = [e[2]["vol_20"] for e in entries]
        n20 = _minmax_norm(r20)
        n60 = _minmax_norm(r60)
        nv = _minmax_norm(v20)
        inv_v = [1.0 - x for x in nv]
        w = (0.35, 0.35, 0.3)
        scores = [w[0] * n20[i] + w[1] * n60[i] + w[2] * inv_v[i] for i in range(len(entries))]
    elif preset == "volume_pulse":
        vr = [e[2]["vol_ratio"] for e in entries]
        pv = [e[2]["price_vs_ma5"] for e in entries]
        nvr = _minmax_norm(vr)
        npv = _minmax_norm(pv)
        w = (0.55, 0.45)
        scores = [w[0] * nvr[i] + w[1] * npv[i] for i in range(len(entries))]
    elif preset == "ma_alignment":
        sp = [e[2]["ma_spread"] for e in entries]
        sl = [e[2]["sma5_slope"] for e in entries]
        nsp = _minmax_norm(sp)
        nsl = _minmax_norm(sl)
        w = (0.5, 0.5)
        scores = [w[0] * nsp[i] + w[1] * nsl[i] for i in range(len(entries))]
    elif preset == "low_volatility":
        v20 = [e[2]["vol_20"] for e in entries]
        v60 = [e[2]["vol_60"] for e in entries]
        nv20 = _minmax_norm(v20)
        nv60 = _minmax_norm(v60)
        inv20 = [1.0 - x for x in nv20]
        inv60 = [1.0 - x for x in nv60]
        w = (0.5, 0.5)
        scores = [w[0] * inv20[i] + w[1] * inv60[i] for i in range(len(entries))]
    elif preset == "short_reversal":
        neg5 = [-e[2]["ret_5"] for e in entries]
        neg10 = [-e[2]["ret_10"] for e in entries]
        n5 = _minmax_norm(neg5)
        n10 = _minmax_norm(neg10)
        w = (0.5, 0.5)
        scores = [w[0] * n5[i] + w[1] * n10[i] for i in range(len(entries))]
    elif preset == "liquidity":
        rv = [e[2]["rel_vol"] for e in entries]
        am = [e[2]["amihud_illiq_20"] for e in entries]
        nrv = _minmax_norm(rv)
        nam = _minmax_norm(am)
        inv_am = [1.0 - x for x in nam]
        w = (0.5, 0.5)
        scores = [w[0] * nrv[i] + w[1] * inv_am[i] for i in range(len(entries))]
    elif preset == "value_tilt":
        pe = [e[2]["pe_ttm"] for e in entries]
        pb = [e[2]["pb"] for e in entries]
        neg_pe = [-p for p in pe]
        neg_pb = [-p for p in pb]
        npe = _minmax_norm(neg_pe)
        npb = _minmax_norm(neg_pb)
        w = (0.5, 0.5)
        scores = [w[0] * npe[i] + w[1] * npb[i] for i in range(len(entries))]
    elif preset == "low_pe":
        pe = [e[2]["pe_ttm"] for e in entries]
        neg_pe = [-p for p in pe]
        scores = _minmax_norm(neg_pe)
    elif preset == "low_pb":
        pb = [e[2]["pb"] for e in entries]
        neg_pb = [-p for p in pb]
        scores = _minmax_norm(neg_pb)
    elif preset == "low_ps":
        ps = [e[2]["ps_ttm"] for e in entries]
        neg_ps = [-p for p in ps]
        scores = _minmax_norm(neg_ps)
    elif preset == "quality_value":
        pe = [e[2]["pe_ttm"] for e in entries]
        pb = [e[2]["pb"] for e in entries]
        roe = [e[2]["roe"] for e in entries]
        npe = _minmax_norm([-p for p in pe])
        npb = _minmax_norm([-p for p in pb])
        nroe = _minmax_norm(roe)
        w = (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
        scores = [
            w[0] * npe[i] + w[1] * npb[i] + w[2] * nroe[i] for i in range(len(entries))
        ]
    elif preset == "dividend_tilt":
        dy = [e[2]["dividend_yield"] for e in entries]
        scores = _minmax_norm(dy)
    else:
        return []

    items: list[ScreenItem] = []
    for i, (sym, name, bd) in enumerate(entries):
        items.append(
            ScreenItem(
                symbol=sym,
                name=name,
                score=float(scores[i]),
                breakdown={k: float(v) for k, v in bd.items()},
            )
        )
    items.sort(key=lambda x: x.score, reverse=True)
    return items


def run_screen(req: ScreenRequest) -> ScreenResponse:
    universe, universe_note = fetch_a_share_universe(
        req.max_universe,
        seed=req.seed,
    )

    fetch_warnings: list[str] = []
    skipped_short = 0
    skipped_breakdown = 0
    skipped_fundamental = 0
    failed = 0
    failed_fundamental = 0

    prepared: list[tuple[str, str | None, pd.DataFrame]] = []

    use_range = bool(
        req.start_date
        and req.start_date.strip()
        and req.end_date
        and req.end_date.strip()
    )
    bars = req.bars if req.bars is not None else 120
    min_need = _min_bars_for_request(req)
    factor_fields = _configured_factor_fields(req.factor_config)
    technical_factor_fields = factor_fields & set(TECHNICAL_FACTOR_MIN_BARS)
    needs_fundamental = _needs_fundamental(req)

    for u in universe:
        sym = u["symbol"]
        name = u.get("name") or ""
        try:
            if use_range:
                df = fetch_a_share_daily(
                    sym,
                    start=req.start_date.strip(),
                    end=req.end_date.strip(),
                )
            else:
                df = fetch_a_share_daily(sym, limit=max(bars, min_need))
        except (MarketDataError, ValueError):
            failed += 1
            continue

        if len(df) < min_need:
            skipped_short += 1
            continue
        prepared.append((sym, name or None, df))

    entries: list[tuple[str, str | None, dict[str, float]]] = []
    for sym, name, df in prepared:
        if req.factor_config is not None:
            entry: dict[str, float] = {}
            if technical_factor_fields:
                bd = _technical_breakdown_for_fields(df, technical_factor_fields)
                if bd is None:
                    skipped_breakdown += 1
                    continue
                entry.update(bd)
            if needs_fundamental:
                try:
                    fund = fetch_a_share_valuation_snapshot(sym)
                except MarketDataError:
                    failed_fundamental += 1
                    continue
                if fund is None:
                    skipped_fundamental += 1
                    continue
                entry.update(fund)
            if not _entry_ok_for_factor_config(req.factor_config, entry):
                skipped_fundamental += 1 if needs_fundamental else 0
                skipped_breakdown += 0 if needs_fundamental else 1
                continue
            entries.append((sym, name, {rule.field: entry[rule.field] for rule in req.factor_config.factors}))
            continue

        if req.preset in FUNDAMENTAL_PRESETS:
            try:
                fund = fetch_a_share_valuation_snapshot(sym)
            except MarketDataError:
                failed_fundamental += 1
                continue
            if fund is None or not _fundamental_entry_ok(req.preset, fund):
                skipped_fundamental += 1
                continue
            entries.append((sym, name, fund))
            continue

        bd = _breakdown_for_preset(df, req.preset)
        if bd is None:
            skipped_breakdown += 1
            continue
        entries.append((sym, name, bd))

    if req.factor_config is not None:
        items = _score_from_factor_config(req.factor_config, entries)[: req.top_k]
    else:
        items = _score_from_breakdowns(req.preset, entries)[: req.top_k]

    if skipped_short:
        fetch_warnings.append(f"K 线不足（需≥{min_need}）跳过: {skipped_short} 只")
    if skipped_breakdown:
        fetch_warnings.append(f"因子计算失败跳过: {skipped_breakdown} 只")
    if skipped_fundamental:
        fetch_warnings.append(f"估值数据缺失跳过: {skipped_fundamental} 只")
    if failed:
        fetch_warnings.append(f"行情拉取失败: {failed} 只")
    if failed_fundamental:
        fetch_warnings.append(f"估值拉取失败: {failed_fundamental} 只")

    return ScreenResponse(
        items=items,
        warnings=fetch_warnings,
        universe_note=universe_note,
        preset=req.preset,
        disclaimer=DISCLAIMER,
    )


def screen_presets_catalog() -> dict[str, Any]:
    """供 GET /api/screen-presets 使用。"""
    return {
        "presets": [
            {
                "id": "momentum",
                "name": "动量 + 波动惩罚",
                "description": "20/60 日收益率横截面归一化加权，并用 20 日收益波动率做惩罚（高波动降权）。",
                "weights": {"ret_20": 0.35, "ret_60": 0.35, "vol_20_penalty": 0.3},
                "min_bars": MIN_BARS,
            },
            {
                "id": "volume_pulse",
                "name": "量比脉冲",
                "description": "当前量比（相对 20 日均量）与收盘价相对 5 日均价的偏离度。",
                "weights": {"vol_ratio": 0.55, "price_vs_ma5": 0.45},
                "min_bars": MIN_BARS,
            },
            {
                "id": "ma_alignment",
                "name": "均线多头形态",
                "description": "5 日与 20 日均线相对强弱，以及 5 日均线近端斜率。",
                "weights": {"ma_spread": 0.5, "sma5_slope": 0.5},
                "min_bars": MIN_BARS,
            },
            {
                "id": "low_volatility",
                "name": "低波动",
                "description": "20/60 日收益波动率越低得分越高（横截面内对波动率取反归一化后加权）。",
                "weights": {"vol_20_inv": 0.5, "vol_60_inv": 0.5},
                "min_bars": PRESET_MIN_BARS["low_volatility"],
            },
            {
                "id": "short_reversal",
                "name": "短期反转（超跌）",
                "description": "与「动量」相反：近 5/10 日收益越低得分越高，偏短期超跌/反转风格；仅为演示因子。",
                "weights": {"neg_ret_5": 0.5, "neg_ret_10": 0.5},
                "min_bars": PRESET_MIN_BARS["short_reversal"],
            },
            {
                "id": "liquidity",
                "name": "流动性（量与冲击代理）",
                "description": "相对成交量（今日/20 日均量）越高越好；并用 |日收益|/成交量 的 20 日均值作非流动性惩罚（越低越好）。仅用成交量近似，非成交额。",
                "weights": {"rel_vol": 0.5, "amihud_inv": 0.5},
                "min_bars": MIN_BARS,
            },
            {
                "id": "value_tilt",
                "name": "估值偏低（PE & PB）",
                "description": "乐咕数据源最新 PE(TTM) 与 PB：二者越低得分越高（横截面内对 -PE、-PB 归一化）。全市场逐只请求较慢，且可能有缺失。",
                "weights": {"neg_pe_ttm": 0.5, "neg_pb": 0.5},
                "min_bars": MIN_BARS,
                "requires_fundamental": True,
                "required_fields": ["pe_ttm", "pb"],
            },
            {
                "id": "low_pe",
                "name": "低市盈率",
                "description": "仅 PE(TTM)：越低得分越高（对 -PE 横截面归一化）。需有有效 PE。",
                "weights": {"neg_pe_ttm": 1.0},
                "min_bars": MIN_BARS,
                "requires_fundamental": True,
                "required_fields": ["pe_ttm"],
            },
            {
                "id": "low_pb",
                "name": "低市净率",
                "description": "仅 PB：越低得分越高（对 -PB 横截面归一化）。需有有效 PB。",
                "weights": {"neg_pb": 1.0},
                "min_bars": MIN_BARS,
                "requires_fundamental": True,
                "required_fields": ["pb"],
            },
            {
                "id": "low_ps",
                "name": "低市销率",
                "description": "仅 PS(TTM)：越低得分越高（对 -PS 横截面归一化）。若接口无市销率列则多数标的会被跳过。",
                "weights": {"neg_ps_ttm": 1.0},
                "min_bars": MIN_BARS,
                "requires_fundamental": True,
                "required_fields": ["ps_ttm"],
            },
            {
                "id": "quality_value",
                "name": "质量价值（低 PE/PB + 高 ROE）",
                "description": "低 PE、低 PB、高 ROE 三等分加权：norm(-PE)+norm(-PB)+norm(ROE)。三者均需存在且为有效正数（ROE 在快照中已统一为小数量纲）。",
                "weights": {"neg_pe_ttm": 1 / 3, "neg_pb": 1 / 3, "roe": 1 / 3},
                "min_bars": MIN_BARS,
                "requires_fundamental": True,
                "required_fields": ["pe_ttm", "pb", "roe"],
            },
            {
                "id": "dividend_tilt",
                "name": "股息率倾斜",
                "description": "股息率越高得分越高（横截面归一化）。若数据源无股息率列或大量缺失，结果可能为空。",
                "weights": {"dividend_yield": 1.0},
                "min_bars": MIN_BARS,
                "requires_fundamental": True,
                "required_fields": ["dividend_yield"],
            },
        ],
        "custom_factor_fields": {
            "technical": sorted(TECHNICAL_FACTOR_MIN_BARS),
            "fundamental": sorted(FUNDAMENTAL_FACTOR_FIELDS),
            "directions": ["higher", "lower"],
        },
        "disclaimer": DISCLAIMER,
    }
