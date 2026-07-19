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
from app.portfolio import list_portfolio_strategies, run_portfolio_request
from app.portfolio.registry import PORTFOLIO_STRATEGIES
from app.schemas import (
    BacktestRequest,
    DiscoveryRequest,
    PortfolioBacktestRequest,
    ScreenRequest,
)
from app.stock_discovery import run_discovery
from app.stock_screening import run_screen, screen_presets_catalog
from app.strategies.registry import STRATEGIES


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


def _path_from_output_option(value: Any, key: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"output_options.{key} 需为非空字符串路径")
    return Path(value)


def _normalize_plot_path(path: Path | None) -> Path | None:
    """图表默认保存为 SVG；显式扩展名（如 .png/.pdf）仍按原样保留。"""
    if path is None:
        return None
    if path.suffix:
        return path
    return path.with_suffix(".svg")


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


def _resolve_backtest_output_options(args: argparse.Namespace) -> tuple[Path | None, bool, Path | None]:
    output_options = _load_output_options(args)

    json_option = output_options.get("json", False)
    if not isinstance(json_option, bool):
        raise ValueError("output_options.json 需为布尔值")

    output = args.output or _path_from_output_option(output_options.get("output"), "output")
    as_json = args.json or json_option
    plot = _normalize_plot_path(args.plot or _path_from_output_option(output_options.get("plot"), "plot"))
    return output, as_json, plot


def _resolve_json_output_options(args: argparse.Namespace) -> tuple[Path | None, bool]:
    output_options = _load_output_options(args)

    json_option = output_options.get("json", False)
    if not isinstance(json_option, bool):
        raise ValueError("output_options.json 需为布尔值")

    output = args.output or _path_from_output_option(output_options.get("output"), "output")
    as_json = args.json or json_option
    return output, as_json


def _resolve_discovery_output_options(args: argparse.Namespace) -> tuple[Path | None, bool, Path | None]:
    output_options = _load_output_options(args)

    json_option = output_options.get("json", False)
    if not isinstance(json_option, bool):
        raise ValueError("output_options.json 需为布尔值")

    output = args.output or _path_from_output_option(output_options.get("output"), "output")
    as_json = args.json or json_option
    plot = _normalize_plot_path(args.plot or _path_from_output_option(output_options.get("plot"), "plot"))
    return output, as_json, plot


def _resolve_portfolio_output_options(
    args: argparse.Namespace,
) -> tuple[Path | None, bool, Path | None, Path | None]:
    """组合回测输出：JSON / plot / 交互 HTML report。

    report 优先级：显式 --report / output_options.report；
    否则若配置了 plot，自动派生同 stem 的 .html。
    """
    output_options = _load_output_options(args)

    json_option = output_options.get("json", False)
    if not isinstance(json_option, bool):
        raise ValueError("output_options.json 需为布尔值")

    output = args.output or _path_from_output_option(output_options.get("output"), "output")
    as_json = args.json or json_option
    plot = _normalize_plot_path(args.plot or _path_from_output_option(output_options.get("plot"), "plot"))

    report_arg = getattr(args, "report", None)
    report = _normalize_report_path(
        report_arg or _path_from_output_option(output_options.get("report"), "report")
    )
    if report is None and plot is not None:
        report = plot.with_suffix(".html")
    return output, as_json, plot, report


def _resolve_backtest_body(args: argparse.Namespace) -> BacktestRequest:
    base: dict[str, Any] = BacktestRequest().model_dump()
    if args.request is not None:
        req_data = _load_json_file(args.request)
        if not isinstance(req_data, dict):
            raise ValueError("--request 需为 JSON 对象")
        base.update(req_data)

    cli_map: dict[str, Any] = {}
    strategy_params: dict[str, Any] = dict(base.get("strategy_params") or {})
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
    if args.symbol is not None:
        cli_map["symbol"] = args.symbol
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
        strategy_params["stop_loss_pct"] = args.stop_loss_pct
    if args.take_profit_pct is not None:
        strategy_params["take_profit_pct"] = args.take_profit_pct
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


def _resolve_discovery_body(args: argparse.Namespace) -> DiscoveryRequest:
    base: dict[str, Any] = DiscoveryRequest().model_dump()
    if args.request is not None:
        req_data = _load_json_file(args.request)
        if not isinstance(req_data, dict):
            raise ValueError("--request 需为 JSON 对象")
        base.update(req_data)

    cli_map: dict[str, Any] = {}
    if args.symbols is not None:
        cli_map["symbols"] = args.symbols
    if args.universe is not None:
        cli_map["universe"] = args.universe
    if args.strategies is not None:
        cli_map["strategies"] = args.strategies
    if args.top_k is not None:
        cli_map["top_k"] = args.top_k
    if args.max_universe is not None:
        cli_map["max_universe"] = args.max_universe
    if args.seed is not None:
        cli_map["seed"] = args.seed
    if args.no_benchmarks:
        cli_map["include_benchmarks"] = False
    if args.bars is not None:
        cli_map["bars"] = args.bars
    if args.start_date is not None:
        cli_map["start_date"] = args.start_date
    if args.end_date is not None:
        cli_map["end_date"] = args.end_date
    if args.initial_cash is not None:
        cli_map["initial_cash"] = args.initial_cash
    if args.commission is not None:
        cli_map["commission"] = args.commission
    if args.min_bars is not None:
        cli_map["min_bars"] = args.min_bars
    if args.min_avg_volume is not None:
        cli_map["min_avg_volume"] = args.min_avg_volume
    if args.min_last_close is not None:
        cli_map["min_last_close"] = args.min_last_close
    if args.max_last_close is not None:
        cli_map["max_last_close"] = args.max_last_close
    if args.min_trades is not None:
        cli_map["min_trades"] = args.min_trades
    if args.min_total_return is not None:
        cli_map["min_total_return"] = args.min_total_return
    if args.max_drawdown is not None:
        cli_map["max_drawdown"] = args.max_drawdown
    if args.min_pe_ttm is not None:
        cli_map["min_pe_ttm"] = args.min_pe_ttm
    if args.max_pe_ttm is not None:
        cli_map["max_pe_ttm"] = args.max_pe_ttm
    if args.min_pb is not None:
        cli_map["min_pb"] = args.min_pb
    if args.max_pb is not None:
        cli_map["max_pb"] = args.max_pb
    if args.min_market_cap is not None:
        cli_map["min_market_cap"] = args.min_market_cap
    if args.max_market_cap is not None:
        cli_map["max_market_cap"] = args.max_market_cap
    if args.min_float_market_cap is not None:
        cli_map["min_float_market_cap"] = args.min_float_market_cap
    if args.max_float_market_cap is not None:
        cli_map["max_float_market_cap"] = args.max_float_market_cap
    if args.min_turnover_rate is not None:
        cli_map["min_turnover_rate"] = args.min_turnover_rate
    if args.max_turnover_rate is not None:
        cli_map["max_turnover_rate"] = args.max_turnover_rate
    if args.robustness_window is not None:
        cli_map["robustness_windows"] = args.robustness_window
    if args.param_perturbation_pct is not None:
        cli_map["param_perturbation_pct"] = args.param_perturbation_pct
    if args.max_perturbation_sets is not None:
        cli_map["max_perturbation_sets"] = args.max_perturbation_sets
    base.update(cli_map)
    return DiscoveryRequest.model_validate(base)


def _cmd_backtest(args: argparse.Namespace) -> int:
    if args.list_strategies:
        for sid in sorted(STRATEGIES.keys()):
            spec = STRATEGIES[sid]
            print(f"{spec.id}\t{spec.name}")
        return 0
    output, as_json, plot = _resolve_backtest_output_options(args)
    body = _resolve_backtest_body(args)
    out = run_backtest_request(body)
    _write_json(out, output=output, as_json=as_json)
    if plot is not None:
        from app.cli_plot import render_backtest_figure

        saved = render_backtest_figure(out, plot)
        print("图表已保存: " + " | ".join(str(p) for p in saved))
        _open_file_with_default_app(plot)
    if not as_json:
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


def _cmd_discover(args: argparse.Namespace) -> int:
    output, as_json, plot = _resolve_discovery_output_options(args)
    body = _resolve_discovery_body(args)
    out = run_discovery(body).model_dump(mode="json")
    _write_json(out, output=output, as_json=as_json)
    if plot is not None:
        from app.cli_plot import render_discovery_figure

        saved = render_discovery_figure(out, plot)
        print("对比图已保存: " + " | ".join(str(p) for p in saved))
        _open_file_with_default_app(plot)
    if as_json or output is not None:
        return 0

    print(f"发现完成: 候选 {len(out['candidates'])} 个 | 执行结果 {len(out['runs'])} 条")
    if out.get("universe_note"):
        print(out["universe_note"])
    if out.get("warnings"):
        print("提示: " + "；".join(out["warnings"]))
    summary = out.get("summary", {})
    print(
        "汇总: "
        f"平均收益 {_fmt_pct(summary.get('average_total_return'))} | "
        f"平均超额 {_fmt_pct(summary.get('average_excess_total_return'))} | "
        f"平均回撤 {_fmt_pct(summary.get('average_max_drawdown'))} | "
        f"平均 Sharpe {_fmt_float(summary.get('average_sharpe'))}"
    )
    benchmark = out.get("benchmarks", {}).get("hs300_equal_weight")
    if benchmark:
        metrics = benchmark.get("metrics", {})
        print(
            "基准: 沪深300等权 | "
            f"成分 {int(float(metrics.get('member_count', 0)))} | "
            f"收益 {_fmt_pct(metrics.get('total_return'))} | "
            f"回撤 {_fmt_pct(metrics.get('max_drawdown'))} | "
            f"Sharpe {_fmt_float(metrics.get('sharpe'))}"
        )
    for idx, item in enumerate(out["candidates"], start=1):
        metrics = item.get("metrics", {})
        filters = item.get("filters", {})
        print(
            f"{idx}. {item['symbol']} {item.get('name') or ''} | "
            f"{item['strategy_id']} | score {_fmt_float(item.get('score'), 4)} | "
            f"收益 {_fmt_pct(metrics.get('total_return'))} | "
            f"超额 {_fmt_pct(metrics.get('excess_total_return'))} | "
            f"回撤 {_fmt_pct(metrics.get('max_drawdown'))} | "
            f"Sharpe {_fmt_float(metrics.get('sharpe'))} | "
            f"信号 {item.get('latest_signal')} | "
            f"收盘 {_fmt_float(filters.get('last_close'))}"
        )
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


def _resolve_portfolio_body(args: argparse.Namespace) -> PortfolioBacktestRequest:
    strategy_id = getattr(args, "strategy_id", None) or "market_auntie"
    spec = PORTFOLIO_STRATEGIES.get(strategy_id)
    default_universe = spec.default_universe if spec is not None else "all_a"
    base: dict[str, Any] = {
        "strategy_id": strategy_id,
        "mode": "backtest",
        "universe": default_universe,
        "symbols": [],
        "max_universe": 80,
        "rebalance_freq": 20,
        "initial_cash": 100_000,
        "commission": 0.0003,
        "min_commission": 5.0,
        "slippage": 0.01,
        "lot_size": 100,
        "use_cache": True,
        "force_refresh": False,
        "max_workers": 8,
        "strategy_params": {},
    }
    if args.request is not None:
        req_data = _load_json_file(args.request)
        if not isinstance(req_data, dict):
            raise ValueError("--request 需为 JSON 对象")
        base.update({k: v for k, v in req_data.items() if k != "output_options"})

    strategy_params: dict[str, Any] = dict(base.get("strategy_params") or {})
    cli_map: dict[str, Any] = {}
    if getattr(args, "strategy_id", None) is not None:
        cli_map["strategy_id"] = args.strategy_id
    if getattr(args, "mode", None) is not None:
        cli_map["mode"] = args.mode
    if getattr(args, "universe", None) is not None:
        cli_map["universe"] = args.universe
    if getattr(args, "symbols", None):
        cli_map["symbols"] = args.symbols
    if getattr(args, "max_universe", None) is not None:
        cli_map["max_universe"] = args.max_universe
    if getattr(args, "seed", None) is not None:
        cli_map["seed"] = args.seed
    if getattr(args, "start_date", None) is not None:
        cli_map["start_date"] = args.start_date
    if getattr(args, "end_date", None) is not None:
        cli_map["end_date"] = args.end_date
    if getattr(args, "rebalance_freq", None) is not None:
        cli_map["rebalance_freq"] = args.rebalance_freq
    if getattr(args, "initial_cash", None) is not None:
        cli_map["initial_cash"] = args.initial_cash
    if getattr(args, "commission", None) is not None:
        cli_map["commission"] = args.commission
    if getattr(args, "min_commission", None) is not None:
        cli_map["min_commission"] = args.min_commission
    if getattr(args, "slippage", None) is not None:
        cli_map["slippage"] = args.slippage
    if getattr(args, "force_refresh", False):
        cli_map["force_refresh"] = True
    if getattr(args, "no_cache", False):
        cli_map["use_cache"] = False
    if getattr(args, "max_workers", None) is not None:
        cli_map["max_workers"] = args.max_workers

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

    base.update(cli_map)

    sid = str(base.get("strategy_id") or strategy_id)
    spec2 = PORTFOLIO_STRATEGIES.get(sid)
    if spec2 is not None and not base.get("symbols"):
        req_has_universe = False
        if args.request is not None:
            raw = _load_json_file(args.request)
            req_has_universe = isinstance(raw, dict) and "universe" in raw
        if getattr(args, "universe", None) is None and not req_has_universe:
            base["universe"] = spec2.default_universe

    base["strategy_params"] = strategy_params
    return PortfolioBacktestRequest.model_validate(base)


def _cmd_portfolio(args: argparse.Namespace) -> int:
    if getattr(args, "list_strategies", False):
        for spec in list_portfolio_strategies():
            print(f"{spec.id}\t{spec.name}\t{spec.description}")
        return 0
    output, as_json, plot, report = _resolve_portfolio_output_options(args)
    body = _resolve_portfolio_body(args)
    out = run_portfolio_request(body).model_dump(mode="json")
    _write_json(out, output=output, as_json=as_json)
    if plot is not None and out.get("equity"):
        from app.cli_plot import render_portfolio_figure

        saved = render_portfolio_figure(out, plot)
        print("图表已保存: " + " | ".join(str(p) for p in saved))
        _open_file_with_default_app(plot)
    if report is not None and out.get("equity"):
        from app.cli_portfolio_report import render_portfolio_html

        report_path = render_portfolio_html(out, report)
        print(f"交互报告已保存: {report_path}")
        _open_file_with_default_app(report_path)
    if as_json or output is not None:
        if not as_json:
            print(
                f"组合回测完成: {out.get('strategy_id')} | mode={out.get('mode')} | "
                f"持仓 {len(out.get('holdings') or [])} | 调仓 {len(out.get('rebalances') or [])}"
            )
        return 0

    print(f"组合完成: {out.get('strategy_id')} | mode={out.get('mode')} | asof={out.get('asof')}")
    if out.get("universe_note"):
        print(out["universe_note"])
    if out.get("warnings"):
        print("提示: " + "；".join(out["warnings"]))
    metrics = out.get("metrics") or {}
    if metrics:
        print(
            "指标: "
            f"总收益 {_fmt_pct(metrics.get('total_return'))} | "
            f"年化 {_fmt_pct(metrics.get('annualized_return'))} | "
            f"最大回撤 {_fmt_pct(metrics.get('max_drawdown'))} | "
            f"Sharpe {_fmt_float(metrics.get('sharpe'))} | "
            f"交易 {int(float(metrics.get('num_trades', 0)))}"
        )
    holdings = out.get("holdings") or []
    if holdings:
        print("最新目标持仓:")
        for idx, item in enumerate(holdings, start=1):
            print(
                f"{idx}. {item.get('symbol')} {item.get('name') or ''} | "
                f"价 {_fmt_float(item.get('close'))} | "
                f"市值 {_fmt_float(item.get('market_cap'), 0)} | "
                f"流通市值 {_fmt_float(item.get('float_market_cap') or item.get('rank_market_cap'), 0)} | "
                f"PEG {_fmt_float(item.get('peg'))} | "
                f"股息率 {_fmt_pct(item.get('dividend_yield'))}"
            )
    return 0


def _build_backtest_cmd(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("backtest", help="运行回测")
    p.set_defaults(handler=_cmd_backtest)
    p.add_argument("--request", type=Path, metavar="FILE.json")
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", type=Path, metavar="FILE.json")
    p.add_argument("--plot", type=Path, metavar="PATH", help="保存图表路径（默认 .svg）")
    p.add_argument("--list-strategies", action="store_true")
    p.add_argument("--strategy", "-s", dest="strategy_id")
    p.add_argument(
        "--data-source",
        choices=("a_stock_data",),
        dest="data_source",
    )
    p.add_argument("--bars", type=int)
    p.add_argument("--initial-cash", type=float)
    p.add_argument("--commission", type=float)
    p.add_argument("--symbol")
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
    p.add_argument("--stop-loss-pct", type=float, dest="stop_loss_pct")
    p.add_argument("--take-profit-pct", type=float, dest="take_profit_pct")


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


def _build_discover_cmd(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("discover", help="批量回测驱动的股票发现")
    p.set_defaults(handler=_cmd_discover)
    p.add_argument("--request", type=Path, metavar="FILE.json")
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", type=Path, metavar="FILE.json")
    p.add_argument("--plot", type=Path, metavar="PATH", help="保存对比图路径（默认 .svg）")
    p.add_argument("--symbols", nargs="+", help="直接指定股票池，如 600000 000001")
    p.add_argument("--universe", choices=("all_a", "hs300"), help="symbols 为空时使用的股票池")
    p.add_argument("--strategy", dest="strategies", action="append", choices=tuple(sorted(STRATEGIES)))
    p.add_argument("--top-k", type=int, dest="top_k")
    p.add_argument("--max-universe", type=int, dest="max_universe")
    p.add_argument("--seed", type=int)
    p.add_argument("--no-benchmarks", action="store_true", help="不计算股票池基准")
    p.add_argument("--bars", type=int)
    p.add_argument("--start-date", dest="start_date")
    p.add_argument("--end-date", dest="end_date")
    p.add_argument("--initial-cash", type=float)
    p.add_argument("--commission", type=float)
    p.add_argument("--min-bars", type=int, dest="min_bars")
    p.add_argument("--min-avg-volume", type=float, dest="min_avg_volume")
    p.add_argument("--min-last-close", type=float, dest="min_last_close")
    p.add_argument("--max-last-close", type=float, dest="max_last_close")
    p.add_argument("--min-trades", type=int, dest="min_trades")
    p.add_argument("--min-total-return", type=float, dest="min_total_return")
    p.add_argument("--max-drawdown", type=float, dest="max_drawdown")
    p.add_argument("--min-pe-ttm", type=float, dest="min_pe_ttm")
    p.add_argument("--max-pe-ttm", type=float, dest="max_pe_ttm")
    p.add_argument("--min-pb", type=float, dest="min_pb")
    p.add_argument("--max-pb", type=float, dest="max_pb")
    p.add_argument("--min-market-cap", type=float, dest="min_market_cap")
    p.add_argument("--max-market-cap", type=float, dest="max_market_cap")
    p.add_argument("--min-float-market-cap", type=float, dest="min_float_market_cap")
    p.add_argument("--max-float-market-cap", type=float, dest="max_float_market_cap")
    p.add_argument("--min-turnover-rate", type=float, dest="min_turnover_rate")
    p.add_argument("--max-turnover-rate", type=float, dest="max_turnover_rate")
    p.add_argument("--robustness-window", type=int, action="append")
    p.add_argument("--param-perturbation-pct", type=float, dest="param_perturbation_pct")
    p.add_argument("--max-perturbation-sets", type=int, dest="max_perturbation_sets")


def _build_stock_search_cmd(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("stock-search", help="通过腾讯财经按名称搜索股票代码")
    p.set_defaults(handler=_cmd_stock_search)
    p.add_argument("keyword", help="股票名称或关键字，如 贵州茅台")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", type=Path, metavar="FILE.json")


def _build_portfolio_cmd(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("portfolio", help="低频组合：截面选股与周期调仓回测")
    p.set_defaults(handler=_cmd_portfolio)
    p.add_argument("--request", type=Path, metavar="FILE.json")
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", type=Path, metavar="FILE.json")
    p.add_argument("--plot", type=Path, metavar="PATH", help="保存权益曲线路径（默认 .svg）")
    p.add_argument(
        "--report",
        type=Path,
        metavar="PATH",
        help="保存交互 HTML 报告路径（默认 .html；未指定时若有 --plot 则派生同名 .html）",
    )
    p.add_argument("--list-strategies", action="store_true", dest="list_strategies")
    p.add_argument("--mode", choices=("backtest", "screen"), default=None)
    p.add_argument(
        "--strategy",
        "-s",
        dest="strategy_id",
        choices=tuple(sorted(PORTFOLIO_STRATEGIES)) or ("market_auntie",),
    )
    p.add_argument("--universe", choices=("all_a", "hs300", "zz399101"))
    p.add_argument("--symbols", nargs="+", help="直接指定股票池")
    p.add_argument("--max-universe", type=int, dest="max_universe")
    p.add_argument("--seed", type=int)
    p.add_argument("--start-date", dest="start_date")
    p.add_argument("--end-date", dest="end_date")
    p.add_argument(
        "--rebalance-freq",
        type=int,
        dest="rebalance_freq",
        help="调仓间隔（交易日），如 20≈月频、5≈周频",
    )
    p.add_argument("--initial-cash", type=float)
    p.add_argument("--commission", type=float)
    p.add_argument("--min-commission", type=float, dest="min_commission")
    p.add_argument("--slippage", type=float)
    p.add_argument("--max-workers", type=int, dest="max_workers")
    p.add_argument("--force-refresh", action="store_true", dest="force_refresh")
    p.add_argument("--no-cache", action="store_true", dest="no_cache")
    p.add_argument("--top-n", type=int, dest="top_n")
    p.add_argument("--min-price", type=float, dest="min_price")
    p.add_argument("--max-price", type=float, dest="max_price")
    p.add_argument("--min-dividend-yield", type=float, dest="min_dividend_yield")
    p.add_argument("--min-peg", type=float, dest="min_peg")
    p.add_argument("--max-peg", type=float, dest="max_peg")
    p.add_argument("--limit-pct-threshold", type=float, dest="limit_pct_threshold")
    p.add_argument("--no-dividend-filter", action="store_true", dest="no_dividend_filter")
    p.add_argument("--no-peg-filter", action="store_true", dest="no_peg_filter")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="纯脚本版量化工具：回测与选股均通过子命令执行。",
    )
    sub = p.add_subparsers(dest="command", required=True)
    _build_backtest_cmd(sub)
    _build_screen_cmd(sub)
    _build_discover_cmd(sub)
    _build_portfolio_cmd(sub)
    _build_stock_search_cmd(sub)
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
