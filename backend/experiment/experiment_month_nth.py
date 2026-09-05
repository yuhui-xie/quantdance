"""ETF 轮动「每月第 n 个交易日调仓」对照实验脚本。

验证固定月度节奏下、仅换锚定日（当月第 n 个交易日）对绩效的影响。n 取代表性点
[1, 3, 5, 10, 15, 20]：n=1 即每月首个交易日（等价 decision_frequency=monthly +
decision_anchor=start），n 越大调仓越靠当月月中/月末。

用法（在 backend/ 下）：
    .venv/Scripts/python.exe -m experiment.experiment_month_nth
    # 或指定基线请求 + 股票池（第二个参数覆盖 universe）：
    .venv/Scripts/python.exe -m experiment.experiment_month_nth examples/cross_section/backtest_shared_etf_rotation.json etf_core

脚本复用 runner 的 run_backtest_request，行情走 K 线缓存（use_cache=True），
重复跑只需重新执行选股与撮合。
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
from app.strategies.cross_section.decision import decision_dates_by_frequency

# ---- 实验网格：当月第 n 个交易日（代表性点）× top_n（集中度） ----
NTH = [1, 3, 5, 10, 15, 20]
TOP_N = [1, 2, 3, 4, 5]

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


def run_case(
    base: dict[str, Any], *, nth: int, top_n: int, universe: str | None = None
) -> dict[str, Any]:
    body_dict = copy.deepcopy(base)
    if universe:
        body_dict["universe"] = universe
    body_dict["strategy_params"] = dict(body_dict.get("strategy_params") or {})
    body_dict["strategy_params"].update(
        {
            "decision_frequency": "monthly_nth",
            "decision_month_nth": nth,
            "top_n": top_n,
        }
    )
    # 实验只关心指标，关掉多余的落盘/HTML 输出
    body_dict["output_options"] = {"json": False}
    request = BacktestRequest.model_validate(body_dict)
    return run_backtest_request(request)


def main(base_path: Path | None = None, universe: str | None = None) -> int:
    base_path = base_path or DEFAULT_BASE
    base = load_base(base_path)
    if universe:
        base["universe"] = universe

    header = ["n(当月第几交易日)"]
    header += [_shorten(c) for c in METRIC_COLS]
    header += ["调仓次数"]
    widths = [len(h) + 2 for h in header]

    def row_fmt(values: list[str]) -> str:
        return " | ".join(v.ljust(w) for v, w in zip(values, widths))

    print("基线请求:", base_path)
    print(f"区间: {base.get('start_date')} ~ {base.get('end_date')} | 初资 {base.get('initial_cash')} | "
          f"手续费 {base.get('commission')} 滑点 {base.get('slippage')} | 池 {base.get('universe')}")
    print("注: n=1 即每月首个交易日，等价 decision_frequency=monthly + decision_anchor=start")
    print()

    all_results: list[dict[str, Any]] = []
    for top_n in TOP_N:
        print(f"=========== top_n = {top_n} ===========")
        print(row_fmt(header))
        print(" | ".join("-" * w for w in widths))
        for nth in NTH:
            label = f"n={nth}/top{top_n}"
            print(f"[运行] {label} ...", file=sys.stderr, flush=True)
            try:
                out = run_case(base, nth=nth, top_n=top_n, universe=universe)
            except Exception as exc:  # noqa: BLE001 —— 单点失败不中断整批
                print(f"[失败] {label}: {exc}", file=sys.stderr)
                continue
            metrics = out.get("metrics") or {}
            rebalances = out.get("rebalances") or []
            values = [str(nth)]
            values += [_pct(metrics.get(c)) if c in {
                "total_return", "annualized_return", "max_drawdown",
            } else _num(metrics.get(c, float("nan"))) for c in METRIC_COLS]
            values += [str(len(rebalances))]
            print(row_fmt(values))
            all_results.append(
                {
                    "top_n": top_n,
                    "nth": nth,
                    "rebalances": len(rebalances),
                    "metrics": metrics,
                }
            )
        print()

    print()
    _print_insights(all_results)
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
    """按 top_n 分组：看锚定日（当月第 n 个交易日）的敏感性是否随分散收敛。"""
    if not results:
        return
    print("锚定敏感性小结（同一 top_n 下，各 n 总收益的极差/波动）:")
    print("  " + " | ".join(["top_n", "最优n", "最优收益", "最差n", "最差收益", "极差(pp)", "收益标准差"]))
    for top_n in TOP_N:
        group = [r for r in results if r["top_n"] == top_n and r["metrics"].get("total_return") is not None]
        if not group:
            continue
        best = max(group, key=lambda r: r["metrics"]["total_return"])
        worst = min(group, key=lambda r: r["metrics"]["total_return"])
        vals = [r["metrics"]["total_return"] for r in group]
        mean = sum(vals) / len(vals)
        std = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
        spread = (best["metrics"]["total_return"] - worst["metrics"]["total_return"]) * 100
        cells = [
            f"{top_n!s:>5}",
            f"n={best['nth']!s:>2}",
            f"{_pct(best['metrics']['total_return'])}",
            f"n={worst['nth']!s:>2}",
            f"{_pct(worst['metrics']['total_return'])}",
            f"{spread:7.1f}",
            f"{std * 100:9.1f}",
        ]
        print("  " + " | ".join(c.ljust(12) for c in cells))
    print("（极差/标准差越小 = 锚定日越不敏感，策略越稳）")


if __name__ == "__main__":
    base_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    universe = sys.argv[2] if len(sys.argv) > 2 else None
    raise SystemExit(main(base_path, universe=universe))
