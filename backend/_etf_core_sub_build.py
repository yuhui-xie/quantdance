"""临时脚本：构建细分行业 ETF 精选池 etf_core_sub。

与 etf_core（每个大类一只）不同，etf_core_sub 把大类拆成可交易的细分行业，
每个细分行业只取一只、选近 1 年日均成交额最高的一只（细分行业多为 2016 后上市，
故不适用 etf_core 的 2016 验证，改为按近期流动性挑选，以保证实际可轮动）。

挑选规则：
- 近 1 年窗口 W = [2025-08-26, 2026-08-26]，取窗口内日均成交额最高者。
- 要求窗口内交易日 >= 200（确保流动性真实可交易）。
- 要求成立 >= 2024-12-31（避免选到刚上市、规模不稳的次新基金）。
- 排除货币/现金理财类；无法归入任何细分行业的跳过。

输出：
- out/etf_core_sub_candidates.json：全量细分候选。
- out/etf_core_sub_new.json：每细分行业一只的挑选结果。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date

from app.data_sources.market_data import (
    MarketDataError,
    fetch_a_share_daily,
    fetch_etf_universe,
)
from app.strategies.cross_section.industry_pool import classify_industry

T = date(2026, 8, 26)
W_START = date(2025, 8, 26)        # 近 1 年窗口起点
MIN_BARS = 200                     # 窗口内最少交易日
LISTED_BEFORE = date(2024, 12, 31)  # 成立下限（非次新）

_EXCLUDE_NAMES = ("货币", "理财", "现金", "逆回购", "同业存单")


def classify(name: str) -> str | None:
    """细分行业分类（单一来源 app.strategies.cross_section.industry_pool）。"""
    return classify_industry(name)


def main() -> int:
    rows, note = fetch_etf_universe(5000, seed=None)
    print(f"全市场 ETF {len(rows)} 只 | {note}", file=sys.stderr)

    records: list[dict] = []
    processed = 0
    for row in rows:
        sym = row["symbol"]
        name = row.get("name") or ""
        if any(kw in name for kw in _EXCLUDE_NAMES):
            continue
        cat = classify(name)
        if cat is None:
            continue
        try:
            df = fetch_a_share_daily(sym, limit=5000)
        except MarketDataError:
            continue
        if df.empty or "amount" not in df.columns:
            continue
        first = df.index[0].date()
        if first > LISTED_BEFORE:
            continue  # 次新，跳过
        amount = df["amount"].astype(float)
        win = amount.loc[(df.index.date >= W_START) & (df.index.date <= T)]
        if len(win) < MIN_BARS:
            continue
        records.append({
            "symbol": sym, "name": name, "category": cat,
            "first_date": str(first),
            "avg_amt_1y": round(float(win.mean()), 0),
            "lifetime_avg_amt": round(float(amount.mean()), 0),
        })
        processed += 1
        if processed % 100 == 0:
            print(f"已处理 {processed} 只", file=sys.stderr)

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_cat[r["category"]].append(r)

    # 跨境基金标记（QDII/港股通/恒生等），用于优先 A 股细分 ETF
    XB_MARKERS = ("QDII", "港股通", "恒生", "中韩", "纳斯达克", "标普", "港股", "海外", "全球")

    def is_ashare(r: dict) -> bool:
        return not any(m in r["name"] for m in XB_MARKERS)

    selection: dict[str, dict] = {}
    detail: dict[str, list] = {}
    for cat, recs in sorted(by_cat.items()):
        recs.sort(key=lambda r: -r["avg_amt_1y"])
        ashare = [r for r in recs if is_ashare(r)]
        pool = ashare if ashare else recs  # 优先 A 股细分；无 A 股候选才退回跨境
        pick = {**pool[0], "rule": "近1年日均成交额最高" + ("（A股）" if ashare else "（跨境）")}
        selection[cat] = pick
        detail[cat] = [{
            "symbol": r["symbol"], "name": r["name"], "first_date": r["first_date"],
            "avg_amt_1y": r["avg_amt_1y"], "lifetime_avg_amt": r["lifetime_avg_amt"],
        } for r in recs]

    with open("out/etf_core_sub_candidates.json", "w", encoding="utf-8") as fh:
        json.dump(detail, fh, ensure_ascii=False, indent=2)
    with open("out/etf_core_sub_new.json", "w", encoding="utf-8") as fh:
        json.dump(selection, fh, ensure_ascii=False, indent=2)

    print("\n=== 每细分行业挑选结果 ===", file=sys.stderr)
    for cat, rec in sorted(selection.items()):
        print(
            f"{cat:<6} {rec['symbol']} {rec['name'][:18]:<18} "
            f"first={rec['first_date']} 近1年日均={rec['avg_amt_1y']/1e8:.2f}亿",
            file=sys.stderr,
        )
    print(f"\n候选: out/etf_core_sub_candidates.json\n挑选: out/etf_core_sub_new.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
