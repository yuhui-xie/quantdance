"""东财基本面工具函数测试（不访问外网）。"""

from __future__ import annotations

import pandas as pd

from app.data_sources.em_fundamentals import (
    _normalize_dividends,
    trailing_dividend_yield,
)


def test_normalize_dividends_from_cache_columns():
    raw = pd.DataFrame(
        {
            "ex_date": ["2024-06-07", "2023-07-18"],
            "cash_per_share": [0.2309, 0.2222],
        }
    )
    out = _normalize_dividends(raw)
    assert len(out) == 2
    assert float(out.iloc[-1]["cash_per_share"]) == 0.2309


def test_normalize_dividends_from_cninfo_columns():
    raw = pd.DataFrame(
        {
            "除权日": ["2024-06-07", None],
            "派息比例": [2.309, 1.0],
            "派息日": ["2024-06-07", "2023-01-01"],
        }
    )
    out = _normalize_dividends(raw)
    # 除权日缺失时回退到派息日，两行都应保留
    assert len(out) == 2
    by_date = {str(r.ex_date)[:10]: float(r.cash_per_share) for r in out.itertuples()}
    assert abs(by_date["2024-06-07"] - 0.2309) < 1e-6
    assert abs(by_date["2023-01-01"] - 0.1) < 1e-6


def test_trailing_dividend_yield():
    div = pd.DataFrame(
        {
            "ex_date": ["2024-06-07", "2023-07-18"],
            "cash_per_share": [0.23, 0.22],
        }
    )
    dy = trailing_dividend_yield(div, asof="2024-12-31", price=5.0)
    assert dy is not None
    assert abs(dy - 0.23 / 5.0) < 1e-9
