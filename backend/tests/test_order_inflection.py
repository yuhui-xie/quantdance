"""订单开工拐点选股与组合回测测试（不访问外网）。"""

from __future__ import annotations

import pandas as pd

from app.portfolio.base import PortfolioSelectContext
from app.portfolio.order_inflection import OrderInflectionParams, select_order_inflection
from app.portfolio.runner import run_portfolio_request
from app.schemas import PortfolioBacktestRequest


def _value_df(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    # date, close, market_cap, pct_change
    return pd.DataFrame(
        [
            {
                "date": d,
                "close": c,
                "pct_change": pct,
                "market_cap": mv,
                "float_market_cap": mv,
                "pe_ttm": 12.0,
                "pb": 1.5,
                "peg": 0.8,
                "ps_ttm": 2.0,
            }
            for d, c, mv, pct in rows
        ]
    )


def _fin_df(
    *,
    report_date: str,
    notice_date: str,
    contract_liab_yoy: float,
    gross_margin: float,
    gross_margin_yoy_delta: float,
    netcash_operate: float,
    inventory_yoy: float,
    contract_liab: float = 1e8,
    inventory: float = 2e8,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "report_date": report_date,
                "notice_date": notice_date,
                "contract_liab": contract_liab,
                "contract_liab_yoy": contract_liab_yoy,
                "inventory": inventory,
                "inventory_yoy": inventory_yoy,
                "operate_income": 1e9,
                "operate_cost": 1e9 * (1 - gross_margin),
                "gross_margin": gross_margin,
                "gross_margin_yoy_delta": gross_margin_yoy_delta,
                "netcash_operate": netcash_operate,
                "netcash_operate_yoy": 20.0,
            }
        ]
    )


def test_select_filters_five_steps_and_ranks_by_score():
    asof = "2024-09-02"
    panel = {
        "AAA001": {  # 高分：订单大增 + 毛利改善
            "value": _value_df([(asof, 10.0, 1e9, 1.0)]),
            "financials": _fin_df(
                report_date="2024-06-30",
                notice_date="2024-08-15",
                contract_liab_yoy=80.0,
                gross_margin=0.35,
                gross_margin_yoy_delta=0.03,
                netcash_operate=5e7,
                inventory_yoy=25.0,
            ),
        },
        "BBB002": {  # 合格但分数更低
            "value": _value_df([(asof, 10.0, 2e9, 1.0)]),
            "financials": _fin_df(
                report_date="2024-06-30",
                notice_date="2024-08-15",
                contract_liab_yoy=10.0,
                gross_margin=0.25,
                gross_margin_yoy_delta=0.0,
                netcash_operate=1e7,
                inventory_yoy=5.0,
            ),
        },
        "CCC003": {  # 合同负债萎缩
            "value": _value_df([(asof, 10.0, 5e8, 1.0)]),
            "financials": _fin_df(
                report_date="2024-06-30",
                notice_date="2024-08-15",
                contract_liab_yoy=-30.0,
                gross_margin=0.40,
                gross_margin_yoy_delta=0.05,
                netcash_operate=1e8,
                inventory_yoy=10.0,
            ),
        },
        "DDD004": {  # 经营现金流为负
            "value": _value_df([(asof, 10.0, 5e8, 1.0)]),
            "financials": _fin_df(
                report_date="2024-06-30",
                notice_date="2024-08-15",
                contract_liab_yoy=50.0,
                gross_margin=0.30,
                gross_margin_yoy_delta=0.02,
                netcash_operate=-1e7,
                inventory_yoy=10.0,
            ),
        },
        "EEE005": {  # 存货暴增疑似滞销
            "value": _value_df([(asof, 10.0, 5e8, 1.0)]),
            "financials": _fin_df(
                report_date="2024-06-30",
                notice_date="2024-08-15",
                contract_liab_yoy=50.0,
                gross_margin=0.30,
                gross_margin_yoy_delta=0.02,
                netcash_operate=1e7,
                inventory_yoy=150.0,
            ),
        },
    }
    names = {k: "普通" for k in panel}

    symbols, details = select_order_inflection(
        asof,
        PortfolioSelectContext(panel=panel, names=names),
        OrderInflectionParams(top_n=2, min_contract_liab_yoy=0.0, min_gross_margin=0.15),
    )
    assert symbols == ["AAA001", "BBB002"]
    assert "CCC003" not in symbols
    assert "DDD004" not in symbols
    assert "EEE005" not in symbols
    assert details[0]["inflection_score"] >= details[1]["inflection_score"]


def test_run_order_inflection_portfolio_with_mocks(monkeypatch):
    dates = pd.bdate_range("2024-01-02", periods=40)
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]

    def make_stock(close0: float, mv0: float, cl_yoy: float) -> dict:
        rows = [(d, close0 + i * 0.01, mv0 + i, 0.5) for i, d in enumerate(date_strs)]
        return {
            "value": _value_df(rows),
            "financials": _fin_df(
                report_date="2023-12-31",
                notice_date="2024-03-20",
                contract_liab_yoy=cl_yoy,
                gross_margin=0.28,
                gross_margin_yoy_delta=0.01,
                netcash_operate=2e7,
                inventory_yoy=12.0,
            ),
        }

    panel = {
        "000001": make_stock(8.0, 1.0e9, 60.0),
        "000002": make_stock(9.0, 2.0e9, 30.0),
        "000003": make_stock(10.0, 3.0e9, 15.0),
        "000004": make_stock(11.0, 4.0e9, 5.0),
    }

    monkeypatch.setattr(
        "app.portfolio.runner.resolve_universe",
        lambda _req, default_universe=None: (
            [{"symbol": s, "name": "测试"} for s in panel],
            "mock universe",
        ),
    )
    monkeypatch.setattr(
        "app.portfolio.runner.load_fundamentals_panel",
        lambda symbols, **_kw: {
            s: {"value": panel[s]["value"], "dividend": pd.DataFrame()}
            for s in symbols
            if s in panel
        },
    )
    monkeypatch.setattr(
        "app.portfolio.runner.load_financials_panel",
        lambda symbols, **_kw: {
            s: panel[s]["financials"] for s in symbols if s in panel
        },
    )

    resp = run_portfolio_request(
        PortfolioBacktestRequest(
            strategy_id="order_inflection",
            mode="backtest",
            symbols=list(panel),
            start_date="2024-01-02",
            end_date=date_strs[-1],
            initial_cash=50_000,
            commission=0.0003,
            min_commission=0.0,
            slippage=0.0,
            strategy_params={"top_n": 2, "max_price": 50.0, "max_notice_age_days": 400},
        )
    )
    assert resp.mode == "backtest"
    assert resp.metrics["final_equity"] > 0
    assert len(resp.rebalances) >= 1
    assert len(resp.equity) == len(date_strs)
