"""低频组合策略统一执行：截面选股 / 周期调仓回测。"""

from __future__ import annotations

import time
from typing import Any, Mapping

import pandas as pd
from pydantic import BaseModel

from app.data_sources.em_fundamentals import load_fundamentals_panel
from app.data_sources.financial_reports import load_financials_panel
from app.data_sources.market_data import MarketDataError
from app.portfolio.base import PortfolioSelectContext, PortfolioStrategySpec
from app.portfolio.benchmarks import attach_hs300_benchmark
from app.portfolio.registry import get_portfolio_strategy
from app.portfolio.timing import StageTimer
from app.portfolio.universe import resolve_universe
from app.portfolio_engine import (
    PortfolioBacktestResult,
    build_close_panel,
    every_n_trading_days,
    run_equal_weight_rebalance,
)
from app.schemas import PortfolioBacktestRequest, PortfolioBacktestResponse

# 最近一次组合请求的分阶段耗时（供 CLI 附加输出阶段后一并打印）
LAST_PROFILE: StageTimer | None = None


def _params_for(spec: PortfolioStrategySpec, req: PortfolioBacktestRequest) -> BaseModel:
    raw = req.strategy_params or {}
    if not isinstance(raw, dict):
        raise ValueError("strategy_params 必须是对象")
    try:
        return spec.params_model.model_validate(raw)
    except Exception as e:
        raise ValueError(f"{spec.id} 策略参数无效: {e}") from e


def _latest_asof(panel: Mapping[str, Mapping[str, pd.DataFrame]]) -> str:
    latest: list[str] = []
    for payload in panel.values():
        v = payload.get("value")
        if v is not None and not v.empty:
            latest.append(str(v["date"].iloc[-1])[:10])
    if not latest:
        raise MarketDataError("基本面面板无有效日期")
    return max(latest)


def _load_panel(
    symbols: list[str],
    req: PortfolioBacktestRequest,
    *,
    needs_dividend: bool,
    needs_financials: bool = False,
    timer: StageTimer | None = None,
) -> dict[str, dict[str, pd.DataFrame]]:
    t0 = time.perf_counter()
    panel = load_fundamentals_panel(
        symbols,
        use_cache=req.use_cache,
        force_refresh=req.force_refresh,
        max_workers=req.max_workers,
        include_dividend=needs_dividend,
    )
    if timer is not None:
        timer.add("load_fundamentals_panel", time.perf_counter() - t0)
    if not panel:
        raise MarketDataError("未能加载任何基本面数据")
    if needs_financials:
        t1 = time.perf_counter()
        fin = load_financials_panel(
            list(panel.keys()),
            use_cache=req.use_cache,
            force_refresh=req.force_refresh,
            max_workers=min(4, max(1, req.max_workers)),
        )
        for sym, payload in panel.items():
            payload["financials"] = fin.get(
                sym, pd.DataFrame(columns=["report_date", "notice_date"])
            )
        if timer is not None:
            timer.add("load_financials_panel", time.perf_counter() - t1)
    return panel


def run_portfolio_screen(
    req: PortfolioBacktestRequest,
    spec: PortfolioStrategySpec,
    *,
    profile: bool = False,
) -> PortfolioBacktestResponse:
    global LAST_PROFILE
    timer = StageTimer(title=f"portfolio screen · {spec.id}")

    params = _params_for(spec, req)
    universe, note = resolve_universe(req, default_universe=spec.default_universe)
    symbols = [u["symbol"] for u in universe]
    names = {u["symbol"]: u.get("name") or "" for u in universe}
    timer.mark("resolve_universe")
    timer.note(f"universe={len(symbols)} symbols")

    panel = _load_panel(
        symbols,
        req,
        needs_dividend=spec.needs_dividend,
        needs_financials=spec.needs_financials,
        timer=timer,
    )
    timer.mark("load_panel")
    timer.note(f"panel_loaded={len(panel)}")

    asof = (req.end_date or "").strip() or _latest_asof(panel)
    ctx = PortfolioSelectContext(panel=panel, names=names)
    t_sel = time.perf_counter()
    _syms, details = spec.select(asof, ctx, params)
    timer.add("select_once", time.perf_counter() - t_sel)
    timer.mark("select")

    resp = PortfolioBacktestResponse(
        strategy_id=spec.id,
        mode="screen",
        universe_note=note,
        asof=asof,
        holdings=details,
        equity=[],
        trades=[],
        rebalances=[],
        metrics={},
        warnings=list(spec.warnings)
        + ["截面选股使用当前成分股/名称过滤，存在幸存者偏差与 ST 名称时效偏差。"],
        disclaimer="演示用途，不构成投资建议。",
    )
    timer.mark("build_response")
    LAST_PROFILE = timer
    if profile:
        timer.print()
    return resp


def run_portfolio_backtest(
    req: PortfolioBacktestRequest,
    spec: PortfolioStrategySpec,
    *,
    profile: bool = False,
) -> PortfolioBacktestResponse:
    global LAST_PROFILE
    timer = StageTimer(title=f"portfolio backtest · {spec.id}")

    start = (req.start_date or "").strip()
    end = (req.end_date or "").strip()
    if not start or not end:
        raise ValueError("组合回测须同时提供 start_date 与 end_date")

    params = _params_for(spec, req)
    top_n = int(getattr(params, "top_n", spec.default_top_n))
    universe, note = resolve_universe(req, default_universe=spec.default_universe)
    symbols = [u["symbol"] for u in universe]
    names = {u["symbol"]: u.get("name") or "" for u in universe}
    timer.mark("resolve_universe")
    timer.note(f"universe={len(symbols)} symbols")

    panel = _load_panel(
        symbols,
        req,
        needs_dividend=spec.needs_dividend,
        needs_financials=spec.needs_financials,
        timer=timer,
    )
    timer.mark("load_panel")
    timer.note(f"panel_loaded={len(panel)}")

    clipped: dict[str, pd.DataFrame] = {}
    for sym, payload in panel.items():
        v = payload["value"].copy()
        v = v[(v["date"] >= start) & (v["date"] <= end)]
        if len(v) >= 5:
            clipped[sym] = v
    if len(clipped) < max(top_n, 1):
        raise MarketDataError(
            f"区间内有效股票不足（{len(clipped)} < top_n={top_n}），请扩大股票池或放宽过滤"
        )
    timer.mark("clip_panel")
    timer.note(f"clipped={len(clipped)}")

    close_panel = build_close_panel(clipped)
    rebalance_dates = every_n_trading_days(
        close_panel.index.tolist(),
        int(req.rebalance_freq),
    )
    rebalance_dates = [d for d in rebalance_dates if start <= d <= end]
    if not rebalance_dates:
        raise ValueError("指定区间内无调仓日")
    timer.mark("build_close_panel")
    timer.note(f"days={len(close_panel.index)} rebalances={len(rebalance_dates)}")

    selection_cache: dict[str, list[dict[str, Any]]] = {}
    ctx = PortfolioSelectContext(panel=panel, names=names)
    select_calls = 0

    def _select(asof: str, _ctx: Mapping[str, Any]) -> list[str]:
        nonlocal select_calls
        t0 = time.perf_counter()
        syms, details = spec.select(asof, ctx, params)
        timer.add("select_total", time.perf_counter() - t0)
        select_calls += 1
        selection_cache[asof] = details
        return syms

    t_eng = time.perf_counter()
    result: PortfolioBacktestResult = run_equal_weight_rebalance(
        close_panel,
        rebalance_dates=rebalance_dates,
        select_holdings=_select,
        initial_cash=req.initial_cash,
        commission=req.commission,
        min_commission=req.min_commission,
        slippage=req.slippage,
        lot_size=req.lot_size,
        take_profit_arm_pct=req.take_profit_arm_pct,
        take_profit_exit_pct=req.take_profit_exit_pct,
        stop_loss_pct=req.stop_loss_pct,
    )
    eng_secs = time.perf_counter() - t_eng
    select_secs = timer.extras.get("select_total", 0.0)
    timer.add("engine_sim_only", max(0.0, eng_secs - select_secs))
    timer.mark("rebalance_engine")
    timer.note(f"select_calls={select_calls}")

    warnings = list(spec.warnings) + [
        f"调仓间隔: 每 {req.rebalance_freq} 个交易日。",
        "估值来自东财 stock_value_em；涨跌停/停牌为近似过滤。",
    ]
    if req.take_profit_arm_pct is not None and req.take_profit_exit_pct is not None:
        warnings.append(
            f"通用止盈: 浮盈≥{req.take_profit_arm_pct:.0%} 启动，回落至 "
            f"{req.take_profit_exit_pct:.0%} 卖出（仅非调仓日）。"
        )
    if req.stop_loss_pct is not None:
        warnings.append(
            f"通用止损: 浮亏≥{req.stop_loss_pct:.0%} 卖出（仅非调仓日）。"
        )
    if len(panel) < len(symbols):
        warnings.append(
            f"股票池 {len(symbols)} 只中成功加载基本面 {len(panel)} 只，其余已跳过。"
        )

    raw = {
        "strategy_id": spec.id,
        "mode": "backtest",
        "universe_note": note,
        "asof": rebalance_dates[-1],
        "holdings": selection_cache.get(rebalance_dates[-1], []),
        "equity": result.equity,
        "trades": result.trades,
        "rebalances": [
            {**rb, "selection": selection_cache.get(rb["date"], [])}
            for rb in result.rebalances
        ],
        "metrics": dict(result.metrics),
        "benchmarks": {},
        "warnings": warnings,
        "disclaimer": "演示用途，历史回测不构成投资建议。",
    }
    timer.mark("assemble_result")

    enriched = attach_hs300_benchmark(raw, initial_cash=req.initial_cash)
    timer.mark("attach_hs300_benchmark")

    resp = PortfolioBacktestResponse.model_validate(enriched)
    timer.mark("validate_response")

    LAST_PROFILE = timer
    if profile:
        timer.print()
    return resp


def run_portfolio_request(
    req: PortfolioBacktestRequest,
    *,
    profile: bool = False,
) -> PortfolioBacktestResponse:
    spec = get_portfolio_strategy(req.strategy_id)
    if spec is None:
        known = ", ".join(sorted(_strategy_ids()))
        raise ValueError(f"未知组合策略: {req.strategy_id}；可选: {known}")
    if req.mode == "screen":
        return run_portfolio_screen(req, spec, profile=profile)
    return run_portfolio_backtest(req, spec, profile=profile)


def _strategy_ids() -> list[str]:
    from app.portfolio.registry import PORTFOLIO_STRATEGIES

    return list(PORTFOLIO_STRATEGIES.keys())
