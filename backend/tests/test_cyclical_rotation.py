"""顺周期行业轮动策略测试：状态机、周期入场/离场、组合构建与 screen 编排。"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from app.backtest.cross_section_runner import run_cross_section_screen
from app.schemas import BacktestRequest
from app.strategies.base import CrossSectionContext
from app.strategies.cross_section.cyclical_rotation import (
    STRATEGY,
    CycleGroup,
    CyclicalRotationParams,
    select_cyclical_rotation,
)

ALL_SYMBOLS = ["518880", "161226", "512400", "501018", "515220", "159985", "159825"]


def _dates(n: int, start: str = "2024-01-02") -> list[str]:
    """生成连续交易日（跳过周末）日期字符串列表。"""
    d = dt.date.fromisoformat(start)
    out: list[str] = []
    i = 0
    while len(out) < n:
        day = d + dt.timedelta(days=i)
        if day.weekday() < 5:
            out.append(day.isoformat())
        i += 1
    return out


def _value(dates: list[str], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"date": list(dates), "close": list(closes)})


def _panel(specs: dict[str, list[float]], dates: list[str]) -> dict[str, dict[str, pd.DataFrame]]:
    return {sym: {"value": _value(dates, closes)} for sym, closes in specs.items()}


def _params(**overrides) -> CyclicalRotationParams:
    base = dict(
        decision_frequency="monthly",
        top_n_per_cycle=1,
        signal_momentum_days=2,
        signal_trend_days=20,
        exit_ma_days=5,
        exit_drawdown_pct=20.0,
        energy_entry_drawdown_pct=30.0,
        min_cycle_days=5,
        exclude_limit=False,
        exclude_suspended=False,
    )
    base.update(overrides)
    return CyclicalRotationParams(**base)


def _rise(n: int, start: float = 10.0, step: float = 0.5) -> list[float]:
    return [start + i * step for i in range(n)]


def _flat(n: int, v: float = 10.0) -> list[float]:
    return [v] * n


def _crash_recover(n: int = 24) -> list[float]:
    """24 根：高位平台 → 暴跌（-40%）→ 强反弹，末根走强且窗口内有深跌。"""
    return [25.0] * 10 + [15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28][: n - 10]


def _gentle_rise(n: int = 24) -> list[float]:
    """24 根：慢牛，20 根窗口内区间小（无深跌），末根走强。"""
    body = [32.0 + i * 0.25 for i in range(n - 3)]
    return [30.0, 31.0, 32.0] + body


def _rise_then_crash(n: int = 26) -> list[float]:
    """26 根：前半段上行，第 20 根见顶后连续下跌。"""
    rise = [10.0 + 0.5 * i for i in range(19)]
    crash = [19.5 - 0.8 * i for i in range(n - 19)]
    return rise + crash


# ────────────────────────── 周期状态机 ──────────────────────────


def test_nonferrous_activates_on_gold_uptrend():
    """金银上行 → 有色周期激活，买入持仓池龙头。"""
    dates = _dates(24)
    panel = _panel(
        {
            "518880": _rise(24),
            "161226": _rise(24, start=5.0, step=0.1),
            "512400": _rise(24, start=1.0, step=0.1),
            "501018": _flat(24, 8.0),   # 能源信号走平
            "515220": _flat(24, 2.0),
            "159985": _flat(24, 4.0),   # 农业走平（且能源未激活）
            "159825": _flat(24, 1.0),
        },
        dates,
    )
    targets, details = select_cyclical_rotation(
        dates[-1], CrossSectionContext(panel=panel, names={}), _params()
    )
    assert targets == ["512400"]
    assert details[0]["cycle_id"] == "nonferrous"
    assert details[0]["cycle_name"] == "有色金属"


def test_nonferrous_exits_after_gold_breakdown():
    """金银见顶下跌、超出保护期 → 有色清仓；保护期内不因短期回摆离场。"""
    dates = _dates(26)
    gold = _rise_then_crash(26)
    panel = _panel(
        {
            "518880": gold,
            "161226": _flat(26, 5.0),  # 白银走平，不维持周期
            "512400": _rise(26, start=1.0, step=0.1),
            "501018": _flat(26, 8.0),
            "515220": _flat(26, 2.0),
            "159985": _flat(26, 4.0),
            "159825": _flat(26, 1.0),
        },
        dates,
    )
    params = _params(min_cycle_days=5)
    ctx = CrossSectionContext(panel=panel, names={})
    t19, _ = select_cyclical_rotation(dates[19], ctx, params)  # 上行见顶前入场
    t23, _ = select_cyclical_rotation(dates[23], ctx, params)  # 已转弱，保护期内
    t25, _ = select_cyclical_rotation(dates[25], ctx, params)  # 超出保护期 → 离场
    assert t19 == ["512400"]
    assert t23 == ["512400"]
    assert t25 == []


def test_energy_requires_prior_deep_drawdown():
    """原油慢牛、窗口内从未深跌 30% → 能源不入场（近似“布伦特跌穿60”门槛）。"""
    dates = _dates(24)
    panel = _panel(
        {
            "501018": _gentle_rise(24),
            "515220": _rise(24, start=2.0, step=0.1),
            "518880": _flat(24, 6.0),
            "161226": _flat(24, 3.0),
            "159985": _flat(24, 4.0),
            "512400": _flat(24, 1.0),
            "159825": _flat(24, 1.0),
        },
        dates,
    )
    targets, _ = select_cyclical_rotation(
        dates[-1], CrossSectionContext(panel=panel, names={}), _params()
    )
    assert targets == []


def test_energy_enters_after_drawdown_recovery():
    """原油先深跌（≥30%）后强反弹 → 能源入场。"""
    dates = _dates(24)
    panel = _panel(
        {
            "501018": _crash_recover(24),
            "515220": _rise(24, start=2.0, step=0.1),
            "518880": _flat(24, 6.0),
            "161226": _flat(24, 3.0),
            "159985": _flat(24, 4.0),
            "512400": _flat(24, 1.0),
            "159825": _flat(24, 1.0),
        },
        dates,
    )
    targets, details = select_cyclical_rotation(
        dates[-1], CrossSectionContext(panel=panel, names={}), _params()
    )
    assert targets == ["515220"]
    assert details[0]["cycle_id"] == "energy"


def test_agriculture_blocked_without_energy_active():
    """能源周期未激活 → 农业即使信号走强也不入场。"""
    dates = _dates(24)
    panel = _panel(
        {
            "159985": _rise(24, start=3.0, step=0.1),
            "159825": _rise(24, start=1.0, step=0.1),
            "501018": _flat(24, 8.0),   # 能源信号走平，未激活
            "515220": _flat(24, 2.0),
            "518880": _flat(24, 6.0),
            "161226": _flat(24, 3.0),
            "512400": _flat(24, 1.0),
        },
        dates,
    )
    targets, _ = select_cyclical_rotation(
        dates[-1], CrossSectionContext(panel=panel, names={}), _params()
    )
    assert "159825" not in targets


def test_agriculture_enters_when_energy_active():
    """能源激活后（石油启动），农业信号走强即可入场。"""
    dates = _dates(24)
    panel = _panel(
        {
            "501018": _crash_recover(24),   # 能源先激活
            "515220": _rise(24, start=2.0, step=0.1),
            "159985": _rise(24, start=3.0, step=0.1),
            "159825": _rise(24, start=1.0, step=0.1),
            "518880": _flat(24, 6.0),
            "161226": _flat(24, 3.0),
            "512400": _flat(24, 1.0),
        },
        dates,
    )
    targets, details = select_cyclical_rotation(
        dates[-1], CrossSectionContext(panel=panel, names={}), _params()
    )
    assert "515220" in targets
    assert "159825" in targets
    assert {d["cycle_id"] for d in details} == {"energy", "agriculture"}


def test_all_cycles_off_holds_cash():
    """所有周期信号均不满足 → 持现金。"""
    dates = _dates(24)
    panel = _panel(
        {
            "518880": _flat(24, 6.0),
            "161226": _flat(24, 3.0),
            "501018": _flat(24, 8.0),
            "159985": _flat(24, 4.0),
            "512400": _flat(24, 1.0),
            "515220": _flat(24, 2.0),
            "159825": _flat(24, 1.0),
        },
        dates,
    )
    targets, details = select_cyclical_rotation(
        dates[-1], CrossSectionContext(panel=panel, names={}), _params()
    )
    assert targets == []
    assert details == []


# ────────────────────────── 组合构建 ──────────────────────────


def test_top_n_per_cycle_selects_strongest_holding():
    """周期内按动量选 top_n_per_cycle 龙头。"""
    cycle = CycleGroup(
        id="nonferrous", name="有色", signal_symbols=["518880"], holdings=["512400", "159881"]
    )
    dates = _dates(24)
    panel = _panel(
        {
            "518880": _rise(24),
            "512400": _rise(24, start=1.0, step=0.05),  # 动量较小
            "159881": _rise(24, start=1.0, step=0.2),   # 动量更大
        },
        dates,
    )
    targets, details = select_cyclical_rotation(
        dates[-1], CrossSectionContext(panel=panel, names={}), _params(cycles=[cycle])
    )
    assert targets == ["159881"]
    assert details[0]["momentum"] > 0


def test_allocation_all_holds_all_active_cycles():
    """allocation=all：所有激活周期的持仓一起持有。"""
    c1 = CycleGroup(id="a", name="A", signal_symbols=["S1"], holdings=["H1"])
    c2 = CycleGroup(id="b", name="B", signal_symbols=["S2"], holdings=["H2"])
    dates = _dates(24)
    panel = _panel(
        {
            "S1": _rise(24),
            "S2": _rise(24, start=20.0, step=1.0),
            "H1": _rise(24, start=1.0, step=0.1),
            "H2": _rise(24, start=1.0, step=0.1),
        },
        dates,
    )
    params = _params(allocation="all", cycles=[c1, c2])
    targets, details = select_cyclical_rotation(
        dates[-1], CrossSectionContext(panel=panel, names={}), params
    )
    assert set(targets) == {"H1", "H2"}
    assert len(details) == 2


def test_allocation_strongest_selects_one_cycle():
    """allocation=strongest：只持信号强度最高的周期。"""
    c1 = CycleGroup(id="a", name="A", signal_symbols=["S1"], holdings=["H1"])
    c2 = CycleGroup(id="b", name="B", signal_symbols=["S2"], holdings=["H2"])
    dates = _dates(24)
    panel = _panel(
        {
            "S1": _rise(24, start=10.0, step=0.1),  # 动量较小
            "S2": _rise(24, start=20.0, step=1.0),  # 动量更大
            "H1": _rise(24, start=1.0, step=0.1),
            "H2": _rise(24, start=1.0, step=0.1),
        },
        dates,
    )
    params = _params(allocation="strongest", cycles=[c1, c2])
    targets, details = select_cyclical_rotation(
        dates[-1], CrossSectionContext(panel=panel, names={}), params
    )
    assert targets == ["H2"]
    assert details[0]["cycle_id"] == "b"


def test_missing_signal_symbol_keeps_cycle_inactive():
    """信号资产数据缺失 → 其余可用信号正常判定，周期不因缺失符号失败。"""
    cycle = CycleGroup(
        id="nonferrous", name="有色", signal_symbols=["518880", "MISSING"], holdings=["512400"]
    )
    dates = _dates(24)
    panel = _panel(
        {"518880": _rise(24), "512400": _rise(24, start=1.0, step=0.1)},
        dates,
    )
    targets, _ = select_cyclical_rotation(
        dates[-1], CrossSectionContext(panel=panel, names={}), _params(cycles=[cycle])
    )
    assert targets == ["512400"]


# ────────────────────────── 编排与注册 ──────────────────────────


def test_screen_mode_via_runner(monkeypatch):
    """screen 模式走 runner：黄金上行 → 有色入选。"""
    dates = _dates(24)
    panel = {
        "518880": {"value": _value(dates, _rise(24))},
        "161226": {"value": _value(dates, _rise(24, start=5.0, step=0.1))},
        "512400": {"value": _value(dates, _rise(24, start=1.0, step=0.1))},
        "501018": {"value": _value(dates, _flat(24, 8.0))},
        "515220": {"value": _value(dates, _flat(24, 2.0))},
        "159985": {"value": _value(dates, _flat(24, 4.0))},
        "159825": {"value": _value(dates, _flat(24, 1.0))},
    }
    symbols = [{"symbol": s, "name": s} for s in ALL_SYMBOLS]
    monkeypatch.setattr(
        "app.backtest.cross_section_runner._resolve_universe",
        lambda *_args: (symbols, "test"),
    )
    monkeypatch.setattr(
        "app.backtest.cross_section_runner._load_panel",
        lambda *_args, **_kwargs: panel,
    )
    request = BacktestRequest(
        strategy_id="cyclical_rotation",
        mode="screen",
        symbols=list(ALL_SYMBOLS),
        end_date=dates[-1],
        initial_cash=100_000,
        strategy_params={
            "signal_trend_days": 20,
            "exit_ma_days": 5,
            "signal_momentum_days": 2,
            "min_cycle_days": 5,
            "exclude_limit": False,
            "exclude_suspended": False,
        },
    )
    result = run_cross_section_screen(request, STRATEGY)
    assert [h["symbol"] for h in result.holdings] == ["512400"]
    assert result.asof == dates[-1]


def test_registry_metadata():
    """注册元数据：内置默认池、无需基本面、允许免 symbols 运行。"""
    assert STRATEGY.id == "cyclical_rotation"
    assert STRATEGY.requires_symbols is False
    assert STRATEGY.needs_fundamentals is False
    assert set(STRATEGY.default_symbols) == set(ALL_SYMBOLS)
