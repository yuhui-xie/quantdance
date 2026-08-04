from __future__ import annotations

import pytest

from app.backtest_aggregate import equal_weight_equity_curve


def test_equal_weight_equity_curve_normalizes_independent_capital():
    aggregate = equal_weight_equity_curve(
        [
            [
                {"date": "2024-01-02", "equity": 100_000},
                {"date": "2024-01-03", "equity": 110_000},
            ],
            [
                {"date": "2024-01-02", "equity": 100_000},
                {"date": "2024-01-03", "equity": 90_000},
            ],
        ],
        initial_cash=100_000,
    )

    assert aggregate is not None
    assert aggregate["equity"][-1]["equity"] == pytest.approx(100_000)
    assert aggregate["metrics"]["total_return"] == pytest.approx(0.0)
    assert aggregate["metrics"]["member_count"] == 2.0


def test_equal_weight_equity_curve_uses_available_members_by_date():
    aggregate = equal_weight_equity_curve(
        [
            [
                {"date": "2024-01-02", "equity": 100_000},
                {"date": "2024-01-03", "equity": 110_000},
            ],
            [
                {"date": "2024-01-03", "equity": 100_000},
                {"date": "2024-01-04", "equity": 120_000},
            ],
        ],
        initial_cash=100_000,
    )

    assert aggregate is not None
    values = {row["date"]: row["equity"] for row in aggregate["equity"]}
    assert values["2024-01-02"] == pytest.approx(100_000)
    assert values["2024-01-03"] == pytest.approx(105_000)
    assert values["2024-01-04"] == pytest.approx(120_000)
