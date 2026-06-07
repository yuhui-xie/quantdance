"""script backtest 参数解析测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas import BacktestRequest
from app.script import (
    _normalize_plot_path,
    _resolve_backtest_body,
    _resolve_backtest_output_options,
    _resolve_discovery_output_options,
    _write_backtest_summary,
    build_parser,
)


def test_backtest_symbol_default_to_a_stock_data():
    args = build_parser().parse_args(["backtest", "--symbol", "600000"])
    body = _resolve_backtest_body(args)
    assert body.symbol == "600000"
    assert body.data_source == "a_stock_data"


def test_backtest_explicit_a_stock_data_source_keeps_priority():
    args = build_parser().parse_args(
        ["backtest", "--symbol", "600000", "--data-source", "a_stock_data"]
    )
    body = _resolve_backtest_body(args)
    assert body.symbol == "600000"
    assert body.data_source == "a_stock_data"


def test_backtest_cli_data_source_rejects_legacy_source():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["backtest", "--data-source", "tickflow"])


def test_backtest_cli_strategy_params_are_nested():
    args = build_parser().parse_args(
        ["backtest", "--fast-period", "5", "--slow-period", "20", "--high-ratio", "1.8"]
    )
    body = _resolve_backtest_body(args)
    assert body.strategy_params == {
        "fast_period": 5,
        "slow_period": 20,
        "high_ratio": 1.8,
    }


def test_backtest_rejects_top_level_strategy_params():
    with pytest.raises(ValueError, match="strategy_params"):
        BacktestRequest.model_validate({"fast_period": 5})


def test_backtest_request_can_define_output_options(tmp_path):
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "strategy_id": "ma_crossover",
                "output_options": {
                    "json": True,
                    "plot": "out/example.svg",
                },
            }
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(["backtest", "--request", str(request_path)])

    output, as_json, plot = _resolve_backtest_output_options(args)

    assert output is None
    assert as_json is True
    assert plot is not None
    assert plot.as_posix() == "out/example.svg"


def test_discover_request_can_define_plot_output_options(tmp_path):
    request_path = tmp_path / "discover.json"
    request_path.write_text(
        json.dumps(
            {
                "universe": "hs300",
                "output_options": {
                    "json": False,
                    "output": "out/discover.json",
                    "plot": "out/discover.svg",
                },
            }
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(["discover", "--request", str(request_path)])

    output, as_json, plot = _resolve_discovery_output_options(args)

    assert output is not None
    assert output.as_posix() == "out/discover.json"
    assert as_json is False
    assert plot is not None
    assert plot.as_posix() == "out/discover.svg"


def test_plot_path_without_suffix_defaults_to_svg():
    assert _normalize_plot_path(Path("out/chart")).as_posix() == "out/chart.svg"
    assert _normalize_plot_path(Path("out/chart.png")).as_posix() == "out/chart.png"
    assert _normalize_plot_path(None) is None


def test_plot_output_paths_always_include_svg_and_png(tmp_path):
    from app.cli_plot import _plot_output_paths

    svg_dest = tmp_path / "chart.svg"
    paths = _plot_output_paths(svg_dest)
    assert [p.suffix for p in paths] == [".svg", ".png"]
    assert paths[0] == svg_dest.resolve()
    assert paths[1] == (tmp_path / "chart.png").resolve()

    png_dest = tmp_path / "chart.png"
    paths = _plot_output_paths(png_dest)
    assert {p.suffix for p in paths} == {".svg", ".png"}


def test_backtest_summary_prints_compact_result(capsys):
    body = BacktestRequest(strategy_id="ma_crossover")
    out = {
        "metrics": {
            "initial_cash": 100000.0,
            "final_equity": 108500.0,
            "total_return": 0.085,
            "max_drawdown": 0.032,
            "sharpe": 1.23,
            "num_trades": 2.0,
        },
        "equity": [
            {"date": "2024-01-02T15:00:00", "equity": 100000.0},
            {"date": "2024-02-01T15:00:00", "equity": 108500.0},
        ],
        "trades": [
            {
                "date": "2024-01-10T15:00:00",
                "side": "buy",
                "price": 10.0,
                "shares": 9970,
                "cash_after": 0.0,
            },
            {
                "date": "2024-01-20T15:00:00",
                "side": "sell",
                "price": 10.5,
                "shares": 9970,
                "cash_after": 104653.95,
            },
        ],
        "price": [{"close": 10.0}, {"close": 10.8}],
    }

    _write_backtest_summary(out, body, output=None)

    printed = capsys.readouterr().out
    assert "回测完成: ma_crossover" in printed
    assert "总收益 8.50%" in printed
    assert "买卖节点:" in printed
    assert "序号 | 日期" in printed
    assert "2024-01-10 | 买入" in printed
    assert "2024-01-20 | 卖出" in printed
    assert "完整结果: 使用 --json 打印，或 --output FILE.json 保存" in printed


def test_stock_search_command_accepts_keyword_and_limit():
    args = build_parser().parse_args(["stock-search", "贵州茅台", "--limit", "3", "--json"])
    assert args.keyword == "贵州茅台"
    assert args.limit == 3
    assert args.json is True
