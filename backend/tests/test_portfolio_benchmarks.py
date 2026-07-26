"""组合沪深300基准（不访问外网）。"""

from __future__ import annotations

import pandas as pd

from app.portfolio.benchmarks import attach_hs300_benchmark, build_hs300_benchmark


def test_build_hs300_benchmark_aligns_and_normalizes(monkeypatch):
    idx = pd.DataFrame(
        {
            "date": ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"],
            "close": [4000.0, 4040.0, 4080.0, 4120.0],
        }
    )
    monkeypatch.setattr(
        "app.portfolio.benchmarks.fetch_hs300_index_daily",
        lambda start, end: idx,
    )
    bm = build_hs300_benchmark(
        ["2020-01-02", "2020-01-03", "2020-01-07"],
        initial_cash=100_000.0,
    )
    assert bm is not None
    assert bm["name"] == "沪深300"
    assert abs(bm["equity"][0]["nav"] - 1.0) < 1e-9
    assert abs(bm["equity"][-1]["nav"] - 4120.0 / 4000.0) < 1e-9
    assert abs(bm["metrics"]["total_return"] - (4120.0 / 4000.0 - 1.0)) < 1e-9


def test_attach_hs300_benchmark_skips_when_present():
    out = {
        "equity": [{"date": "2020-01-02", "equity": 1.0}],
        "metrics": {},
        "benchmarks": {"hs300": {"name": "已有"}},
        "warnings": [],
    }
    got = attach_hs300_benchmark(out)
    assert got["benchmarks"]["hs300"]["name"] == "已有"


def test_attach_hs300_benchmark_records_warning_on_failure(monkeypatch):
    monkeypatch.setattr(
        "app.portfolio.benchmarks.build_hs300_benchmark",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = {
        "equity": [
            {"date": "2020-01-02", "equity": 100000.0},
            {"date": "2020-01-03", "equity": 101000.0},
        ],
        "metrics": {"initial_cash": 100000.0, "total_return": 0.01},
        "warnings": [],
    }
    got = attach_hs300_benchmark(out)
    assert any("沪深300" in w for w in got["warnings"])
