"""纯脚本入口：在 backend 目录执行 ``python -m app.script --help``。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", message=r"pkg_resources is deprecated as an API.*")
warnings.simplefilter("ignore", ResourceWarning)

from app.backtest_runner import run_backtest_request
from app.data_sources.tencent_finance_sdk import TencentFinanceSDK
from app.data_sources.market_data import MarketDataError
from app.ic_analysis import FACTOR_REGISTRY, compute_factor_ic, factor_source
from app.schemas import (
    BacktestRequest,
    ScreenRequest,
)
from app.stock_screening import run_screen, screen_presets_catalog
from app.strategies.registry import ALL_STRATEGIES
from app.universe import resolve_universe_rows


def _load_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _open_file_with_default_app(path: Path) -> None:
    resolved = path.expanduser().resolve()
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(resolved))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(resolved)])
        else:
            subprocess.Popen(["xdg-open", str(resolved)])
    except OSError as exc:
        print(f"图表已保存，但自动打开失败: {exc}")


def _print_backtest_progress(done: int, total: int, current: str = "") -> None:
    """把逐票/逐决策日进度写到 stderr，回车刷新、不污染 stdout 结果。"""
    pct = (done / total * 100.0) if total else 100.0
    sys.stderr.write(f"\r回测进度: {done}/{total} ({pct:5.1f}%) {current:<12}")
    if done >= total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def _write_json(payload: dict[str, Any], *, output: Path | None, as_json: bool) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if as_json:
        print(text)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(f"结果已写入: {output.resolve()}")


def _fmt_float(value: Any, digits: int = 2) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{v:.{digits}f}"


def _fmt_pct(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{v * 100:.2f}%"


def _trade_side_text(value: Any) -> str:
    side = str(value or "").lower()
    if side == "buy":
        return "买入"
    if side == "sell":
        return "卖出"
    return str(value or "-")


def _print_trade_table(trades: list[dict[str, Any]]) -> None:
    print("买卖节点:")
    if not trades:
        print("  无")
        return

    headers = ("序号", "日期", "方向", "价格", "股数", "交易后现金")
    rows = [
        (
            str(i),
            str(trade.get("date", ""))[:10] or "-",
            _trade_side_text(trade.get("side")),
            _fmt_float(trade.get("price")),
            _fmt_float(trade.get("shares"), 0),
            _fmt_float(trade.get("cash_after")),
        )
        for i, trade in enumerate(trades, start=1)
    ]
    widths = [
        max(len(str(row[idx])) for row in (headers, *rows))
        for idx in range(len(headers))
    ]

    def render(row: tuple[str, ...]) -> str:
        return " | ".join(str(cell).ljust(widths[idx]) for idx, cell in enumerate(row))

    print("  " + render(headers))
    print("  " + "-+-".join("-" * width for width in widths))
    for row in rows:
        print("  " + render(row))


def _write_backtest_summary(
    out: dict[str, Any],
    body: BacktestRequest,
    *,
    output: Path | None,
) -> None:
    metrics = out.get("metrics", {})
    equity = out.get("equity", [])
    trades = out.get("trades", [])
    price = out.get("price", [])

    start = str(equity[0].get("date", ""))[:10] if equity else "-"
    end = str(equity[-1].get("date", ""))[:10] if equity else "-"
    symbol = body.symbol or "(未填写代码)"

    print(f"回测完成: {body.strategy_id} | {symbol} | {body.data_source}")
    print(f"区间: {start} ~ {end} | K线: {len(price)} | 交易: {int(float(metrics.get('num_trades', len(trades))))}")
    print(
        "资金: "
        f"初始 {_fmt_float(metrics.get('initial_cash'))} -> "
        f"最终 {_fmt_float(metrics.get('final_equity'))}"
    )
    print(
        "指标: "
        f"总收益 {_fmt_pct(metrics.get('total_return'))} | "
        f"最大回撤 {_fmt_pct(metrics.get('max_drawdown'))} | "
        f"Sharpe {_fmt_float(metrics.get('sharpe'))}"
    )
    _print_trade_table(trades)
    if output is not None:
        print(f"完整结果: {output.resolve()}")
    else:
        print("完整结果: 使用 --json 打印，或 --output FILE.json 保存")


def _write_backtest_universe_summary(
    out: dict[str, Any],
    body: BacktestRequest,
    *,
    output: Path | None,
) -> None:
    summary = out.get("summary", {})
    aggregate = out.get("aggregate", {})
    metrics = aggregate.get("metrics", {})
    print(
        f"批量回测完成: {body.strategy_id} | {body.universe} | "
        f"成功 {summary.get('succeeded', 0)}/{summary.get('requested', 0)}"
    )
    if out.get("universe_note"):
        print(out["universe_note"])
    print(
        "等权汇总: "
        f"总收益 {_fmt_pct(metrics.get('total_return'))} | "
        f"最大回撤 {_fmt_pct(metrics.get('max_drawdown'))} | "
        f"Sharpe {_fmt_float(metrics.get('sharpe'))}"
    )
    print(
        "单票平均: "
        f"收益 {_fmt_pct(summary.get('average_total_return'))} | "
        f"回撤 {_fmt_pct(summary.get('average_max_drawdown'))} | "
        f"Sharpe {_fmt_float(summary.get('average_sharpe'))}"
    )
    ranked = sorted(
        (run for run in out.get("runs", []) if run.get("status") == "ok"),
        key=lambda run: int(run.get("rank") or 10**9),
    )
    if ranked:
        print("收益排行榜:")
        for run in ranked[:10]:
            run_metrics = run.get("metrics", {})
            print(
                f"  {run.get('rank')}. {run.get('symbol')} {run.get('name') or ''} | "
                f"收益 {_fmt_pct(run_metrics.get('total_return'))} | "
                f"回撤 {_fmt_pct(run_metrics.get('max_drawdown'))} | "
                f"Sharpe {_fmt_float(run_metrics.get('sharpe'))}"
            )
    if out.get("warnings"):
        print("提示: " + "；".join(out["warnings"]))
    if output is not None:
        print(f"完整结果: {output.resolve()}")
    else:
        print("完整结果: 使用 --json 打印，或 --output FILE.json 保存")


def _path_from_output_option(value: Any, key: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"output_options.{key} 需为非空字符串路径")
    return Path(value)


def _normalize_report_path(path: Path | None) -> Path | None:
    """交互报告默认保存为 HTML。"""
    if path is None:
        return None
    if path.suffix:
        return path
    return path.with_suffix(".html")


def _load_output_options(args: argparse.Namespace) -> dict[str, Any]:
    if args.request is None:
        return {}
    req_data = _load_json_file(args.request)
    if not isinstance(req_data, dict):
        raise ValueError("--request 需为 JSON 对象")
    raw_options = req_data.get("output_options", {})
    if raw_options is None:
        raw_options = {}
    if not isinstance(raw_options, dict):
        raise ValueError("output_options 需为 JSON 对象")
    return raw_options


def _resolve_backtest_output_options(
    args: argparse.Namespace,
) -> tuple[Path | None, bool, Path | None, Path | None, int | None]:
    output_options = _load_output_options(args)

    json_option = output_options.get("json", False)
    if not isinstance(json_option, bool):
        raise ValueError("output_options.json 需为布尔值")

    output = args.output or _path_from_output_option(output_options.get("output"), "output")
    as_json = args.json or json_option
    report = _normalize_report_path(
        getattr(args, "report", None)
        or _path_from_output_option(output_options.get("report"), "report")
    )
    report_top_k = getattr(args, "report_top_k", None)
    if report_top_k is None:
        raw = output_options.get("report_top_k")
        if raw is not None:
            report_top_k = int(raw) if str(raw).strip() else None
    report_price_top_k = getattr(args, "report_price_top_k", None)
    if report_price_top_k is None:
        raw = output_options.get("report_price_top_k")
        if raw is not None:
            report_price_top_k = int(raw) if str(raw).strip() else None
    return output, as_json, report, report_top_k, report_price_top_k


def _resolve_json_output_options(args: argparse.Namespace) -> tuple[Path | None, bool]:
    output_options = _load_output_options(args)

    json_option = output_options.get("json", False)
    if not isinstance(json_option, bool):
        raise ValueError("output_options.json 需为布尔值")

    output = args.output or _path_from_output_option(output_options.get("output"), "output")
    as_json = args.json or json_option
    return output, as_json


def _resolve_backtest_body(args: argparse.Namespace) -> BacktestRequest:
    base: dict[str, Any] = {}
    if args.request is not None:
        req_data = _load_json_file(args.request)
        if not isinstance(req_data, dict):
            raise ValueError("--request 需为 JSON 对象")
        base.update(req_data)

    cli_map: dict[str, Any] = {}
    strategy_params: dict[str, Any] = dict(base.get("strategy_params") or {})
    if getattr(args, "mode", None) is not None:
        cli_map["mode"] = args.mode
    if args.strategy_id is not None:
        cli_map["strategy_id"] = args.strategy_id
    if args.data_source is not None:
        cli_map["data_source"] = args.data_source
    if args.bars is not None:
        cli_map["bars"] = args.bars
    if args.initial_cash is not None:
        cli_map["initial_cash"] = args.initial_cash
    if args.commission is not None:
        cli_map["commission"] = args.commission
    if getattr(args, "min_commission", None) is not None:
        cli_map["min_commission"] = args.min_commission
    if getattr(args, "slippage", None) is not None:
        cli_map["slippage"] = args.slippage
    if getattr(args, "lot_size", None) is not None:
        cli_map["lot_size"] = args.lot_size
    if args.symbol is not None:
        cli_map["symbol"] = args.symbol
    if getattr(args, "universe", None) is not None:
        cli_map["universe"] = args.universe
        if getattr(args, "mode", None) is None:
            cli_map["mode"] = "universe"
    if getattr(args, "symbols", None):
        cli_map["symbols"] = args.symbols
        if getattr(args, "mode", None) is None:
            cli_map["mode"] = "universe"
    if getattr(args, "max_universe", None) is not None:
        cli_map["max_universe"] = args.max_universe
    if getattr(args, "seed", None) is not None:
        cli_map["seed"] = args.seed
    if getattr(args, "max_workers", None) is not None:
        cli_map["max_workers"] = args.max_workers
    if getattr(args, "force_refresh", False):
        cli_map["force_refresh"] = True
    if getattr(args, "no_cache", False):
        cli_map["use_cache"] = False
    if getattr(args, "include_equity", None) is not None:
        cli_map["include_equity"] = args.include_equity
    if getattr(args, "include_trades", None) is not None:
        cli_map["include_trades"] = args.include_trades
    if getattr(args, "include_price", None) is not None:
        cli_map["include_price"] = args.include_price
    if args.start_date is not None:
        cli_map["start_date"] = args.start_date
    if args.end_date is not None:
        cli_map["end_date"] = args.end_date
    if args.fast_period is not None:
        strategy_params["fast_period"] = args.fast_period
    if args.slow_period is not None:
        strategy_params["slow_period"] = args.slow_period
    if args.signal_period is not None:
        strategy_params["signal_period"] = args.signal_period
    if args.volume_ma_period is not None:
        strategy_params["volume_ma_period"] = args.volume_ma_period
    if args.volume_metric is not None:
        strategy_params["volume_metric"] = args.volume_metric
    if args.threshold_mode is not None:
        strategy_params["threshold_mode"] = args.threshold_mode
    if args.high_ratio is not None:
        strategy_params["high_ratio"] = args.high_ratio
    if args.low_ratio is not None:
        strategy_params["low_ratio"] = args.low_ratio
    if args.high_percentile is not None:
        strategy_params["high_percentile"] = args.high_percentile
    if args.low_percentile is not None:
        strategy_params["low_percentile"] = args.low_percentile
    if args.percentile_lookback is not None:
        strategy_params["percentile_lookback"] = args.percentile_lookback
    if args.price_ma_period is not None:
        strategy_params["price_ma_period"] = args.price_ma_period
    if args.trend_ma_period is not None:
        strategy_params["trend_ma_period"] = args.trend_ma_period
    if args.breakout_period is not None:
        strategy_params["breakout_period"] = args.breakout_period
    if args.require_price_above_ma:
        strategy_params["require_price_above_ma"] = True
    if args.require_trend_up:
        strategy_params["require_trend_up"] = True
    if args.require_breakout:
        strategy_params["require_breakout"] = True
    if args.entry_confirm_bars is not None:
        strategy_params["entry_confirm_bars"] = args.entry_confirm_bars
    if args.stop_loss_pct is not None:
        cli_map["stop_loss_pct"] = args.stop_loss_pct
    if getattr(args, "take_profit_arm_pct", None) is not None:
        cli_map["take_profit_arm_pct"] = args.take_profit_arm_pct
    if getattr(args, "take_profit_exit_pct", None) is not None:
        cli_map["take_profit_exit_pct"] = args.take_profit_exit_pct
    if args.take_profit_pct is not None:
        strategy_params["take_profit_pct"] = args.take_profit_pct
    for key in (
        "top_n",
        "min_price",
        "max_price",
        "min_dividend_yield",
        "min_peg",
        "max_peg",
        "limit_pct_threshold",
    ):
        value = getattr(args, key, None)
        if value is not None:
            strategy_params[key] = value
    if getattr(args, "no_dividend_filter", False):
        strategy_params["require_dividend"] = False
    if getattr(args, "no_peg_filter", False):
        strategy_params["require_peg"] = False
    if strategy_params:
        cli_map["strategy_params"] = strategy_params
    base.update(cli_map)
    return BacktestRequest.model_validate(base)


def _resolve_screen_body(args: argparse.Namespace) -> ScreenRequest:
    base: dict[str, Any] = ScreenRequest().model_dump()
    if args.request is not None:
        req_data = _load_json_file(args.request)
        if not isinstance(req_data, dict):
            raise ValueError("--request 需为 JSON 对象")
        base.update(req_data)

    cli_map: dict[str, Any] = {}
    if args.preset is not None:
        cli_map["preset"] = args.preset
    if args.top_k is not None:
        cli_map["top_k"] = args.top_k
    if args.max_universe is not None:
        cli_map["max_universe"] = args.max_universe
    if args.start_date is not None:
        cli_map["start_date"] = args.start_date
    if args.end_date is not None:
        cli_map["end_date"] = args.end_date
    if args.bars is not None:
        cli_map["bars"] = args.bars
    if args.random_seed:
        cli_map["seed"] = None
    elif args.seed is not None:
        cli_map["seed"] = args.seed
    base.update(cli_map)
    return ScreenRequest.model_validate(base)


def _cmd_backtest(args: argparse.Namespace) -> int:
    if args.list_strategies:
        for sid in sorted(ALL_STRATEGIES):
            spec = ALL_STRATEGIES[sid]
            print(f"{spec.id}\t{spec.name}\t{spec.description}")
        return 0
    output, as_json, report, report_top_k, report_price_top_k = _resolve_backtest_output_options(args)
    body = _resolve_backtest_body(args)
    shared = body.strategy_id in {
        sid for sid, spec in ALL_STRATEGIES.items() if hasattr(spec, "select")
    }
    if not shared and report is not None and body.mode in {"universe", "single"} and not body.include_price:
        # 逐票指标随 price overlay 返回；报告需要这些序列来绘制指标图。
        body = body.model_copy(update={"include_price": True})
    out = run_backtest_request(body, progress=_print_backtest_progress)
    _write_json(out, output=output, as_json=as_json)
    if report is not None:
        from app.cli_backtest_report import render_backtest_html

        # report_top_k：0 或 None=全部；正数=仅前 N 名保留逐票明细图
        top_k = None if report_top_k is None or report_top_k <= 0 else report_top_k
        # report_price_top_k：0 或 None=全部；正数=仅前 N 名补拉 K 线
        price_top_k = (
            None if report_price_top_k is None or report_price_top_k <= 0 else report_price_top_k
        )
        if not shared and body.mode == "single":
            # 单票结果不含 symbol，注入以便报告标题与明细展示。
            out = {**out, "symbol": body.symbol}
        report_path = render_backtest_html(out, report, top_k=top_k, price_top_k=price_top_k)
        print(
            f"交互报告已保存: {report_path}"
            + (f"（明细图 top-{top_k}）" if top_k else "（含全部明细图）")
            + ("" if price_top_k else "（全部补拉 K 线）")
        )
        _open_file_with_default_app(report_path)
    if not as_json:
        if shared and body.mode != "per_stock":
            metrics = out.get("metrics") or {}
            print(
                f"共享资金回测完成: {body.strategy_id} | mode={body.mode} | "
                f"收益 {_fmt_pct(metrics.get('total_return'))} | "
                f"持仓 {len(out.get('holdings') or [])}"
            )
        elif body.mode in {"universe", "per_stock"}:
            _write_backtest_universe_summary(out, body, output=output)
        else:
            _write_backtest_summary(out, body, output=output)
    return 0


def _cmd_screen(args: argparse.Namespace) -> int:
    if args.list_presets:
        cat = screen_presets_catalog()
        for pr in cat.get("presets", []):
            pid = pr.get("id", "")
            name = pr.get("name", "")
            print(f"{pid}\t{name}")
        return 0
    body = _resolve_screen_body(args)
    out = run_screen(body).model_dump(mode="json")
    _write_json(out, output=args.output, as_json=args.json)
    if not args.json and args.output is None:
        print("执行成功（使用 --json 查看完整结果）")
    return 0


def _cmd_stock_search(args: argparse.Namespace) -> int:
    sdk = TencentFinanceSDK()
    items = sdk.search_stocks(args.keyword, limit=args.limit)
    out = {"items": items, "source": "tencent_smartbox"}
    _write_json(out, output=args.output, as_json=args.json)
    if not args.json and args.output is None:
        for item in items:
            print(f'{item["symbol"]}\t{item["code"]}\t{item["name"]}')
    return 0


def _cmd_chip_dist(args: argparse.Namespace) -> int:
    """筹码分布子命令：拉取含换手率日线，计算筹码成本分布。"""
    from app.data_sources.market_data import (
        fetch_a_share_daily_turnover,
        normalize_a_share_symbol,
    )
    from app.factors.chip_distribution import chip_cost_distribution

    symbol = normalize_a_share_symbol(args.symbol)
    try:
        df = fetch_a_share_daily_turnover(symbol, limit=args.days)
    except MarketDataError as e:
        print(f"数据获取失败: {e}", file=sys.stderr)
        return 3

    name: str | None = None
    try:
        # 用 A 股股票池查名称（mootdx 仅含 A 股，避免腾讯搜索把 000001 匹配成上证指数）。
        from app.data_sources.a_stock_data import AStockDataSDK

        for item in AStockDataSDK().get_universe():
            if item["symbol"] == symbol:
                name = item["name"]
                break
    except Exception:
        pass

    metrics = chip_cost_distribution(
        df,
        bins=args.bins,
        include_distribution=args.detailed,
    )

    out: dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "profit_ratio": metrics["profit_ratio"],
        "average_cost": metrics["average_cost"],
        "peak_price": metrics["peak_price"],
        "interval_90_low": metrics["interval_90_low"],
        "interval_90_high": metrics["interval_90_high"],
        "concentration_90": metrics["concentration_90"],
        "interval_70_low": metrics["interval_70_low"],
        "interval_70_high": metrics["interval_70_high"],
        "concentration_70": metrics["concentration_70"],
        "current_price": float(df.iloc[-1]["close"]),
        "days_used": int(len(df)),
        "disclaimer": "演示用途，筹码分布分析不构成投资建议。",
    }
    if args.detailed and "price_grid" in metrics:
        out["price_grid"] = metrics["price_grid"]
        out["distribution"] = metrics["distribution"]
    if "warning" in metrics:
        out["warning"] = metrics["warning"]

    _write_json(out, output=args.output, as_json=args.json)

    if not args.json and args.output is None:
        print(f"筹码分布分析: {symbol}" + (f" {name}" if name else ""))
        print(f"  数据区间: {str(df.index[0])[:10]} ~ {str(df.index[-1])[:10]}  ({len(df)} 条)")
        print(f"  最新收盘价: {_fmt_float(out['current_price'])}")
        print(f"  获利盘比例: {_fmt_pct(out['profit_ratio'])}")
        print(f"  平均成本:   {_fmt_float(out['average_cost'])}")
        print(f"  筹码峰值:   {_fmt_float(out['peak_price'])}")
        print(
            f"  90% 成本区间: "
            f"[{_fmt_float(out['interval_90_low'])}, {_fmt_float(out['interval_90_high'])}]"
        )
        print(f"  90% 集中度:   {_fmt_float(out['concentration_90'], 4)}")
        print(
            f"  70% 成本区间: "
            f"[{_fmt_float(out['interval_70_low'])}, {_fmt_float(out['interval_70_high'])}]"
        )
        print(f"  70% 集中度:   {_fmt_float(out['concentration_70'], 4)}")
        if out.get("warning"):
            print(f"  提示: {out['warning']}")
    return 0






def _resolve_factor_ic_body(args: argparse.Namespace) -> dict[str, Any]:
    """把 --request JSON 与 CLI 参数合并为 factor-ic 参数（CLI 显式值优先）。

    支持在请求 JSON 里放 ``symbols``/``universe``/``max_universe``/``seed`` 及全部
    分析参数（start_date/end_date/limit/horizons/min_cs/factors/max_workers），
    与 backtest 的 ``--request`` 用法对齐。
    """
    base: dict[str, Any] = {}
    if args.request is not None:
        req_data = _load_json_file(args.request)
        if not isinstance(req_data, dict):
            raise ValueError("--request 需为 JSON 对象")
        base.update(req_data)

    cli_map: dict[str, Any] = {}
    for key in (
        "symbols",
        "universe",
        "max_universe",
        "seed",
        "start_date",
        "end_date",
        "limit",
        "horizons",
        "min_cs",
        "factors",
        "max_workers",
    ):
        value = getattr(args, key, None)
        if value is not None:
            cli_map[key] = value
    base.update(cli_map)

    # 解析默认值
    body = {
        "symbols": base.get("symbols"),
        "universe": base.get("universe"),
        "max_universe": int(base.get("max_universe", 300)),
        "seed": base.get("seed"),
        "start_date": base.get("start_date"),
        "end_date": base.get("end_date"),
        "limit": int(base.get("limit", 1200)),
        "horizons": [int(h) for h in (base.get("horizons") or [5, 10, 20])],
        "min_cs": int(base.get("min_cs", 10)),
        "factors": base.get("factors"),
        "max_workers": int(base.get("max_workers", 8)),
    }
    if body["horizons"] and all(h >= 1 for h in body["horizons"]):
        pass
    else:
        raise ValueError("horizons 须为不小于 1 的整数列表")
    return body


def _resolve_factor_ic_output(
    args: argparse.Namespace,
) -> tuple[Path | None, bool, Path | None]:
    """factor-ic 输出：--json/--output/--report 优先，否则取请求 JSON 的 output_options。"""
    if args.request is None:
        return args.output, args.json, _normalize_report_path(getattr(args, "report", None))
    output_options = _load_output_options(args)
    json_option = output_options.get("json", False)
    if not isinstance(json_option, bool):
        raise ValueError("output_options.json 需为布尔值")
    output = args.output or _path_from_output_option(output_options.get("output"), "output")
    as_json = args.json or json_option
    report = _normalize_report_path(
        getattr(args, "report", None)
        or _path_from_output_option(output_options.get("report"), "report")
    )
    return output, as_json, report


def _cmd_factor_ic(args: argparse.Namespace) -> int:
    """因子 IC 分析：横截面评价因子对未来收益的秩相关预测力。"""
    if args.list_factors:
        src_label = {"screening": "选股技术", "momentum": "动量/趋势", "strategy": "策略信号"}
        print("可选因子（来源\t名称）:")
        for name in sorted(FACTOR_REGISTRY):
            print(f"  {src_label.get(factor_source(name), factor_source(name))}\t{name}")
        return 0

    body = _resolve_factor_ic_body(args)
    output, as_json, report = _resolve_factor_ic_output(args)
    if report is None:
        # 默认产出交互 HTML 报告；可用 --report 或请求 JSON 的 output_options.report 覆盖
        report = Path("out/ic_report.html")

    start = (body["start_date"] or "").strip() or None
    end = (body["end_date"] or "").strip() or None
    if (start is None) != (end is None):
        raise ValueError("start_date 与 end_date 须同时提供，或都省略（用尾部窗口）")

    rows, note = resolve_universe_rows(
        symbols=body["symbols"],
        universe=body["universe"],
        max_universe=body["max_universe"],
        seed=body["seed"],
        default_universe="all_a",
        asof=start,
    )
    if not rows:
        raise ValueError("股票池为空，无法进行 IC 分析")
    symbols = [row["symbol"] for row in rows]

    limit = None
    if start is None:
        limit = body["limit"]

    out = compute_factor_ic(
        symbols,
        start=start,
        end=end,
        limit=limit,
        horizons=tuple(body["horizons"]),
        min_cs=body["min_cs"],
        factors=body["factors"],
        max_workers=body["max_workers"],
    )
    out["universe_note"] = note

    _write_json(out, output=output, as_json=as_json)

    if not as_json:
        print(f"因子 IC 分析: {note}")
        print(f"标的 {out['count']} 只 | 区间 {out['start_date']} ~ {out['end_date']} | "
              f"持有期 {out['horizons']} | 横截面下限 {out['min_cs']}")
        print(
            "  因子 | 持有期 | 均值 RankIC | RankICIR | t 值 | IC>0 占比 | 期数"
        )
        for factor in out.get("results", []):
            for h in factor.get("horizons", []):
                n = h.get("n_dates", 0)
                mean = h.get("mean_ic")
                icir = h.get("icir")
                t = h.get("t_stat")
                pos = h.get("ic_positive_ratio")
                if n == 0 or mean is None:
                    mean_s = icir_s = t_s = pos_s = "-"
                else:
                    mean_s = f"{mean:.4f}"
                    icir_s = f"{icir:.3f}" if icir is not None else "-"
                    t_s = f"{t:.3f}" if t is not None else "-"
                    pos_s = f"{pos * 100:.1f}%"
                print(
                    f"  {factor['factor']:<22} | {h['horizon']:>4}  | {mean_s:>10} | "
                    f"{icir_s:>8} | {t_s:>6} | {pos_s:>8} | {n:>4}"
                )
        if out.get("warnings"):
            print("提示: " + "；".join(out["warnings"]))
        if output is not None:
            print(f"完整结果: {output.resolve()}")
        else:
            print("完整结果: 使用 --json 打印，或 --output FILE.json 保存")
    if report is not None:
        from app.ic_report import render_factor_ic_html

        report_path = render_factor_ic_html(out, report)
        print(f"交互报告已保存: {report_path}")
        _open_file_with_default_app(report_path)
    return 0


def _build_backtest_cmd(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("backtest", help="运行回测")
    p.set_defaults(handler=_cmd_backtest)
    p.add_argument("--request", type=Path, metavar="FILE.json")
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", type=Path, metavar="FILE.json")
    p.add_argument(
        "--report",
        type=Path,
        metavar="FILE.html",
        help="交互报告（HTML）输出路径",
    )
    p.add_argument(
        "--report-top-k",
        type=int,
        metavar="N",
        default=None,
        help="交互报告仅嵌入前 N 名逐票明细图（排行榜保留全部指标）；0 或省略=全部",
    )
    p.add_argument(
        "--report-price-top-k",
        type=int,
        metavar="N",
        default=None,
        help="从行情缓存补拉 K 线的股票数；0 或省略=全部，正数=仅前 N 名",
    )
    p.add_argument("--list-strategies", action="store_true")
    p.add_argument("--mode", choices=("single", "universe", "screen", "per_stock"), default=None)
    p.add_argument("--strategy", "-s", dest="strategy_id")
    p.add_argument(
        "--data-source",
        choices=("a_stock_data",),
        dest="data_source",
    )
    p.add_argument("--bars", type=int)
    p.add_argument("--initial-cash", type=float)
    p.add_argument("--commission", type=float)
    p.add_argument("--min-commission", type=float, dest="min_commission")
    p.add_argument("--slippage", type=float)
    p.add_argument("--lot-size", type=int, dest="lot_size")
    p.add_argument("--symbol")
    p.add_argument(
        "--universe",
        choices=("all_a", "hs300", "zz500", "zz399101", "zz1000", "gz2000", "star50", "star_board", "etf"),
    )
    p.add_argument("--symbols", nargs="+", help="批量回测时直接指定股票池")
    p.add_argument("--max-universe", type=int, dest="max_universe")
    p.add_argument("--seed", type=int)
    p.add_argument("--max-workers", type=int, dest="max_workers")
    p.add_argument("--force-refresh", action="store_true", dest="force_refresh")
    p.add_argument("--no-cache", action="store_true", dest="no_cache")
    p.add_argument(
        "--include-equity",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="批量结果是否包含逐票净值",
    )
    p.add_argument(
        "--include-trades",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="批量结果是否包含逐票成交",
    )
    p.add_argument(
        "--include-price",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="批量结果是否包含逐票行情与指标覆盖层",
    )
    p.add_argument("--start-date", dest="start_date")
    p.add_argument("--end-date", dest="end_date")
    p.add_argument("--fast-period", type=int, dest="fast_period")
    p.add_argument("--slow-period", type=int, dest="slow_period")
    p.add_argument("--signal-period", type=int, dest="signal_period")
    p.add_argument("--volume-ma-period", type=int, dest="volume_ma_period")
    p.add_argument(
        "--volume-metric",
        choices=("volume", "amount"),
        dest="volume_metric",
        help="量比用成交量或成交额",
    )
    p.add_argument(
        "--threshold-mode",
        choices=("fixed", "percentile"),
        dest="threshold_mode",
        help="量比阈值：固定倍数或滚动分位",
    )
    p.add_argument("--high-ratio", type=float, dest="high_ratio")
    p.add_argument("--low-ratio", type=float, dest="low_ratio")
    p.add_argument("--high-percentile", type=float, dest="high_percentile")
    p.add_argument("--low-percentile", type=float, dest="low_percentile")
    p.add_argument("--percentile-lookback", type=int, dest="percentile_lookback")
    p.add_argument("--price-ma-period", type=int, dest="price_ma_period")
    p.add_argument("--trend-ma-period", type=int, dest="trend_ma_period")
    p.add_argument("--breakout-period", type=int, dest="breakout_period")
    p.add_argument("--require-price-above-ma", action="store_true")
    p.add_argument("--require-trend-up", action="store_true")
    p.add_argument("--require-breakout", action="store_true")
    p.add_argument(
        "--entry-confirm-bars",
        type=int,
        dest="entry_confirm_bars",
        help="放量后再观察 N 日确认买入（0=当日买）",
    )
    p.add_argument(
        "--stop-loss-pct",
        type=float,
        dest="stop_loss_pct",
        help="通用止损：相对本次买入价的跌幅（如 0.1=跌10%%）",
    )
    p.add_argument("--take-profit-pct", type=float, dest="take_profit_pct")
    p.add_argument("--take-profit-arm-pct", type=float, dest="take_profit_arm_pct")
    p.add_argument("--take-profit-exit-pct", type=float, dest="take_profit_exit_pct")
    p.add_argument("--top-n", type=int, dest="top_n")
    p.add_argument("--min-price", type=float, dest="min_price")
    p.add_argument("--max-price", type=float, dest="max_price")
    p.add_argument("--min-dividend-yield", type=float, dest="min_dividend_yield")
    p.add_argument("--min-peg", type=float, dest="min_peg")
    p.add_argument("--max-peg", type=float, dest="max_peg")
    p.add_argument("--limit-pct-threshold", type=float, dest="limit_pct_threshold")
    p.add_argument("--no-dividend-filter", action="store_true", dest="no_dividend_filter")
    p.add_argument("--no-peg-filter", action="store_true", dest="no_peg_filter")


def _build_screen_cmd(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("screen", help="运行选股")
    p.set_defaults(handler=_cmd_screen)
    p.add_argument("--request", type=Path, metavar="FILE.json")
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", type=Path, metavar="FILE.json")
    p.add_argument("--list-presets", action="store_true")
    p.add_argument(
        "--preset",
        choices=(
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
        ),
    )
    p.add_argument("--top-k", type=int, dest="top_k")
    p.add_argument("--max-universe", type=int, dest="max_universe")
    p.add_argument("--start-date", dest="start_date")
    p.add_argument("--end-date", dest="end_date")
    p.add_argument("--bars", type=int)
    p.add_argument("--seed", type=int)
    p.add_argument("--random-seed", action="store_true")


def _build_stock_search_cmd(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("stock-search", help="通过腾讯财经按名称搜索股票代码")
    p.set_defaults(handler=_cmd_stock_search)
    p.add_argument("keyword", help="股票名称或关键字，如 贵州茅台")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", type=Path, metavar="FILE.json")


def _build_chip_dist_cmd(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("chip-dist", help="筹码分布分析（基于历史换手衰减算法）")
    p.set_defaults(handler=_cmd_chip_dist)
    p.add_argument("symbol", help="A 股代码，如 000001 或 600000.SH")
    p.add_argument("--days", type=int, default=500, help="回溯交易日数量（默认 500）")
    p.add_argument("--bins", type=int, default=200, help="价格区间网格数（默认 200）")
    p.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    p.add_argument("--output", type=Path, metavar="FILE.json", help="结果写入 JSON 文件")
    p.add_argument(
        "--detailed",
        action="store_true",
        help="包含完整筹码分布数组（price_grid + distribution）",
    )




def _build_factor_ic_cmd(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("factor-ic", help="因子 IC 分析（横截面秩相关预测力评价）")
    p.set_defaults(handler=_cmd_factor_ic)
    p.add_argument("--request", type=Path, metavar="FILE.json", help="请求 JSON（可含股票池与全部分析参数）")
    p.add_argument(
        "--universe",
        choices=(
            "all_a", "hs300", "zz500", "zz399101", "zz1000", "gz2000",
            "star50", "star_board", "etf",
        ),
    )
    p.add_argument("--symbols", nargs="+", help="直接指定股票池（与 --universe 二选一）")
    p.add_argument("--max-universe", type=int, dest="max_universe")
    p.add_argument("--seed", type=int)
    p.add_argument("--start-date", dest="start_date")
    p.add_argument("--end-date", dest="end_date")
    p.add_argument(
        "--limit",
        type=int,
        help="未指定起止日期时回溯的交易日数量（默认 1200）",
    )
    p.add_argument(
        "--horizons",
        type=int,
        nargs="+",
        help="未来收益持有期（交易日），可传多个，如 5 10 20（默认 5 10 20）",
    )
    p.add_argument(
        "--min-cs",
        type=int,
        dest="min_cs",
        help="每个时点横截面有效样本下限（低于则跳过该期，默认 10）",
    )
    p.add_argument(
        "--factors",
        nargs="+",
        help="要评价的因子名（省略则全部），用 --list-factors 查看",
    )
    p.add_argument("--list-factors", action="store_true", help="列出全部可用因子")
    p.add_argument("--max-workers", type=int, dest="max_workers")
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", type=Path, metavar="FILE.json")
    p.add_argument(
        "--report",
        type=Path,
        metavar="FILE.html",
        help="交互 HTML 报告输出路径（不传则默认 out/ic_report.html，并自动浏览器打开）",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="纯脚本版量化工具：回测、选股、筹码分布与因子 IC 分析均通过子命令执行。",
    )
    sub = p.add_subparsers(dest="command", required=True)
    _build_backtest_cmd(sub)
    _build_screen_cmd(sub)
    _build_stock_search_cmd(sub)
    _build_chip_dist_cmd(sub)
    _build_factor_ic_cmd(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = build_parser().parse_args(argv)
    handler = getattr(args, "handler")
    try:
        return handler(args)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    except MarketDataError as e:
        print(str(e), file=sys.stderr)
        return 3
    except Exception as e:
        print(f"执行失败: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
