"""与 HTTP 无关的回测执行逻辑，供 API 与 CLI 复用。"""

from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import BaseModel

from app.data_sources.market_data import MarketDataError, fetch_a_share_daily
from app.schemas import BacktestRequest
from app.strategies.base import BaseBacktestParams, StrategySpec
from app.strategies.registry import get_strategy


def params_for_strategy(body: BacktestRequest, spec: StrategySpec) -> BaseModel:
    keys = set(spec.params_model.model_fields.keys())
    data = body.strategy_params
    sub = {k: data[k] for k in keys if k in data}
    try:
        return spec.params_model.model_validate(sub)
    except Exception as e:
        raise ValueError(f"策略参数无效: {e}") from e


def load_ohlcv(body: BacktestRequest) -> pd.DataFrame:
    if not body.symbol or not str(body.symbol).strip():
        raise ValueError("A 股回测需要填写股票代码 symbol")

    if body.start_date and body.end_date:
        return fetch_a_share_daily(
            body.symbol.strip(),
            start=body.start_date.strip(),
            end=body.end_date.strip(),
            data_source=body.data_source,
        )
    return fetch_a_share_daily(body.symbol.strip(), limit=body.bars, data_source=body.data_source)


def run_backtest_request(body: BacktestRequest) -> dict[str, Any]:
    """
    执行回测，返回与 POST /api/backtest 相同的字典结构。
    失败时抛出 ValueError（参数/业务）或 MarketDataError（行情源）。
    """
    spec = get_strategy(body.strategy_id)
    if spec is None:
        raise ValueError(f"未知策略: {body.strategy_id}")

    params = params_for_strategy(body, spec)
    base = BaseBacktestParams(initial_cash=body.initial_cash, commission=body.commission)

    df = load_ohlcv(body)
    if df.empty:
        raise ValueError("行情数据为空，无法回测")
    need = spec.min_bars(params)
    if len(df) < need:
        raise ValueError(f"K 线数量不足：当前 {len(df)} 根，该策略至少需要 {need} 根")

    result = spec.run(df, base, params)
    return {
        "metrics": result.metrics,
        "equity": result.equity,
        "trades": result.trades,
        "price": result.price,
    }
