"""横截面策略的数据加载、决策预计算与共享账户执行编排。"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Mapping

import pandas as pd
from pydantic import BaseModel

from app.backtest.benchmarks import attach_hs300_benchmark
from app.backtest.shared_engine import build_close_panel, run_shared_backtest
from app.data_sources.em_fundamentals import load_fundamentals_panel
from app.data_sources.financial_reports import load_financials_panel
from app.data_sources.market_data import MarketDataError, fetch_a_share_daily
from app.position_management import PositionManagementPolicy
from app.schemas import BacktestRequest, BacktestSharedResponse
from app.strategies.base import CrossSectionContext, CrossSectionStrategySpec
from app.universe import resolve_universe_rows

def params_for_cross_section(
    spec: CrossSectionStrategySpec,
    request: BacktestRequest,
) -> BaseModel:
    raw = request.strategy_params or {}
    try:
        return spec.params_model.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"{spec.id} 策略参数无效: {exc}") from exc


def _resolve_universe(
    request: BacktestRequest,
    spec: CrossSectionStrategySpec,
) -> tuple[list[dict[str, str]], str]:
    requested_universe = request.universe
    if "universe" not in request.model_fields_set:
        requested_universe = None
    return resolve_universe_rows(
        symbols=request.symbols,
        universe=requested_universe,
        max_universe=request.max_universe,
        seed=request.seed,
        default_universe=spec.default_universe,
    )


def _load_panel(
    symbols: list[str],
    request: BacktestRequest,
    *,
    spec: CrossSectionStrategySpec,
) -> dict[str, dict[str, pd.DataFrame]]:
    if not spec.needs_fundamentals:
        panel: dict[str, dict[str, pd.DataFrame]] = {}

        def load_daily(symbol: str) -> tuple[str, pd.DataFrame]:
            daily = fetch_a_share_daily(
                symbol,
                limit=5000,
                data_source=request.data_source,
            )
            value = daily.reset_index()
            value = value.rename(columns={value.columns[0]: "date"})
            value["date"] = pd.to_datetime(
                value["date"], errors="coerce"
            ).dt.strftime("%Y-%m-%d")
            value["close"] = pd.to_numeric(value["close"], errors="coerce")
            value["pct_change"] = value["close"].pct_change() * 100.0
            return symbol, value.dropna(subset=["date", "close"])

        with ThreadPoolExecutor(max_workers=min(request.max_workers, len(symbols))) as pool:
            futures = [pool.submit(load_daily, symbol) for symbol in symbols]
            for future in as_completed(futures):
                try:
                    symbol, value = future.result()
                except (MarketDataError, ValueError):
                    continue
                if not value.empty:
                    panel[symbol] = {"value": value}
        if not panel:
            raise MarketDataError("未能加载任何日线行情数据")
        return panel

    panel = load_fundamentals_panel(
        symbols,
        use_cache=request.use_cache,
        force_refresh=request.force_refresh,
        max_workers=request.max_workers,
        include_dividend=spec.needs_dividend,
    )
    if not panel:
        raise MarketDataError("未能加载任何基本面数据")
    if spec.needs_financials:
        financials = load_financials_panel(
            list(panel),
            use_cache=request.use_cache,
            force_refresh=request.force_refresh,
            max_workers=min(4, max(1, request.max_workers)),
        )
        for symbol, payload in panel.items():
            payload["financials"] = financials.get(
                symbol,
                pd.DataFrame(columns=["report_date", "notice_date"]),
            )
    return panel


def _latest_asof(
    panel: Mapping[str, Mapping[str, pd.DataFrame]],
    cutoff: str | None,
) -> str:
    latest: list[str] = []
    for payload in panel.values():
        value = payload.get("value")
        if value is None or value.empty:
            continue
        dates = value["date"].astype(str).str[:10]
        if cutoff:
            dates = dates[dates <= cutoff]
        if not dates.empty:
            latest.append(str(dates.iloc[-1])[:10])
    if not latest:
        raise MarketDataError("基本面面板无有效日期")
    counts = Counter(latest)
    return max(counts, key=lambda day: (counts[day], day))


def run_cross_section_screen(
    request: BacktestRequest,
    spec: CrossSectionStrategySpec,
) -> BacktestSharedResponse:
    params = params_for_cross_section(spec, request)
    if spec.requires_symbols and not request.symbols:
        raise ValueError(f"{spec.id} 策略须通过 symbols 显式提供标的池")
    universe, note = _resolve_universe(request, spec)
    symbols = [row["symbol"] for row in universe]
    names = {row["symbol"]: row.get("name") or "" for row in universe}
    panel = _load_panel(symbols, request, spec=spec)
    requested = (request.end_date or "").strip()[:10]
    asof = _latest_asof(panel, requested or None)
    _, details = spec.select(asof, CrossSectionContext(panel=panel, names=names), params)
    warnings = list(spec.warnings)
    if requested and requested != asof:
        warnings.append(f"请求截面日期 {requested} 无广泛可用交易数据，已回退至 {asof}。")
    return BacktestSharedResponse.model_validate({
        "strategy_id": spec.id,
        "mode": "screen",
        "universe_note": note,
        "asof": asof,
        "holdings": details,
        "equity": [],
        "trades": [],
        "rebalances": [],
        "metrics": {},
        "benchmarks": {},
        "warnings": warnings,
        "disclaimer": "演示用途，不构成投资建议。",
    })


def run_cross_section_backtest(
    request: BacktestRequest,
    spec: CrossSectionStrategySpec,
) -> BacktestSharedResponse:
    """运行一个横截面策略；策略选择只在其决策日调用一次。"""
    start = (request.start_date or "").strip()
    end = (request.end_date or "").strip()
    if not start or not end:
        raise ValueError("横截面回测须同时提供 start_date 与 end_date")
    params = params_for_cross_section(spec, request)
    top_n = int(getattr(params, "top_n", spec.default_top_n))
    if spec.requires_symbols and not request.symbols:
        raise ValueError(f"{spec.id} 策略须通过 symbols 显式提供标的池")

    universe, note = _resolve_universe(request, spec)
    symbols = [row["symbol"] for row in universe]
    names = {row["symbol"]: row.get("name") or "" for row in universe}
    panel = _load_panel(symbols, request, spec=spec)
    clipped: dict[str, pd.DataFrame] = {}
    for symbol, payload in panel.items():
        value = payload["value"].copy()
        value = value[(value["date"] >= start) & (value["date"] <= end)]
        if len(value) >= 5:
            clipped[symbol] = value
    if len(clipped) < max(top_n, 1):
        raise MarketDataError(
            f"区间内有效股票不足（{len(clipped)} < top_n={top_n}），请扩大股票池或放宽过滤"
        )

    close_panel = build_close_panel(clipped)
    context = CrossSectionContext(panel=panel, names=names)
    decision_dates = [
        day
        for day in spec.decision_dates(close_panel.index.tolist(), context, params)
        if start <= day <= end
    ]
    if not decision_dates:
        raise ValueError("指定区间内无决策日")

    targets_by_date: dict[str, list[str]] = {}
    selection_by_date: dict[str, list[dict[str, Any]]] = {}
    for asof in decision_dates:
        targets, details = spec.select(asof, context, params)
        targets_by_date[asof] = targets
        selection_by_date[asof] = details

    policy_config = getattr(request, "position_management", None)
    policy = (
        PositionManagementPolicy(**policy_config.model_dump())
        if policy_config is not None
        else None
    )
    result = run_shared_backtest(
        close_panel,
        targets_by_date=targets_by_date,
        initial_cash=request.initial_cash,
        commission=request.commission,
        min_commission=request.min_commission,
        slippage=request.slippage,
        lot_size=request.lot_size,
        take_profit_arm_pct=getattr(request, "take_profit_arm_pct", None),
        take_profit_exit_pct=getattr(request, "take_profit_exit_pct", None),
        stop_loss_pct=request.stop_loss_pct,
        position_policy=policy,
    )
    latest = decision_dates[-1]
    out = {
        "strategy_id": spec.id,
        "mode": "backtest",
        "universe_note": note,
        "asof": latest,
        "holdings": selection_by_date.get(latest, []),
        "equity": result.equity,
        "trades": result.trades,
        "rebalances": [
            {**rebalance, "selection": selection_by_date.get(rebalance["date"], [])}
            for rebalance in result.rebalances
        ],
        "metrics": dict(result.metrics),
        "benchmarks": {},
        "warnings": list(spec.warnings),
        "disclaimer": "演示用途，历史回测不构成投资建议。",
    }
    return BacktestSharedResponse.model_validate(
        attach_hs300_benchmark(out, initial_cash=request.initial_cash)
    )
