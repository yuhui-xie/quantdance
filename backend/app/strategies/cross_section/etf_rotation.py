"""ETF 动量轮动（斜率版）：选择归一化收盘价回归斜率（×R²）最强且未过热的 ETF。

对每只 ETF 取近 ``slope_days`` 根 K 线的收盘价，按首值归一化后做最小二乘线性回归，
得 ``斜率 × R²``（``slope_momentum``）：斜率>0 表示区间整体上行，R² 刻画趋势是否
贴近一条直线（拟合质量）。排序得分取该生值的**横截面 z 值**（当日池内均值0、标准差1），
得分高 = 高于当日池内平均斜率强度。

可选叠加三条**绝对**过滤（见 §2.1，默认关闭），把"比别的强"与"自身够稳、未过热"
分开：

- ``require_raw_trend``：标的自身原始（非横截面）N 日回归斜率>0 才纳入候选；
- ``max_annualized_vol``：近 20 日对数收益年化波动率上限（过滤高波动）；
- ``max_recent_gain_pct``：近 10 日涨幅上限（避免追高短期已急拉的标的）。

``require_positive_score`` 开启时只保留得分>0 的候选，某调仓日全部不满足则持现金。
``rotate_threshold=1.0``（默认）关闭轮动惰性、纯按得分调仓。周期默认每自然月首个
交易日，等权持有 top-N。

可选短期过热压制：``short_term_damp_coef>0`` 时最终得分 = z(长窗 slope_days 斜率) −
系数 × z(短窗 short_term_slope_days 斜率)，扣减近端急拉者追高；``coef=0``（默认）等价
纯长窗斜率 z。

可选**量价综合（近期异常放量压制）**：``amount_surge_damp_coef>0`` 时在（短期过热调整后
的）斜率得分上再扣减 ``系数 × z( LOG( 近 amount_recent_days 日均成交额 / 紧邻其前
amount_baseline_days 日均成交额 ) )`` —— 用成交额(amount)刻画近端量能相对自身近期基线
是否异常扩张，横截面 z 标准化后对当日池内近期相对放量者扣分，避免买入刚异常放量的 ETF；
``coef=0``（默认）关闭，保持向后兼容。

可选**动态行业池发现（消除池成员前视）**：``pool_discovery=True``（配合 universe=
"etf_market" 一次性载入全市场全历史面板）时，不再用固定 config 池，而是每个决策日按
asof 从全市场按 9 大类（细分方向为主、大类兜底）重新发现候选池——排除货币/除5年国债外债券/无法
细分/其它跨境(仅留恒生国企与纳斯达克)，要求上市满
``discover_min_listing_days``、近 ``discover_amount_days`` 个交易日内有效成交日 ≥
``discover_min_valid_days``；每方向按历史日均成交额取 ≤ ``discover_per_direction`` 只、
方向内部按 ``discover_corr_days`` 日收益做 ``discover_corr_threshold`` 上限的去冗余
（代表性、可交易、彼此低相关），再分层填充（先各方向第1名…）封顶 ``discover_max_pool``。
再把发现的成员交给上方动量斜率 z 排序选 top_n。发现/相关性窗口只读 ``date<=asof``，
无前视，可自然纳入每月新上市标的。详见
``app.strategies.cross_section.industry_pool.discover_industry_pool``。默认关闭=沿用静态
config 池，向后兼容。
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import Field

from app.strategies.base import (
    CrossSectionContext,
    CrossSectionStrategySpec,
)
from app.strategies.cross_section.common import (
    apply_score_threshold_rotation,
    asof_tradeable_row,
    record_decision_pool,
)

from app.factors.cross_section import zscore
from app.factors.momentum import price_history, slope_momentum
from app.strategies.cross_section.decision import DecisionFrequencyParams
from app.strategies.cross_section.industry_pool import (
    POOL_INDEX_CACHE_KEY,
    build_industry_pool_index,
    discover_industry_pool,
)


# ctx.cache 中保存上一调仓日持仓 symbol 列表的键；用于跨决策日的轮动惰性比较。
CACHE_KEY = "etf_rotation_prev_holdings"


def annualized_volatility(closes: np.ndarray, lookback: int) -> float | None:
    """近 ``lookback`` 根 K 线收盘价的对数收益年化波动率（std × √252）。

    用最近 ``lookback+1`` 个收盘价取 ``lookback`` 个日对数收益，样本标准差
    (ddof=1) 再按每年约 252 个交易日年化。返回小数（0.40 即 40%）；数据不足
    或方差无法计算时返回 None。
    """
    closes = np.asarray(closes, dtype=float)
    window = closes[-(lookback + 1):]
    if len(window) < lookback + 1:
        return None
    log_p = np.log(window)
    rets = np.diff(log_p)
    if len(rets) < 2:
        return None
    std = float(np.std(rets, ddof=1))
    if not np.isfinite(std):
        return None
    return std * np.sqrt(252.0)


def recent_gain(closes: np.ndarray, days: int) -> float | None:
    """近 ``days`` 根 K 线的简单涨幅（小数）：close[-1]/close[-1-days] - 1。"""
    closes = np.asarray(closes, dtype=float)
    if len(closes) < days + 1:
        return None
    base = closes[-1 - days]
    last = closes[-1]
    if not np.isfinite(base) or not np.isfinite(last) or base <= 0:
        return None
    return float(last / base - 1.0)


def amount_history(value_df: pd.DataFrame, asof: str) -> np.ndarray | None:
    """取 asof 及之前、与 close 对齐的日线成交额(amount)数组，按日期排序去重。

    只保留当日有正成交额(amount>0 且有限)的交易日，避免停牌/缺失日把均值拉低。
    ``amount`` 列缺失时返回 None（调用方据此跳过成交额相关计算）。
    """
    if "date" not in value_df.columns or "amount" not in value_df.columns:
        return None
    cols = ["date", "close", "amount"]
    h = value_df.loc[
        value_df["date"].astype(str).str[:10] <= str(asof)[:10], cols
    ].copy()
    h["date"] = pd.to_datetime(h["date"], errors="coerce")
    for c in ("close", "amount"):
        h[c] = pd.to_numeric(h[c], errors="coerce")
    h = h.dropna(subset=["date", "close"])
    h = h[h["close"] > 0]
    h = h.sort_values("date").drop_duplicates("date", keep="last")
    amt = h["amount"].astype(float).to_numpy()
    amt = amt[np.isfinite(amt) & (amt > 0)]
    if amt.size == 0:
        return None
    return amt


def log_amount_spike(
    value_df: pd.DataFrame,
    asof: str,
    recent_days: int,
    baseline_days: int,
) -> float | None:
    """量价综合生值：LOG( 近 ``recent_days`` 日均成交额 / 紧邻其前 ``baseline_days`` 日均成交额 )。

    非重叠窗口：分子=截至 asof 最近 ``recent_days`` 个有效交易日的日均成交额，分母=紧邻
    该近期窗口之前 ``baseline_days`` 个交易日的日均成交额。正值=近端相对自身近期基线放量，
    负值=缩量；该值在当日池内再做横截面 z。数据不足、均值非正或成交额列缺失返回 None。
    """
    amt = amount_history(value_df, asof)
    if amt is None:
        return None
    need = recent_days + baseline_days
    if amt.size < need:
        return None
    recent = amt[-recent_days:]
    baseline = amt[-(recent_days + baseline_days):-recent_days]
    recent_mean = float(recent.mean())
    baseline_mean = float(baseline.mean())
    if (
        not np.isfinite(recent_mean)
        or not np.isfinite(baseline_mean)
        or recent_mean <= 0
        or baseline_mean <= 0
    ):
        return None
    return float(np.log(recent_mean / baseline_mean))


class EtfRotationParams(DecisionFrequencyParams):
    decision_anchor: Literal["start", "end"] = Field(
        "start",
        description="月度决策锚点：start=每自然月首个交易日（默认）| end=每自然月末",
    )
    top_n: int = Field(1, description="持有得分（归一化收盘价斜率 z 值）最高的 ETF 数量")
    slope_days: int = Field(
        60,
        ge=2,
        le=504,
        description="归一化收盘价线性回归窗口（斜率趋势的交易日数）",
    )
    short_term_slope_days: int = Field(
        20,
        ge=2,
        le=252,
        description=(
            "短窗斜率回看（检测短期过热）：短窗斜率明显强于池内平均即近端急拉，配合"
            "short_term_damp_coef 从最终得分中扣减，压制追高。仅 short_term_damp_coef>0 "
            "时生效。"
        ),
    )
    short_term_damp_coef: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "短期过热压制系数。>0 时最终得分 = z(长窗 slope_days 斜率) − 该系数 × "
            "z(短窗 short_term_slope_days 斜率)：扣减近期斜率远强于长期者的得分，压制"
            "短期过热。0 = 关闭（默认，得分退化为纯长窗斜率 z，向后兼容）。典型 0.2。"
        ),
    )
    # ---- 量价综合：近期异常放量压制（可选，默认关闭）----
    amount_recent_days: int = Field(
        5,
        ge=1,
        le=60,
        description=(
            "量价综合的近期窗口：截至调仓日最近 amount_recent_days 个有效交易日的日均"
            "成交额作为比值分子。仅 amount_surge_damp_coef>0 时生效。"
        ),
    )
    amount_baseline_days: int = Field(
        10,
        ge=1,
        le=120,
        description=(
            "量价综合的分母基线窗口：紧邻近期窗口之前 amount_baseline_days 个交易日的"
            "日均成交额。仅 amount_surge_damp_coef>0 时生效。"
        ),
    )
    amount_surge_damp_coef: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "量价综合（近期异常放量压制）系数。>0 时在短期过热调整后的斜率得分上再"
            "扣减 该系数 × z( LOG( 近 amount_recent_days 日均成交额 / 紧邻其前 "
            "amount_baseline_days 日均成交额 ) )：对当日池内近期相对放量（成交额扩张高于"
            "池内平均）的 ETF 扣分，避免买入刚异常放量的标的。0 = 关闭（默认，向后兼容）。"
            "典型 0.1。"
        ),
    )
    min_score: float = Field(
        0.0,
        description="配合 require_positive_score=True 时的分数下限（score≥min_score）",
    )
    require_positive_score: bool = Field(
        False,
        description=(
            "开启后只保留得分>0 的候选（得分=池内斜率 z 值，>0 即高于当日池内平均）；"
            "某调仓日全部候选都不满足 → 持现金，而非买入得分仍为负的最弱标的。"
            "默认关闭以保持历史行为。"
        ),
    )
    rotate_threshold: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description=(
            "轮动惰性阈值：对仍可作为候选的当前持仓，若其【当前决策日】的得分不低于"
            "新入选标的的当前得分×该阈值，则保持持仓、不调仓；仅当新候选明显更优"
            "（当前得分超过当前持仓的 1/阈值）才轮动。取值 1.0 表示关闭惰性、纯按"
            "得分轮动；越接近 0 惰性越强（0 时几乎永不调仓）。"
        ),
    )
    # ---- 绝对趋势/风险/过热过滤（默认关闭，逐条可选启用）----
    require_raw_trend: bool = Field(
        False,
        description=(
            "要求标的自身【原始（非横截面）】归一化收盘价回归斜率为正才纳入候选："
            "对近 raw_trend_days 根 K 线收盘价按首值归一化后做最小二乘线性回归，"
            "回归斜率>0 才保留（绝对上升趋势门槛，不随当日池内相对强弱漂移）。"
        ),
    )
    raw_trend_days: int = Field(
        40, ge=2, le=504, description="原始趋势门槛的线性回归回看交易日数"
    )
    max_annualized_vol: float | None = Field(
        None,
        ge=0,
        description=(
            "近 vol_lookback 日（默认 20）对数收益年化波动率上限（小数，0.40 即 40%）；"
            "超出则剔除（过滤高波动标的）；None 关闭。年化按日 std×√252。"
        ),
    )
    vol_lookback: int = Field(
        20, ge=2, le=250, description="年化波动率回看交易日数"
    )
    max_recent_gain_pct: float | None = Field(
        None,
        ge=0,
        description=(
            "近 recent_gain_days 日（默认 10）简单涨幅上限（%）；超出则剔除，"
            "用于避免追高短期已大幅拉升的标的；None 关闭。"
        ),
    )
    recent_gain_days: int = Field(
        10, ge=2, le=250, description="近期涨幅回看交易日数"
    )
    max_gain_pct: float | None = Field(
        None,
        ge=0,
        description=(
            "更长区间累计简单涨幅上限（%），对近 gain_days 日（如 40/60）收盘累计涨幅设限，"
            "用于压制已从更早起点涨幅过大（远离启动位）的标的；None 关闭。与 "
            "max_recent_gain_pct（近 10 日、防急拉追高）叠加使用时，形成"
            "近端(短)+中段(长)两道涨速门槛。"
        ),
    )
    gain_days: int = Field(
        40, ge=2, le=504, description="长窗累计涨幅上限的回看交易日数"
    )
    exclude_limit: bool = True
    exclude_suspended: bool = True
    limit_pct_threshold: float = Field(9.5, ge=1.0, le=30.0)

    # ---- 动态行业池发现（可选，默认关闭；向后兼容）----
    # 开启后不再用固定 config 池，而是每个决策日按 asof 从全市场 etf 面板（需
    # universe="etf_market" 一次性载入全市场全历史）按 9 大类（细分方向为主、大类兜底）重新发现候选池：
    # 排除货币/除5年国债外债券/无法细分/其它跨境(仅留恒生国企与纳斯达克)，要求上市满
    # discover_min_listing_days、近
    # discover_amount_days 个交易日内有效成交日 ≥ discover_min_valid_days；每个方向按
    # 历史日均成交额排序取 ≤ discover_per_direction 只（同方向内部按 corr 去冗余），
    # 再分层填充（先各方向第1名、再第2名…）封顶 discover_max_pool。再交给上方动量斜率
    # z 排序选 top_n 实际持有。发现/相关性窗口只读 date<=asof，无前视。
    pool_discovery: bool = Field(
        False,
        description=(
            "开启动态行业池发现：每决策日按 asof 从全市场 etf 面板按 9 大类(细分方向为主、大类兜底)重新发现"
            "候选池(流动性排序+每方向≤K+方向内低相关+分层封顶)，再交给动量斜率 z 选 "
            "top_n。需配合 universe='etf_market'。默认关闭=沿用静态 config 池，向后兼容。"
        ),
    )
    discover_min_listing_days: int = Field(
        182,
        ge=30,
        description=(
            "候选池成员上市时长门槛：首根K线距 asof 至少这么多自然日（182≈满6个月）。"
        ),
    )
    discover_amount_days: int = Field(
        120,
        ge=30,
        le=504,
        description=(
            "候选流动性判定回看窗口：近 N 个交易日(≤asof)内须有 ≥discover_min_valid_days "
            "个有效成交日，并以其中日均成交额(amount，缺列回退 volume)作为同方向内排序依据。"
        ),
    )
    discover_min_valid_days: int = Field(
        60,
        ge=1,
        le=504,
        description=(
            "近 discover_amount_days 个交易日中须含的最少有效成交日数（60/120≈半年约60个"
            "成交日，剔除长期停牌/新上市不足额标的）。"
        ),
    )
    discover_per_direction: int = Field(
        2,
        ge=1,
        le=20,
        description="每个方向(细分行业)最多保留的候选 ETF 数量。",
    )
    discover_max_pool: int = Field(
        90,
        ge=10,
        le=1000,
        description=(
            "分层填充后的池总上限：先各方向第1名、再第2名…按方向顺序填满至此封顶。"
        ),
    )
    discover_corr_days: int = Field(
        60,
        ge=10,
        le=250,
        description="方向内部相关性计算的日收益回看窗口(≤asof)。",
    )
    discover_corr_threshold: float = Field(
        0.7,
        ge=0.0,
        le=1.0,
        description=(
            "方向内相关性上限：候选与同方向任一已接受成员日收益相关 ≥ 此值则拒绝，"
            "只保留彼此低相关的代表。"
        ),
    )


def select_etf_rotation(
    asof: str,
    ctx: CrossSectionContext,
    params: EtfRotationParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    # 打分需至少 max(slope_days, 短窗) 根 K 线；量价综合开启时还须近+基线窗足额；
    # 各可选过滤的更长回看在其内部单独守卫。
    required_bars = max(params.slope_days, params.short_term_slope_days)
    if params.amount_surge_damp_coef > 0:
        required_bars = max(
            required_bars,
            params.amount_recent_days + params.amount_baseline_days,
        )

    # 动态行业池发现（可选）：开启时先算出本决策日的候选成员集，仅对这些成员打分，
    # 否则沿用整个 ctx.panel。发现只读 date<=asof，无前视。
    pool_members: set[str] | None = None
    if params.pool_discovery:
        # ctx.panel 在一次回测内不变，索引只建一次并挂在 ctx.cache 上跨决策日复用：
        # 否则每个决策日都要对全市场 ETF（约 1700 只）重扫全表 + 逐票排序去重，
        # 单次发现即 15s 量级，81 个决策日就是数分钟。
        index = ctx.cache.get(POOL_INDEX_CACHE_KEY)
        if index is None:
            index = build_industry_pool_index(ctx.panel, ctx.names)
            ctx.cache[POOL_INDEX_CACHE_KEY] = index
        pool_members = set(
            discover_industry_pool(
                ctx.panel,
                ctx.names,
                asof,
                min_listing_days=params.discover_min_listing_days,
                window_days=params.discover_amount_days,
                min_valid_days=params.discover_min_valid_days,
                per_direction=params.discover_per_direction,
                max_total=params.discover_max_pool,
                corr_days=params.discover_corr_days,
                corr_threshold=params.discover_corr_threshold,
                index=index,
            )
        )
        # 空池也要登记：这正是调参时最需要看到的诊断信息（该日为何无标的可买）。
        record_decision_pool(ctx, asof, pool_members, source="pool_discovery")
        if not pool_members:
            return [], []

    for symbol, payload in ctx.panel.items():
        if pool_members is not None and symbol not in pool_members:
            continue
        value_df = payload.get("value")
        if value_df is None or value_df.empty:
            continue

        row = asof_tradeable_row(
            value_df,
            asof,
            exclude_suspended=params.exclude_suspended,
            exclude_limit=params.exclude_limit,
            limit_pct_threshold=params.limit_pct_threshold,
        )
        if row is None:
            continue

        history = price_history(value_df, asof)
        if len(history) < required_bars:
            continue

        close = float(history["close"].iloc[-1])
        closes = history["close"].astype(float).to_numpy()
        n_bars = len(closes)

        # ---- 绝对趋势/风险/过热过滤（逐条可选）----
        # 只使用调仓日及之前的收盘价，不使用未来数据。
        if params.require_raw_trend:
            if n_bars < params.raw_trend_days:
                continue
            raw_slope = slope_momentum(closes, params.raw_trend_days)
            # slope_momentum = 10000×斜率×R²，R²≥0，故符号即回归斜率符号
            if raw_slope is None or not np.isfinite(raw_slope) or raw_slope <= 0:
                continue
        if params.max_annualized_vol is not None:
            if n_bars < params.vol_lookback + 1:
                continue
            vol = annualized_volatility(closes, params.vol_lookback)
            if vol is None or vol > params.max_annualized_vol:
                continue
        if params.max_recent_gain_pct is not None:
            if n_bars < params.recent_gain_days + 1:
                continue
            gain = recent_gain(closes, params.recent_gain_days)
            if gain is None or gain * 100.0 > params.max_recent_gain_pct:
                continue
        if params.max_gain_pct is not None:
            if n_bars < params.gain_days + 1:
                continue
            gain = recent_gain(closes, params.gain_days)
            if gain is None or gain * 100.0 > params.max_gain_pct:
                continue

        # 打分生值：长/短窗归一化收盘价回归斜率×R²（生值保留量级，可超 1，供核查）
        slope_raw = slope_momentum(closes, params.slope_days)
        if slope_raw is None or not np.isfinite(slope_raw):
            continue
        slope_short: float | None = None
        if params.short_term_damp_coef > 0:
            slope_short = slope_momentum(closes, params.short_term_slope_days)
            if slope_short is None or not np.isfinite(slope_short):
                continue

        # 量价综合生值：近端(amount_recent_days)相对紧邻其前(amount_baseline_days)基线的
        # 对数成交额扩张；仅在 amount_surge_damp_coef>0 时计算。
        amount_log_ratio: float | None = None
        if params.amount_surge_damp_coef > 0:
            amount_log_ratio = log_amount_spike(
                value_df,
                asof,
                params.amount_recent_days,
                params.amount_baseline_days,
            )
            if amount_log_ratio is None:
                continue

        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "asof": str(asof)[:10],
                "close": close,
                "score": slope_raw,  # 下方改为最终（可扣减）横截面 z 值
                "slope_raw": slope_raw,
                "slope_short": slope_short,
                "slope_score": slope_raw,
                "amount_log_ratio": amount_log_ratio,
            }
        )

    # 非动态发现模式下没有"发现的池"，登记的即本决策日通过全部闸门、进入打分的候选集合。
    # 记在提前返回【之前】：候选为空（当日全被过滤掉）恰恰是最需要看到的诊断信息。
    # 此处池成员与返回值 details 一致（详见下方 require_positive_score 后的复核登记）。
    if pool_members is None:
        record_decision_pool(
            ctx, asof, (c["symbol"] for c in candidates), source="eligibility"
        )

    if not candidates:
        return [], []
    # 基线分：长窗斜率 z。short_term_damp_coef>0 时扣减 短窗斜率 z × 系数，压制近期斜率
    # 远强于长期（短期过热）的追高；amount_surge_damp_coef>0 时再扣减 量价综合 z × 系数，
    # 压制近期异常放量。z 均为当日池内横截面（均值0、标准差1）。
    z_slope = zscore([c["slope_raw"] for c in candidates])
    z_short = (
        zscore([float(c["slope_short"]) for c in candidates])
        if params.short_term_damp_coef > 0
        else None
    )
    for idx, cand in enumerate(candidates):
        base = float(z_slope[idx])
        if z_short is not None:
            base -= params.short_term_damp_coef * float(z_short[idx])
        cand["slope_score"] = float(base)

    # 量价综合（近期异常放量压制）：扣减 coef × z(LOG 放量生值)。放量生值横截面 z 高者
    # = 近端量能扩张高于池内平均，得分被下调；缩量者 z 为负，轻微加分（对称口径）。
    if params.amount_surge_damp_coef > 0:
        z_amt = zscore([float(c["amount_log_ratio"]) for c in candidates])
        for idx, cand in enumerate(candidates):
            cand["amount_score"] = float(z_amt[idx])
            cand["score"] = float(cand["slope_score"]) - (
                params.amount_surge_damp_coef * float(z_amt[idx])
            )
    else:
        for cand in candidates:
            cand["amount_score"] = None
            cand["score"] = cand["slope_score"]

    # require_positive_score：只保留得分>0（高于池内平均）的候选。某调仓日全部候选
    # 都不满足 → 无候选 → 本轮持现金，而非买入得分仍为负的最弱标的。默认关闭以保持
    # 历史行为（始终在可用候选取 top-N）。开启时按 min_score 一并卡分数下限。
    if params.require_positive_score:
        candidates = [
            c for c in candidates
            if float(c["score"]) > 0 and float(c["score"]) >= params.min_score
        ]
        # 该开关会缩小候选集，上面登记的池需复核一次，保证池成员 == details
        # （进度行的"通过筛选 N 只"与 dump 的 count 随之自洽）。开关关闭时无需复核。
        if pool_members is None:
            record_decision_pool(
                ctx, asof, (c["symbol"] for c in candidates), source="eligibility"
            )

    candidates.sort(key=lambda item: (-item["score"], item["symbol"]))

    # 每个候选标注排名，details 返回完整排序候选，便于报告展示全部标的的指标对比。
    for idx, cand in enumerate(candidates):
        cand["rank"] = idx + 1

    # ---- 轮动惰性（降低调仓频率）----
    # 对仍可作为候选的当前持仓，用其【当前决策日】得分对比新入选标的的【当前】得分，
    # 不低于新得分×rotate_threshold 时继续持有，仅当新候选明显更优才轮动，避免在
    # 得分相近的标的间来回切换。上一调仓日持仓从 ctx.cache 读取（runner 复用同一
    # context，cache 跨决策日保留），本次结果由该函数写回 cache。
    selected = apply_score_threshold_rotation(
        ctx,
        candidates,
        params.top_n,
        params.rotate_threshold,
        cache_key=CACHE_KEY,
    )

    selected_set = set(selected)
    for item in candidates:
        item["selected"] = item["symbol"] in selected_set
    return selected, candidates


STRATEGY = CrossSectionStrategySpec(
    id="etf_rotation",
    name="ETF 动量轮动",
    description=(
        "在 ETF 池（默认 config/etf_core_pool.json 精选池）中选择归一化收盘价回归"
        "斜率（×R²）横截面 z 值最强的标的，每月首个交易日等权调仓；可选叠加原始趋势/"
        "波动率/近期涨幅过滤、得分>0 现金规则，以及短期过热/近期异常放量（量价综合）"
        "两类压制项"
    ),
    params_model=EtfRotationParams,
    select=select_etf_rotation,
    default_universe="etf_core",
    requires_symbols=False,
    needs_fundamentals=False,
    default_top_n=1,
    warnings=(),
)
