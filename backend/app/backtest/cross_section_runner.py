"""横截面策略的数据加载、决策预计算与共享账户执行编排。"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Mapping

# 进度回调：done / total / current（标的或决策日）/ kind
# kind="load"（加载行情，高并发、原地刷新）| "decision"（决策日，逐行永久打印）
# | "symbol"（逐票回测，高并发、原地刷新）
ProgressCallback = Callable[[int, int, str, str], None]

# 落盘回调：接收 {asof: {"source": ..., "members": [...]}} 形式的决策日池字典
PoolSink = Callable[[dict[str, Any]], None]

import numpy as np
import pandas as pd
from pydantic import BaseModel

from app.backtest_aggregate import equal_weight_equity_curve
from app.backtest.benchmarks import attach_hs300_benchmark
from app.backtest.shared_engine import build_close_panel, build_open_panel, run_shared_backtest
from app.backtest_engine import run_from_signals
from app.data_sources.em_fundamentals import load_fundamentals_panel
from app.data_sources.financial_reports import load_financials_panel
from app.fundamental_filter import apply_fundamental_filter
from app.data_sources.market_data import (
    MarketDataError,
    fetch_a_share_daily,
    fetch_a_share_daily_turnover,
    normalize_a_share_symbol,
)
from app.position_management import PositionManagementPolicy
from app.schemas import (
    BacktestRequest,
    BacktestSharedResponse,
    BacktestSymbolRun,
    BacktestUniverseResponse,
    BacktestUniverseSummary,
)
from app.strategies.base import CrossSectionContext, CrossSectionStrategySpec
from app.strategies.cross_section.common import POOL_BY_DATE_CACHE_KEY
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


def execution_clipped_panel(
    clipped: dict[str, pd.DataFrame],
    spec: CrossSectionStrategySpec,
) -> dict[str, pd.DataFrame]:
    """返回供成交/估值使用的价格面板：`close` 列统一为**前复权**口径。

    两条数据分支的 `close` 口径原本不一致：

    - 日线分支（`needs_fundamentals=False`）：来自 `fetch_a_share_daily`，已是前复权；
    - 基本面分支：来自东财估值面板，`close` 是**不复权**真实成交价，而同一行
      `pct_change` 却是复权口径。

    共享账户回测用 `close` 面板给持仓估值、按价成交，不复权口径会在每个除权日凭空记一笔
    跳空亏损（分红没进账户、送转直接当暴跌），而收益口径（`pct_change`）又是复权的——
    同一次回测里入场价与收益来自两种口径。这里统一为前复权，与日线分支一致。

    注意只改这份**副本**：`panel[symbol]["value"]` 仍是原始面板，策略自身的价格区间过滤
    （`min_price`/`max_price`）与股息率（按真实现金分红 / 当时实际股价）继续用不复权价。
    """
    if not spec.needs_fundamentals:
        return clipped
    out: dict[str, pd.DataFrame] = {}
    for symbol, value in clipped.items():
        if "close_qfq" not in value.columns:
            raise MarketDataError(
                f"{spec.id} 的估值面板缺少 close_qfq（前复权收盘价）列，无法按统一口径回测；"
                "请确认面板来自 em_fundamentals（会由 pct_change 还原该列）"
            )
        out[symbol] = value.assign(
            close=pd.to_numeric(value["close_qfq"], errors="coerce")
        )
    return out


def deprecated_decision_interval_warning(request: BacktestRequest) -> str | None:
    """旧请求里带 decision_interval（交易日间隔）时提示改用它已被弃用。

    决策日已改为统一由 decision_frequency + decision_every_n 控制（daily/weekly/
    monthly 自然锚定），decision_interval 不再生效；Pydantic 会静默忽略未知字段，
    这里显式提示避免用户误以为仍在调频。
    """
    raw = request.strategy_params or {}
    if not isinstance(raw, Mapping) or "decision_interval" not in raw:
        return None
    return (
        "strategy_params.decision_interval 已弃用并被忽略；决策日由 decision_frequency "
        "控制（daily=每日、weekly=每周末、monthly=每自然月末），配合 decision_every_n "
        "设置步长（monthly+3=季度）。"
    )


def _resolve_universe(
    request: BacktestRequest,
    spec: CrossSectionStrategySpec,
    *,
    asof: str | None = None,
    asof_filter_config: bool = True,
) -> tuple[list[dict[str, str]], str]:
    requested_universe = request.universe
    if "universe" not in request.model_fields_set:
        requested_universe = None

    # 策略内置默认池：用户未显式提供 symbols 与 universe 时，直接用 default_symbols
    # （如 ETF 轮动策略的固定商品/行业 ETF 池），避免回退到全 A 股票池。
    if (
        spec.default_symbols
        and not request.symbols
        and not requested_universe
    ):
        seen: set[str] = set()
        rows: list[dict[str, str]] = []
        for raw in spec.default_symbols:
            code = normalize_a_share_symbol(raw)
            if code in seen:
                continue
            seen.add(code)
            rows.append({"symbol": code, "name": ""})
        if rows:
            return rows, f"使用策略内置标的池，共 {len(rows)} 只。"

    return resolve_universe_rows(
        symbols=request.symbols,
        universe=requested_universe,
        max_universe=request.max_universe,
        seed=request.seed,
        default_universe=spec.default_universe,
        asof=asof,
        asof_filter_config=asof_filter_config,
    )


def _apply_universe_fundamental_filter(
    universe: list[dict[str, str]],
    panel: dict[str, dict[str, pd.DataFrame]],
    request: BacktestRequest,
    *,
    start: str | None,
) -> tuple[list[dict[str, str]], str]:
    """复用已载入的估值面板对股票池做基本面筛选，返回 (筛选后的 rows, 追加说明)。"""
    if not request.fundamental_filter:
        return universe, ""
    asof = (request.fundamental_asof or start or "").strip()
    if not asof:
        asof = _latest_asof(panel, None)
    kept, filter_warnings = apply_fundamental_filter(
        universe,
        request.fundamental_filter,
        asof=asof,
        panel=panel,
    )
    warnings = list(filter_warnings)
    if kept:
        warnings.append(
            f"基本面筛选（{asof}）后剩 {len(kept)} 只。"
        )
    return kept, "；".join(warnings)


def _load_panel(
    symbols: list[str],
    request: BacktestRequest,
    *,
    spec: CrossSectionStrategySpec,
    progress: ProgressCallback | None = None,
) -> dict[str, dict[str, pd.DataFrame]]:
    if not spec.needs_fundamentals:
        panel: dict[str, dict[str, pd.DataFrame]] = {}

        def load_daily(symbol: str) -> tuple[str, pd.DataFrame]:
            if spec.needs_turnover:
                daily = fetch_a_share_daily_turnover(
                    symbol,
                    limit=5000,
                )
            else:
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
            if spec.needs_turnover and "turnover_rate" in value.columns:
                value["turnover_rate"] = pd.to_numeric(
                    value["turnover_rate"], errors="coerce"
                )
            value["pct_change"] = value["close"].pct_change() * 100.0
            return symbol, value.dropna(subset=["date", "close"])

        total = len(symbols)
        with ThreadPoolExecutor(max_workers=min(request.max_workers, len(symbols))) as pool:
            futures = [pool.submit(load_daily, symbol) for symbol in symbols]
            # 失败/空数据也要计入进度，否则进度条会卡在未完成状态。
            for done, future in enumerate(as_completed(futures), start=1):
                try:
                    symbol, value = future.result()
                except (MarketDataError, ValueError):
                    if progress is not None:
                        progress(done, total, "", "load")
                    continue
                if not value.empty:
                    panel[symbol] = {"value": value}
                if progress is not None:
                    progress(done, total, symbol, "load")
        if not panel:
            raise MarketDataError("未能加载任何日线行情数据")
        return panel

    # 上游回调是逐票单参数；这里用计数器包一层，转成统一的 (done, total, current, kind) 签名。
    def _counting_progress(total: int) -> Callable[[str], None]:
        counter = {"done": 0}

        def _one(current: str) -> None:
            counter["done"] += 1
            progress(counter["done"], total, current, "load")  # type: ignore[misc]

        return _one

    panel = load_fundamentals_panel(
        symbols,
        use_cache=request.use_cache,
        force_refresh=request.force_refresh,
        max_workers=request.max_workers,
        include_dividend=spec.needs_dividend,
        progress=_counting_progress(len(symbols)) if progress is not None else None,
    )
    if not panel:
        raise MarketDataError("未能加载任何基本面数据")
    if spec.needs_financials:
        financials = load_financials_panel(
            list(panel),
            use_cache=request.use_cache,
            force_refresh=request.force_refresh,
            max_workers=min(4, max(1, request.max_workers)),
            progress=_counting_progress(len(panel)) if progress is not None else None,
        )
        for symbol, payload in panel.items():
            payload["financials"] = financials.get(
                symbol,
                pd.DataFrame(columns=["report_date", "notice_date"]),
            )
    return panel


def emit_pool_by_date(
    cache: Mapping[str, Any],
    names: Mapping[str, str],
    pool_sink: PoolSink | None,
) -> None:
    """把策略经 record_decision_pool 登记的决策日池成员交给落盘回调。

    策略侧只存了 symbol（见 common.record_decision_pool），这里用权威的 names 映射补齐
    名称，让落盘内容自带可读性。没有任何登记时静默跳过，不产生空产物。
    """
    if pool_sink is None:
        return
    by_date = cache.get(POOL_BY_DATE_CACHE_KEY)
    if not by_date:
        return
    payload: dict[str, Any] = {}
    for asof, entry in sorted(by_date.items()):
        members = entry.get("members", [])
        payload[asof] = {
            "source": entry.get("source", ""),
            "count": len(members),
            "members": [
                {"symbol": symbol, "name": names.get(symbol, "")} for symbol in members
            ],
        }
    pool_sink(payload)


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


def _nominal_decision_date(requested_month: str, params: BaseModel) -> str:
    """在目标月份尚无行情数据时，给出该月名义决策日的日历日期。

    仅用于报告标注（真实行情日历无法生成该日）。monthly 按决策锚点取该月
    月初（anchor=start）或月末（anchor=end）的日历日，其他频率回退到月初。
    """
    frequency = str(getattr(params, "decision_frequency", "monthly"))
    anchor = str(getattr(params, "decision_anchor", "end") or "end")
    year, month = int(requested_month[:4]), int(requested_month[5:7])
    if frequency == "monthly" and anchor == "end":
        import calendar as _calendar

        last_day = _calendar.monthrange(year, month)[1]
        return f"{requested_month}-{last_day:02d}"
    return f"{requested_month}-01"


def run_cross_section_screen(
    request: BacktestRequest,
    spec: CrossSectionStrategySpec,
    progress: ProgressCallback | None = None,
    pool_sink: PoolSink | None = None,
) -> BacktestSharedResponse:
    params = params_for_cross_section(spec, request)
    if spec.requires_symbols and not request.symbols:
        raise ValueError(f"{spec.id} 策略须通过 symbols 显式提供标的池")
    universe, note = _resolve_universe(
        request,
        spec,
        asof=(request.fundamental_asof or request.start_date or "").strip() or None,
    )
    symbols = [row["symbol"] for row in universe]
    names = {row["symbol"]: row.get("name") or "" for row in universe}
    panel = _load_panel(symbols, request, spec=spec, progress=progress)
    filter_note = ""
    if request.fundamental_filter:
        universe, filter_note = _apply_universe_fundamental_filter(
            universe, panel, request, start=request.start_date
        )
        symbols = [row["symbol"] for row in universe]
        names = {row["symbol"]: row.get("name") or "" for row in universe}
    requested = (request.end_date or "").strip()[:10]
    asof = _latest_asof(panel, requested or None)
    context = CrossSectionContext(panel=panel, names=names)
    _, details = spec.select(asof, context, params)
    emit_pool_by_date(context.cache, names, pool_sink)
    warnings = list(spec.warnings)
    if filter_note:
        note = f"{note}（{filter_note}）"
        warnings.append(filter_note)
    if warn := deprecated_decision_interval_warning(request):
        warnings.append(warn)
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
    progress: ProgressCallback | None = None,
    pool_sink: PoolSink | None = None,
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

    # 多时点回测：config ETF 池用完整池（asof_filter_config=False），不按 start_date
    # 过滤，否则会永久剔除上市晚于 start_date 的细分行业 ETF；其上市后由 select() 在
    # 每个决策日按可用 K 线根数自然纳入（见 universe.py::_maybe_asof_filter_config）。
    universe, note = _resolve_universe(
        request, spec, asof=start or None, asof_filter_config=False
    )
    symbols = [row["symbol"] for row in universe]
    names = {row["symbol"]: row.get("name") or "" for row in universe}
    panel = _load_panel(symbols, request, spec=spec, progress=progress)
    filter_note = ""
    if request.fundamental_filter:
        universe, filter_note = _apply_universe_fundamental_filter(
            universe, panel, request, start=start
        )
        symbols = [row["symbol"] for row in universe]
        names = {row["symbol"]: row.get("name") or "" for row in universe}
    clipped: dict[str, pd.DataFrame] = {}
    for symbol, payload in panel.items():
        if symbol not in set(symbols):
            continue
        value = payload["value"].copy()
        value = value[(value["date"] >= start) & (value["date"] <= end)]
        if len(value) >= 5:
            clipped[symbol] = value
    if len(clipped) < max(top_n, 1):
        raise MarketDataError(
            f"区间内有效股票不足（{len(clipped)} < top_n={top_n}），请扩大股票池或放宽过滤"
        )

    execution_timing = getattr(request, "execution_timing", "next_day_open")
    # 成交/估值统一走前复权口径（详见 execution_clipped_panel）。
    exec_clipped = execution_clipped_panel(clipped, spec)
    close_panel = build_close_panel(exec_clipped)
    if execution_timing == "next_day_open":
        if spec.needs_fundamentals:
            # 东财估值面板只有收盘价，没有开盘价；此前会在 build_open_panel 里抛出
            # 无从下手的「open价面板为空」。这里给出可执行的提示，不做静默降级
            # （降级成收盘价成交等于偷偷改掉成交时点语义）。
            raise ValueError(
                f"{spec.id} 使用东财估值面板（不含开盘价），无法按 execution_timing="
                "next_day_open 成交；请改用 same_day_close 或 next_day_close，"
                "或换用基于 TDX 日线的策略。"
            )
        open_panel = build_open_panel(exec_clipped)
    else:
        open_panel = None
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
    total_dates = len(decision_dates)
    for done, asof in enumerate(decision_dates, start=1):
        targets, details = spec.select(asof, context, params)
        targets_by_date[asof] = targets
        selection_by_date[asof] = details
        if progress is not None:
            picked = ",".join(targets[:5]) or "空仓"
            # 文案在 runner 拼：``通过筛选`` 的只数只有这里有（来自 details），且它比
            # 动态行业池发现出的池小，措辞需与 dump 里的池成员区分开。
            progress(
                done,
                total_dates,
                f"{str(asof)[:10]} 选中 {picked} | 通过筛选 {len(details)} 只",
                "decision",
            )

    # 尾部“当前推荐”决策：end_date 所在月份尚无行情数据时的兜底。
    # 月度决策锚定在每月首个/末个交易日；若 end_date 所在月（如 2026-09）还没有
    # 任何交易数据，真实行情日历无法生成该月决策日，报告会停在上一有数据的月份
    # （如 08-03）。此时追加一个名义决策日（该月锚定交易日），其选股以最新可用
    # 交易日为截面（即“<= 最新数据日”），使报告末尾展示的是当前该持有的标的。
    # 名义日期不在真实行情日历上，run_shared_backtest 会自然跳过其换仓执行，
    # 只影响报告末尾的 asof 与最终持仓展示。
    tail_note: str | None = None
    calendar_days = [str(day)[:10] for day in close_panel.index]
    last_data_month = calendar_days[-1][:7] if calendar_days else ""
    requested_month = (end or "").strip()[:7]
    if requested_month and last_data_month and requested_month > last_data_month:
        nominal = _nominal_decision_date(requested_month, params)
        compute_asof = _latest_asof(panel, None)
        tail_targets, tail_details = spec.select(compute_asof, context, params)
        decision_dates.append(nominal)
        targets_by_date[nominal] = tail_targets
        selection_by_date[nominal] = tail_details
        tail_note = (
            f"end_date {end} 所在月份 {requested_month} 尚无交易数据，报告末尾以最新"
            f"可用交易日 {compute_asof} 计算一次“当前推荐”决策（名义决策日 {nominal}）。"
        )
        # 池登记按真实截面日 compute_asof 写入，但报告/换仓都按名义日 nominal 索引；
        # 补一个别名，否则 dump 的日期与决策日对不上。
        by_date = context.cache.get(POOL_BY_DATE_CACHE_KEY)
        if by_date and compute_asof in by_date:
            by_date.setdefault(nominal, by_date[compute_asof])

    emit_pool_by_date(context.cache, names, pool_sink)

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
        stop_loss_tier_pct=request.stop_loss_tier_pct,
        stop_loss_tier_sell_fraction=request.stop_loss_tier_sell_fraction,
        stop_loss_tier_anchor=request.stop_loss_tier_anchor,
        position_policy=policy,
        rebalance_mode=getattr(request, "rebalance_mode", "full"),
        execution_timing=execution_timing,
        open_panel=open_panel,
    )
    latest = decision_dates[-1]
    warnings = list(spec.warnings)
    if filter_note:
        note = f"{note}（{filter_note}）"
        warnings.append(filter_note)
    if tail_note:
        warnings.append(tail_note)
    if warn := deprecated_decision_interval_warning(request):
        warnings.append(warn)
    out = {
        "strategy_id": spec.id,
        "mode": "backtest",
        "universe_note": note,
        "asof": latest,
        "holdings": selection_by_date.get(latest, []),
        "equity": result.equity,
        "trades": result.trades,
        "rebalances": [
            {
                **rebalance,
                # 候选 selection 以决策日 asof 为键；rebalance.date 是实际成交日，
                # next_day 模式下二者不同，须用决策日关联，否则 selection 全部落空。
                "selection": selection_by_date.get(
                    rebalance.get("decision_date") or rebalance["date"], []
                ),
            }
            for rebalance in result.rebalances
        ],
        "metrics": dict(result.metrics),
        "benchmarks": {},
        "warnings": warnings,
        "disclaimer": "演示用途，历史回测不构成投资建议。",
    }
    return BacktestSharedResponse.model_validate(
        attach_hs300_benchmark(out, initial_cash=request.initial_cash)
    )


def _generate_selection_signal(
    symbol: str,
    stock_dates: list[str],
    decision_dates: list[str],
    targets_by_date: dict[str, set[str]],
) -> np.ndarray:
    """为单只股票生成买卖信号数组：被选入→1，被移出→-1，其他→0。"""
    signal = np.zeros(len(stock_dates), dtype=np.int8)
    was_selected = False
    dd_idx = 0
    n_dd = len(decision_dates)

    for i, date_str in enumerate(stock_dates):
        while dd_idx < n_dd and decision_dates[dd_idx] <= date_str:
            dd_idx += 1

        recent_dd = decision_dates[dd_idx - 1] if dd_idx > 0 else None
        is_selected = (
            recent_dd is not None
            and symbol in targets_by_date.get(recent_dd, set())
        )

        if is_selected and not was_selected:
            signal[i] = 1
        elif not is_selected and was_selected:
            signal[i] = -1

        was_selected = is_selected

    return signal


def run_cross_section_per_stock_backtest(
    request: BacktestRequest,
    spec: CrossSectionStrategySpec,
    progress: ProgressCallback | None = None,
    pool_sink: PoolSink | None = None,
) -> BacktestUniverseResponse:
    """横截面策略的独立资金逐票回测：每只股票用 select 结果作为买卖信号。

    与共享资金模式的关键区别：
    - 每只股票独立 initial_cash，全仓进出
    - top_n 自动放宽至全池，让所有通过筛选的股票产生信号
    - 返回 BacktestUniverseResponse（含 per-stock 排名），而非 BacktestSharedResponse
    """
    start = (request.start_date or "").strip()
    end = (request.end_date or "").strip()
    if not start or not end:
        raise ValueError("per_stock 回测须同时提供 start_date 与 end_date")

    params = params_for_cross_section(spec, request)
    if spec.requires_symbols and not request.symbols:
        raise ValueError(f"{spec.id} 策略须通过 symbols 显式提供标的池")

    universe, note = _resolve_universe(
        request, spec, asof=start or None, asof_filter_config=False
    )
    symbols = [row["symbol"] for row in universe]
    names = {row["symbol"]: row.get("name") or "" for row in universe}
    panel = _load_panel(symbols, request, spec=spec, progress=progress)
    filter_note = ""
    if request.fundamental_filter:
        universe, filter_note = _apply_universe_fundamental_filter(
            universe, panel, request, start=start
        )
        symbols = [row["symbol"] for row in universe]
        names = {row["symbol"]: row.get("name") or "" for row in universe}

    # ---- 裁剪数据区间 ----
    clipped: dict[str, pd.DataFrame] = {}
    for symbol, payload in panel.items():
        if symbol not in set(symbols):
            continue
        value = payload["value"].copy()
        value = value[(value["date"] >= start) & (value["date"] <= end)]
        if len(value) >= 5:
            clipped[symbol] = value

    if not clipped:
        raise MarketDataError("区间内无有效股票数据")

    # ---- 决策日与选股 ----
    close_panel = build_close_panel(execution_clipped_panel(clipped, spec))
    calendar = close_panel.index.tolist()
    context = CrossSectionContext(panel=panel, names=names)
    decision_dates = [
        day
        for day in spec.decision_dates(calendar, context, params)
        if start <= day <= end
    ]
    if not decision_dates:
        raise ValueError("指定区间内无决策日")

    # per_stock 模式：放宽 top_n 至全池，让所有通过筛选的股票都产生信号
    if hasattr(params, "top_n") and "top_n" in type(params).model_fields:
        relaxed_top_n = max(len(symbols), 1)
        params = params.model_copy(update={"top_n": relaxed_top_n})

    targets_by_date: dict[str, set[str]] = {}
    total_dates = len(decision_dates)
    for done, asof in enumerate(decision_dates, start=1):
        targets, details = spec.select(asof, context, params)
        targets_by_date[asof] = set(targets)
        if progress is not None:
            picked = ",".join(targets[:5]) or "空仓"
            progress(
                done,
                total_dates,
                f"{str(asof)[:10]} 选中 {picked} | 通过筛选 {len(details)} 只",
                "decision",
            )

    emit_pool_by_date(context.cache, names, pool_sink)

    decision_dates_sorted = sorted(targets_by_date.keys())

    # ---- 逐票独立回测 ----
    def run_one(symbol: str, name: str | None) -> tuple[BacktestSymbolRun, list[dict[str, Any]]]:
        try:
            df = fetch_a_share_daily(
                symbol,
                start=start,
                end=end,
                data_source=request.data_source,
            )
        except (MarketDataError, ValueError) as exc:
            return (
                BacktestSymbolRun(
                    symbol=symbol, name=name, status="failed", reason=str(exc),
                ),
                [],
            )

        if df.empty or len(df) < 5:
            return (
                BacktestSymbolRun(
                    symbol=symbol, name=name, status="skipped",
                    reason="行情数据为空或不足",
                ),
                [],
            )

        stock_dates = [
            d.isoformat() if hasattr(d, "isoformat") else str(d)
            for d in df.index
        ]
        signal = _generate_selection_signal(
            symbol, stock_dates, decision_dates_sorted, targets_by_date,
        )

        try:
            result = run_from_signals(
                df,
                signal,
                initial_cash=request.initial_cash,
                commission=request.commission,
                stop_loss_pct=request.stop_loss_pct,
            )
        except Exception as exc:
            return (
                BacktestSymbolRun(
                    symbol=symbol, name=name, status="failed",
                    reason=f"策略执行失败: {exc}",
                ),
                [],
            )

        return (
            BacktestSymbolRun(
                symbol=symbol,
                name=name,
                status="ok",
                metrics=result.metrics,
                equity=result.equity if request.include_equity else [],
                trades=result.trades if request.include_trades else [],
                price=result.price if request.include_price else [],
            ),
            result.equity,
        )

    total = len(universe)
    with ThreadPoolExecutor(max_workers=min(request.max_workers, total)) as pool:
        futures = {
            pool.submit(run_one, item["symbol"], item.get("name") or None): item
            for item in universe
        }
        result_by_symbol: dict[str, tuple[BacktestSymbolRun, list[dict[str, Any]]]] = {}
        for done, future in enumerate(as_completed(futures), start=1):
            item = futures[future]
            result_by_symbol[item["symbol"]] = future.result()
            if progress is not None:
                progress(done, total, item["symbol"], "symbol")
    completed = [result_by_symbol[item["symbol"]] for item in universe]

    runs = [item[0] for item in completed]
    curves = [curve for run, curve in completed if run.status == "ok" and curve]
    successful = [run for run in runs if run.status == "ok"]

    if not successful:
        raise ValueError("股票池中没有可用的回测结果")

    # 排名
    ranked = sorted(
        successful,
        key=lambda run: float(run.metrics.get("total_return", float("-inf"))),
        reverse=True,
    )
    for rank, run in enumerate(ranked, start=1):
        run.rank = rank

    # 等权汇总
    aggregate = equal_weight_equity_curve(curves, initial_cash=request.initial_cash)
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
    warnings: list[str] = list(spec.warnings)
    if filter_note:
        note = f"{note}（{filter_note}）"
        warnings.append(filter_note)
    if warn := deprecated_decision_interval_warning(request):
        warnings.append(warn)
    if skipped:
        warnings.append(f"{skipped} 只股票因行情为空或数据不足被跳过。")
    if failed:
        warnings.append(f"{failed} 只股票因数据或策略执行失败。")

    return BacktestUniverseResponse(
        strategy_id=spec.id,
        universe_note=note,
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
