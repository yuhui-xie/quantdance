"""量价/短期过热阻尼系数的稳健标定（防过拟合）：train/test 分段 + 邻域平滑 + 最轻有效选取。

不是「在回测区间上选绩效最大的系数」——那是对历史的过拟合。本脚本把标定变成三层：

1. **永久 hold-out test 段**：把整段回测按日期切成 ``train``（基期~train_end）与
   ``test``（test_start~期末）两段，**同一段资金/特征曲线**上分别量绩效（不做两次冷启动，
   动量特征天然有热启动，避免 test 段开头缺回看导致的前几十个交易日哑火）。
   选系数只看 train 段指标；test 段只在最后看一眼，用于暴露 in-sample/train 的虚高。

2. **邻域平滑 + 最轻有效选取**：对 train 段 Sharpe 沿系数网格做邻域均值（塌掉孤立尖峰），
   然后取「平滑后仍在最优 tol 以内、系数**最小**」的点。阻尼是正则项，经济直觉是：
   **能用最小的惩罚显著改善就用最小，不追最高点**。若最小有效点 = 0，说明该阻尼不值得开。

3. **先各开 1D 再联合**：单开 ``short_term_damp_coef`` / ``amount_surge_damp_coef`` 时，
   **另一阻尼保留基线配方里的当前值**（如锁定配方里 short=0.1），直接回答「在当前配方上
   要不要再开/调这个阻尼」；``--joint`` 再扫二维联合看两者交互（急拉常伴放量，两阻尼相关，
   联合峰不可当单开结论）。

用法（backend/ 下）：
    .venv/Scripts/python.exe -m experiment.calibrate_damp \
        examples/cross_section/backtest_shared_etf_rotation.json \
        [--train-end 2024-12-31] [--test-start 2025-01-01] \
        [--damp short|amount|joint] \
        [--short-grid 0,0.05,0.1,0.15,0.2,0.3] \
        [--amount-grid 0,0.05,0.1,0.15,0.2,0.3] \
        [--tol 0.05] [--smooth 3] [--output out/calibrate_damp.json]

说明：
    - 每个网格单元对**整段**跑一次连续回测（热启动、单条资金曲线），再按日期切 train/test
      指标，故网格单元数 × 整段回测 = 总耗时。先 1D 小网格验证跑通再放大 / 开 --joint。
    - Sharpe/回撤/年化一律复用 ``app.backtest_engine`` 的同款算法，保证与主回测口径一致。
    - 阻尼窗口（amount_recent_days/baseline_days 及 slope 等）沿用 BASE 请求，不在此扫。
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import sys
from pathlib import Path
from typing import Any, Sequence

# 保证从任意 cwd / 以文件路径方式运行时 backend/ 可被 import（pytest 由 pythonpath 覆盖）
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import numpy as np

from app.backtest_engine import _max_drawdown, _sharpe
from app.backtest_runner import run_backtest_request
from app.schemas import BacktestRequest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 标定关心的候选系数。Base 里 short_term_damp_coef 可能已是 0.1（锁定配方），
# 做 1D 边际时会把另一阻尼强制为 0，得到「该阻尼单独相对纯斜率基线的价值」。
DEFAULT_SHORT_GRID = [0.0, 0.05, 0.1, 0.15, 0.2, 0.3]
DEFAULT_AMOUNT_GRID = [0.0, 0.05, 0.1, 0.15, 0.2, 0.3]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_grid(raw: str | None, default: list[float]) -> list[float]:
    if raw is None:
        return default
    vals = [float(x) for x in raw.split(",") if x.strip() != ""]
    vals = sorted(set(vals))
    if not vals:
        raise ValueError("空系数网格")
    return vals


def _run(base: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """跑一次整段连续回测，返回 equity / rebalances / 全段 metrics。"""
    body = copy.deepcopy(base)
    body["strategy_params"] = dict(body.get("strategy_params") or {})
    body["strategy_params"].update(params)
    body["output_options"] = {"json": False}  # 标定只关心指标，关掉落盘/HTML
    request = BacktestRequest.model_validate(body)
    out = run_backtest_request(request)
    if hasattr(out, "model_dump"):
        out = out.model_dump()
    return out


def _seg_dates(rows: Sequence[dict[str, Any]]) -> list[str]:
    return [str(r.get("date", ""))[:10] for r in rows]


def segment_metrics(
    equity: list[dict[str, Any]],
    rebalances: list[dict[str, Any]],
    lo: str | None,
    hi: str | None,
) -> dict[str, float]:
    """在一条完整 equity 曲线上切绩效：取「到账日 in (lo, hi]」的日收益与其资金路径。

    复用引擎同款：日简单收益 → _sharpe；在段内归一化路径上算最大回撤与累计收益；
    换手 = 段内 rebalance 数（rebalance.date 为成交日，同取 in (lo, hi]）。lo/hi 为
    None 时对应开区间（train 从基期起、test 到期末）。
    """
    dates = _seg_dates(equity)
    eq = np.asarray([float(r.get("equity", 0.0)) for r in equity], dtype=float)
    n = len(eq)
    zero = {"sharpe": 0.0, "calmar": 0.0, "total_return": 0.0,
            "max_drawdown": 0.0, "n_rebalance": 0.0}
    if n < 2:
        return zero

    # 第 i 个收益从 eq[i-1] 涨到 eq[i]，到账日为 dates[i]
    sel = [i for i in range(1, n)
           if (not lo or dates[i] > lo) and (not hi or dates[i] <= hi)]
    if not sel:
        return zero
    i0, iN = sel[0], sel[-1]
    rets = np.asarray([eq[i] / eq[i - 1] - 1.0 for i in sel], dtype=float)
    rets = rets[np.isfinite(rets) & np.isfinite(eq[i0 - 1]) & (eq[i0 - 1] > 0)]
    path = eq[i0 - 1:iN + 1]  # 自段首基准日(含前一边界值)到段末
    if path.size < 2 or path[0] <= 0 or rets.size == 0:
        return zero

    norm = path / path[0]
    sharpe = _sharpe(rets)
    mdd = _max_drawdown(norm)
    total_return = float(norm[-1] - 1.0)
    calmar = float(total_return / mdd) if mdd > 1e-12 else 0.0
    n_reb = float(sum(1 for d in _seg_dates(rebalances)
                      if (not lo or d > lo) and (not hi or d <= hi)))
    return {"sharpe": float(sharpe), "calmar": float(calmar),
            "total_return": total_return, "max_drawdown": float(mdd),
            "n_rebalance": n_reb}


def moving_average(values: list[float], width: int) -> list[float]:
    """沿系数网格做邻域均值（边界用可用窗口），用于塌掉孤立尖峰。"""
    n = len(values)
    if width <= 1:
        return list(values)
    half = width // 2
    out: list[float] = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out.append(float(np.mean(values[lo:hi])))
    return out


def lightest_effective(
    coefs: list[float],
    smoothed: list[float],
    tol: float,
) -> tuple[float, int]:
    """最轻有效选取：取「平滑后 ≥ 最优 − tol、且系数最小」的点（tol 越小越严）。

    阻尼是正则项 → 用能显著改善的**最小**惩罚，不追尖峰。若返回 0，则该阻尼不值得开。
    """
    best = max(smoothed)
    for c, s in zip(coefs, smoothed):
        if best - s <= tol:
            return c, coefs.index(c)
    return coefs[0], 0


def _axis_table(
    coefs: list[float],
    cells: list[dict[str, Any]],
    tol: float,
    smooth: int,
    key: str,
    metric_label: str,
) -> None:
    rows: list[tuple[float, dict[str, float], dict[str, float], float]] = []
    for c, cell in zip(coefs, cells):
        rows.append((c, cell["train"], cell["test"], cell["full_sharpe"]))
    train_sh = [r[1]["sharpe"] for r in rows]
    sm = moving_average(train_sh, smooth)
    chosen, chosen_i = lightest_effective(coefs, sm, tol)
    argmax_i = int(np.argmax(sm))

    hdr = (f"{metric_label:>7} | " + " | ".join(
        f"{c:>6}" for c in coefs) + "  <- 系数")
    print(hdr)
    grid = []
    print("train Sharpe       | " + " | ".join(f"{v:6.3f}" for v in train_sh))
    print("  -> 邻域平滑      | " + " | ".join(f"{v:6.3f}" for v in sm))
    print("train Calmar       | " + " | ".join(f"{r[1]['calmar']:6.3f}" for r in rows))
    print("OOS test Sharpe    | " + " | ".join(f"{r[2]['sharpe']:6.3f}" for r in rows))
    print("test Calmar        | " + " | ".join(f"{r[2]['calmar']:6.3f}" for r in rows))
    print("full-window Sharpe | " + " | ".join(f"{r[3]:6.3f}" for r in rows))
    print(f"{key} 系数取值: {coefs}")
    print(f"平滑后最优点: coef={coefs[argmax_i]:.2f} (train S={sm[argmax_i]:.3f})；"
          f"最轻有效选取(tol={tol}): coef={chosen:.2f} (train S={sm[chosen_i]:.3f})")
    if chosen <= 1e-9:
        print(f"  -> 最轻有效点 = 0：{key} 未见稳健收益，建议【不开启】。")
    else:
        gap = rows[chosen_i][1]["sharpe"] - rows[chosen_i][2]["sharpe"]
        print(f"  -> 建议 {key} = {chosen:.2f}；该点 train−test Sharpe 差 = {gap:+.3f}"
              f"（越大越可疑=虚高）。")
    print()


def _print_joint(
    short_grid: list[float],
    amount_grid: list[float],
    cells: list[dict[str, Any]],
    train_or_test: str,
    label: str,
) -> None:
    print(f"--- 联合 {train_or_test} 段 {label}（行=short_coef，列=amount_coef）---")
    print("       | " + " | ".join(f"amt{c:>4.2f}" for c in amount_grid))
    k = 0
    for s in short_grid:
        vals = []
        for _a in amount_grid:
            vals.append(cells[k][train_or_test][label])
            k += 1
        print(f"sht{s:>4.2f} | " + " | ".join(f"{v:8.3f}" for v in vals))
    print()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="etf_rotation 阻尼系数稳健标定（train/test + 平滑 + 最轻有效）")
    p.add_argument("base", type=Path, help="基线回测请求 JSON（含 slope/过滤/成交时点等）")
    p.add_argument("--train-end", default="2024-12-31", help="train 段含止日期（默认 2024-12-31）")
    p.add_argument("--test-start", default="2025-01-01", help="test 段起始日期（默认 2025-01-01，永久 hold-out）")
    p.add_argument("--damp", choices=("short", "amount", "joint"), default="short",
                   help="扫描模式：先各开1D(short|amount)，--joint 再扫二维联合")
    p.add_argument("--short-grid", default=None, help="short 系数逗号网格")
    p.add_argument("--amount-grid", default=None, help="amount 系数逗号网格")
    p.add_argument("--tol", type=float, default=0.05, help="最轻有效选取容忍的 Sharpe 差距（默认 0.05）")
    p.add_argument("--smooth", type=int, default=3, help="邻域平滑宽度（默认 3）")
    p.add_argument("--output", type=Path, default=None, help="落盘完整结果 JSON")
    args = p.parse_args(argv)

    base = load_json(args.base)
    start = base.get("start_date", "")
    end = base.get("end_date", "")
    short_grid = parse_grid(args.short_grid, DEFAULT_SHORT_GRID)
    amount_grid = parse_grid(args.amount_grid, DEFAULT_AMOUNT_GRID)
    train_hi, test_lo = args.train_end, args.test_start

    # 单开 1D 时另一阻尼**保留基线配方里的值**（而非强制 0），这样才能回答
    # 「在当前生产配方(short=0.1)上要不要再开 amount」这类决策。基线里没有的字段给 0。
    base_sp = base.get("strategy_params") or {}
    base_short = float(base_sp.get("short_term_damp_coef", 0.0))
    base_amount = float(base_sp.get("amount_surge_damp_coef", 0.0))

    def combos():
        if args.damp == "short":
            for c in short_grid:
                yield {"label": f"short={c:.2f}", "params": {"short_term_damp_coef": c,
                                                              "amount_surge_damp_coef": base_amount}}
        elif args.damp == "amount":
            for c in amount_grid:
                yield {"label": f"amount={c:.2f}", "params": {"short_term_damp_coef": base_short,
                                                               "amount_surge_damp_coef": c}}
        else:  # joint
            for s, a in itertools.product(short_grid, amount_grid):
                yield {"label": f"short={s:.2f},amount={a:.2f}",
                       "params": {"short_term_damp_coef": s, "amount_surge_damp_coef": a}}

    combos_l = list(combos())
    print(f"基线: {args.base} | {start} ~ {end}")
    print(f"train 段 ≤ {train_hi}，test 段 ≥ {test_lo}（永久 hold-out，最后只看一次）")
    print(f"模式: {args.damp} | 组合数: {len(combos_l)} | tol={args.tol} 平滑宽度={args.smooth}")
    print()

    cells: list[dict[str, Any]] = []
    for i, cb in enumerate(combos_l, 1):
        print(f"[{i}/{len(combos_l)}] {cb['label']} ...", file=sys.stderr, flush=True)
        try:
            out = _run(base, cb["params"])
        except Exception as exc:  # noqa: BLE001 单点失败不中断
            print(f"[失败] {cb['label']}: {exc}", file=sys.stderr)
            continue
        eq = out.get("equity") or []
        reb = out.get("rebalances") or []
        train = segment_metrics(eq, reb, None, train_hi)
        test = segment_metrics(eq, reb, test_lo, None)
        full_sharpe = float((out.get("metrics") or {}).get("sharpe", 0.0))
        cells.append({"label": cb["label"], "params": cb["params"],
                      "train": train, "test": test, "full_sharpe": full_sharpe})
        print(f"    train S={train['sharpe']:.3f} (C={train['calmar']:.2f}, 换手{int(train['n_rebalance'])})"
              f" | test S={test['sharpe']:.3f} (C={test['calmar']:.2f}, 换手{int(test['n_rebalance'])})"
              f" | full S={full_sharpe:.3f}", file=sys.stderr)

    if not cells:
        print("无成功组合", file=sys.stderr)
        return 2

    print("\n===== 逐点结果（train 用于选，test 只看一次）=====\n")
    if args.damp == "short":
        _axis_table(short_grid, cells, args.tol, args.smooth, "short_term_damp_coef", "short")
    elif args.damp == "amount":
        _axis_table(amount_grid, cells, args.tol, args.smooth, "amount_surge_damp_coef", "amount")
    else:
        for lbl, lab in (("train", "sharpe"), ("train", "calmar"),
                         ("test", "sharpe"), ("test", "calmar")):
            _print_joint(short_grid, amount_grid, cells, lbl, lab)

    print(f"区间口径：train = 基期~{train_hi}，test = {test_lo}~期末。"
          f"系数在 train 段按邻域平滑后的 Sharpe 选取，避免在整段/单点上选最大造成过拟合。")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        dump = {"base": base, "mode": args.damp, "train_end": train_hi, "test_start": test_lo,
                "tol": args.tol, "smooth": args.smooth,
                "short_grid": short_grid, "amount_grid": amount_grid,
                "cells": cells}
        args.output.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果已落盘: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
