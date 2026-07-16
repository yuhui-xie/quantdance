"""API 与 CLI 共用的请求体模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class BacktestRequest(BaseModel):
    data_source: Literal["a_stock_data"] = "a_stock_data"
    strategy_id: str = "ma_crossover"
    initial_cash: float = Field(100_000, ge=1000)
    commission: float = Field(0.0003, ge=0, le=0.05)
    bars: int = Field(500, ge=50, le=5000)
    symbol: str | None = Field(None, description="A 股 6 位代码或带 SH/SZ 后缀")
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
            "stop_loss_pct",
            "take_profit_pct",
            "period",
            "oversold",
            "overbought",
            "num_std",
            "k_period",
            "d_period",
            "smooth",
        }
        found = sorted(strategy_keys.intersection(data))
        if found:
            joined = ", ".join(found)
            raise ValueError(f"策略专属参数需放入 strategy_params，不应出现在顶层: {joined}")
        return data


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


class DiscoveryCandidate(BaseModel):
    symbol: str
    name: str | None = None
    strategy_id: str
    score: float
    metrics: dict[str, float] = Field(default_factory=dict)
    latest_signal: str = "hold"
    last_trade_date: str | None = None
    filters: dict[str, float] = Field(default_factory=dict)
    robustness: dict[str, float] = Field(default_factory=dict)


class DiscoveryRun(BaseModel):
    symbol: str
    name: str | None = None
    strategy_id: str
    metrics: dict[str, float] = Field(default_factory=dict)
    latest_signal: str = "hold"
    last_trade_date: str | None = None
    filters: dict[str, float] = Field(default_factory=dict)
    robustness: dict[str, float] = Field(default_factory=dict)
    score: float


class DiscoveryRequest(BaseModel):
    """策略回测驱动的股票发现：股票池批量回测、排序与稳健性过滤。"""

    data_source: Literal["a_stock_data"] = "a_stock_data"
    universe: Literal["all_a", "hs300"] = Field(
        "all_a",
        description="symbols 为空时使用的股票池：all_a 为全 A 股，hs300 为沪深300当前成分股",
    )
    symbols: list[str] = Field(default_factory=list, description="为空时从全 A 股 universe 拉取")
    strategies: list[str] = Field(default_factory=lambda: ["ma_crossover"])
    strategy_params: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="按 strategy_id 分组的策略参数，例如 {'ma_crossover': {'fast_period': 5}}",
    )
    top_k: int = Field(20, ge=1, le=100)
    max_universe: int = Field(300, ge=1, le=500)
    seed: int | None = Field(None, description="symbols 为空且 universe 超上限时可复现抽样")
    include_benchmarks: bool = Field(
        True,
        description="为 hs300 股票池计算沪深300成分股等权基准，并写入超额收益指标",
    )
    initial_cash: float = Field(100_000, ge=1000)
    commission: float = Field(0.0003, ge=0, le=0.05)
    bars: int = Field(500, ge=80, le=5000)
    start_date: str | None = Field(None, description="与 end_date 成对，YYYY-MM-DD")
    end_date: str | None = Field(None, description="与 start_date 成对，YYYY-MM-DD")
    min_bars: int = Field(120, ge=50, le=5000)
    min_avg_volume: float = Field(0.0, ge=0)
    min_last_close: float = Field(0.0, ge=0)
    max_last_close: float | None = Field(None, ge=0)
    min_trades: int = Field(1, ge=0)
    min_total_return: float | None = None
    max_drawdown: float | None = Field(None, ge=0, le=1)
    min_pe_ttm: float | None = Field(None, gt=0)
    max_pe_ttm: float | None = Field(None, gt=0)
    min_pb: float | None = Field(None, gt=0)
    max_pb: float | None = Field(None, gt=0)
    min_market_cap: float | None = Field(None, gt=0)
    max_market_cap: float | None = Field(None, gt=0)
    min_float_market_cap: float | None = Field(None, gt=0)
    max_float_market_cap: float | None = Field(None, gt=0)
    min_turnover_rate: float | None = Field(None, gt=0)
    max_turnover_rate: float | None = Field(None, gt=0)
    robustness_windows: list[int] = Field(
        default_factory=lambda: [252, 126],
        description="用最近 N 根 K 线重跑策略，统计通过率与最差收益",
    )
    param_perturbation_pct: float = Field(
        0.1,
        ge=0,
        le=0.5,
        description="数值型策略参数上下扰动比例；0 表示关闭参数扰动验证",
    )
    max_perturbation_sets: int = Field(8, ge=0, le=50)

    @model_validator(mode="after")
    def check_discovery_request(self) -> DiscoveryRequest:
        s = (self.start_date or "").strip()
        e = (self.end_date or "").strip()
        if (s and not e) or (e and not s):
            raise ValueError("start_date 与 end_date 须同时填写或同时留空")
        if not self.strategies:
            raise ValueError("strategies 至少需要 1 个策略")
        if self.max_last_close is not None and self.max_last_close < self.min_last_close:
            raise ValueError("max_last_close 不能小于 min_last_close")
        pairs = (
            ("pe_ttm", self.min_pe_ttm, self.max_pe_ttm),
            ("pb", self.min_pb, self.max_pb),
            ("market_cap", self.min_market_cap, self.max_market_cap),
            ("float_market_cap", self.min_float_market_cap, self.max_float_market_cap),
            ("turnover_rate", self.min_turnover_rate, self.max_turnover_rate),
        )
        for name, min_value, max_value in pairs:
            if min_value is not None and max_value is not None and max_value < min_value:
                raise ValueError(f"max_{name} 不能小于 min_{name}")
        return self

    model_config = {"extra": "ignore"}


class DiscoveryResponse(BaseModel):
    candidates: list[DiscoveryCandidate]
    runs: list[DiscoveryRun]
    summary: dict[str, float] = Field(default_factory=dict)
    benchmarks: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    universe_note: str = ""
    disclaimer: str = "演示用途，策略回测筛选不构成投资建议。"


class PortfolioBacktestRequest(BaseModel):
    """低频组合：截面选股 / 周期调仓回测请求。"""

    strategy_id: str = Field("market_auntie", description="组合策略 id，见 portfolio --list-strategies")
    mode: Literal["backtest", "screen"] = "backtest"
    data_source: Literal["a_stock_data"] = "a_stock_data"
    universe: Literal["all_a", "hs300", "zz399101"] = Field(
        "all_a",
        description="symbols 为空时的股票池；策略可提供默认值（如中小综指）",
    )
    symbols: list[str] = Field(default_factory=list)
    max_universe: int = Field(80, ge=1, le=1000)
    seed: int | None = Field(None, description="股票池抽样种子")
    start_date: str | None = Field(None, description="回测起始 YYYY-MM-DD")
    end_date: str | None = Field(None, description="回测结束 / 选股截面日 YYYY-MM-DD")
    rebalance_freq: int = Field(
        20,
        ge=1,
        le=252,
        description="调仓间隔（交易日）。20≈月频，5≈周频；从回测首个交易日起每隔 N 日调仓",
    )
    initial_cash: float = Field(100_000, ge=1000)
    commission: float = Field(0.0003, ge=0, le=0.05)
    min_commission: float = Field(5.0, ge=0, description="单笔最低佣金（元）")
    slippage: float = Field(0.01, ge=0, le=0.05, description="单边滑点比例，默认 1%")
    lot_size: int = Field(100, ge=1, description="买入整手数（股）")
    use_cache: bool = True
    force_refresh: bool = False
    max_workers: int = Field(8, ge=1, le=32)
    strategy_params: dict[str, Any] = Field(
        default_factory=dict,
        description="当前组合策略专属参数，如 top_n / max_peg",
    )

    @model_validator(mode="after")
    def check_portfolio_request(self) -> PortfolioBacktestRequest:
        s = (self.start_date or "").strip()
        e = (self.end_date or "").strip()
        if self.mode == "backtest":
            if not s or not e:
                raise ValueError("backtest 模式须同时提供 start_date 与 end_date")
        elif s and not e:
            raise ValueError("填写 start_date 时须同时填写 end_date")
        return self

    model_config = {"extra": "ignore"}


class PortfolioBacktestResponse(BaseModel):
    strategy_id: str = ""
    mode: str = "backtest"
    universe_note: str = ""
    asof: str | None = None
    holdings: list[dict[str, Any]] = Field(default_factory=list)
    equity: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[dict[str, Any]] = Field(default_factory=list)
    rebalances: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str = "演示用途，不构成投资建议。"

