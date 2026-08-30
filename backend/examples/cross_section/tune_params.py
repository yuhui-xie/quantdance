"""通用参数整定（网格调参）脚本：对任意横截面策略做参数网格扫描并排名。

以一份**基线回测请求 JSON** + 一份**网格 JSON** 为输入，对网格中声明的每个
参数字段取候选值列表做**笛卡尔积**，逐一复用 ``run_backtest_request`` 跑回测，
收集绩效指标，按用户指定指标（默认 sharpe）排序打印排名表，并可选落盘结果。

设计动机：周级/双周/月度调仓频率的对照、动量/量能参数的手动整定等，之前散落在
已删除的一次性 ``experiment_*.py`` 里、各自硬编码网格不可复用。本脚本把这些收敛成
一份声明式网格能力，跨策略通用。**周级调仓机制已内置于调度器**
（``decision.py`` 的 ``decision_frequency="weekly"``），此处无需改任何策略代码，
只需在网格里写 ``"decision_frequency": ["weekly", ...]`` 即可扫描。

用法（在 ``backend/`` 下）：
    .venv/Scripts/python.exe -m examples.cross_section.tune_params \
        examples/cross_section/backtest_shared_etf_rotation.json \
        examples/cross_section/grid_etf_rotation_freq.json \
        [--output out/tune.json] [--metric sharpe] [--top 30] [--quiet]

位置参数：
    BASE  基线回测请求 JSON（BacktestRequest，见 examples/cross_section/*.json）
    GRID  网格 JSON，两个可选分区（见下），都不配置则退化为单次基线运行

网格 JSON 格式：
    {
      "strategy_params": {
        "decision_frequency": ["weekly", "monthly", "biweekly"],
        "top_n": [1, 2, 3],
        "llt_period": [20, 30, 40]
      },
      "request": {
        "universe": ["etf_core", "etf_core_sub"],
        "start_date": ["2020-01-01", "2023-01-01"]
      }
    }
    - "strategy_params.*"：每个 key → 候选值列表，覆盖进请求的 ``strategy_params``；
    - "request.*"       ：顶层字段（universe/start_date/…），覆盖进请求顶层；
    - 任一分区的值若为标量（非 list）视作单值列表，统一按列表处理。
    未出现在网格里的字段保持 BASE 请求值。

选项：
    --output FILE.json  把完整结果（每组合 params + metrics + 调仓次数）落盘；
    --metric NAME       排序指标（默认 sharpe；可用 total_return/max_drawdown/…）；
    --top N             仅展示前 N 名（默认全部；不影响运行次数）；
    --quiet             不打印逐组合进度，只打排名表。

注意：网格组合数是各字段候选数的笛卡尔积，随参数增多指数膨胀。**先开小网格验证
跑通**，再放大；行情走 K 线缓存（``use_cache`` 沿用 BASE 请求），重复跑只重算选股与撮合。
单组合失败不中断整批（报 ``[失败]`` 继续）。
"""

from __future__ import annotations

import argparse
import copy
import itertools
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

# Windows 下控制台可能默认 cp936，这里统一以 UTF-8 输出，保证表格中文正常显示
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 排名表展示的指标列（均来自回测结果 ``metrics``）
METRIC_COLS = [
    "total_return",
    "annualized_return",
    "max_drawdown",
    "sharpe",
    "return_drawdown_ratio",
    "num_trades",
    "win_rate",
    "profit_factor",
]
ALIAS = {
    "total_return": "总收益",
    "annualized_return": "年化",
    "max_drawdown": "回撤",
    "sharpe": "Sharpe",
    "return_drawdown_ratio": "收益/回撤",
    "num_trades": "交易",
    "win_rate": "胜率",
    "profit_factor": "盈亏比",
}
_PCT_COLS = {"total_return", "annualized_return", "max_drawdown", "win_rate"}


def _fmt(metric: str, value: Any) -> str:
    """按指标类型格式化为终端列文本；空值/异常返回占位。"""
    if value is None:
        return "-"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if metric in _PCT_COLS:
        return f"{f * 100:6.1f}%"
    return f"{f:7.2f}"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _as_list(value: Any) -> list[Any]:
    """把网格中某个字段的值规整为候选值列表（标量 → 单值列表）。"""
    return value if isinstance(value, list) else [value]


def build_sweep(grid: dict[str, Any]) -> tuple[list[tuple[str, ...]], list[dict[str, list[Any]]]]:
    """把网格展开为 (所有组合的参数字典列表) + (各组合的变更来源分区)。

    网格可含两个分区 ``strategy_params`` 与 ``request``，各自内部字段值做笛卡尔积，
    两分区之间也做笛卡尔积。返回的每个组合 dict 形如 ``{"strategy_params": {...},
    "request": {...}}``，仅含该组合要覆盖的字段（键 → 单值）。
    """
    sections: dict[str, dict[str, list[Any]]] = {}
    keys: list[tuple[str, str]] = []
    for section_name in ("strategy_params", "request"):
        section = grid.get(section_name) or {}
        if not isinstance(section, dict):
            raise ValueError(f"网格分区 {section_name!r} 应为对象")
        val_lists = {k: _as_list(v) for k, v in section.items()}
        if not val_lists:
            continue
        sections[section_name] = val_lists
        keys.extend((section_name, k) for k in val_lists)

    if not keys:
        return [{}], []

    combos: list[dict[str, Any]] = []
    value_axes = [sections[sn][k] for sn, k in keys]
    for prod in itertools.product(*value_axes):
        combo: dict[str, Any] = {}
        for (sn, k), v in zip(keys, prod):
            combo.setdefault(sn, {})[k] = v
        combos.append(combo)
    return combos, keys


def run_case(base: dict[str, Any], combo: dict[str, Any]) -> dict[str, Any]:
    """对单个参数组合运行回测，返回指标 + 调仓次数。"""
    body = copy.deepcopy(base)
    body["strategy_params"] = dict(body.get("strategy_params") or {})
    body["strategy_params"].update(combo.get("strategy_params") or {})
    body.update(combo.get("request") or {})
    # 整定只关心指标，关掉多余的落盘/HTML 输出
    body["output_options"] = {"json": False}
    request = BacktestRequest.model_validate(body)
    out = run_backtest_request(request)
    metrics = out.get("metrics") or {}
    rebalances = out.get("rebalances") or []
    return {"metrics": metrics, "n_rebalances": len(rebalances)}


def label_for(combo: dict[str, Any]) -> str:
    """把变更字段拼成行标签（如 freq=weekly,top_n=3），未变更字段不展示。"""
    parts: list[str] = []
    for section_name in ("strategy_params", "request"):
        for k, v in (combo.get(section_name) or {}).items():
            parts.append(f"{k}={v}")
    return ",".join(parts) if parts else "baseline"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tune_params",
        description="通用参数整定：对基线回测请求 + 网格做笛卡尔积扫描并按指标排名",
    )
    parser.add_argument("base", type=Path, help="基线回测请求 JSON")
    parser.add_argument("grid", type=Path, help="网格 JSON（strategy_params / request 两个可选分区）")
    parser.add_argument("--output", type=Path, default=None, metavar="FILE.json", help="落盘完整结果 JSON")
    parser.add_argument("--metric", default="sharpe", help="排序指标（默认 sharpe）")
    parser.add_argument("--top", type=int, default=None, help="仅展示前 N 名（默认全部）")
    parser.add_argument("--quiet", action="store_true", help="不打印逐组合进度")
    args = parser.parse_args(argv)

    base = load_json(args.base)
    grid = load_json(args.grid)
    combos, keys = build_sweep(grid)

    if not args.quiet:
        print(f"基线请求: {args.base}")
        print(f"网格: {args.grid} | 组合数: {len(combos)}")
        print(f"区间: {base.get('start_date')} ~ {base.get('end_date')} | 初资 {base.get('initial_cash')} | "
              f"手续费 {base.get('commission')} 滑点 {base.get('slippage')}")
        print()

    results: list[dict[str, Any]] = []
    for i, combo in enumerate(combos, 1):
        label = label_for(combo)
        if not args.quiet:
            print(f"[{i}/{len(combos)}] {label} ...", file=sys.stderr, flush=True)
        try:
            run_out = run_case(base, combo)
        except Exception as exc:  # noqa: BLE001 —— 单点失败不中断整批
            print(f"[失败] {label}: {exc}", file=sys.stderr)
            continue
        results.append({"label": label, "params": combo, **run_out})

    if not results:
        print("无成功组合", file=sys.stderr)
        return 2

    def sort_key(r: dict[str, Any]) -> tuple:
        return (r["metrics"].get(args.metric) is None, -(r["metrics"].get(args.metric) or 0.0))

    results.sort(key=sort_key)

    # 排名表：行标签 + 各指标列 + 调仓次数
    shown = results if args.top is None else results[: args.top]
    header = ["combo"] + [ALIAS.get(c, c) for c in METRIC_COLS] + ["rebals"]
    widths = [len(h) + 2 for h in header]
    widths[0] = max(len(header[0]) + 2, max((len(r["label"]) for r in shown), default=1) + 2)

    def row_fmt(values: list[str]) -> str:
        return " | ".join(v.ljust(w) for v, w in zip(values, widths))

    print(row_fmt(header))
    print(" | ".join("-" * w for w in widths))
    for r in shown:
        m = r["metrics"]
        values = [r["label"]] + [_fmt(c, m.get(c)) for c in METRIC_COLS] + [str(r["n_rebalances"])]
        print(row_fmt(values))

    print(f"\n共 {len(results)} 成功组合，按 {args.metric} 降序。"
          f"{' 展示前 %d' % args.top if args.top else ''}")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        dump = {
            "base": base,
            "grid": grid,
            "metric": args.metric,
            "results": results,
        }
        args.output.write_text(
            json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not args.quiet:
            print(f"完整结果已落盘: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
