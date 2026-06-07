"""策略回测驱动的股票发现：股票池批量扫描、排序与稳健性过滤。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel

from app.data_sources.market_data import (
    MarketDataError,
    fetch_a_share_daily,
    fetch_a_share_universe,
    fetch_a_share_valuation_snapshot,
    fetch_hs300_universe,
    normalize_a_share_symbol,
)
from app.schemas import DiscoveryCandidate, DiscoveryRequest, DiscoveryResponse, DiscoveryRun
from app.strategies.base import BaseBacktestParams, StrategySpec
from app.strategies.registry import get_strategy


def _is_st_stock(name: str | None) -> bool:
    n = (name or "").upper()
    return "ST" in n or "退" in n


def _resolve_universe(req: DiscoveryRequest) -> tuple[list[dict[str, str]], str]:
    if req.symbols:
        seen: set[str] = set()
        rows: list[dict[str, str]] = []
        for raw in req.symbols:
            code = normalize_a_share_symbol(raw)
            if code in seen:
                continue
            seen.add(code)
            rows.append({"symbol": code, "name": ""})
        return rows, f"使用请求传入股票池，共 {len(rows)} 只。"

    if req.universe == "hs300":
        return fetch_hs300_universe(req.max_universe, seed=req.seed)
    return fetch_a_share_universe(req.max_universe, seed=req.seed)


def _load_daily(sym: str, req: DiscoveryRequest) -> pd.DataFrame:
    if req.start_date and req.end_date:
        return fetch_a_share_daily(
            sym,
            start=req.start_date.strip(),
            end=req.end_date.strip(),
            data_source=req.data_source,
        )
    return fetch_a_share_daily(sym, limit=req.bars, data_source=req.data_source)


def _prefilter(df: pd.DataFrame, req: DiscoveryRequest) -> tuple[bool, dict[str, float], str | None]:
    if len(df) < req.min_bars:
        return False, {}, f"K 线不足（需≥{req.min_bars}）"
    if not {"open", "high", "low", "close", "volume"}.issubset(df.columns):
        return False, {}, "OHLCV 列不完整"

    close = pd.to_numeric(df["close"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    last_close = float(close.iloc[-1])
    avg_volume_20 = float(volume.iloc[-20:].mean()) if len(volume) >= 20 else float(volume.mean())
    zero_volume_ratio = float((volume <= 0).mean())
    if not np.isfinite(last_close) or last_close <= 0:
        return False, {}, "最新收盘价无效"
    if avg_volume_20 < req.min_avg_volume:
        return False, {}, "成交量低于阈值"
    if last_close < req.min_last_close:
        return False, {}, "价格低于阈值"
    if req.max_last_close is not None and last_close > req.max_last_close:
        return False, {}, "价格高于阈值"
    if zero_volume_ratio > 0.2:
        return False, {}, "零成交量占比过高"

    return (
        True,
        {
            "last_close": last_close,
            "avg_volume_20": avg_volume_20,
            "zero_volume_ratio": zero_volume_ratio,
            "bars": float(len(df)),
        },
        None,
    )


def _fundamental_filter_enabled(req: DiscoveryRequest) -> bool:
    return any(
        value is not None
        for value in (
            req.min_pe_ttm,
            req.max_pe_ttm,
            req.min_pb,
            req.max_pb,
            req.min_market_cap,
            req.max_market_cap,
            req.min_float_market_cap,
            req.max_float_market_cap,
            req.min_turnover_rate,
            req.max_turnover_rate,
        )
    )


def _passes_range(
    fund: dict[str, float],
    field: str,
    min_value: float | None,
    max_value: float | None,
) -> tuple[bool, str | None]:
    if min_value is None and max_value is None:
        return True, None
    value = fund.get(field)
    if value is None:
        return False, f"{field} 缺失"
    if min_value is not None and value < min_value:
        return False, f"{field} 低于阈值"
    if max_value is not None and value > max_value:
        return False, f"{field} 高于阈值"
    return True, None


def _fundamental_filter(
    fund: dict[str, float] | None,
    req: DiscoveryRequest,
) -> tuple[bool, dict[str, float], str | None]:
    if fund is None:
        return False, {}, "估值数据缺失"

    checks = (
        ("pe_ttm", req.min_pe_ttm, req.max_pe_ttm),
        ("pb", req.min_pb, req.max_pb),
        ("market_cap", req.min_market_cap, req.max_market_cap),
        ("float_market_cap", req.min_float_market_cap, req.max_float_market_cap),
        ("turnover_rate", req.min_turnover_rate, req.max_turnover_rate),
    )
    for field, min_value, max_value in checks:
        ok, reason = _passes_range(fund, field, min_value, max_value)
        if not ok:
            return False, {}, reason

    return True, {k: float(v) for k, v in fund.items()}, None


def _params_for_strategy(req: DiscoveryRequest, spec: StrategySpec) -> BaseModel:
    raw = req.strategy_params.get(spec.id, {})
    if not isinstance(raw, dict):
        raise ValueError(f"{spec.id} 的 strategy_params 必须是对象")
    keys = set(spec.params_model.model_fields.keys())
    data = {k: raw[k] for k in keys if k in raw}
    try:
        return spec.params_model.model_validate(data)
    except Exception as e:
        raise ValueError(f"{spec.id} 策略参数无效: {e}") from e


def _latest_signal(trades: list[dict[str, Any]]) -> tuple[str, str | None]:
    if not trades:
        return "hold_cash", None
    last = trades[-1]
    side = str(last.get("side") or "").lower()
    date = str(last.get("date") or "")[:10] or None
    if side == "buy":
        return "hold_long", date
    if side == "sell":
        return "hold_cash", date
    return "hold", date


def _passes_metric_filters(metrics: dict[str, float], req: DiscoveryRequest) -> bool:
    if metrics.get("closed_trades", 0.0) < req.min_trades:
        return False
    if req.min_total_return is not None and metrics.get("total_return", 0.0) < req.min_total_return:
        return False
    if req.max_drawdown is not None and metrics.get("max_drawdown", 1.0) > req.max_drawdown:
        return False
    return True


def _score(metrics: dict[str, float], robustness: dict[str, float]) -> float:
    total_return = metrics.get("total_return", 0.0)
    annualized = metrics.get("annualized_return", 0.0)
    sharpe = metrics.get("sharpe", 0.0)
    drawdown = metrics.get("max_drawdown", 0.0)
    win_rate = metrics.get("win_rate", 0.0)
    robust_pass = robustness.get("pass_rate", 0.0)
    robust_returns = [robustness.get("worst_total_return", 0.0)]
    if robustness.get("tested_param_sets", 0.0) > 0:
        robust_returns.append(robustness.get("worst_param_total_return", 0.0))
    robust_worst = min(robust_returns)
    return float(
        total_return * 0.25
        + annualized * 0.25
        + sharpe * 0.15
        + win_rate * 0.1
        + robust_pass * 0.15
        + robust_worst * 0.1
        - drawdown * 0.35
    )


def _perturbed_params(params: BaseModel, req: DiscoveryRequest) -> list[BaseModel]:
    if req.param_perturbation_pct <= 0 or req.max_perturbation_sets <= 0:
        return []

    base_data = params.model_dump()
    out: list[BaseModel] = []
    seen: set[tuple[tuple[str, Any], ...]] = set()
    for key, value in base_data.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value == 0:
            continue
        for mult in (1.0 - req.param_perturbation_pct, 1.0 + req.param_perturbation_pct):
            candidate = dict(base_data)
            if isinstance(value, int):
                candidate[key] = max(1, int(round(value * mult)))
            else:
                candidate[key] = float(value * mult)
            frozen = tuple(sorted(candidate.items()))
            if frozen in seen or candidate == base_data:
                continue
            seen.add(frozen)
            try:
                out.append(params.__class__.model_validate(candidate))
            except Exception:
                continue
            if len(out) >= req.max_perturbation_sets:
                return out
    return out


def _robustness(
    df: pd.DataFrame,
    spec: StrategySpec,
    base: BaseBacktestParams,
    params: BaseModel,
    req: DiscoveryRequest,
) -> dict[str, float]:
    windows = sorted({w for w in req.robustness_windows if w > 0})
    window_tested = 0
    window_passed = 0
    returns: list[float] = []
    drawdowns: list[float] = []
    min_need = spec.min_bars(params)

    for window in windows:
        if len(df) < window or window < min_need:
            continue
        window_tested += 1
        result = spec.run(df.iloc[-window:].copy(), base, params)
        metrics = {k: float(v) for k, v in result.metrics.items()}
        returns.append(metrics.get("total_return", 0.0))
        drawdowns.append(metrics.get("max_drawdown", 0.0))
        if _passes_metric_filters(metrics, req):
            window_passed += 1

    param_tested = 0
    param_passed = 0
    param_returns: list[float] = []
    for variant in _perturbed_params(params, req):
        if len(df) < spec.min_bars(variant):
            continue
        param_tested += 1
        result = spec.run(df.copy(), base, variant)
        metrics = {k: float(v) for k, v in result.metrics.items()}
        param_returns.append(metrics.get("total_return", 0.0))
        if _passes_metric_filters(metrics, req):
            param_passed += 1

    total_tested = window_tested + param_tested
    total_passed = window_passed + param_passed
    if total_tested == 0:
        return {
            "tested_windows": 0.0,
            "window_pass_rate": 0.0,
            "tested_param_sets": 0.0,
            "param_pass_rate": 0.0,
            "pass_rate": 0.0,
            "worst_total_return": 0.0,
            "worst_drawdown": 0.0,
            "worst_param_total_return": 0.0,
        }
    return {
        "tested_windows": float(window_tested),
        "window_pass_rate": float(window_passed / window_tested) if window_tested else 0.0,
        "tested_param_sets": float(param_tested),
        "param_pass_rate": float(param_passed / param_tested) if param_tested else 0.0,
        "pass_rate": float(total_passed / total_tested),
        "worst_total_return": float(min(returns)) if returns else 0.0,
        "worst_drawdown": float(max(drawdowns)) if drawdowns else 0.0,
        "worst_param_total_return": float(min(param_returns)) if param_returns else 0.0,
    }


def _clean_metrics(metrics: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in metrics.items():
        try:
            fv = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(fv):
            out[key] = fv
    return out


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / np.where(peak > 0, peak, np.nan)
    if not np.isfinite(dd).any():
        return 0.0
    return float(np.nanmax(dd))


def _sharpe(daily_returns: pd.Series) -> float:
    ret = pd.to_numeric(daily_returns, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if len(ret) < 2:
        return 0.0
    std = float(ret.std(ddof=1))
    if std < 1e-12:
        return 0.0
    return float(np.sqrt(252) * float(ret.mean()) / std)


def _annualized_return(equity: np.ndarray, dates: pd.Index, initial_cash: float) -> float:
    if len(equity) == 0 or initial_cash <= 0:
        return 0.0
    total = float(equity[-1] / initial_cash)
    if total <= 0:
        return -1.0
    years = 0.0
    if len(dates) >= 2:
        parsed = pd.to_datetime(dates, errors="coerce")
        if not parsed.isna().any():
            days = max((parsed[-1] - parsed[0]).days, 1)
            years = days / 365.25
    if years <= 0:
        years = max((len(equity) - 1) / 252.0, 1 / 252.0)
    return float(total ** (1.0 / years) - 1.0)


def _equal_weight_benchmark(
    frames: list[pd.DataFrame],
    *,
    initial_cash: float,
) -> dict[str, Any] | None:
    returns: list[pd.Series] = []
    for df in frames:
        if "close" not in df.columns or df.empty:
            continue
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        close = close[close > 0]
        if len(close) < 2:
            continue
        series = close.pct_change().replace([np.inf, -np.inf], np.nan)
        series.index = pd.to_datetime(series.index).normalize()
        returns.append(series)

    if not returns:
        return None

    ret_df = pd.concat(returns, axis=1).sort_index()
    daily_ret = ret_df.mean(axis=1, skipna=True).fillna(0.0)
    if daily_ret.empty:
        return None

    equity = initial_cash * (1.0 + daily_ret).cumprod()
    eq_arr = equity.to_numpy(dtype=float)
    total_return = float(eq_arr[-1] / initial_cash - 1.0) if initial_cash > 0 else 0.0
    metrics = {
        "initial_cash": float(initial_cash),
        "final_equity": float(eq_arr[-1]),
        "total_return": total_return,
        "annualized_return": _annualized_return(eq_arr, equity.index, initial_cash),
        "max_drawdown": _max_drawdown(eq_arr),
        "sharpe": _sharpe(daily_ret),
        "member_count": float(len(returns)),
        "trading_days": float(len(equity)),
    }
    return {
        "name": "HS300 Equal Weight",
        "description": "沪深300成分股等权买入持有基准，按可用成分股日收益横截面平均合成。",
        "metrics": metrics,
        "equity": [
            {"date": idx.date().isoformat(), "equity": float(value)}
            for idx, value in equity.items()
        ],
    }


def _equal_weight_equity_curve(
    curves: list[list[dict[str, Any]]],
    *,
    initial_cash: float,
    name: str,
    description: str,
) -> dict[str, Any] | None:
    series_list: list[pd.Series] = []
    for curve in curves:
        dates: list[pd.Timestamp] = []
        values: list[float] = []
        for row in curve:
            value = row.get("equity")
            date = row.get("date")
            try:
                fv = float(value)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(fv) or fv <= 0:
                continue
            ts = pd.to_datetime(str(date)[:10], errors="coerce")
            if pd.isna(ts):
                continue
            dates.append(ts.normalize())
            values.append(fv / initial_cash if initial_cash > 0 else fv)
        if len(values) < 2:
            continue
        series = pd.Series(values, index=dates).sort_index()
        series = series[~series.index.duplicated(keep="last")]
        series_list.append(series)

    if not series_list:
        return None

    normalized = pd.concat(series_list, axis=1).sort_index().mean(axis=1, skipna=True).dropna()
    if normalized.empty:
        return None

    equity = normalized * initial_cash
    eq_arr = equity.to_numpy(dtype=float)
    daily_ret = equity.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    total_return = float(eq_arr[-1] / initial_cash - 1.0) if initial_cash > 0 else 0.0
    metrics = {
        "initial_cash": float(initial_cash),
        "final_equity": float(eq_arr[-1]),
        "total_return": total_return,
        "annualized_return": _annualized_return(eq_arr, equity.index, initial_cash),
        "max_drawdown": _max_drawdown(eq_arr),
        "sharpe": _sharpe(daily_ret),
        "member_count": float(len(series_list)),
        "trading_days": float(len(equity)),
    }
    return {
        "name": name,
        "description": description,
        "metrics": metrics,
        "equity": [
            {"date": idx.date().isoformat(), "equity": float(value)}
            for idx, value in equity.items()
        ],
    }


def _attach_benchmark_metrics(
    runs: list[DiscoveryRun],
    benchmark: dict[str, Any] | None,
) -> None:
    if benchmark is None:
        return
    metrics = benchmark.get("metrics", {})
    try:
        benchmark_total_return = float(metrics.get("total_return"))
    except (TypeError, ValueError):
        return
    if not np.isfinite(benchmark_total_return):
        return
    for run in runs:
        total_return = run.metrics.get("total_return", 0.0)
        run.metrics["benchmark_total_return"] = benchmark_total_return
        run.metrics["excess_total_return"] = float(total_return - benchmark_total_return)


def _average_metric(runs: list[DiscoveryRun], key: str) -> float:
    values = [run.metrics[key] for run in runs if key in run.metrics and np.isfinite(run.metrics[key])]
    return float(np.mean(values)) if values else 0.0


def _summary_from_runs(runs: list[DiscoveryRun], candidates: list[DiscoveryCandidate]) -> dict[str, float]:
    return {
        "run_count": float(len(runs)),
        "candidate_count": float(len(candidates)),
        "average_total_return": _average_metric(runs, "total_return"),
        "average_annualized_return": _average_metric(runs, "annualized_return"),
        "average_max_drawdown": _average_metric(runs, "max_drawdown"),
        "average_sharpe": _average_metric(runs, "sharpe"),
        "average_excess_total_return": _average_metric(runs, "excess_total_return"),
    }


def run_discovery(req: DiscoveryRequest) -> DiscoveryResponse:
    universe, universe_note = _resolve_universe(req)
    base = BaseBacktestParams(initial_cash=req.initial_cash, commission=req.commission)
    warnings: list[str] = []
    runs: list[DiscoveryRun] = []
    skipped_st = 0
    skipped_prefilter: dict[str, int] = {}
    skipped_fundamental: dict[str, int] = {}
    fetch_failed = 0
    valuation_failed = 0
    strategy_failed = 0
    use_fundamentals = _fundamental_filter_enabled(req)
    benchmark_frames: list[pd.DataFrame] = []
    strategy_curves: list[list[dict[str, Any]]] = []

    specs: list[StrategySpec] = []
    params_by_id: dict[str, BaseModel] = {}
    for strategy_id in req.strategies:
        spec = get_strategy(strategy_id)
        if spec is None:
            raise ValueError(f"未知策略: {strategy_id}")
        params = _params_for_strategy(req, spec)
        specs.append(spec)
        params_by_id[spec.id] = params

    min_strategy_bars = max(spec.min_bars(params_by_id[spec.id]) for spec in specs)
    min_bars = max(req.min_bars, min_strategy_bars)

    for item in universe:
        sym = item["symbol"]
        name = item.get("name") or None
        if _is_st_stock(name):
            skipped_st += 1
            continue
        try:
            df = _load_daily(sym, req)
        except (MarketDataError, ValueError):
            fetch_failed += 1
            continue

        req_for_filter = req.model_copy(update={"min_bars": min_bars})
        ok, filters, reason = _prefilter(df, req_for_filter)
        if not ok:
            skipped_prefilter[reason or "基础过滤失败"] = skipped_prefilter.get(reason or "基础过滤失败", 0) + 1
            continue
        if req.include_benchmarks and req.universe == "hs300":
            benchmark_frames.append(df.copy())

        if use_fundamentals:
            try:
                fund = fetch_a_share_valuation_snapshot(sym)
            except MarketDataError:
                valuation_failed += 1
                continue
            ok, fund_filters, reason = _fundamental_filter(fund, req)
            if not ok:
                skipped_fundamental[reason or "估值过滤失败"] = (
                    skipped_fundamental.get(reason or "估值过滤失败", 0) + 1
                )
                continue
            filters.update(fund_filters)

        for spec in specs:
            params = params_by_id[spec.id]
            try:
                result = spec.run(df, base, params)
            except Exception:
                strategy_failed += 1
                continue
            metrics = _clean_metrics(result.metrics)
            if not _passes_metric_filters(metrics, req):
                continue
            strategy_curves.append(result.equity)
            rb = _robustness(df, spec, base, params, req)
            latest_signal, last_trade_date = _latest_signal(result.trades)
            score = _score(metrics, rb)
            runs.append(
                DiscoveryRun(
                    symbol=sym,
                    name=name,
                    strategy_id=spec.id,
                    metrics=metrics,
                    latest_signal=latest_signal,
                    last_trade_date=last_trade_date,
                    filters=filters,
                    robustness=rb,
                    score=score,
                )
            )

    benchmarks: dict[str, Any] = {}
    if req.include_benchmarks:
        strategy_curve = _equal_weight_equity_curve(
            strategy_curves,
            initial_cash=req.initial_cash,
            name="Strategy Equal Weight",
            description="所有通过过滤的策略回测结果按净值等权合成的策略曲线。",
        )
        if strategy_curve is not None:
            benchmarks["strategy_equal_weight"] = strategy_curve
    if req.include_benchmarks and req.universe == "hs300":
        benchmark = _equal_weight_benchmark(benchmark_frames, initial_cash=req.initial_cash)
        if benchmark is not None:
            benchmarks["hs300_equal_weight"] = benchmark
            _attach_benchmark_metrics(runs, benchmark)
        else:
            warnings.append("沪深300等权基准无法生成：可用成分股行情不足")

    runs.sort(key=lambda x: x.score, reverse=True)
    candidates = [
        DiscoveryCandidate.model_validate(run.model_dump())
        for run in runs[: req.top_k]
    ]
    summary = _summary_from_runs(runs, candidates)

    if skipped_st:
        warnings.append(f"ST/退市风险名称跳过: {skipped_st} 只")
    for reason, count in sorted(skipped_prefilter.items()):
        warnings.append(f"{reason}: {count} 只")
    for reason, count in sorted(skipped_fundamental.items()):
        warnings.append(f"{reason}: {count} 只")
    if fetch_failed:
        warnings.append(f"行情拉取失败: {fetch_failed} 只")
    if valuation_failed:
        warnings.append(f"估值拉取失败: {valuation_failed} 只")
    if strategy_failed:
        warnings.append(f"策略执行失败: {strategy_failed} 次")

    return DiscoveryResponse(
        candidates=candidates,
        runs=runs,
        summary=summary,
        benchmarks=benchmarks,
        warnings=warnings,
        universe_note=universe_note,
    )
