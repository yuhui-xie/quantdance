"""etf_rotation 动量参数网格实验（新 16 只 etf_core 池）。

围绕基线请求逐一扫描动量相关参数，再做少量有希望的组合：
- llt_period / llt_window（LLT 平滑与拟合窗口）
- min_r2 / min_score（趋势质量过滤）
- top_n（集中度）
- rotate_threshold（轮动惰性，降换手）
- volume_confirm（量能确认）

用法（backend/ 下，行情走 K 线缓存）：
    .venv/Scripts/python.exe -m examples.cross_section.experiment_params \
        examples/cross_section/backtest_shared_etf_rotation.json
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.backtest_runner import run_backtest_request
from app.schemas import BacktestRequest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

METRIC_COLS = [
    "total_return", "annualized_return", "max_drawdown", "sharpe",
    "return_drawdown_ratio", "num_trades", "win_rate", "profit_factor",
]
ALIAS = {
    "total_return": "总收益", "annualized_return": "年化", "max_drawdown": "回撤",
    "sharpe": "Sharpe", "return_drawdown_ratio": "收益/回撤", "num_trades": "交易",
    "win_rate": "胜率", "profit_factor": "盈亏比",
}

# 基线：当前请求里的参数
BASELINE = {
    "decision_frequency": "monthly",
    "top_n": 1,
    "llt_period": 30,
    "llt_window": 30,
    "min_r2": 0.5,
    "min_score": 0.0,
    "volume_confirm": False,
    "rotate_threshold": 0.9,
}

# 逐一扫描：参数 -> 候选值列表（None 表示保持基线）
SWEEPS: list[tuple[str, list[Any]]] = [
    ("llt_period", [20, 40, 60]),
    ("llt_window", [15, 45, 60]),
    ("min_r2", [0.3, 0.7, 0.85]),
    ("min_score", [0.0005, 0.001]),
    ("top_n", [2, 3]),
    ("rotate_threshold", [0.95, 1.0]),
    ("volume_confirm", [True]),
]

# 有希望的组合（基于单扫结果手动挑）
COMBOS: list[tuple[str, Any]] = []


def _pct(v: float) -> str:
    return f"{v * 100:8.2f}%" if v is not None else "      -"


def _num(v: float) -> str:
    return f"{v:8.2f}" if v is not None else "      -"


def run_case(base: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(base)
    sp = dict(body.get("strategy_params") or {})
    sp.update(params)
    body["strategy_params"] = sp
    body["output_options"] = {"json": False}
    return run_backtest_request(BacktestRequest.model_validate(body))


def print_table(rows: list[tuple[str, dict[str, Any]]]) -> None:
    header = ["case"] + [_shorten(c) for c in METRIC_COLS]
    print(" | ".join(h.ljust(10) for h in header))
    print(" | ".join("-" * 10 for _ in header))
    for label, metrics in rows:
        vals = [label]
        vals += [
            _pct(metrics.get(c)) if c in {
                "total_return", "annualized_return", "max_drawdown",
            } else _num(metrics.get(c, float("nan")))
            for c in METRIC_COLS
        ]
        print(" | ".join(v.ljust(10) for v in vals))
    print()


def _shorten(c: str) -> str:
    return ALIAS.get(c, c)


def fmt(params: dict[str, Any]) -> str:
    return " ".join(f"{k}={v}" for k, v in params.items() if v is not None)


def main(base_path: Path | None = None) -> int:
    base_path = base_path or Path(
        __file__).resolve().parent / "backtest_shared_etf_rotation.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))

    print("基线:", fmt(BASELINE))
    print("区间:", base.get("start_date"), "~", base.get("end_date"),
          "| 初资", base.get("initial_cash"))
    print()

    base_metrics = run_case(base, BASELINE).get("metrics") or {}
    print("[基线]")
    print_table([("base", base_metrics)])

    results: dict[str, dict[str, Any]] = {"base": base_metrics}
    for key, values in SWEEPS:
        print(f"[单扫] {key} = {values}")
        rows = [("base", base_metrics)]
        for v in values:
            params = dict(BASELINE)
            params[key] = v
            label = f"{key}={v}"
            try:
                m = run_case(base, params).get("metrics") or {}
            except Exception as exc:  # noqa: BLE001
                print(f"  [失败] {label}: {exc}", file=sys.stderr)
                continue
            results[label] = m
            rows.append((label, m))
        print_table(rows)

    if COMBOS:
        print("[组合]")
        rows = [("base", base_metrics)]
        for label, params in COMBOS:
            try:
                m = run_case(base, params).get("metrics") or {}
            except Exception as exc:  # noqa: BLE001
                print(f"  [失败] {label}: {exc}", file=sys.stderr)
                continue
            results["combo:" + label] = m
            rows.append((label, m))
        print_table(rows)

    # 小结：按收益/回撤降序
    print("小结（按 收益/回撤 降序，前 12）:")
    ranked = sorted(
        results.items(),
        key=lambda kv: kv[1].get("return_drawdown_ratio", -1.0) or -1.0,
        reverse=True,
    )
    print(" | ".join(h.ljust(10) for h in ["case"] + [_shorten(c) for c in METRIC_COLS]))
    print(" | ".join("-" * 10 for _ in ["case"] + METRIC_COLS))
    for label, m in ranked[:12]:
        vals = [label]
        vals += [
            _pct(m.get(c)) if c in {
                "total_return", "annualized_return", "max_drawdown",
            } else _num(m.get(c, float("nan")))
            for c in METRIC_COLS
        ]
        print(" | ".join(v.ljust(10) for v in vals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else None))
