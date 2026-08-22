"""独立资金批量回测的交互报告视图模型。"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from functools import partial
from typing import Any

from app.backtest.shared_report_model import _fifo_round_trips, _load_symbol_bars


_BASE_PRICE_FIELDS = {"date", "datetime", "open", "high", "low", "close", "volume"}
_PRICE_OVERLAY_FIELDS = {
    "fast_ma",
    "slow_ma",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "donchian_high",
    "donchian_low",
    "llt",
}
_FIELD_LABELS = {
    "fast_ma": "快线",
    "slow_ma": "慢线",
    "bb_upper": "布林上轨",
    "bb_middle": "布林中轨",
    "bb_lower": "布林下轨",
    "donchian_high": "唐奇安上轨",
    "donchian_low": "唐奇安下轨",
    "llt": "LLT",
    "llt_dt": "LLT 斜率 D_t",
    "macd": "DIF",
    "macd_signal": "DEA",
    "macd_hist": "能量柱",
    "rsi": "RSI",
    "stoch_k": "%K",
    "stoch_d": "%D",
    "volume_ma": "成交量均线",
    "vol_ratio": "量比",
    "high_th": "放量阈值",
    "low_th": "缩量阈值",
    "higher_moment": "高阶矩",
    "higher_moment_ema": "EMA 高阶矩",
    "ema_alpha": "EMA Alpha",
    "composite_score": "组合得分",
    "composite_position": "组合持仓",
}
_CHART_COLORS = [
    "#55b6ff",
    "#f5bd55",
    "#55d187",
    "#ff7b72",
    "#ce93d8",
    "#4fc3f7",
    "#ffab91",
    "#b39ddb",
]
_INDICATOR_GROUPS = [
    ("移动平均", ("fast_ma", "slow_ma")),
    ("布林带", ("bb_upper", "bb_middle", "bb_lower")),
    ("唐奇安通道", ("donchian_high", "donchian_low")),
    ("LLT 趋势", ("llt",)),
    ("LLT 斜率 D_t", ("llt_dt",)),
    ("MACD 指标", ("macd", "macd_signal", "macd_hist")),
    ("RSI", ("rsi",)),
    ("随机指标", ("stoch_k", "stoch_d")),
    ("量价指标", ("vol_ratio", "high_th", "low_th")),
    ("高阶矩", ("higher_moment", "higher_moment_ema")),
    ("EMA 参数", ("ema_alpha",)),
]

# 需以柱状图渲染的指标字段（如 MACD 能量柱）
_BAR_INDICATOR_FIELDS = frozenset({"macd_hist"})


def _nf(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _date(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _equity_series(rows: list[Any], initial_cash: float) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    peak = 0.0
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        day = _date(raw.get("date"))
        equity = _nf(raw.get("equity"))
        if not day or equity is None:
            continue
        peak = max(peak, equity)
        series.append(
            {
                "date": day,
                "equity": equity,
                "nav": equity / initial_cash if initial_cash > 0 else 0.0,
                "drawdown": equity / peak - 1.0 if peak > 0 else 0.0,
            }
        )
    return series


def _price_rows(rows: list[Any]) -> tuple[list[list[Any]], bool]:
    prices: list[list[Any]] = []
    has_ohlc = False
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        day = _date(raw.get("date") or raw.get("datetime"))
        close = _nf(raw.get("close"))
        if not day or close is None:
            continue
        open_ = _nf(raw.get("open"))
        high = _nf(raw.get("high"))
        low = _nf(raw.get("low"))
        if None not in (open_, high, low):
            prices.append([day, open_, high, low, close])
            has_ohlc = True
        else:
            prices.append([day, close])
    return prices, has_ohlc


def _split_leg_indicator(key: str) -> tuple[str, str, str] | None:
    """解析 leg_序号_策略id__指标名，策略 id 可包含下划线。"""
    if not key.startswith("leg_") or "__" not in key:
        return None
    leg_key, indicator_key = key.split("__", 1)
    parts = leg_key.split("_", 2)
    if len(parts) != 3 or not parts[1].isdigit() or not indicator_key:
        return None
    return parts[1], parts[2], indicator_key


def _is_bar_field(key: str) -> bool:
    """该指标字段是否应以柱状图渲染（如 MACD 能量柱）。"""
    leg = _split_leg_indicator(key)
    indicator_key = leg[2] if leg else key
    return indicator_key in _BAR_INDICATOR_FIELDS


def _field_config(key: str, color_index: int) -> dict[str, str]:
    leg_indicator = _split_leg_indicator(key)
    label = _FIELD_LABELS.get(leg_indicator[2]) if leg_indicator else _FIELD_LABELS.get(key)
    if label is None and key.startswith("leg_") and leg_indicator is None:
        parts = key.split("_", 2)
        label = f"子策略 {parts[1]} · {parts[2]}" if len(parts) == 3 else key
    if label is None and leg_indicator:
        label = leg_indicator[2].replace("_", " ")
    return {
        "key": key,
        "label": label or key.replace("_", " "),
        "color": _CHART_COLORS[color_index % len(_CHART_COLORS)],
    }


def _indicator_data(
    rows: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """提取数值型策略 overlay，并生成价格叠加线与指标分组配置。"""
    keys: list[str] = []
    seen: set[str] = set()
    normalized: list[tuple[str, dict[str, float]]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        day = _date(raw.get("date") or raw.get("datetime"))
        if not day:
            continue
        values: dict[str, float] = {}
        for key, value in raw.items():
            if key in _BASE_PRICE_FIELDS:
                continue
            number = _nf(value)
            if number is None:
                continue
            values[key] = number
            if key not in seen:
                seen.add(key)
                keys.append(key)
        normalized.append((day, values))

    series = [{"date": day, **values} for day, values in normalized]
    field_index = {key: index for index, key in enumerate(keys)}
    price_fields = [
        _field_config(key, field_index[key])
        for key in keys
        if key in _PRICE_OVERLAY_FIELDS
        or (
            (leg_indicator := _split_leg_indicator(key)) is not None
            and leg_indicator[2] in _PRICE_OVERLAY_FIELDS
        )
    ]

    charts: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for title, candidates in _INDICATOR_GROUPS:
        group_keys = [key for key in candidates if key in seen]
        if not group_keys:
            continue
        consumed.update(group_keys)
        chart: dict[str, Any] = {
            "title": title,
            "fields": [_field_config(key, field_index[key]) for key in group_keys],
        }
        bars = [key for key in group_keys if _is_bar_field(key)]
        if bars:
            chart["bars"] = bars
        charts.append(chart)

    leg_fields: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for key in keys:
        leg_indicator = _split_leg_indicator(key)
        if leg_indicator is None:
            continue
        index, strategy_id, indicator_key = leg_indicator
        leg_fields.setdefault((index, strategy_id), []).append((key, indicator_key))

    for (index, strategy_id), fields in leg_fields.items():
        grouped: set[str] = set()
        strategy_title = strategy_id.replace("_", " ")
        for group_title, candidates in _INDICATOR_GROUPS:
            group_keys = [
                key for key, indicator_key in fields if indicator_key in candidates
            ]
            if not group_keys:
                continue
            grouped.update(group_keys)
            consumed.update(group_keys)
            leg_group_chart: dict[str, Any] = {
                "title": f"子策略 {index} · {strategy_title} · {group_title}",
                "fields": [
                    _field_config(key, field_index[key]) for key in group_keys
                ],
            }
            leg_bars = [key for key in group_keys if _is_bar_field(key)]
            if leg_bars:
                leg_group_chart["bars"] = leg_bars
            charts.append(leg_group_chart)
        for key, indicator_key in fields:
            if key in grouped:
                continue
            consumed.add(key)
            leg_fallback_chart: dict[str, Any] = {
                "title": (
                    f"子策略 {index} · {strategy_title} · "
                    f"{_FIELD_LABELS.get(indicator_key, indicator_key.replace('_', ' '))}"
                ),
                "fields": [_field_config(key, field_index[key])],
            }
            if _is_bar_field(key):
                leg_fallback_chart["bars"] = [key]
            charts.append(leg_fallback_chart)

    composite_keys = [
        key
        for key in keys
        if (key.startswith("leg_") and _split_leg_indicator(key) is None)
        or key.startswith("composite_")
    ]
    if composite_keys:
        consumed.update(composite_keys)
        charts.append(
            {
                "title": "组合信号",
                "fields": [_field_config(key, field_index[key]) for key in composite_keys],
            }
        )

    for key in keys:
        if key not in consumed:
            fallback_chart: dict[str, Any] = {
                "title": _FIELD_LABELS.get(key, key.replace("_", " ")),
                "fields": [_field_config(key, field_index[key])],
            }
            if _is_bar_field(key):
                fallback_chart["bars"] = [key]
            charts.append(fallback_chart)
    return series, price_fields, charts


def _apply_ref_lines(
    charts: list[dict[str, Any]], slope_threshold: float | None
) -> list[dict[str, Any]]:
    """给 LLT 斜率图挂上 ±threshold 参考线（死区边界），便于观察过滤区间。"""
    if not slope_threshold or slope_threshold <= 0:
        return charts
    for chart in charts:
        keys = [field.get("key") for field in chart.get("fields") or []]
        if "llt_dt" not in keys:
            continue
        chart["refs"] = [
            {"value": slope_threshold, "label": f"看多阈值 +{slope_threshold}"},
            {"value": -slope_threshold, "label": f"看空阈值 -{slope_threshold}"},
        ]
    return charts


def _trade_rows(run: dict[str, Any]) -> list[dict[str, Any]]:
    symbol = str(run.get("symbol") or "")
    name = str(run.get("name") or "")
    rows: list[dict[str, Any]] = []
    for raw in run.get("trades") or []:
        if not isinstance(raw, dict):
            continue
        price = _nf(raw.get("price")) or 0.0
        shares = _nf(raw.get("shares")) or 0.0
        row = dict(raw)
        row.update(
            {
                "date": _date(raw.get("date")),
                "symbol": symbol,
                "name": name,
                "side": str(raw.get("side") or "").lower(),
                "price": price,
                "shares": shares,
                "notional": abs(price * shares),
            }
        )
        rows.append(row)
    return rows


def _build_one_detail(
    run: dict[str, Any],
    default_initial_cash: float,
    *,
    slope_threshold: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """构建单只股票的明细视图；模块级函数以便进程池 worker 可序列化调用。"""
    symbol = str(run.get("symbol") or "")
    metrics = dict(run.get("metrics") or {})
    run_initial = _nf(metrics.get("initial_cash")) or default_initial_cash
    trades = _trade_rows(run)
    raw_prices = list(run.get("price") or [])
    prices, has_ohlc = _price_rows(raw_prices)
    indicator_series, price_overlay_fields, indicator_charts = _indicator_data(raw_prices)
    indicator_charts = _apply_ref_lines(indicator_charts, slope_threshold)
    return (
        symbol,
        {
            "symbol": symbol,
            "name": str(run.get("name") or ""),
            "rank": run.get("rank"),
            "metrics": metrics,
            "equity": _equity_series(list(run.get("equity") or []), run_initial),
            "trades": trades,
            "round_trips": _fifo_round_trips(
                trades,
                names={symbol: str(run.get("name") or "")},
            ),
            "prices": prices,
            "has_ohlc": has_ohlc,
            "indicator_series": indicator_series,
            "price_overlay_fields": price_overlay_fields,
            "indicator_charts": indicator_charts,
        },
    )


def _build_all_details(
    runs: list[dict[str, Any]],
    default_initial_cash: float,
    *,
    slope_threshold: float | None = None,
) -> dict[str, dict[str, Any]]:
    """并行构建全部逐票明细；进程池不可用时回退到单进程。

    ``QUANTDANCE_REPORT_PARALLEL`` 置为 ``0`` 可强制关闭并行（便于排查/测试）。
    """
    details: dict[str, dict[str, Any]] = {}
    if not runs:
        return details
    if len(runs) == 1:
        symbol, detail = _build_one_detail(
            runs[0],
            default_initial_cash,
            slope_threshold=slope_threshold,
        )
        details[symbol] = detail
        return details

    parallel = os.environ.get("QUANTDANCE_REPORT_PARALLEL", "1").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    try:
        if parallel:
            workers = max(1, min((os.cpu_count() or 2) - 1, len(runs)))
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for symbol, detail in pool.map(
                    partial(
                        _build_one_detail,
                        default_initial_cash=default_initial_cash,
                        slope_threshold=slope_threshold,
                    ),
                    runs,
                ):
                    details[symbol] = detail
            return details
    except Exception:
        # 进程池不可用（冻结环境、序列化失败等）时回退到单进程
        details = {}
    for run in runs:
        symbol, detail = _build_one_detail(
            run,
            default_initial_cash,
            slope_threshold=slope_threshold,
        )
        details[symbol] = detail
    return details


def _rank_row(run: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(run.get("metrics") or {})
    return {
        "symbol": str(run.get("symbol") or ""),
        "name": str(run.get("name") or ""),
        "status": str(run.get("status") or ""),
        "rank": run.get("rank"),
        "reason": run.get("reason"),
        "metrics": metrics,
        "total_return": _nf(metrics.get("total_return")),
        "annualized_return": _nf(metrics.get("annualized_return")),
        "max_drawdown": _nf(metrics.get("max_drawdown")),
        "sharpe": _nf(metrics.get("sharpe")),
        "return_drawdown_ratio": _nf(metrics.get("return_drawdown_ratio")),
        "num_trades": _nf(metrics.get("num_trades")),
        "win_rate": _nf(metrics.get("win_rate")),
        "profit_factor": _nf(metrics.get("profit_factor")),
    }


def build_backtest_universe_report_model(
    out: dict[str, Any],
    *,
    top_k: int | None = None,
    price_top_k: int | None = None,
    load_prices: bool = True,
) -> dict[str, Any]:
    """派生详细报告；默认保留全部明细，默认给全部标的补载行情。"""
    if str(out.get("mode") or "") != "universe":
        raise ValueError("批量回测报告仅支持 mode='universe'")

    strategy_params = dict(out.get("strategy_params") or {})
    slope_threshold = _nf(strategy_params.get("slope_threshold"))
    aggregate = dict(out.get("aggregate") or {})
    aggregate_metrics = dict(aggregate.get("metrics") or {})
    initial_cash = _nf(aggregate_metrics.get("initial_cash")) or 1.0
    aggregate_equity = _equity_series(list(aggregate.get("equity") or []), initial_cash)

    runs = [run for run in (out.get("runs") or []) if isinstance(run, dict)]
    ranked_index = [_rank_row(run) for run in runs]
    ranked_index.sort(
        key=lambda row: (
            row["status"] != "ok",
            int(row["rank"]) if row["rank"] is not None else 10**9,
            row["symbol"],
        )
    )
    successful = [run for run in runs if run.get("status") == "ok"]
    successful.sort(key=lambda run: int(run.get("rank") or 10**9))
    detail_runs = successful if top_k is None else successful[: max(0, top_k)]

    details = _build_all_details(detail_runs, initial_cash, slope_threshold=slope_threshold)

    # price_top_k：None 或 0=全部；正数=仅前 N 名补拉行情
    price_targets = (
        detail_runs if price_top_k is None or price_top_k <= 0 else detail_runs[: price_top_k]
    )
    price_symbols = [
        str(run.get("symbol") or "") for run in price_targets if str(run.get("symbol") or "")
    ]
    if load_prices and price_symbols:
        def load_one(symbol: str) -> tuple[str, list[list[Any]], bool]:
            detail = details[symbol]
            if detail["prices"] and detail["has_ohlc"]:
                return symbol, detail["prices"], True
            equity = detail["equity"]
            start = equity[0]["date"] if equity else ""
            end = equity[-1]["date"] if equity else ""
            trade_dates = [trade["date"] for trade in detail["trades"] if trade["date"]]
            fetched, has_ohlc = _load_symbol_bars(
                symbol,
                start,
                end,
                fetch_missing=True,
                cover_dates=trade_dates,
            )
            if has_ohlc:
                return symbol, fetched, True
            # 补拉失败时回退到已有序列（可能是回测自带的收盘价）
            if detail["prices"]:
                return symbol, detail["prices"], False
            return symbol, fetched, False

        with ThreadPoolExecutor(max_workers=min(8, len(price_symbols))) as pool:
            futures = [pool.submit(load_one, symbol) for symbol in price_symbols]
            for future in as_completed(futures):
                symbol, prices, has_ohlc = future.result()
                details[symbol]["prices"] = prices
                details[symbol]["has_ohlc"] = has_ohlc

    returns = [
        row["total_return"]
        for row in ranked_index
        if row["status"] == "ok" and row["total_return"] is not None
    ]
    return {
        "mode": "universe",
        "strategy_id": str(out.get("strategy_id") or ""),
        "universe_note": str(out.get("universe_note") or ""),
        "summary": dict(out.get("summary") or {}),
        "aggregate": {
            "name": aggregate.get("name") or "独立回测等权汇总",
            "description": aggregate.get("description") or "",
            "metrics": aggregate_metrics,
            "equity": aggregate_equity,
        },
        "ranked_index": ranked_index,
        "details": details,
        "detail_limit": len(details),
        "price_detail_limit": (
            len(details) if price_top_k is None or price_top_k <= 0 else min(len(details), price_top_k)
        ),
        "distribution": {
            "returns": returns,
            "positive": sum(value > 0 for value in returns),
            "negative": sum(value < 0 for value in returns),
            "flat": sum(value == 0 for value in returns),
        },
        "warnings": list(out.get("warnings") or []),
        "disclaimer": str(out.get("disclaimer") or ""),
    }
