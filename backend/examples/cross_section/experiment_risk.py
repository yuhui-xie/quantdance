"""ETF 轮动「半导体亏损」诊断 + 风控参数扫描。

针对用户反馈：monthly 调仓下半导体 ETF（512480）近期回落时持仓套牢、亏损多。
本脚本：
1. 跑 monthly baseline，打印近期（末段窗口）净值回撤与 512480 的成交流水/已实现盈亏；
2. 扫描共享引擎的每日止损（stop_loss_pct）与追踪止盈（take_profit_arm/exit），
   看能否截断半导体回落、改善近期与全程绩效。

用法（backend/ 下）：
    .venv/Scripts/python.exe -m examples.cross_section.experiment_risk
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.backtest_runner import run_backtest_request
from app.schemas import BacktestRequest

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_BASE = BASE_DIR / "backtest_shared_etf_rotation.json"

SEMI = "512480"          # 半导体ETF
TOP_N = 1                 # 默认集中持仓，最暴露半导体风险
RECENT_DAYS = 120         # "近期"窗口（约半年交易日）

STOP_LOSS_SWEEP = [None, 0.05, 0.08, 0.10, 0.12, 0.15]
# (arm, exit)：trailing 止盈——浮盈触及 +arm 后，回落至 +exit 卖出
TAKE_PROFIT_SWEEP = [(None, None), (0.10, 0.02), (0.15, 0.03), (0.20, 0.05)]


def load_base(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_case(
    base: dict[str, Any],
    *,
    top_n: int,
    stop_loss_pct: float | None,
    arm: float | None,
    exit_level: float | None,
) -> dict[str, Any]:
    body = copy.deepcopy(base)
    body["strategy_params"] = dict(body.get("strategy_params") or {})
    body["strategy_params"].update(
        {"decision_frequency": "monthly", "top_n": top_n, "rotate_threshold": 0.9}
    )
    body["stop_loss_pct"] = stop_loss_pct
    body["take_profit_arm_pct"] = arm
    body["take_profit_exit_pct"] = exit_level
    body["output_options"] = {"json": False}
    return run_backtest_request(BacktestRequest.model_validate(body))


def _pct(v: float) -> str:
    return f"{v * 100:7.2f}%"


def summarize(out: dict[str, Any], *, recent_days: int) -> dict[str, Any]:
    metrics = out.get("metrics") or {}
    trades = out.get("trades") or []
    equity = out.get("equity") or []
    # 近期窗口：末段 recent_days 个交易日的净值
    eq_tail = [float(e["equity"]) for e in equity[-recent_days:]] if equity else []
    recent_ret = (eq_tail[-1] / eq_tail[0] - 1) if len(eq_tail) > 1 else float("nan")
    # 半导体已实现盈亏合计
    semi_pnl = sum(float(t.get("pnl", 0.0)) for t in trades
                   if t.get("symbol") == SEMI and t.get("side") == "sell")
    stop_loss_n = sum(1 for t in trades if t.get("reason") == "stop_loss")
    take_profit_n = sum(1 for t in trades if t.get("reason") == "take_profit")
    return {
        "total_return": metrics.get("total_return", float("nan")),
        "max_drawdown": metrics.get("max_drawdown", float("nan")),
        "sharpe": metrics.get("sharpe", float("nan")),
        "recent_ret": recent_ret,
        "semi_pnl": semi_pnl,
        "stop_loss_n": stop_loss_n,
        "take_profit_n": take_profit_n,
    }


def main(base_path: Path | None = None) -> int:
    base_path = base_path or DEFAULT_BASE
    base = load_base(base_path)
    print(f"基线请求: {base_path}")
    print(f"区间: {base.get('start_date')} ~ {base.get('end_date')} | 近期窗口: {RECENT_DAYS} 交易日 | 半导体: {SEMI}\n")

    # ---- 1) baseline 诊断：半导体成交 + 回撤位置 + 期末持仓 ----
    base_out = run_case(base, top_n=TOP_N, stop_loss_pct=None, arm=None, exit_level=None)
    trades = base_out.get("trades") or []
    semi_trades = [t for t in trades if t.get("symbol") == SEMI]
    print(f"== baseline (monthly, top_n={TOP_N}, 无风控) ==")
    b = summarize(base_out, recent_days=RECENT_DAYS)
    print(f"  全程: 总收益 {_pct(b['total_return'])} | 回撤 {_pct(b['max_drawdown'])} | Sharpe {b['sharpe']:.2f}")
    print(f"  近期({RECENT_DAYS}d): 收益 {_pct(b['recent_ret'])} | 半导体已实现盈亏 {b['semi_pnl']:,.0f}")
    # 回撤区间：找净值峰值后回落到谷底的时间段
    eq = [(e["date"], float(e["equity"])) for e in (base_out.get("equity") or [])]
    if eq:
        peak, valley = eq[0][1], 1.0
        peak_d = valley_d = eq[0][0]
        for d, v in eq:
            if v > peak:
                peak, peak_d = v, d
            if peak and v / peak < valley:
                valley, valley_d = v / peak, d
        print(f"  最大回撤发生在 {peak_d} 峰值 -> {valley_d} 谷底（幅度 {_pct(1 - valley)}）")
    # 期末持仓
    holdings = base_out.get("holdings") or []
    if holdings:
        last_pos = holdings[-1].get("positions") or {}
        last_pos = {k: v for k, v in last_pos.items() if v > 0}
        print(f"  期末({holdings[-1]['date']})持仓: {last_pos or '空仓(现金)'}")
    print(f"  半导体全部成交流水（{len(semi_trades)} 笔）:")
    for t in semi_trades:
        pnl = f"pnl={t['pnl']:,.0f}" if t["side"] == "sell" else "买入"
        print(f"    {t['date']} {t['side']:<4} 价 {t['price']:8.3f} 股 {t['shares']:7.0f} {pnl} [{t['reason']}]")
    print()

    # ---- 2) 止损扫描 ----
    print("== 每日止损扫描 (monthly top_n=%d) ==" % TOP_N)
    hdr = ["stop_loss", "总收益", "回撤", "Sharpe", "近期收益", "半导体盈亏", "止损数"]
    print("  " + " | ".join(f"{h:<12}" for h in hdr))
    rows = []
    for sl in STOP_LOSS_SWEEP:
        out = run_case(base, top_n=TOP_N, stop_loss_pct=sl, arm=None, exit_level=None)
        s = summarize(out, recent_days=RECENT_DAYS)
        rows.append((sl, s))
        print("  " + " | ".join([
            f"{('无' if sl is None else f'{sl:.0%}'):<12}",
            f"{_pct(s['total_return']):>12}",
            f"{_pct(s['max_drawdown']):>12}",
            f"{s['sharpe']:>12.2f}",
            f"{_pct(s['recent_ret']):>12}",
            f"{s['semi_pnl']:>12,.0f}",
            f"{s['stop_loss_n']:>12}",
        ]))
    print()

    # ---- 3) 追踪止盈扫描 ----
    print("== 追踪止盈扫描 (monthly top_n=%d) ==" % TOP_N)
    print("  " + " | ".join(f"{h:<14}" for h in ["arm/exit", "总收益", "回撤", "Sharpe", "近期收益", "半导体盈亏", "止盈数"]))
    for arm, ex in TAKE_PROFIT_SWEEP:
        out = run_case(base, top_n=TOP_N, stop_loss_pct=None, arm=arm, exit_level=ex)
        s = summarize(out, recent_days=RECENT_DAYS)
        print("  " + " | ".join([
            f"{(f'{arm:.0%}/{ex:.0%}' if arm else '无'):<14}",
            f"{_pct(s['total_return']):>14}",
            f"{_pct(s['max_drawdown']):>14}",
            f"{s['sharpe']:>14.2f}",
            f"{_pct(s['recent_ret']):>14}",
            f"{s['semi_pnl']:>14,.0f}",
            f"{s['take_profit_n']:>14}",
        ]))
    return 0


if __name__ == "__main__":
    base_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    raise SystemExit(main(base_path))
