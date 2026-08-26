"""临时脚本：按用户要求重建 etf_core 精选池（v2，两层挑选）。

规则：
- 最早时间 T = 2016-08-26。验证窗口 W = [2015-08-26, 2016-08-26]（回看 1 年）。
- 每个行业/大类一只。同类别只取一只（天然分散）。
- 优先层：该行业有【2016-08 前已上市】的 ETF 时，取窗口 W 内日均成交额最高的一只
  （即"用最早时间验证，回看1年，选交易最活跃"）。
- 回退层：该行业没有 2016-08 前已上市的 ETF（如半导体/新能源/传媒/房地产/资源周期），
  取成立满 5 年（<=2021-08-26）且历史日均成交额最高的一只作代表，保证每个行业都有覆盖。
- 排除货币基金/现金理财类（"货币/理财/现金/金" 属宽泛匹配误伤，单独剔除）。

输出：
- out/etf_core_candidates.json：全量候选（含所属类别、首根K线、窗口/历史成交额）。
- out/etf_core_new.json：自动挑选结果（每类一只，含 rule=priority|fallback）。
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

T = date(2016, 8, 26)          # 最早时间（2016-08 验证）
W_START = date(2015, 8, 26)
W_END = date(2016, 8, 26)
AGE5 = date(2021, 8, 26)       # 成立满 5 年的回退边界
MIN_WINDOW_BARS = 60           # 2015-16 窗口至少 60 个交易日才算有效

# 货币基金/现金理财：名称含这些关键词的剔除（不属于行业轮动池）
_EXCLUDE_NAMES = ("货币", "理财", "现金", "逆回购", "同业存单", "货币E")


def classify(name: str) -> str:
    n = (name or "").upper()
    rules: list[tuple[str, tuple[str, ...]]] = [
        ("石油", ("石油", "油气", "原油", "能源")),
        ("黄金", ("黄金", "贵金属")),
        ("债券", ("国债", "债", "转债", "信用", "政金")),
        ("红利", ("红利",)),
        ("房地产", ("房地产", "地产")),
        # 宽基放特定行业关键词之后，且不把"中证全指"当宽基（它常作行业ETF前缀，
        # 如"中证全指证券公司/医药/房地产"）。
        ("宽基", ("沪深300", "上证50", "中证500", "中证1000", "中证2000", "中证A50",
                  "A500", "创业板", "科创50", "科创100", "双创", "上证", "深证",
                  "综指")),
        ("半导体", ("半导体", "芯片", "集成电路")),
        ("科技", ("人工智能", "AI", "云计算", "计算机", "软件", "通信", "5G",
                  "大数据", "信息技术", "电子", "数字经济", "科技")),
        ("医药", ("医药", "医疗", "创新药", "中药", "生物", "疫苗", "医疗器械")),
        ("消费", ("消费", "白酒", "酒", "食品", "养殖", "农业", "家电", "零售",
                  "可选消费", "饮料")),
        ("金融", ("银行", "证券", "券商", "非银", "保险", "金融")),
        ("新能源", ("新能源", "光伏", "电池", "锂", "风电", "储能", "新能源车", "碳中和")),
        ("资源周期", ("煤炭", "有色", "稀土", "钢铁", "化工", "建材", "矿业", "材料")),
        ("军工", ("军工", "国防")),
        ("海外", ("纳指", "纳斯达克", "标普", "恒生", "港股", "中概", "海外",
                  "美国", "日经", "亚太", "全球", "东南亚")),
        ("传媒", ("传媒",)),
    ]
    for cat, kws in rules:
        for kw in kws:
            if kw.upper() in n:
                return cat
    return "其他"


def main() -> int:
    rows, note = fetch_etf_universe(5000, seed=None)
    print(f"全市场 ETF {len(rows)} 只 | {note}", file=sys.stderr)

    records: list[dict] = []
    processed = 0
    for row in rows:
        sym = row["symbol"]
        name = row.get("name") or ""
        if any(kw in name for kw in _EXCLUDE_NAMES):
            continue  # 排除货币/现金理财类
        cat = classify(name)
        if cat == "其他":
            continue  # 无法归类的（多为新题材/小众）不入池
        try:
            df = fetch_a_share_daily(sym, limit=5000)
        except MarketDataError:
            continue
        if df.empty or "amount" not in df.columns:
            continue
        first = df.index[0].date()
        amount = df["amount"].astype(float)

        def _window_mean(d0: date, d1: date) -> float | None:
            m = (df.index.date >= d0) & (df.index.date <= d1)
            win = amount.loc[m]
            if len(win) < MIN_WINDOW_BARS:
                return None
            return float(win.mean())

        records.append({
            "symbol": sym,
            "name": name,
            "category": cat,
            "first_date": str(first),
            "avg_amt_2015_16": _window_mean(W_START, W_END),   # 最早时间回看1年
            "lifetime_avg_amt": round(float(amount.mean()), 0),  # 全史日均（回退用）
        })
        processed += 1
        if processed % 100 == 0:
            print(f"已处理 {processed} 只", file=sys.stderr)

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_cat[r["category"]].append(r)

    # 挑选
    selection: dict[str, dict] = {}
    detail: dict[str, list] = {}
    for cat, recs in sorted(by_cat.items()):
        recs.sort(key=lambda r: -(r["avg_amt_2015_16"] if r["avg_amt_2015_16"] else 0))
        priority = [r for r in recs if r["avg_amt_2015_16"] is not None]
        if priority:
            pick = priority[0]
            pick = {**pick, "rule": "priority(2016验证)"}
        else:
            # 回退：成立满5年且历史日均最高
            recs5 = [r for r in recs if date.fromisoformat(r["first_date"]) <= AGE5]
            pool = recs5 or recs
            pool.sort(key=lambda r: -r["lifetime_avg_amt"])
            pick = {**pool[0], "rule": f"fallback(无2016数据，取{pool[0]['first_date']}起)"}
        selection[cat] = pick
        detail[cat] = [{
            "symbol": r["symbol"], "name": r["name"], "first_date": r["first_date"],
            "avg_amt_2015_16": r["avg_amt_2015_16"], "lifetime_avg_amt": r["lifetime_avg_amt"],
        } for r in recs]

    with open("out/etf_core_candidates.json", "w", encoding="utf-8") as fh:
        json.dump(detail, fh, ensure_ascii=False, indent=2)
    with open("out/etf_core_new.json", "w", encoding="utf-8") as fh:
        json.dump(selection, fh, ensure_ascii=False, indent=2)

    print("\n=== 每类挑选结果 ===", file=sys.stderr)
    for cat, rec in sorted(selection.items()):
        a = rec["avg_amt_2015_16"]
        metric = f"2015-16日均额={a/1e8:.3f}亿" if a else f"历史日均额={rec['lifetime_avg_amt']/1e8:.3f}亿"
        print(f"{cat:<4} {rec['symbol']} {rec['name']:<16} first={rec['first_date']} {metric} [{rec['rule']}]", file=sys.stderr)
    print(f"\n候选: out/etf_core_candidates.json\n挑选: out/etf_core_new.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
