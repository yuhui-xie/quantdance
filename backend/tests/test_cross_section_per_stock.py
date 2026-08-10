"""横截面策略 per_stock 独立资金逐票验证模式测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pydantic import BaseModel

from app.backtest.cross_section_runner import (
    _generate_selection_signal,
    run_cross_section_per_stock_backtest,
)
from app.backtest_runner import run_backtest_request
from app.schemas import BacktestRequest, BacktestUniverseResponse
from app.strategies.base import CrossSectionStrategySpec


# ── 工具函数 ──────────────────────────────────────────────


def _make_ohlcv(close_values: list[float], start_date: str = "2024-01-02") -> pd.DataFrame:
    """构造含 OHLCV 列的 DataFrame，DatetimeIndex。"""
    dates = pd.date_range(start_date, periods=len(close_values), freq="B")
    return pd.DataFrame(
        {
            "open": close_values,
            "high": [v * 1.01 for v in close_values],
            "low": [v * 0.99 for v in close_values],
            "close": close_values,
            "volume": [1_000_000] * len(close_values),
        },
        index=dates,
    )


def _make_value_df(dates: list[str], closes: list[float]) -> pd.DataFrame:
    """构造 panel value DataFrame（含 date + close）。"""
    return pd.DataFrame({"date": dates, "close": closes})


class _PerStockParams(BaseModel):
    top_n: int = 1


# ── _generate_selection_signal 单元测试 ───────────────────


class TestGenerateSelectionSignal:
    def test_signal_buy_on_first_selection(self):
        """首次被选中时应生成买入信号（1）。"""
        dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
        dd = ["2024-01-02"]
        targets = {"2024-01-02": {"A"}}

        sig = _generate_selection_signal("A", dates, dd, targets)
        np.testing.assert_array_equal(sig, [1, 0, 0])

    def test_signal_sell_on_exit(self):
        """移出选中名单时应生成卖出信号（-1）。"""
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        dd = ["2024-01-02", "2024-01-04"]
        targets = {"2024-01-02": {"A"}, "2024-01-04": set()}

        sig = _generate_selection_signal("A", dates, dd, targets)
        # 第1天买入，第3天卖出（决策日 01-04 的下一个交易日）
        np.testing.assert_array_equal(sig, [1, 0, -1, 0])

    def test_signal_hold_while_selected(self):
        """持续被选中期间无新信号。"""
        dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
        dd = ["2024-01-02"]
        targets = {"2024-01-02": {"A"}}

        sig = _generate_selection_signal("A", dates, dd, targets)
        assert sig[0] == 1
        assert sig[1] == 0  # 维持持仓，无信号
        assert sig[2] == 0

    def test_signal_rebuy_after_reentry(self):
        """卖出后再次被选中时应重新买入。"""
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        dd = ["2024-01-02", "2024-01-03", "2024-01-04"]
        targets = {
            "2024-01-02": {"A"},
            "2024-01-03": set(),    # 移出 → 卖出
            "2024-01-04": {"A"},    # 重新选入 → 买入
        }

        sig = _generate_selection_signal("A", dates, dd, targets)
        np.testing.assert_array_equal(sig, [1, -1, 1, 0])

    def test_signal_never_selected_all_zeros(self):
        """从未被选中的股票信号全为零。"""
        dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
        dd = ["2024-01-02"]
        targets = {"2024-01-02": {"B"}}  # 只有 B，没有 A

        sig = _generate_selection_signal("A", dates, dd, targets)
        np.testing.assert_array_equal(sig, [0, 0, 0])

    def test_signal_forward_filled_between_decisions(self):
        """决策日之间信号维持上一决策日的选中状态。"""
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        dd = ["2024-01-02", "2024-01-04"]
        targets = {"2024-01-02": {"A"}, "2024-01-04": {"A"}}

        sig = _generate_selection_signal("A", dates, dd, targets)
        # 第一天买入，中间维持，第三天仍在选中名单（无卖出信号）
        np.testing.assert_array_equal(sig, [1, 0, 0, 0])

    def test_signal_no_decision_date_before_first_date(self):
        """第一个交易日之前无决策日时，信号全为零。"""
        dates = ["2024-01-02", "2024-01-03"]
        dd = ["2024-01-05"]  # 决策日在所有交易日之后
        targets = {"2024-01-05": {"A"}}

        sig = _generate_selection_signal("A", dates, dd, targets)
        np.testing.assert_array_equal(sig, [0, 0])


# ── Schema 验证测试 ──────────────────────────────────────


class TestPerStockSchemaValidation:
    def test_per_stock_requires_start_and_end(self):
        """per_stock 模式须同时提供 start_date 与 end_date。"""
        with pytest.raises(ValueError, match="start_date 与 end_date"):
            BacktestRequest(
                strategy_id="test",
                mode="per_stock",
                start_date="2024-01-01",
            )

    def test_per_stock_forbids_symbol_field(self):
        """per_stock 模式不应填写 symbol，应使用 symbols。"""
        with pytest.raises(ValueError, match="symbols"):
            BacktestRequest(
                strategy_id="test",
                mode="per_stock",
                symbol="000001",
                start_date="2024-01-01",
                end_date="2024-01-31",
            )

    def test_per_stock_accepts_symbols(self):
        """per_stock 模式正确接受 symbols 列表。"""
        req = BacktestRequest(
            strategy_id="test",
            mode="per_stock",
            symbols=["000001", "000002"],
            start_date="2024-01-01",
            end_date="2024-01-31",
        )
        assert req.mode == "per_stock"
        assert req.symbols == ["000001", "000002"]


# ── 集成测试：run_cross_section_per_stock_backtest ───────


class TestRunCrossSectionPerStockBacktest:
    def test_per_stock_basic_flow(self, monkeypatch):
        """完整流程：两股票，A 始终选中、B 后被选中 → 独立回测 + 汇总。"""

        def select(_asof, _ctx, params):
            # A 始终被选，B 仅第二个决策日被选
            if _asof == "2024-01-02":
                return ["A"], [{"symbol": "A", "score": 1.0}]
            return ["A", "B"], [
                {"symbol": "A", "score": 1.0},
                {"symbol": "B", "score": 0.5},
            ]

        def decision_dates(calendar, _ctx, _params):
            return [calendar[0], calendar[2]]

        spec = CrossSectionStrategySpec(
            id="per_stock_test",
            name="test",
            description="test",
            params_model=_PerStockParams,
            select=select,
            decision_dates=decision_dates,
            needs_fundamentals=False,
            requires_symbols=True,
        )

        value_a = _make_value_df(
            ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
            [10, 11, 12, 13, 14],
        )
        value_b = _make_value_df(
            ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
            [20, 19, 18, 17, 16],
        )

        monkeypatch.setattr(
            "app.backtest.cross_section_runner._resolve_universe",
            lambda *_args: (
                [{"symbol": "A", "name": "A"}, {"symbol": "B", "name": "B"}],
                "test",
            ),
        )
        monkeypatch.setattr(
            "app.backtest.cross_section_runner._load_panel",
            lambda *_args, **_kwargs: {
                "A": {"value": value_a},
                "B": {"value": value_b},
            },
        )
        monkeypatch.setattr(
            "app.backtest.cross_section_runner.fetch_a_share_daily",
            lambda symbol, start, end, data_source: {
                "A": _make_ohlcv([10, 11, 12, 13, 14]),
                "B": _make_ohlcv([20, 19, 18, 17, 16]),
            }[symbol],
        )

        request = BacktestRequest(
            strategy_id="per_stock_test",
            mode="per_stock",
            symbols=["A", "B"],
            start_date="2024-01-02",
            end_date="2024-01-08",
            initial_cash=100_000,
            commission=0,
            min_commission=0,
            slippage=0,
            lot_size=1,
            strategy_params={"top_n": 1},
        )

        result = run_cross_section_per_stock_backtest(request, spec)

        # 返回类型
        assert isinstance(result, BacktestUniverseResponse)
        assert result.strategy_id == "per_stock_test"

        # 两条 per-stock run
        assert result.summary.requested == 2
        assert result.summary.succeeded == 2
        ok_runs = [r for r in result.runs if r.status == "ok"]
        assert len(ok_runs) == 2

        # A 始终被选中：首日买入，之后维持（无卖出信号）→ 1 笔买入
        run_a = next(r for r in ok_runs if r.symbol == "A")
        assert len(run_a.trades) >= 1
        assert run_a.trades[0]["side"] == "buy"

        # B 第二个决策日才被选中 → 至少 1 笔买入
        run_b = next(r for r in ok_runs if r.symbol == "B")
        assert len(run_b.trades) >= 1
        assert run_b.trades[0]["side"] == "buy"

        # A 始终持有全程上涨 → 正收益；B 买入后下跌 → 负收益
        assert run_a.metrics["total_return"] > 0
        assert run_b.metrics["total_return"] < 0

        # 聚合存在
        assert result.aggregate
        assert "equity" in result.aggregate
        assert len(result.aggregate["equity"]) > 0

    def test_per_stock_skips_failed_symbols(self, monkeypatch):
        """一只股票无行情数据时应标记为 failed。"""

        def select(_asof, _ctx, _params):
            return ["A"], [{"symbol": "A", "score": 1.0}]

        def decision_dates(calendar, _ctx, _params):
            return [calendar[0]]

        spec = CrossSectionStrategySpec(
            id="per_stock_skip",
            name="test",
            description="test",
            params_model=_PerStockParams,
            select=select,
            decision_dates=decision_dates,
            needs_fundamentals=False,
            requires_symbols=True,
        )

        value_a = _make_value_df(
            ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
            [10, 11, 12, 13, 14],
        )
        value_b = _make_value_df(
            ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
            [20, 19, 18, 17, 16],
        )

        monkeypatch.setattr(
            "app.backtest.cross_section_runner._resolve_universe",
            lambda *_args: (
                [{"symbol": "A", "name": "A"}, {"symbol": "B", "name": "B"}],
                "test",
            ),
        )
        monkeypatch.setattr(
            "app.backtest.cross_section_runner._load_panel",
            lambda *_args, **_kwargs: {
                "A": {"value": value_a},
                "B": {"value": value_b},
            },
        )

        def mock_fetch(symbol, start, end, data_source):
            if symbol == "A":
                return _make_ohlcv([10, 11, 12, 13, 14])
            raise ValueError("无数据")

        monkeypatch.setattr(
            "app.backtest.cross_section_runner.fetch_a_share_daily",
            mock_fetch,
        )

        request = BacktestRequest(
            strategy_id="per_stock_skip",
            mode="per_stock",
            symbols=["A", "B"],
            start_date="2024-01-02",
            end_date="2024-01-08",
            initial_cash=100_000,
            commission=0,
            min_commission=0,
            slippage=0,
            lot_size=1,
        )

        result = run_cross_section_per_stock_backtest(request, spec)

        assert result.summary.requested == 2
        assert result.summary.succeeded == 1
        assert result.summary.failed == 1
        assert any("无数据" in (r.reason or "") for r in result.runs)

    def test_per_stock_top_n_relaxed(self, monkeypatch):
        """验证 per_stock 模式下 top_n 被自动放宽。"""
        captured_top_n = []

        def select(_asof, _ctx, params):
            captured_top_n.append(params.top_n)
            return ["A", "B"], [
                {"symbol": "A", "score": 1.0},
                {"symbol": "B", "score": 0.5},
            ]

        def decision_dates(calendar, _ctx, _params):
            return [calendar[0]]

        spec = CrossSectionStrategySpec(
            id="per_stock_topn",
            name="test",
            description="test",
            params_model=_PerStockParams,
            select=select,
            decision_dates=decision_dates,
            needs_fundamentals=False,
            requires_symbols=True,
        )

        closes = [10, 11, 12, 13, 14]
        dates_list = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
        value_a = _make_value_df(dates_list, closes)
        value_b = _make_value_df(dates_list, [20, 19, 18, 17, 16])

        monkeypatch.setattr(
            "app.backtest.cross_section_runner._resolve_universe",
            lambda *_args: (
                [{"symbol": "A", "name": "A"}, {"symbol": "B", "name": "B"}],
                "test",
            ),
        )
        monkeypatch.setattr(
            "app.backtest.cross_section_runner._load_panel",
            lambda *_args, **_kwargs: {
                "A": {"value": value_a},
                "B": {"value": value_b},
            },
        )
        monkeypatch.setattr(
            "app.backtest.cross_section_runner.fetch_a_share_daily",
            lambda symbol, start, end, data_source: {
                "A": _make_ohlcv(closes),
                "B": _make_ohlcv([20, 19, 18, 17, 16]),
            }[symbol],
        )

        request = BacktestRequest(
            strategy_id="per_stock_topn",
            mode="per_stock",
            symbols=["A", "B"],
            start_date="2024-01-02",
            end_date="2024-01-08",
            initial_cash=100_000,
            commission=0,
            min_commission=0,
            slippage=0,
            lot_size=1,
            strategy_params={"top_n": 1},
        )

        run_cross_section_per_stock_backtest(request, spec)

        # top_n 应从请求中的 1 被自动放宽为 len(symbols)=2
        assert captured_top_n == [2]

    def test_per_stock_ranking(self, monkeypatch):
        """验证 per_stock 结果按收益率降序排列。"""

        def select(_asof, _ctx, _params):
            return ["A", "B"], [
                {"symbol": "A", "score": 1.0},
                {"symbol": "B", "score": 0.5},
            ]

        def decision_dates(calendar, _ctx, _params):
            return [calendar[0]]

        spec = CrossSectionStrategySpec(
            id="per_stock_rank",
            name="test",
            description="test",
            params_model=_PerStockParams,
            select=select,
            decision_dates=decision_dates,
            needs_fundamentals=False,
            requires_symbols=True,
        )

        closes_a = [10, 11, 12, 13, 14]
        closes_b = [20, 19, 18, 17, 16]
        dates_list = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
        value_a = _make_value_df(dates_list, closes_a)
        value_b = _make_value_df(dates_list, closes_b)

        monkeypatch.setattr(
            "app.backtest.cross_section_runner._resolve_universe",
            lambda *_args: (
                [{"symbol": "A", "name": "A"}, {"symbol": "B", "name": "B"}],
                "test",
            ),
        )
        monkeypatch.setattr(
            "app.backtest.cross_section_runner._load_panel",
            lambda *_args, **_kwargs: {
                "A": {"value": value_a},
                "B": {"value": value_b},
            },
        )
        monkeypatch.setattr(
            "app.backtest.cross_section_runner.fetch_a_share_daily",
            lambda symbol, start, end, data_source: {
                "A": _make_ohlcv(closes_a),
                "B": _make_ohlcv(closes_b),
            }[symbol],
        )

        request = BacktestRequest(
            strategy_id="per_stock_rank",
            mode="per_stock",
            symbols=["A", "B"],
            start_date="2024-01-02",
            end_date="2024-01-08",
            initial_cash=100_000,
            commission=0,
            min_commission=0,
            slippage=0,
            lot_size=1,
        )

        result = run_cross_section_per_stock_backtest(request, spec)

        ok_runs = [r for r in result.runs if r.status == "ok"]
        assert ok_runs[0].rank == 1
        assert ok_runs[1].rank == 2
        # A 上涨（正收益），B 下跌（负收益），rank 1 的 total_return 应 >= rank 2
        assert ok_runs[0].metrics["total_return"] >= ok_runs[1].metrics["total_return"]


# ── 调度测试：run_backtest_request 路由 ──────────────────


class TestPerStockDispatch:
    def test_unified_runner_routes_per_stock(self, monkeypatch):
        """run_backtest_request 将 mode=per_stock 路由到正确的 runner。"""
        from app.schemas import BacktestUniverseSummary

        spec = CrossSectionStrategySpec(
            id="per_stock_route",
            name="test",
            description="test",
            params_model=_PerStockParams,
            select=lambda *_args: ([], []),
            decision_dates=lambda *_args: [],
        )
        sentinel = BacktestUniverseResponse(
            strategy_id="per_stock_route",
            summary=BacktestUniverseSummary(),
            aggregate={"name": "test", "metrics": {}, "equity": []},
        )
        monkeypatch.setattr(
            "app.backtest_runner.get_registered_strategy",
            lambda _strategy_id: spec,
        )
        monkeypatch.setattr(
            "app.backtest_runner.run_cross_section_per_stock_backtest",
            lambda request, received_spec: (
                sentinel
                if request.strategy_id == "per_stock_route" and received_spec is spec
                else None
            ),
        )

        response = run_backtest_request(
            BacktestRequest(
                strategy_id="per_stock_route",
                mode="per_stock",
                start_date="2024-01-01",
                end_date="2024-01-31",
            )
        )

        assert response == sentinel.model_dump(mode="json")

    def test_unified_runner_rejects_per_stock_for_timeseries(self, monkeypatch):
        """时序策略不支持 per_stock 模式。"""
        from app.strategies.base import StrategySpec
        from app.backtest_engine import BacktestResult

        ts_spec = StrategySpec(
            id="ts_test",
            name="test",
            description="test",
            params_model=_PerStockParams,
            min_bars=lambda _p: 5,
            run=lambda _df, _base, _params: BacktestResult(
                equity=[], trades=[], metrics={}, price=[]
            ),
        )
        monkeypatch.setattr(
            "app.backtest_runner.get_registered_strategy",
            lambda _strategy_id: ts_spec,
        )

        with pytest.raises(ValueError, match="不支持"):
            run_backtest_request(
                BacktestRequest(
                    strategy_id="ts_test",
                    mode="per_stock",
                    start_date="2024-01-01",
                    end_date="2024-01-31",
                )
            )
