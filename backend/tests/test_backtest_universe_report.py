"""批量回测交互报告测试（不访问外网）。"""

from __future__ import annotations

from pathlib import Path

from app.cli_backtest_report import (
    build_backtest_universe_report_model,
    render_backtest_universe_html,
)


def _mini_out() -> dict:
    def run(symbol: str, rank: int, total_return: float) -> dict:
        return {
            "symbol": symbol,
            "name": f"股票{symbol}",
            "status": "ok",
            "rank": rank,
            "metrics": {
                "initial_cash": 100_000,
                "total_return": total_return,
                "annualized_return": total_return / 2,
                "max_drawdown": 0.05,
                "sharpe": 1.2,
                "num_trades": 2,
                "win_rate": 1.0,
            },
            "equity": [
                {"date": "2024-01-02", "equity": 100_000},
                {"date": "2024-01-31", "equity": 100_000 * (1 + total_return)},
            ],
            "trades": [
                {"date": "2024-01-03", "side": "buy", "price": 10, "shares": 1000},
                {"date": "2024-01-30", "side": "sell", "price": 11, "shares": 1000},
            ],
            "price": [
                {
                    "date": "2024-01-03",
                    "open": 9.8,
                    "high": 10.2,
                    "low": 9.7,
                    "close": 10,
                    "llt": 9.95,
                }
            ],
        }

    return {
        "mode": "universe",
        "strategy_id": "llt_trend",
        "universe_note": "测试股票池",
        "summary": {
            "requested": 3,
            "succeeded": 2,
            "skipped": 1,
            "failed": 0,
            "average_total_return": 0.05,
            "average_max_drawdown": 0.05,
            "average_sharpe": 1.2,
        },
        "aggregate": {
            "metrics": {
                "initial_cash": 100_000,
                "total_return": 0.05,
                "annualized_return": 0.025,
                "max_drawdown": 0.02,
                "sharpe": 1.1,
            },
            "equity": [
                {"date": "2024-01-02", "equity": 100_000},
                {"date": "2024-01-31", "equity": 105_000},
            ],
        },
        "runs": [
            run("600000", 1, 0.1),
            run("000001", 2, 0.0),
            {
                "symbol": "000002",
                "name": "跳过股票",
                "status": "skipped",
                "reason": "K 线不足",
            },
        ],
        "warnings": ["1 只股票被跳过"],
        "disclaimer": "测试用途",
    }


def test_build_report_model_contains_ranking_and_top_details():
    model = build_backtest_universe_report_model(
        _mini_out(),
        top_k=1,
        load_prices=False,
    )

    assert [row["symbol"] for row in model["ranked_index"]] == [
        "600000",
        "000001",
        "000002",
    ]
    assert set(model["details"]) == {"600000"}
    detail = model["details"]["600000"]
    assert detail["equity"][-1]["nav"] == 1.1
    assert detail["has_ohlc"] is True
    assert detail["indicator_series"] == [
        {"date": "2024-01-03", "llt": 9.95}
    ]
    assert detail["price_overlay_fields"][0]["key"] == "llt"
    assert detail["indicator_charts"][0]["title"] == "LLT 趋势"
    assert detail["indicator_charts"][0]["fields"][0]["key"] == "llt"
    assert len(detail["trades"]) == 2
    assert detail["round_trips"][0]["pnl"] == 1000
    assert model["distribution"] == {
        "returns": [0.1, 0.0],
        "positive": 1,
        "negative": 0,
        "flat": 1,
    }


def test_build_report_model_includes_all_successful_details_by_default():
    model = build_backtest_universe_report_model(_mini_out(), load_prices=False)

    assert set(model["details"]) == {"600000", "000001"}
    assert model["detail_limit"] == 2
    assert model["price_detail_limit"] == 2


def test_build_report_model_groups_composite_indicator_fields():
    out = _mini_out()
    out["runs"][0]["price"][0].update(
        {
            "leg_1_ema_crossover": 1,
            "leg_1_ema_crossover__fast_ma": 10.1,
            "leg_1_ema_crossover__slow_ma": 9.9,
            "leg_2_llt_trend": 0,
            "leg_2_llt_trend__llt": 9.95,
            "composite_score": 0.6,
            "composite_position": 1,
        }
    )

    detail = build_backtest_universe_report_model(
        out, top_k=1, load_prices=False
    )["details"]["600000"]
    composite = next(
        chart for chart in detail["indicator_charts"] if chart["title"] == "组合信号"
    )

    assert {field["key"] for field in composite["fields"]} == {
        "leg_1_ema_crossover",
        "leg_2_llt_trend",
        "composite_score",
        "composite_position",
    }
    ema = next(
        chart
        for chart in detail["indicator_charts"]
        if chart["title"] == "子策略 1 · ema crossover · 移动平均"
    )
    assert {field["key"] for field in ema["fields"]} == {
        "leg_1_ema_crossover__fast_ma",
        "leg_1_ema_crossover__slow_ma",
    }
    assert {field["label"] for field in ema["fields"]} == {"快线", "慢线"}
    assert {
        field["key"] for field in detail["price_overlay_fields"]
    }.issuperset(
        {
            "llt",
            "leg_1_ema_crossover__fast_ma",
            "leg_1_ema_crossover__slow_ma",
            "leg_2_llt_trend__llt",
        }
    )


def test_render_backtest_universe_html(tmp_path: Path):
    path = render_backtest_universe_html(
        _mini_out(),
        tmp_path / "batch.html",
        top_k=2,
        load_prices=False,
    )

    text = path.read_text(encoding="utf-8")
    assert path == (tmp_path / "batch.html").resolve()
    assert "独立资金批量回测报告" in text
    assert "llt_trend" in text
    assert "600000" in text
    assert "全量结果排行" in text
    assert "FIFO" in text
    assert "策略指标" in text
    assert "indicator_charts" in text
    assert 'id="detailIndicators"' in text
    assert "const DATA=" in text
    assert "round_trips" in text
    assert "滚轮缩放 · 拖拽平移 · 悬停查看" in text
    assert 'class="chart-tooltip"' in text
    assert 'addEventListener("wheel"' in text
    assert 'addEventListener("dblclick"' in text
