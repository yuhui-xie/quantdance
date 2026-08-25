"""biweekly vs monthly 调仓频率对照实验脚本。

验证"为何 biweekly 与 monthly 绩效差异大"的几个候选原因：
1. 决策日采样（frequency 对比，monthly 默认锚 start、biweekly 锚定偶数 ISO 周）；
2. 集中度放大（top_n：1 vs 3 vs 5）；
3. 换手/摩擦（观察 rebalances / trades 次数）；
4. 轮动惰性（rotate_threshold：0.9 默认 vs 1.0 关闭）。

用法（在 backend/ 下）：
    .venv/Scripts/python.exe -m examples.cross_section.experiment_frequency
    # 或指定基线请求：
    .venv/Scripts/python.exe -m examples.cross_section.experiment_frequency examples/cross_section/backtest_shared_etf_rotation.json

脚本复用 runner 的 run_backtest_request，不访问网络以外的新数据源；
行情走 K 线缓存（use_cache=True），重复跑只需重新执行选股与撮合。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

# 保证从任意 cwd / 以文件路径方式运行时 backend/ 可被 import（pytest 由 pythonpath 覆盖）
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.backtest_runner import run_backtest_request
from app.schemas import BacktestRequest

# ---- 实验网格 ----
FREQUENCIES = ["monthly", "biweekly", "monthly_2x"]   # 主问题：三种频率（monthly_2x=每月第一、第三个周一）
TOP_N = [1, 3, 5]                          # 集中度：验证 top_n 放大效应
ROTATE_THRESHOLDS = [0.9, 1.0]             # 惰性：1.0 关闭惰性

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_BASE = BASE_DIR / "backtest_shared_etf_rotation.json"

# Windows 下控制台可能默认 cp936，这里统一以 UTF-8 输出，保证表格中文正常显示
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

METRIC_COLS = [
    "total_return",
    "annualized_return",
    "max_drawdown",
    "sharpe",
    "return_drawdown_ratio",
    "num_trades",
    "closed_trades",
    "win_rate",
    "profit_factor",
]


def _pct(value: float) -> str:
    return f"{value * 100:8.2f}%" if value is not None else "      -"


def _num(value: float) -> str:
    return f"{value:8.2f}" if value is not None else "      -"


def load_base(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_case(base: dict[str, Any], *, frequency: str, top_n: int, rotate_threshold: float) -> dict[str, Any]:
    body_dict = copy.deepcopy(base)
    body_dict["strategy_params"] = dict(body_dict.get("strategy_params") or {})
    body_dict["strategy_params"].update(
        {
            "decision_frequency": frequency,
            "top_n": top_n,
            "rotate_threshold": rotate_threshold,
        }
    )
    # 实验只关心指标，关掉多余的落盘/HTML 输出
    body_dict["output_options"] = {"json": False}
    request = BacktestRequest.model_validate(body_dict)
    return run_backtest_request(request)


def main(base_path: Path | None = None) -> int:
    base_path = base_path or DEFAULT_BASE
    base = load_base(base_path)

    header = ["frequency", "top_n", "rotate"]
    header += [_shorten(c) for c in METRIC_COLS]
    header += ["rebals"]
    widths = [len(h) + 2 for h in header]

    def row_fmt(values: list[str]) -> str:
        return " | ".join(v.ljust(w) for v, w in zip(values, widths))

    print("基线请求:", base_path)
    print(f"区间: {base.get('start_date')} ~ {base.get('end_date')} | 初资 {base.get('initial_cash')} | "
          f"手续费 {base.get('commission')} 滑点 {base.get('slippage')}")
    print()
    print(row_fmt(header))
    print(" | ".join("-" * w for w in widths))

    results: list[dict[str, Any]] = []
    for frequency in FREQUENCIES:
        for top_n in TOP_N:
            for rt in ROTATE_THRESHOLDS:
                label = f"{frequency}/{top_n}/{rt}"
                print(f"[运行] {label} ...", file=sys.stderr, flush=True)
                try:
                    out = run_case(base, frequency=frequency, top_n=top_n, rotate_threshold=rt)
                except Exception as exc:  # noqa: BLE001 —— 单点失败不中断整批
                    print(f"[失败] {label}: {exc}", file=sys.stderr)
                    continue
                metrics = out.get("metrics") or {}
                rebalances = out.get("rebalances") or []
                values = [frequency, str(top_n), f"{rt:g}"]
                values += [_pct(metrics.get(c)) if c in {
                    "total_return", "annualized_return", "max_drawdown",
                } else _num(metrics.get(c, float("nan"))) for c in METRIC_COLS]
                values += [str(len(rebalances))]
                print(row_fmt(values))
                results.append(
                    {
                        "frequency": frequency,
                        "top_n": top_n,
                        "rotate_threshold": rt,
                        "rebalances": len(rebalances),
                        "metrics": metrics,
                    }
                )

    print()
    _print_insights(results)
    return 0


def _shorten(col: str) -> str:
    aliases = {
        "total_return": "总收益",
        "annualized_return": "年化",
        "max_drawdown": "回撤",
        "sharpe": "Sharpe",
        "return_drawdown_ratio": "收益/回撤",
        "num_trades": "交易数",
        "closed_trades": "平仓数",
        "win_rate": "胜率",
        "profit_factor": "盈亏比",
    }
    return aliases.get(col, col)


def _print_insights(results: list[dict[str, Any]]) -> None:
    """简单按频率聚合，看 top_n/惰性放大与否，提示主要差异来源。"""
    if not results:
        return
    # 每个 (top_n, rotate) 分组内，按总收益降序看各频率谁胜出
    print("对照小结（同一 top_n 与 rotate 下，各频率总收益，降序）:")
    print("  " + " | ".join(["top_n", "rotate", "频率(收益)…"]))
    seen = set()
    for r in sorted(results, key=lambda x: (-x["top_n"], x["rotate_threshold"])):
        key = (r["top_n"], r["rotate_threshold"])
        if key in seen:
            continue
        seen.add(key)
        group = [x for x in results if (x["top_n"], x["rotate_threshold"]) == key]
        group.sort(key=lambda x: x["metrics"].get("total_return", 0.0), reverse=True)
        cells = " ".join(
            f"{x['frequency']}={_pct(x['metrics'].get('total_return', 0.0))}"
            for x in group
        )
        print(f"  top_n={r['top_n']:>1} rotate={r['rotate_threshold']:>3g}  {cells}")


if __name__ == "__main__":
    base_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    raise SystemExit(main(base_path))
