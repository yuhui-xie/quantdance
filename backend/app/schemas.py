"""API 与 CLI 共用的请求体模型。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# 内置 universe 预设；config/*_pool.json 里的池子以文件名前缀动态识别（如 etf_core）。
# etf_dynamic / etf_asof 为 etf 的别名，带 asof 时按 K 线覆盖重构「当时已存在」的动态池。
_KNOWN_UNIVERSES = frozenset(
    {"all_a", "hs300", "zz500", "zz399101", "zz1000", "gz2000", "star50", "star_board",
     "etf", "etf_dynamic", "etf_asof"}
)
_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _is_valid_universe(value: str) -> bool:
    if value in _KNOWN_UNIVERSES:
        return True
    if value.startswith("config:"):
        return (_CONFIG_DIR / f"{value.split(':', 1)[1]}_pool.json").exists()
    return (_CONFIG_DIR / f"{value}_pool.json").exists()


class FundamentalFilterRule(BaseModel):
    """股票池的基本面筛选规则：对某个估值字段设定下界/上界（含）。"""

    field: Literal[
        "pe_ttm", "pb", "ps_ttm", "peg", "market_cap", "float_market_cap", "close", "dividend_yield"
    ]
    min: float | None = Field(None, description="下界（含），null 表示不限")
    max: float | None = Field(None, description="上界（含），null 表示不限")

    @model_validator(mode="after")
    def check_bound(self) -> FundamentalFilterRule:
        if self.min is None and self.max is None:
            raise ValueError("fundamental_filter 规则须至少设置 min 或 max 之一")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("fundamental_filter 的 min 不应大于 max")
        return self


class BacktestRequest(BaseModel):
    mode: Literal["single", "universe", "screen", "per_stock"] = "single"
    data_source: Literal["a_stock_data"] = "a_stock_data"
    strategy_id: str = "ma_crossover"
    initial_cash: float = Field(100_000, ge=1000)
    commission: float = Field(0.0003, ge=0, le=0.05)
    min_commission: float = Field(5.0, ge=0)
    slippage: float = Field(0.01, ge=0, le=0.05)
    lot_size: int = Field(100, ge=1)
    stop_loss_pct: float | None = Field(
        None,
        gt=0,
        le=0.8,
        description="通用止损：相对本次买入价下跌该比例时卖出；null 表示关闭",
    )
    bars: int = Field(500, ge=50, le=5000)
    symbol: str | None = Field(None, description="A 股 6 位代码或带 SH/SZ 后缀")
    universe: str = Field(
        "all_a",
        description="universe 模式且 symbols 为空时使用的股票池（内置预设或 config/*_pool.json 文件名前缀）",
    )
    symbols: list[str] = Field(default_factory=list, description="universe 模式的自定义股票池")
    fundamental_filter: list[FundamentalFilterRule] | None = Field(
        None,
        description="用估值字段在股票池上做基本面筛选，全部规则须同时满足",
    )
    fundamental_asof: str | None = Field(
        None,
        description="基本面评估时点 YYYY-MM-DD；默认取 start_date",
    )
    max_universe: int = Field(80, ge=1, le=10000)
    seed: int | None = Field(None, description="股票池超上限时的可复现抽样种子")
    max_workers: int = Field(8, ge=1, le=32)
    use_cache: bool = True
    force_refresh: bool = False
    take_profit_arm_pct: float | None = Field(None, gt=0, le=5)
    take_profit_exit_pct: float | None = Field(None, ge=0, le=5)
    position_management: "PositionManagementConfig | None" = None
    rebalance_mode: Literal["full", "incremental"] = Field(
        "full",
        description="换仓模式：full=目标变动时全仓清空重建；incremental=只交易差异，保留共同持仓",
    )
    include_equity: bool = True
    include_trades: bool = True
    include_price: bool = False
    start_date: str | None = Field(None, description="A 股区间起始 YYYY-MM-DD，与 end_date 成对")
    end_date: str | None = Field(None, description="A 股区间结束 YYYY-MM-DD")
    strategy_params: dict[str, Any] = Field(default_factory=dict, description="当前策略专属参数")

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def forbid_top_level_strategy_params(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        strategy_keys = {
            "fast_period",
            "slow_period",
            "signal_period",
            "volume_ma_period",
            "volume_metric",
            "threshold_mode",
            "high_ratio",
            "low_ratio",
            "high_percentile",
            "low_percentile",
            "percentile_lookback",
            "require_bull_bar",
            "require_bear_bar",
            "price_ma_period",
            "trend_ma_period",
            "breakout_period",
            "require_price_above_ma",
            "require_trend_up",
            "require_breakout",
            "entry_confirm_bars",
            "take_profit_pct",
            "period",
            "oversold",
            "overbought",
            "num_std",
            "k_period",
            "d_period",
            "smooth",
            # supertrend
            "atr_period",
            "multiplier",
            "adx_period",
            "min_adx",
        }
        found = sorted(strategy_keys.intersection(data))
        if found:
            joined = ", ".join(found)
            raise ValueError(f"策略专属参数需放入 strategy_params，不应出现在顶层: {joined}")
        return data

    @model_validator(mode="after")
    def check_backtest_request(self) -> BacktestRequest:
        if not _is_valid_universe(self.universe):
            raise ValueError(
                f"未知 universe: {self.universe!r}（内置预设或 config/*_pool.json 文件名前缀）"
            )
        start = (self.start_date or "").strip()
        end = (self.end_date or "").strip()
        if self.mode == "screen":
            if start and not end:
                raise ValueError("screen 模式填写 start_date 时须同时填写 end_date")
        elif (start and not end) or (end and not start):
            raise ValueError("start_date 与 end_date 须同时填写或同时留空")
        if self.mode in {"universe", "per_stock"}:
            if not start or not end:
                raise ValueError(f"{self.mode} 模式须同时提供 start_date 与 end_date")
            if self.symbol and self.symbol.strip():
                raise ValueError(f"{self.mode} 模式请使用 symbols，不应填写 symbol")
        arm = self.take_profit_arm_pct
        exit_level = self.take_profit_exit_pct
        if (arm is None) ^ (exit_level is None):
            raise ValueError("take_profit_arm_pct 与 take_profit_exit_pct 须同时设置或同时为空")
        if arm is not None and exit_level is not None and exit_level > 0 and exit_level >= arm:
            raise ValueError("take_profit_exit_pct 必须小于 take_profit_arm_pct")
        if self.position_management is not None and (
            self.stop_loss_pct is not None or arm is not None or exit_level is not None
        ):
            raise ValueError("position_management 不能与旧版止损止盈字段同时设置")
        return self


class BacktestSymbolRun(BaseModel):
    symbol: str
    name: str | None = None
    status: Literal["ok", "skipped", "failed"]
    rank: int | None = None
    reason: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    equity: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[dict[str, Any]] = Field(default_factory=list)
    price: list[dict[str, Any]] = Field(default_factory=list)


class BacktestUniverseSummary(BaseModel):
    requested: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    average_total_return: float = 0.0
    average_max_drawdown: float = 0.0
    average_sharpe: float = 0.0


class BacktestUniverseResponse(BaseModel):
    mode: Literal["universe"] = "universe"
    strategy_id: str
    universe_note: str = ""
    strategy_params: dict[str, Any] = Field(
        default_factory=dict,
        description="回测所用策略专属参数，供报告渲染（如 LLT 斜率阈值参考线）",
    )
    summary: BacktestUniverseSummary
    aggregate: dict[str, Any]
    runs: list[BacktestSymbolRun] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str = "演示用途，独立资金批量回测不构成投资建议。"


class ScreenItem(BaseModel):
    symbol: str
    name: str | None = None
    score: float
    breakdown: dict[str, float] = Field(default_factory=dict)


class ScreenFactorRule(BaseModel):
    field: str = Field(..., min_length=1, description="因子字段名，如 ret_20、vol_20、pe_ttm")
    weight: float = Field(..., gt=0, description="因子权重，运行时会按权重总和归一")
    direction: Literal["higher", "lower"] = Field(
        "higher",
        description="higher 表示数值越高越好；lower 表示数值越低越好",
    )


class ScreenFactorConfig(BaseModel):
    factors: list[ScreenFactorRule] = Field(..., min_length=1, description="自定义因子打分规则")


class ScreenRequest(BaseModel):
    """选股：预设因子或自定义因子配置 + 股票池上限。"""

    preset: Literal[
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
    ] = "momentum"
    top_k: int = Field(20, ge=1, le=100)
    max_universe: int = Field(200, ge=1, le=500)
    start_date: str | None = Field(None, description="与 end_date 成对，YYYY-MM-DD")
    end_date: str | None = Field(None, description="与 start_date 成对")
    bars: int | None = Field(
        120,
        ge=60,
        le=5000,
        description="未指定完整起止日时，取最近 bars 条日线",
    )
    seed: int | None = Field(None, description="股票池超过上限时随机抽样用，可复现")
    factor_config: ScreenFactorConfig | None = Field(
        None,
        description="自定义因子配置；填写后优先于 preset 的内置权重",
    )

    @model_validator(mode="after")
    def check_dates_or_bars(self) -> ScreenRequest:
        s = (self.start_date or "").strip()
        e = (self.end_date or "").strip()
        if (s and not e) or (e and not s):
            raise ValueError("start_date 与 end_date 须同时填写或同时留空")
        if self.factor_config is not None:
            fields = [rule.field for rule in self.factor_config.factors]
            duplicated = sorted({field for field in fields if fields.count(field) > 1})
            if duplicated:
                raise ValueError(f"factor_config.factors 不应重复配置同一字段: {', '.join(duplicated)}")
        return self

    model_config = {"extra": "ignore"}


class ScreenResponse(BaseModel):
    items: list[ScreenItem]
    warnings: list[str] = Field(default_factory=list)
    universe_note: str = ""
    preset: str
    disclaimer: str = "演示用途，技术面筛选不构成投资建议。"


class PositionManagementConfig(BaseModel):
    """组合级渐进建仓、加仓与退出规则。"""

    max_positions: int = Field(..., ge=1, le=1000, description="最大持仓股票数量 x")
    initial_allocation_pct: float = Field(
        0.5,
        gt=0,
        le=1,
        description="首次买入占单票资金上限 N/x 的比例",
    )
    add_allocation_pct: float = Field(
        0.25,
        gt=0,
        le=1,
        description="每次加仓占单票资金上限 N/x 的比例",
    )
    add_trigger_pct: float = Field(
        0.10,
        gt=0,
        le=5,
        description="相对首次买入价每上涨一个档位触发一次加仓",
    )
    stop_loss_pct: float | None = Field(
        0.10,
        gt=0,
        le=0.8,
        description="相对当前平均成本的止损比例；null 表示关闭",
    )
    take_profit_mode: Literal["none", "fixed", "trailing"] = Field(
        "none",
        description="止盈模式：关闭、达到阈值直接止盈、启动后按最高价回撤止盈",
    )
    take_profit_pct: float | None = Field(
        None,
        gt=0,
        le=5,
        description="fixed 的直接止盈阈值，或 trailing 的启动阈值",
    )
    trailing_drawdown_pct: float | None = Field(
        None,
        gt=0,
        lt=1,
        description="trailing 启动后相对最高价的回撤比例",
    )

    @model_validator(mode="after")
    def check_take_profit(self) -> PositionManagementConfig:
        if self.take_profit_mode != "none" and self.take_profit_pct is None:
            raise ValueError("启用止盈时须设置 take_profit_pct")
        if self.take_profit_mode == "trailing" and self.trailing_drawdown_pct is None:
            raise ValueError("trailing 止盈须设置 trailing_drawdown_pct")
        return self


class BacktestSharedResponse(BaseModel):
    """横截面策略的共享资金选股/回测响应。"""

    strategy_id: str = ""
    mode: str = "backtest"
    universe_note: str = ""
    asof: str | None = None
    holdings: list[dict[str, Any]] = Field(default_factory=list)
    equity: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[dict[str, Any]] = Field(default_factory=list)
    rebalances: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    benchmarks: dict[str, Any] = Field(
        default_factory=dict,
        description="基准曲线，如 hs300=沪深300指数买入持有",
    )
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str = "演示用途，不构成投资建议。"


class ChipDistRequest(BaseModel):
    """筹码分布分析请求。"""

    symbol: str = Field(..., min_length=1, description="A 股 6 位代码或带 SH/SZ 后缀")
    days: int = Field(500, ge=50, le=5000, description="回溯交易日数量")
    bins: int = Field(200, ge=50, le=1000, description="价格区间网格数")
    detailed: bool = Field(False, description="是否包含完整分布数组")

    model_config = {"extra": "ignore"}


class ChipDistResponse(BaseModel):
    """筹码分布分析结果。"""

    symbol: str
    name: str | None = None
    profit_ratio: float = Field(..., description="获利盘比例 (0~1)")
    average_cost: float = Field(..., description="平均成本（元）")
    peak_price: float = Field(..., description="筹码峰值价格（元）")
    interval_90_low: float = Field(..., description="90% 成本区间下沿")
    interval_90_high: float = Field(..., description="90% 成本区间上沿")
    concentration_90: float = Field(..., description="90% 集中度，越小越集中")
    interval_70_low: float = Field(..., description="70% 成本区间下沿")
    interval_70_high: float = Field(..., description="70% 成本区间上沿")
    concentration_70: float = Field(..., description="70% 集中度，越小越集中")
    current_price: float = Field(..., description="最新收盘价")
    days_used: int = Field(..., description="实际使用的交易日数")
    price_grid: list[float] | None = Field(None, description="价格网格（detailed=true 时返回）")
    distribution: list[float] | None = Field(None, description="筹码分布（detailed=true 时返回）")
    warning: str | None = None
    disclaimer: str = "演示用途，筹码分布分析不构成投资建议。"

    model_config = {"extra": "ignore"}


BacktestRequest.model_rebuild()

