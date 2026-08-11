"""三因子ETF轮动策略：因子计算、Z-Score 标准化与调仓阈值滞后带测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd

import app.strategies.cross_section.etf_rotation_3factor as mod
from app.factors.cross_section import zscore
from app.factors.momentum import bias_momentum, efficiency_momentum, slope_momentum
from app.strategies.base import CrossSectionContext
from app.strategies.cross_section.etf_rotation_3factor import (
    STRATEGY,
    select_three_factor,
)


def _value_df(closes: list[float]) -> pd.DataFrame:
    """构造含 OHLC 的日线 value_df，索引为工作日，末行为可交易截面。"""
    dates = pd.bdate_range("2023-01-02", periods=len(closes))
    dates = [d.strftime("%Y-%m-%d") for d in dates]
    closes = [float(c) for c in closes]
    df = pd.DataFrame(
        {
            "date": dates,
            "open": [c * 0.99 for c in closes],
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.98 for c in closes],
            "close": closes,
        }
    )
    df["pct_change"] = df["close"].pct_change() * 100.0
    return df


def _make_panel(prices: dict[str, list[float]]) -> tuple[CrossSectionContext, str]:
    panel: dict[str, dict[str, pd.DataFrame]] = {}
    names: dict[str, str] = {}
    last_date = ""
    for symbol, closes in prices.items():
        df = _value_df(closes)
        panel[symbol] = {"value": df}
        names[symbol] = f"名称{symbol}"
        last_date = df["date"].iloc[-1]
    return CrossSectionContext(panel=panel, names=names), last_date


# ── 因子计算 ──


def test_bias_momentum_trend_detection():
    # 加速上涨（偏离均线扩大）→ 正；减速上涨（偏离收敛）→ 负
    accelerating = np.exp(np.linspace(0.0, 1.0, 80) ** 2)
    decelerating = np.sqrt(np.linspace(1.0, 4.0, 80))
    assert bias_momentum(accelerating, 20, 25) > 0
    assert bias_momentum(decelerating, 20, 25) < 0
    # 历史不足返回 None
    assert bias_momentum(np.linspace(1, 2, 10), 20, 25) is None


def test_slope_momentum_trend_detection():
    up = np.linspace(1.0, 2.0, 80)
    down = np.linspace(2.0, 1.0, 80)
    assert slope_momentum(up, 60) > 0
    assert slope_momentum(down, 60) < 0
    assert slope_momentum(np.linspace(1, 2, 10), 60) is None


def test_efficiency_momentum_trend_detection():
    up = _value_df(list(np.linspace(1.0, 2.0, 80)))
    assert efficiency_momentum(up, 60) > 0
    down = _value_df(list(np.linspace(2.0, 1.0, 80)))
    assert efficiency_momentum(down, 60) < 0


def test_zscore_standardization():
    z = zscore([1.0, 2.0, 3.0])
    assert len(z) == 3
    assert abs(sum(z)) < 1e-9  # 均值 ≈ 0
    assert z[2] > z[0]  # 输入越大 z 越大
    # 方差为 0 或样本不足 → 全 0
    assert zscore([1.0, 1.0, 1.0]) == [0.0, 0.0, 0.0]
    assert zscore([1.0]) == [0.0]


# ── 端到端选股（真实因子）──


def test_select_picks_strongest_rising_symbol():
    ctx, asof = _make_panel(
        {
            "A": list(np.linspace(10, 20, 80)),  # 陡峭上涨
            "B": list(np.linspace(10, 11, 80)),  # 温和上涨
            "C": list(np.linspace(10, 9, 80)),  # 缓慢下跌
        }
    )
    params = STRATEGY.params_model(rebalance_threshold=1.0)  # 关闭滞后
    targets, details = select_three_factor(asof, ctx, params)
    assert targets == ["A"]
    held = [d for d in details if d["held"]]
    assert [d["symbol"] for d in held] == ["A"]
    # 三因子与 final_score 均已填充
    assert all({"bias_score", "slope_score", "efficiency_score", "final_score"} <= set(d) for d in details)


def test_select_single_candidate_holds_it():
    ctx, asof = _make_panel({"A": list(np.linspace(10, 12, 80))})
    targets, details = select_three_factor(asof, ctx, STRATEGY.params_model())
    assert targets == ["A"]
    assert details[0]["held"] is True


# ── 调仓阈值滞后带（用 mock Z-Score 精确控制 final_score）──

# 候选顺序按 panel 插入顺序：A, B, C
# 每行 = [zA, zB, zC]（等权下 final_score == z）


def test_rebalance_threshold_hysteresis(monkeypatch):
    ctx, asof = _make_panel(
        {
            "A": list(np.linspace(10, 12, 80)),
            "B": list(np.linspace(10, 11, 80)),
            "C": list(np.linspace(10, 9, 80)),
        }
    )
    current = [1.0, 0.5, -1.0]

    def fake_zscore(values):
        return [float(x) for x in current][: len(values)]

    monkeypatch.setattr(mod, "zscore", fake_zscore)
    params = STRATEGY.params_model(rebalance_threshold=1.5)

    # 第 1 次：A 最高 → 持有 A
    current[:] = [1.0, 0.5, -1.0]
    targets1, details1 = select_three_factor(asof, ctx, params)
    assert targets1 == ["A"]
    assert details1[0]["held"] is True

    # 第 2 次：B 成为最高，但未超过 A 得分的 1.5 倍 → 粘性保留 A
    current[:] = [0.9, 1.1, -2.0]
    targets2, _ = select_three_factor(asof, ctx, params)
    assert targets2 == ["A"]

    # 第 3 次：B 超过 A 得分的 1.5 倍 → 切换为 B
    current[:] = [0.9, 2.0, -3.0]
    targets3, details3 = select_three_factor(asof, ctx, params)
    assert targets3 == ["B"]
    assert details3[0]["symbol"] == "B"
    assert details3[0]["held"] is True


def test_rebalance_threshold_holds_cash_when_all_negative(monkeypatch):
    ctx, asof = _make_panel(
        {
            "A": list(np.linspace(10, 12, 80)),
            "B": list(np.linspace(10, 11, 80)),
            "C": list(np.linspace(10, 9, 80)),
        }
    )

    def fake_zscore(values):
        return [-1.0, -2.0, -3.0][: len(values)]

    monkeypatch.setattr(mod, "zscore", fake_zscore)
    targets, details = select_three_factor(
        asof, ctx, STRATEGY.params_model(rebalance_threshold=1.5)
    )
    assert targets == []
    assert all(not d["held"] for d in details)


def test_threshold_1_0_disables_hysteresis(monkeypatch):
    """rebalance_threshold=1.0 时关闭滞后，始终选当前最高分。"""
    ctx, asof = _make_panel(
        {
            "A": list(np.linspace(10, 12, 80)),
            "B": list(np.linspace(10, 11, 80)),
            "C": list(np.linspace(10, 9, 80)),
        }
    )
    current = [1.0, 0.5, -1.0]

    def fake_zscore(values):
        return [float(x) for x in current][: len(values)]

    monkeypatch.setattr(mod, "zscore", fake_zscore)
    params = STRATEGY.params_model(rebalance_threshold=1.0)

    current[:] = [1.0, 0.5, -1.0]
    assert select_three_factor(asof, ctx, params)[0] == ["A"]
    # 即便 A 仍是持仓，B 更高也会立即切换（阈值关闭）
    current[:] = [0.5, 1.0, -1.0]
    assert select_three_factor(asof, ctx, params)[0] == ["B"]
