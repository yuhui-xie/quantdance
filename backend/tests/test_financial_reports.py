"""财报规范化与 asof 对齐测试（不访问外网）。"""

from __future__ import annotations

import pandas as pd

from app.data_sources.financial_reports import (
    asof_financial_row,
    normalize_financial_reports,
)


def _balance_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "REPORT_DATE": ["2024-06-30", "2023-06-30", "2024-03-31"],
            "NOTICE_DATE": ["2024-08-20", "2023-08-18", "2024-04-25"],
            "CONTRACT_LIAB": [2.0e8, 1.0e8, 1.5e8],
            "CONTRACT_LIAB_YOY": [100.0, 20.0, 50.0],
            "INVENTORY": [3.0e8, 2.5e8, 2.8e8],
            "INVENTORY_YOY": [20.0, 10.0, 15.0],
        }
    )


def _profit_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "REPORT_DATE": ["2024-06-30", "2023-06-30", "2024-03-31"],
            "NOTICE_DATE": ["2024-08-20", "2023-08-18", "2024-04-25"],
            "OPERATE_INCOME": [10.0e8, 8.0e8, 4.0e8],
            "OPERATE_COST": [6.0e8, 5.2e8, 2.6e8],
        }
    )


def _cash_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "REPORT_DATE": ["2024-06-30", "2023-06-30", "2024-03-31"],
            "NOTICE_DATE": ["2024-08-20", "2023-08-18", "2024-04-25"],
            "NETCASH_OPERATE": [1.2e8, 0.8e8, 0.5e8],
            "NETCASH_OPERATE_YOY": [50.0, 10.0, 5.0],
        }
    )


def test_normalize_computes_gross_margin_and_yoy_delta():
    out = normalize_financial_reports(_balance_rows(), _profit_rows(), _cash_rows())
    assert set(["report_date", "gross_margin", "gross_margin_yoy_delta"]).issubset(out.columns)
    mid = out[out["report_date"] == "2024-06-30"].iloc[0]
    assert abs(float(mid["gross_margin"]) - 0.4) < 1e-9
    # 2023H1 毛利率 = (8-5.2)/8 = 0.35；delta = 0.05
    assert abs(float(mid["gross_margin_yoy_delta"]) - 0.05) < 1e-9
    assert float(mid["contract_liab_yoy"]) == 100.0


def test_asof_uses_notice_date_not_report_date():
    out = normalize_financial_reports(_balance_rows(), _profit_rows(), _cash_rows())
    # 报告期末 2024-06-30，但 2024-07-01 尚未公告
    early = asof_financial_row(out, "2024-07-01", max_notice_age_days=400)
    assert early is not None
    assert early["report_date"] == "2024-03-31"

    late = asof_financial_row(out, "2024-08-21", max_notice_age_days=400)
    assert late is not None
    assert late["report_date"] == "2024-06-30"


def test_asof_respects_max_notice_age():
    out = normalize_financial_reports(_balance_rows(), _profit_rows(), _cash_rows())
    stale = asof_financial_row(out, "2025-06-01", max_notice_age_days=30)
    assert stale is None
