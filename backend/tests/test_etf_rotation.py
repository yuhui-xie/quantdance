"""ETF 动量轮动策略测试（不访问外网）。

当前判断指标：LLT 拟合趋势（斜率×R²）——对最近 ``llt_window`` 根 K 线的
LLT 趋势线做最小二乘线性回归，得分 = 斜率 × R²。
"""

from __future__ import annotations

import pandas as pd

from app.strategies.registry import get_cross_section_strategy
from app.backtest_runner import run_backtest_request
from app.schemas import BacktestRequest
from app.strategies.base import CrossSectionContext
from app.strategies.cross_section.etf_rotation import (
    EtfRotationParams,
    select_etf_rotation,
)
from app.strategies.cross_section.etf_rotation import STRATEGY as ETF_ROTATION_SPEC


def _value_df(dates: list[str], closes: list[float]) -> pd.DataFrame:
    close = pd.Series(closes, dtype=float)
    return pd.DataFrame(
        {
            "date": dates,
            "close": close,
            "pct_change": close.pct_change().fillna(0.0) * 100.0,
        }
    )


def _value_df_with_volume(
    dates: list[str], closes: list[float], volumes: list[float]
) -> pd.DataFrame:
    close = pd.Series(closes, dtype=float)
    return pd.DataFrame(
        {
            "date": dates,
            "close": close,
            "volume": pd.Series(volumes, dtype=float),
            "pct_change": close.pct_change().fillna(0.0) * 100.0,
        }
    )


def _dates(n: int) -> list[str]:
    return pd.bdate_range("2024-01-02", periods=n).strftime("%Y-%m-%d").tolist()


def test_select_picks_strongest_llt_trend():
    dates = _dates(10)
    panel = {
        "510300": {"value": _value_df(dates, [10 + i * 0.1 for i in range(10)])},  # 缓升
        "510500": {"value": _value_df(dates, [10 + i * 0.2 for i in range(10)])},  # 快升
        "518880": {"value": _value_df(dates, [10 - i * 0.1 for i in range(10)])},  # 下降
    }

    symbols, details = select_etf_rotation(
        dates[-1],
        CrossSectionContext(panel=panel, names={}),
        EtfRotationParams(top_n=1, llt_period=2, llt_window=2, min_r2=0.0),
    )

    assert symbols == ["510500"]
    # targets 仍为 top_n；details 返回全部候选（供报告对比指标）。
    # 下降的 518880 因 score<=min_score 被过滤，故候选为 2 只。
    assert [c["symbol"] for c in details] == ["510500", "510300"]
    assert details[0]["score"] > 0
    assert details[0]["llt_r2"] > 0
    # 全部候选都带排名与入选标记，且仅 top_n 项 selected=True
    assert [c["rank"] for c in details] == [1, 2]
    assert [c["selected"] for c in details] == [True, False]
    # 常量参数不进入逐候选 detail（避免污染通用指标透传）
    assert "llt_period" not in details[0]


def test_select_holds_cash_when_no_etf_passes_filter():
    dates = _dates(10)
    panel = {
        "510300": {"value": _value_df(dates, [10 - i * 0.1 for i in range(10)])},
        "510500": {"value": _value_df(dates, [9 - i * 0.1 for i in range(10)])},
    }

    symbols, details = select_etf_rotation(
        dates[-1],
        CrossSectionContext(panel=panel, names={}),
        EtfRotationParams(llt_period=2, llt_window=2),
    )

    assert symbols == []
    assert details == []


def test_min_r2_filters_poor_fit():
    """拟合质量过滤：R² 低于门槛的震荡标的不纳入候选。"""
    dates = _dates(14)
    choppy = [
        10.0, 10.3, 10.1, 10.4, 10.2, 10.5, 10.3,
        10.6, 10.4, 10.7, 10.5, 10.8, 10.6, 10.9,
    ]  # 缓慢上台阶、窗口内来回震荡 → R² 低
    clean = [10 + i * 0.1 for i in range(14)]  # 单调上升 → R² 高

    # 关闭拟合质量过滤：两只都可候选，单调上升者得分更高
    symbols_off, details_off = select_etf_rotation(
        dates[-1],
        CrossSectionContext(panel={"510300": {"value": _value_df(dates, choppy)},
                                   "510500": {"value": _value_df(dates, clean)}},
                           names={}),
        EtfRotationParams(top_n=1, llt_period=2, llt_window=4, min_r2=0.0),
    )
    assert symbols_off == ["510500"]

    # 开启高 R² 门槛：震荡标的被剔除，只剩单调上升者
    symbols_on, details_on = select_etf_rotation(
        dates[-1],
        CrossSectionContext(panel={"510300": {"value": _value_df(dates, choppy)},
                                   "510500": {"value": _value_df(dates, clean)}},
                           names={}),
        EtfRotationParams(top_n=1, llt_period=2, llt_window=4, min_r2=0.5),
    )
    assert symbols_on == ["510500"]
    assert details_on[0]["llt_r2"] >= 0.5


def test_strategy_defaults_to_etf_core_universe():
    """通用配置：不强制 symbols，默认池为 config/etf_core_pool.json。"""
    assert ETF_ROTATION_SPEC.requires_symbols is False
    assert ETF_ROTATION_SPEC.default_universe == "etf_core"


def test_rotate_threshold_keeps_holding_when_not_much_worse():
    """惰性：对仍为候选的当前持仓，用其当前决策日得分对比新候选当前得分，
    不低于新得分×rotate_threshold 时保持持仓、不调仓。"""
    dates = _dates(10)
    # 510500 略快于 510300：当前得分接近但 510500 是纯 top_n 之首
    panel = {
        "510300": {"value": _value_df(dates, [10 + i * 0.1 for i in range(10)])},   # 缓升
        "510500": {"value": _value_df(dates, [10 + i * 0.11 for i in range(10)])},  # 略快升
    }
    params = EtfRotationParams(
        top_n=1, llt_period=2, llt_window=2, min_r2=0.0, rotate_threshold=0.9,
    )

    # 上一调仓日持有 510300：当前得分 ≥ 新 top（510500）当前得分×0.9 → 惰性保留
    keep_ctx = CrossSectionContext(panel=panel, names={})
    keep_ctx.cache["etf_rotation_prev_holdings"] = ["510300"]
    symbols_kept, details_kept = select_etf_rotation(dates[-1], keep_ctx, params)
    assert symbols_kept == ["510300"]
    assert next(c for c in details_kept if c["symbol"] == "510300")["selected"] is True

    # 新候选明显更优（510500 当前得分远超 510300×0.9）→ 轮动到 510500
    panel_fast = {
        "510300": {"value": _value_df(dates, [10 + i * 0.1 for i in range(10)])},
        "510500": {"value": _value_df(dates, [10 + i * 0.2 for i in range(10)])},  # 快升
    }
    rotate_ctx = CrossSectionContext(panel=panel_fast, names={})
    rotate_ctx.cache["etf_rotation_prev_holdings"] = ["510300"]
    symbols_rotated, _ = select_etf_rotation(dates[-1], rotate_ctx, params)
    assert symbols_rotated == ["510500"]


def test_rotate_threshold_1_disables_inertia():
    """rotate_threshold=1.0 关闭惰性：即使上一期持有 510300，也纯按当前得分选 510500。"""
    dates = _dates(10)
    panel = {
        "510300": {"value": _value_df(dates, [10 + i * 0.1 for i in range(10)])},
        "510500": {"value": _value_df(dates, [10 + i * 0.2 for i in range(10)])},
    }
    params = EtfRotationParams(
        top_n=1, llt_period=2, llt_window=2, min_r2=0.0, rotate_threshold=1.0,
    )
    ctx = CrossSectionContext(panel=panel, names={})
    ctx.cache["etf_rotation_prev_holdings"] = ["510300"]
    symbols, _ = select_etf_rotation(dates[-1], ctx, params)
    assert symbols == ["510500"]


def test_rotate_threshold_0_never_rotates():
    """rotate_threshold=0 时几乎永不调仓：当前得分≥0 即保留当前持仓。"""
    dates = _dates(10)
    panel = {
        "510300": {"value": _value_df(dates, [10 + i * 0.1 for i in range(10)])},
        "510500": {"value": _value_df(dates, [10 + i * 0.2 for i in range(10)])},
    }
    params = EtfRotationParams(
        top_n=1, llt_period=2, llt_window=2, min_r2=0.0, rotate_threshold=0.0,
    )
    ctx = CrossSectionContext(panel=panel, names={})
    ctx.cache["etf_rotation_prev_holdings"] = ["510300"]
    symbols, _ = select_etf_rotation(dates[-1], ctx, params)
    assert symbols == ["510300"]  # 保留，不轮动到更优的 510500


def test_volume_confirm_default_off_ignores_volume():
    """默认 volume_confirm=False：保持纯 LLT 趋势，成交量列不影响选股。"""
    dates = _dates(14)
    a_closes = [10 + i * 0.1 for i in range(14)]  # 稳步上涨，量能放大
    b_closes = [10, 10.2, 10.5, 10.9, 11.4, 12.0, 12.7, 13.3, 14.0, 14.6, 15.2, 14.8, 14.4, 14.1]  # 冲高后回落，量能平淡
    panel = {
        "510300": {"value": _value_df_with_volume(dates, a_closes, [100 + i * 5 for i in range(14)])},
        "510500": {"value": _value_df_with_volume(dates, b_closes, [100] * 14)},
    }

    symbols, details = select_etf_rotation(
        dates[-1],
        CrossSectionContext(panel=panel, names={}),
        EtfRotationParams(top_n=1, llt_period=5, llt_window=8, min_r2=0.0),
    )

    # 按 LLT 得分选 B（累计涨幅更大、得分更高），成交量列被忽略
    assert symbols == ["510500"]
    assert details[0]["score"] > 0


def test_volume_confirm_filters_low_volume_etf():
    """开启 volume_confirm=True：VPT 量价趋势斜率为负的 B 被过滤，改选 A。"""
    dates = _dates(14)
    a_closes = [10 + i * 0.1 for i in range(14)]
    b_closes = [10, 10.2, 10.5, 10.9, 11.4, 12.0, 12.7, 13.3, 14.0, 14.6, 15.2, 14.8, 14.4, 14.1]
    panel = {
        "510300": {"value": _value_df_with_volume(dates, a_closes, [100 + i * 5 for i in range(14)])},
        "510500": {"value": _value_df_with_volume(dates, b_closes, [100] * 14)},
    }

    symbols, details = select_etf_rotation(
        dates[-1],
        CrossSectionContext(panel=panel, names={}),
        EtfRotationParams(
            top_n=1,
            llt_period=5,
            llt_window=8,
            min_r2=0.0,
            volume_confirm=True,
            volume_days=3,
        ),
    )

    assert symbols == ["510300"]
    assert details[0]["vpt_slope"] > 0


def test_run_etf_rotation_backtest_with_market_data(monkeypatch):
    dates = pd.bdate_range("2024-01-02", periods=80)
    symbols = ["510300", "510500", "518880"]
    closes = {
        "510300": [10 + i * 0.02 for i in range(80)],
        "510500": [10 + i * 0.04 for i in range(80)],
        "518880": [10 + i * 0.01 for i in range(80)],
    }

    def fake_daily(symbol: str, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame(
            {"close": closes[symbol]},
            index=dates,
        )

    monkeypatch.setattr("app.backtest.cross_section_runner.fetch_a_share_daily", fake_daily)
    monkeypatch.setattr(
        "app.backtest.cross_section_runner._resolve_universe",
        lambda _req, default_universe=None, **_kwargs: (
            [{"symbol": symbol, "name": symbol} for symbol in symbols],
            "mock ETF pool",
        ),
    )
    monkeypatch.setattr(
        "app.backtest.cross_section_runner.attach_hs300_benchmark",
        lambda raw, **_kwargs: raw,
    )

    response = run_backtest_request(
        BacktestRequest(
            strategy_id="etf_rotation",
            mode="universe",
            symbols=symbols,
            start_date=dates[20].strftime("%Y-%m-%d"),
            end_date=dates[-1].strftime("%Y-%m-%d"),
            slippage=0.0,
            min_commission=0.0,
            strategy_params={
                "top_n": 1,
                "llt_period": 5,
                "llt_window": 10,
            },
        )
    )

    assert get_cross_section_strategy("etf_rotation") is not None
    assert response["strategy_id"] == "etf_rotation"
    assert response["rebalances"]
    assert response["metrics"]["final_equity"] > 0
