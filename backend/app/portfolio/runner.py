"""低频组合策略统一执行：截面选股 / 周期调仓回测。"""

from __future__ import annotations

from typing import Any, Mapping

import pandas as pd
from pydantic import BaseModel

from app.data_sources.em_fundamentals import load_fundamentals_panel
from app.data_sources.financial_reports import load_financials_panel
from app.data_sources.market_data import MarketDataError
from app.portfolio.base import PortfolioSelectContext, PortfolioStrategySpec
from app.portfolio.registry import get_portfolio_strategy
from app.portfolio.universe import resolve_universe
from app.portfolio_engine import (
    PortfolioBacktestResult,
    build_close_panel,
    every_n_trading_days,
    run_equal_weight_rebalance,
)
from app.schemas import PortfolioBacktestRequest, PortfolioBacktestResponse


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
) -> dict[str, dict[str, pd.DataFrame]]:
    panel = load_fundamentals_panel(
        symbols,
        use_cache=req.use_cache,
        force_refresh=req.force_refresh,
        max_workers=req.max_workers,
        include_dividend=needs_dividend,
    )
    if not panel:
        raise MarketDataError("未能加载任何基本面数据")
    if needs_financials:
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
    return panel


def run_portfolio_screen(
    req: PortfolioBacktestRequest,
    spec: PortfolioStrategySpec,
) -> PortfolioBacktestResponse:
    params = _params_for(spec, req)
    universe, note = resolve_universe(req, default_universe=spec.default_universe)
    symbols = [u["symbol"] for u in universe]
    names = {u["symbol"]: u.get("name") or "" for u in universe}
    panel = _load_panel(
        symbols,
        req,
        needs_dividend=spec.needs_dividend,
        needs_financials=spec.needs_financials,
    )

    asof = (req.end_date or "").strip() or _latest_asof(panel)
    ctx = PortfolioSelectContext(panel=panel, names=names)
    _syms, details = spec.select(asof, ctx, params)
    return PortfolioBacktestResponse(
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


def run_portfolio_backtest(
    req: PortfolioBacktestRequest,
    spec: PortfolioStrategySpec,
) -> PortfolioBacktestResponse:
    start = (req.start_date or "").strip()
    end = (req.end_date or "").strip()
    if not start or not end:
        raise ValueError("组合回测须同时提供 start_date 与 end_date")

    params = _params_for(spec, req)
    top_n = int(getattr(params, "top_n", spec.default_top_n))
    universe, note = resolve_universe(req, default_universe=spec.default_universe)
    symbols = [u["symbol"] for u in universe]
    names = {u["symbol"]: u.get("name") or "" for u in universe}
    panel = _load_panel(
        symbols,
        req,
        needs_dividend=spec.needs_dividend,
        needs_financials=spec.needs_financials,
    )

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

    close_panel = build_close_panel(clipped)
    rebalance_dates = every_n_trading_days(
        close_panel.index.tolist(),
        int(req.rebalance_freq),
    )
    rebalance_dates = [d for d in rebalance_dates if start <= d <= end]
    if not rebalance_dates:
        raise ValueError("指定区间内无调仓日")

    selection_cache: dict[str, list[dict[str, Any]]] = {}
    ctx = PortfolioSelectContext(panel=panel, names=names)

    def _select(asof: str, _ctx: Mapping[str, Any]) -> list[str]:
        syms, details = spec.select(asof, ctx, params)
        selection_cache[asof] = details
        return syms

    result: PortfolioBacktestResult = run_equal_weight_rebalance(
        close_panel,
        rebalance_dates=rebalance_dates,
        select_holdings=_select,
        initial_cash=req.initial_cash,
        commission=req.commission,
        min_commission=req.min_commission,
        slippage=req.slippage,
        lot_size=req.lot_size,
    )

    warnings = list(spec.warnings) + [
        f"调仓间隔: 每 {req.rebalance_freq} 个交易日。",
        "估值来自东财 stock_value_em；涨跌停/停牌为近似过滤。",
    ]
    if len(panel) < len(symbols):
        warnings.append(
            f"股票池 {len(symbols)} 只中成功加载基本面 {len(panel)} 只，其余已跳过。"
        )

    return PortfolioBacktestResponse(
        strategy_id=spec.id,
        mode="backtest",
        universe_note=note,
        asof=rebalance_dates[-1],
        holdings=selection_cache.get(rebalance_dates[-1], []),
        equity=result.equity,
        trades=result.trades,
        rebalances=[
            {**rb, "selection": selection_cache.get(rb["date"], [])}
            for rb in result.rebalances
        ],
        metrics=result.metrics,
        warnings=warnings,
        disclaimer="演示用途，历史回测不构成投资建议。",
    )


def run_portfolio_request(req: PortfolioBacktestRequest) -> PortfolioBacktestResponse:
    spec = get_portfolio_strategy(req.strategy_id)
    if spec is None:
        known = ", ".join(sorted(_strategy_ids()))
        raise ValueError(f"未知组合策略: {req.strategy_id}；可选: {known}")
    if req.mode == "screen":
        return run_portfolio_screen(req, spec)
    return run_portfolio_backtest(req, spec)


def _strategy_ids() -> list[str]:
    from app.portfolio.registry import PORTFOLIO_STRATEGIES

    return list(PORTFOLIO_STRATEGIES.keys())
