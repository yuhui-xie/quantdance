"""对比：单一止损 vs 分批止损 vs 移动止盈，在 etf_core 与 etf_core_sub 两池。"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.backtest_runner import run_backtest_request
from app.schemas import BacktestRequest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent / "backtest_shared_etf_rotation.json"
RECENT = 120

CONTROLS = [
    ("仅单一止损10%",     {"stop_loss_pct": 0.10, "stop_loss_tier_pct": None,
                          "stop_loss_tier_sell_fraction": None,
                          "take_profit_arm_pct": None, "take_profit_exit_pct": None}),
    ("批止峰值10/1-3",    {"stop_loss_pct": None, "stop_loss_tier_pct": 0.10,
                          "stop_loss_tier_sell_fraction": 1 / 3,
                          "stop_loss_tier_anchor": "peak",
                          "take_profit_arm_pct": None, "take_profit_exit_pct": None}),
    ("批止峰值10/1-2",    {"stop_loss_pct": None, "stop_loss_tier_pct": 0.10,
                          "stop_loss_tier_sell_fraction": 1 / 2,
                          "stop_loss_tier_anchor": "peak",
                          "take_profit_arm_pct": None, "take_profit_exit_pct": None}),
    ("批止峰值20/1-3",    {"stop_loss_pct": None, "stop_loss_tier_pct": 0.20,
                          "stop_loss_tier_sell_fraction": 1 / 3,
                          "stop_loss_tier_anchor": "peak",
                          "take_profit_arm_pct": None, "take_profit_exit_pct": None}),
    ("移动止盈10/2",      {"stop_loss_pct": 0.10, "stop_loss_tier_pct": None,
                          "stop_loss_tier_sell_fraction": None,
                          "take_profit_arm_pct": 0.10, "take_profit_exit_pct": 0.02}),
]
UNIVERSES = ["etf_core", "etf_core_sub"]


def _pct(v):
    return f"{v * 100:8.2f}%" if v is not None and v == v else "      -"


def run(base, universe, ctl):
    body = copy.deepcopy(base)
    body["universe"] = universe
    sp = dict(body.get("strategy_params") or {})
    sp.update({"decision_frequency": "monthly", "top_n": 1,
               "llt_period": 45, "llt_window": 30, "rotate_threshold": 0.9})
    body["strategy_params"] = sp
    body.update(ctl)
    body["output_options"] = {"json": False}
    return run_backtest_request(BacktestRequest.model_validate(body))


def summarize(out):
    m = out.get("metrics") or {}
    tr = out.get("trades") or []
    eq = out.get("equity") or []
    tail = [float(e["equity"]) for e in eq[-RECENT:]] if eq else []
    recent = (tail[-1] / tail[0] - 1) if len(tail) > 1 else float("nan")
    semi = sum(float(t.get("pnl", 0)) for t in tr
               if t.get("symbol") == "512480" and t.get("side") == "sell")
    return {"total": m.get("total_return"), "dd": m.get("max_drawdown"),
            "sharpe": m.get("sharpe"), "recent": recent, "semi": semi,
            "tiered": sum(1 for t in tr if t.get("reason") == "tiered_stop"),
            "n": len(tr)}


def main():
    base = json.loads(BASE.read_text(encoding="utf-8"))
    for uni in UNIVERSES:
        print(f"\n========== 池: {uni} ==========")
        hdr = ["控制", "总收益", "回撤", "Sharpe", "近120d", "半导盈亏", "批档/单止", "交易"]
        print(" | ".join(h.ljust(10) for h in hdr))
        print(" | ".join("-" * 10 for _ in hdr))
        for label, ctl in CONTROLS:
            s = summarize(run(base, uni, ctl))
            print(" | ".join([
                label.ljust(10),
                _pct(s["total"]), _pct(s["dd"]),
                f"{s['sharpe']:>8.2f} ", _pct(s["recent"]),
                f"{s['semi']:>8,.0f} ",
                f"{s['tiered']:>4}/{s['n']-s['tiered']:<4}".ljust(10),
                f"{s['n']:>5.0f}  ",
            ]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
