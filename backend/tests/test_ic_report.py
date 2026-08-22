"""因子 IC 报告渲染测试：纯内存构造，不访问网络。"""

from __future__ import annotations

import json

from app.ic_report import render_factor_ic_html


def _sample_out() -> dict:
    """构造一份 compute_factor_ic 形状的输出 dict（2 因子 × 2 持有期）。"""
    return {
        "symbols": ["000001", "600000"],
        "count": 2,
        "start_date": "2024-01-01",
        "end_date": "2024-06-30",
        "horizons": [5, 10],
        "min_cs": 2,
        "universe_note": "测试池",
        "warnings": ["某因子无有效标的"],
        "disclaimer": "演示用途，IC 分析不构成投资建议。",
        "results": [
            {
                "factor": "llt_slope",
                "source": "strategy",
                "horizons": [
                    {
                        "horizon": 5,
                        "n_dates": 3,
                        "mean_ic": 0.05,
                        "std_ic": 0.02,
                        "icir": 2.5,
                        "t_stat": 4.33,
                        "ic_positive_ratio": 0.6667,
                        "ic_series": [
                            {"date": "2024-01-05", "ic": 0.03},
                            {"date": "2024-01-06", "ic": 0.06},
                            {"date": "2024-01-07", "ic": 0.06},
                        ],
                    },
                    {
                        "horizon": 10,
                        "n_dates": 2,
                        "mean_ic": -0.02,
                        "std_ic": 0.01,
                        "icir": -2.0,
                        "t_stat": -2.83,
                        "ic_positive_ratio": 0.5,
                        "ic_series": [
                            {"date": "2024-01-05", "ic": -0.01},
                            {"date": "2024-01-06", "ic": -0.03},
                        ],
                    },
                ],
            },
            {
                "factor": "adx",
                "source": "strategy",
                "horizons": [
                    {
                        "horizon": 5,
                        "n_dates": 0,
                        "mean_ic": None,
                        "std_ic": None,
                        "icir": None,
                        "t_stat": None,
                        "ic_positive_ratio": None,
                        "ic_series": [],
                    }
                ],
            },
        ],
    }


def test_render_factor_ic_html_writes_self_contained_file(tmp_path) -> None:
    dest = tmp_path / "ic"
    path = render_factor_ic_html(_sample_out(), dest)

    # 自动补 .html 后缀
    assert path == dest.with_suffix(".html")
    assert path.exists()
    html = path.read_text(encoding="utf-8")

    # 表头列与要求对齐（HTML 中 > 转义为 &gt;）
    for col in ("因子", "持有期", "均值 RankIC", "RankICIR", "t 值", "IC&gt;0 占比", "期数"):
        assert col in html

    # __DATA__ 已替换为真实 JSON payload
    assert "__DATA__" not in html
    payload_start = html.index("const DATA=")
    payload_json = html[payload_start + len("const DATA=") :].split(";")[0]
    data = json.loads(payload_json)
    assert data["count"] == 2
    assert data["results"][0]["factor"] == "llt_slope"

    # 元信息写入
    assert "测试池" in html
    assert "演示用途" in html


def test_render_factor_ic_html_handles_no_data(tmp_path) -> None:
    out = {
        "count": 0,
        "start_date": None,
        "end_date": None,
        "horizons": [5],
        "min_cs": 2,
        "results": [],
        "warnings": [],
        "disclaimer": "演示用途",
    }
    path = render_factor_ic_html(out, tmp_path / "empty")
    assert path.exists()
    html = path.read_text(encoding="utf-8")
    assert "__DATA__" not in html
    assert "无可用序列" in html
