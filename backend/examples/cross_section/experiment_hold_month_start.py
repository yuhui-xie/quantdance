"""实验：ETF 轮动改为「只持有每月月初几天」是否整体更优。

思路：月初(第1交易日)日收益显著为正，月中≈0/负。若策略每月首日选 top-1 后，
只持有当月前 K 个交易日、其余时间持现金，整体收益/夏普/回撤是否优于持有整月。

做法：
- 用真实策略的「每月首日」选股（decision_frequency=monthly_nth, nth=1, top_n=1），
  取每次调仓日的 target 作为当月持仓；
- 对 K ∈ {2,3,5,8,10,15,21} 与「整月(下个调仓日卖出)」分别构建逐交易日权益曲线；
- 用 metrics_from_equity 计算统一指标对比。

说明：买点固定在调仓日收盘（决策依赖当日收盘），故第1交易日自身涨幅无法捕捉；
「整月」= 买在当月首日收盘、卖在下月首日收盘（恰好把下月首日的强势日作为末段）。

用法（backend/ 下）：
    .venv/Scripts/python.exe -m examples.cross_section.experiment_hold_month_start
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.backtest_engine import metrics_from_equity
from app.backtest_runner import run_backtest_request
from app.data_sources.market_data import fetch_a_share_daily, normalize_a_share_symbol
from app.schemas import BacktestRequest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

K_LIST = [2, 3, 5, 8, 10, 15, 21, "full"]
BASE = Path(__file__).resolve().parent / "backtest_shared_etf_rotation.json"


def get_day1_picks(universe, start, end):
    """跑一次真实策略，取每月首日调仓的 (date -> target symbol)。"""
    base = json.loads(BASE.read_text(encoding="utf-8"))
    base["universe"] = universe
    base["start_date"], base["end_date"] = start, end
    base["strategy_params"] = dict(base.get("strategy_params") or {})
    base["strategy_params"].update(
        {"decision_frequency": "monthly_nth", "decision_month_nth": 1, "top_n": 1}
    )
    base["output_options"] = {"json": False}
    req = BacktestRequest.model_validate(base)
    out = run_backtest_request(req)
    picks = {}
    for rb in out["rebalances"]:
        tgt = (rb.get("targets") or [None])[0]
        if tgt:
            picks[rb["date"]] = tgt
    return picks, out


def load_close_map(symbols, start, end):
    close_map = {}
    for s in symbols:
        df = fetch_a_share_daily(s, limit=5000).sort_index()
        df = df.loc[(df.index >= start) & (df.index <= end)]
        close_map[s] = {d.isoformat()[:10]: float(c) for d, c in df["close"].items()}
    return close_map


def trading_days_of_month(calendar, month):
    return [d for d in calendar if d.startswith(month)]


def build_equity(picks, close_map, calendar, k):
    """返回逐交易日权益曲线(list[float])，按买入=调仓日收盘、卖出=第k个交易日收盘。"""
    initial_cash = 100000.0
    n = len(calendar)
    eq = np.full(n, np.nan, dtype=float)  # NaN=未赋值，末段向前填充
    cash = initial_cash
    pick_dates = sorted(picks)
    decision_idx = {d: i for i, d in enumerate(calendar)}
    for i, d in enumerate(pick_dates):
        tgt = picks[d]
        month = d[:7]
        mdays = trading_days_of_month(calendar, month)
        px = close_map.get(tgt) or {}
        pD = px.get(d)
        iD = decision_idx.get(d)
        if pD is None or pD <= 0 or iD is None:
            continue
        # 卖出日：full=下个调仓日（末段取最后一个有价的交易日）；K=当月第k个交易日
        if k == "full":
            if i + 1 < len(pick_dates):
                exit_day = pick_dates[i + 1]
            else:
                cands = [day for day in calendar[iD + 1:] if px.get(day)]
                exit_day = cands[-1] if cands else d
        else:
            ei = min(k - 1, len(mdays) - 1)
            while ei >= 0 and px.get(mdays[ei]) is None:
                ei -= 1
            exit_day = mdays[ei] if ei >= 0 else d
        iE = decision_idx.get(exit_day)
        if iE is None or iE <= iD:
            continue
        pE = px.get(exit_day)
        if pE is None or pE <= 0:
            continue
        seg_ret = pE / pD - 1.0
        # 持有期（d 收盘买 → 持有到 exit_day 收盘卖）：eq[iD..iE] 按 tgt 日收益增长
        for j in range(iD, iE + 1):
            dd = calendar[j]
            pj = px.get(dd)
            if pj is not None and pj > 0:
                eq[j] = cash * (pj / pD)
        cash *= (1.0 + seg_ret)
        # 卖出后到下一调仓日（或末段到序列末尾）持现金（flat）
        nxt = decision_idx.get(pick_dates[i + 1]) if i + 1 < len(pick_dates) else n
        for j in range(iE + 1, nxt):
            eq[j] = cash
    # 向前填充未覆盖的尾部/缝隙（用最近现金值）
    out = np.empty(n, dtype=float)
    last = initial_cash
    for j in range(n):
        if np.isnan(eq[j]):
            out[j] = last
        else:
            last = eq[j]
            out[j] = eq[j]
    return out


def main(universe="etf_core_sub"):
    base = json.loads(BASE.read_text(encoding="utf-8"))
    start, end = base["start_date"], base["end_date"]
    names = (json.loads((_BACKEND_ROOT / "config" / ("etf_core_sub_pool.json" if universe == "etf_core_sub" else "etf_core_pool.json")).read_text(encoding="utf-8"))).get("names") or {}
    symbols = sorted(set(normalize_a_share_symbol(c) for c in names))

    print(f"池: {universe}  区间 {start}~{end}  top_n=1  买入=每月首日收盘", flush=True)
    picks, out = get_day1_picks(universe, start, end)
    print(f"决策(每月首日)调仓 {len(picks)} 次，真实策略月度基线总收益 "
          f"{out['metrics'].get('total_return', 0) * 100:.1f}%", flush=True)
    print("加载行情...", file=sys.stderr, flush=True)
    close_map = load_close_map(symbols, start, end)
    calendar = sorted(set().union(*(set(m) for m in close_map.values())))
    # 日历至少覆盖 picks 与区间
    calendar = sorted(set(calendar) | set(picks) | {start, end})
    calendar = [d for d in calendar if start <= d <= end]
    print(f"交易日 {len(calendar)} 个\n", flush=True)

    header = ["持有方式", "总收益", "年化", "回撤", "Sharpe", "收益/回撤", "在市场中占比"]
    widths = [len(h) + 2 for h in header]
    print(" | ".join(h.ljust(w) for h, w in zip(header, widths)))
    print(" | ".join("-" * w for w in widths))
    rows = {}
    for k in K_LIST:
        eq = build_equity(picks, close_map, calendar, k)
        metrics = metrics_from_equity(np.asarray(eq, float), 100000.0, [], calendar)
        tr = metrics.get("total_return", 0) * 100
        ann = metrics.get("annualized_return", 0) * 100
        mdd = metrics.get("max_drawdown", 0) * 100
        sh = metrics.get("sharpe", 0)
        rdd = metrics.get("return_drawdown_ratio", 0)
        # 在市场中占比：eq 单调上涨段天数比例（粗算持有时长）
        invested = sum(1 for j in range(1, len(eq)) if eq[j] != eq[j - 1])
        pct = invested / max(len(eq) - 1, 1)
        label = "整月(基准)" if k == "full" else f"前{k}个交易日"
        print(f"{label:<10} | {tr:7.1f}% | {ann:6.1f}% | {mdd:6.1f}% | {sh:6.2f} | {rdd:6.2f} | {pct:9.0%}")
        rows[k] = (tr, ann, sh, mdd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "etf_core_sub"))
