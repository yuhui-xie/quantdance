"""API 与 CLI 共用的请求体模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class BacktestRequest(BaseModel):
    mode: Literal["single", "universe"] = "single"
    data_source: Literal["a_stock_data"] = "a_stock_data"
    strategy_id: str = "ma_crossover"
    initial_cash: float = Field(100_000, ge=1000)
    commission: float = Field(0.0003, ge=0, le=0.05)
    stop_loss_pct: float | None = Field(
        None,
        gt=0,
        le=0.8,
        description="通用止损：相对本次买入价下跌该比例时卖出；null 表示关闭",
    )
    bars: int = Field(500, ge=50, le=5000)
    symbol: str | None = Field(None, description="A 股 6 位代码或带 SH/SZ 后缀")
    universe: Literal["all_a", "hs300", "zz500", "zz399101"] = Field(
        "all_a",
        description="universe 模式且 symbols 为空时使用的股票池",
    )
    symbols: list[str] = Field(default_factory=list, description="universe 模式的自定义股票池")
    max_universe: int = Field(80, ge=1, le=10000)
    seed: int | None = Field(None, description="股票池超上限时的可复现抽样种子")
    max_workers: int = Field(8, ge=1, le=32)
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
        }
        found = sorted(strategy_keys.intersection(data))
        if found:
            joined = ", ".join(found)
            raise ValueError(f"策略专属参数需放入 strategy_params，不应出现在顶层: {joined}")
        return data

    @model_validator(mode="after")
    def check_backtest_request(self) -> BacktestRequest:
        start = (self.start_date or "").strip()
        end = (self.end_date or "").strip()
        if (start and not end) or (end and not start):
            raise ValueError("start_date 与 end_date 须同时填写或同时留空")
        if self.mode == "universe":
            if not start or not end:
                raise ValueError("universe 模式须同时提供 start_date 与 end_date")
            if self.symbol and self.symbol.strip():
                raise ValueError("universe 模式请使用 symbols，不应填写 symbol")
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


class PortfolioBacktestRequest(BaseModel):
    """低频组合：截面选股 / 周期调仓回测请求。"""

    strategy_id: str = Field("market_auntie", description="组合策略 id，见 portfolio --list-strategies")
    mode: Literal["backtest", "screen"] = "backtest"
    data_source: Literal["a_stock_data"] = "a_stock_data"
    universe: Literal["all_a", "hs300", "zz500", "zz399101"] = Field(
        "zz500",
        description="symbols 为空时的股票池；默认中证500，策略可提供其他默认值（如中小综指）",
    )
    symbols: list[str] = Field(default_factory=list)
    max_universe: int = Field(
        80,
        ge=1,
        le=10000,
        description="股票池上限；全 A 约 5000+，设够大即可纳入全部（首次拉估值较慢）",
    )
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
    take_profit_arm_pct: float | None = Field(
        None,
        gt=0,
        le=5,
        description="通用止盈阈值 x：相对成本浮盈达到该比例；exit>0 时仅启动、exit=0 时直接卖出",
    )
    take_profit_exit_pct: float | None = Field(
        None,
        ge=0,
        le=5,
        description="通用止盈回落阈值 y：启动后浮盈回落到该比例则卖出（须 < arm）；填 0 表示达到 arm 即直接止盈、不等回落",
    )
    stop_loss_pct: float | None = Field(
        None,
        gt=0,
        le=0.8,
        description="通用止损：相对成本浮亏达到该比例则卖出（如 0.1=跌10%）",
    )
    position_management: PositionManagementConfig | None = Field(
        None,
        description="可选的系统化渐进建仓模块；设置后替代上方旧版止损止盈字段",
    )
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
        arm = self.take_profit_arm_pct
        exit_lvl = self.take_profit_exit_pct
        if (arm is None) ^ (exit_lvl is None):
            raise ValueError("take_profit_arm_pct 与 take_profit_exit_pct 须同时设置或同时为空")
        if arm is not None and exit_lvl is not None and exit_lvl > 0 and exit_lvl >= arm:
            raise ValueError("take_profit_exit_pct 必须小于 take_profit_arm_pct（填 0 表示达到 arm 直接止盈）")
        if self.position_management is not None and (
            arm is not None or exit_lvl is not None or self.stop_loss_pct is not None
        ):
            raise ValueError("position_management 不能与旧版止损止盈字段同时设置")
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
    benchmarks: dict[str, Any] = Field(
        default_factory=dict,
        description="基准曲线，如 hs300=沪深300指数买入持有",
    )
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str = "演示用途，不构成投资建议。"

