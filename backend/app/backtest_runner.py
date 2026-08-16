"""与 HTTP 无关的回测执行逻辑，供 API 与 CLI 复用。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import numpy as np
import pandas as pd
from pydantic import BaseModel

from app.backtest_aggregate import equal_weight_equity_curve
from app.backtest.cross_section_runner import (
    run_cross_section_backtest,
    run_cross_section_per_stock_backtest,
    run_cross_section_screen,
)
from app.data_sources.market_data import fetch_a_share_daily
from app.schemas import (
    BacktestRequest,
    BacktestSymbolRun,
    BacktestUniverseResponse,
    BacktestUniverseSummary,
)
from app.strategies.base import (
    BaseBacktestParams,
    CrossSectionStrategySpec,
    StrategySpec,
)
from app.strategies.registry import get_registered_strategy, get_strategy
from app.universe import resolve_universe_rows

# 进度回调：done(已完成数) / total(总数) / current(当前标的或日期)
ProgressCallback = Callable[[int, int, str], None]


def params_for_strategy(body: BacktestRequest, spec: StrategySpec) -> BaseModel:
    keys = set(spec.params_model.model_fields.keys())
    data = body.strategy_params
    sub = {k: data[k] for k in keys if k in data}
    try:
        return spec.params_model.model_validate(sub)
    except Exception as e:
        raise ValueError(f"策略参数无效: {e}") from e


def load_symbol_ohlcv(symbol: str, body: BacktestRequest) -> pd.DataFrame:
    code = str(symbol).strip()
    if not code:
        raise ValueError("A 股回测需要填写股票代码 symbol")

    if body.start_date and body.end_date:
        return fetch_a_share_daily(
            code,
            start=body.start_date.strip(),
            end=body.end_date.strip(),
            data_source=body.data_source,
        )
    return fetch_a_share_daily(code, limit=body.bars, data_source=body.data_source)


def load_ohlcv(body: BacktestRequest) -> pd.DataFrame:
    if not body.symbol:
        raise ValueError("A 股回测需要填写股票代码 symbol")
    return load_symbol_ohlcv(body.symbol, body)


def _run_strategy_on_symbol(
    symbol: str,
    name: str | None,
    body: BacktestRequest,
    spec: StrategySpec,
    params: BaseModel,
    base: BaseBacktestParams,
) -> tuple[BacktestSymbolRun, list[dict[str, Any]]]:
    try:
        df = load_symbol_ohlcv(symbol, body)
    except Exception as exc:
        return (
            BacktestSymbolRun(
                symbol=symbol,
                name=name,
                status="failed",
                reason=str(exc),
            ),
            [],
        )

    if df.empty:
        return (
            BacktestSymbolRun(
                symbol=symbol,
                name=name,
                status="skipped",
                reason="行情数据为空",
            ),
            [],
        )
    need = spec.min_bars(params)
    if len(df) < need:
        return (
            BacktestSymbolRun(
                symbol=symbol,
                name=name,
                status="skipped",
                reason=f"K 线数量不足：当前 {len(df)} 根，该策略至少需要 {need} 根",
            ),
            [],
        )

    try:
        result = spec.run(df, base, params)
    except Exception as exc:
        return (
            BacktestSymbolRun(
                symbol=symbol,
                name=name,
                status="failed",
                reason=f"策略执行失败: {exc}",
            ),
            [],
        )

    full_equity = result.equity
    return (
        BacktestSymbolRun(
            symbol=symbol,
            name=name,
            status="ok",
            metrics=result.metrics,
            equity=full_equity if body.include_equity else [],
            trades=result.trades if body.include_trades else [],
            price=result.price if body.include_price else [],
        ),
        full_equity,
    )


def run_backtest_universe_request(
    body: BacktestRequest,
    progress: ProgressCallback | None = None,
) -> BacktestUniverseResponse:
    """同一策略在股票池逐票独立回测，并按归一化净值等权汇总。"""
    if body.mode != "universe":
        raise ValueError("批量回测需要 mode=universe")
    spec = get_strategy(body.strategy_id)
    if spec is None:
        raise ValueError(f"未知策略: {body.strategy_id}")
    params = params_for_strategy(body, spec)
    base = BaseBacktestParams(
        initial_cash=body.initial_cash,
        commission=body.commission,
        stop_loss_pct=body.stop_loss_pct,
    )
    universe, note = resolve_universe_rows(
        symbols=body.symbols,
        universe=body.universe,
        max_universe=body.max_universe,
        seed=body.seed,
    )
    if not universe:
        raise ValueError("股票池为空，无法回测")

    def execute(item: dict[str, str]) -> tuple[BacktestSymbolRun, list[dict[str, Any]]]:
        return _run_strategy_on_symbol(
            item["symbol"],
            item.get("name") or None,
            body,
            spec,
            params,
            base,
        )

    total = len(universe)
    with ThreadPoolExecutor(max_workers=min(body.max_workers, total)) as pool:
        futures = {pool.submit(execute, item): item for item in universe}
        result_by_symbol: dict[str, tuple[BacktestSymbolRun, list[dict[str, Any]]]] = {}
        for done, future in enumerate(as_completed(futures), start=1):
            item = futures[future]
            result_by_symbol[item["symbol"]] = future.result()
            if progress is not None:
                progress(done, total, item["symbol"])
    completed = [result_by_symbol[item["symbol"]] for item in universe]

    runs = [item[0] for item in completed]
    curves = [curve for run, curve in completed if run.status == "ok" and curve]
    successful = [run for run in runs if run.status == "ok"]
    if not successful:
        raise ValueError("股票池中没有可用的回测结果")

    ranked = sorted(
        successful,
        key=lambda run: float(run.metrics.get("total_return", float("-inf"))),
        reverse=True,
    )
    for rank, run in enumerate(ranked, start=1):
        run.rank = rank

    aggregate = equal_weight_equity_curve(curves, initial_cash=body.initial_cash)
    if aggregate is None:
        raise ValueError("有效净值曲线不足，无法生成等权汇总")

    def average_metric(key: str) -> float:
        values = [
            float(run.metrics[key])
            for run in successful
            if key in run.metrics and np.isfinite(float(run.metrics[key]))
        ]
        return float(np.mean(values)) if values else 0.0

    skipped = sum(run.status == "skipped" for run in runs)
    failed = sum(run.status == "failed" for run in runs)
    warnings: list[str] = []
    if skipped:
        warnings.append(f"{skipped} 只股票因行情为空或 K 线不足被跳过。")
    if failed:
        warnings.append(f"{failed} 只股票因数据或策略执行失败。")
    return BacktestUniverseResponse(
        strategy_id=body.strategy_id,
        universe_note=note,
        strategy_params=dict(body.strategy_params or {}),
        summary=BacktestUniverseSummary(
            requested=len(runs),
            succeeded=len(successful),
            skipped=skipped,
            failed=failed,
            average_total_return=average_metric("total_return"),
            average_max_drawdown=average_metric("max_drawdown"),
            average_sharpe=average_metric("sharpe"),
        ),
        aggregate=aggregate,
        runs=runs,
        warnings=warnings,
    )


def run_backtest_request(
    body: BacktestRequest,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """
    执行回测，返回与 POST /api/backtest 相同的字典结构。
    失败时抛出 ValueError（参数/业务）或 MarketDataError（行情源）。
    """
    registered = get_registered_strategy(body.strategy_id)
    if registered is None:
        raise ValueError(f"未知策略: {body.strategy_id}")
    if isinstance(registered, CrossSectionStrategySpec):
        if body.mode not in {"universe", "screen", "per_stock"}:
            raise ValueError("横截面策略仅支持 mode=universe、mode=screen 或 mode=per_stock")
        if body.mode == "per_stock":
            response = run_cross_section_per_stock_backtest(body, registered, progress=progress)
        elif body.mode == "screen":
            response = run_cross_section_screen(body, registered)
        else:
            response = run_cross_section_backtest(body, registered, progress=progress)
        return response.model_dump(mode="json")

    if body.mode == "screen":
        raise ValueError("时序策略不支持 mode=screen")
    if body.mode == "per_stock":
        raise ValueError("时序策略不支持 mode=per_stock，仅横截面策略可用")

    if body.mode == "universe":
        return run_backtest_universe_request(body, progress=progress).model_dump(mode="json")

    spec = registered

    params = params_for_strategy(body, spec)
    base = BaseBacktestParams(
        initial_cash=body.initial_cash,
        commission=body.commission,
        stop_loss_pct=body.stop_loss_pct,
    )

    df = load_ohlcv(body)
    if df.empty:
        raise ValueError("行情数据为空，无法回测")
    need = spec.min_bars(params)
    if len(df) < need:
        raise ValueError(f"K 线数量不足：当前 {len(df)} 根，该策略至少需要 {need} 根")

    result = spec.run(df, base, params)
    return {
        "strategy_id": body.strategy_id,
        "strategy_params": dict(body.strategy_params or {}),
        "symbol": str(body.symbols[0]) if body.symbols else "",
        "metrics": result.metrics,
        "equity": result.equity,
        "trades": result.trades,
        "price": result.price,
    }
