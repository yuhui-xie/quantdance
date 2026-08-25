"""ETF 动态 as-of 池：按调仓日动态重构"当时已上市"的 ETF 子池（可复用）。

背景：全市场 ETF 列表是"当前快照"。要在历史调仓日回测，不能把后来才上市
的 ETF 纳入早期决策日（幸存者偏差）。本模块把"按 asof 过滤出当时已存在标的"
抽成共享能力，供任意横截面策略的 ``select()`` 调用。

- 面板加载的是全市场（或 config 池）当前 ETF 列表；本模块按决策日 asof 用
  **K 线覆盖**判定存在性：某标的在 asof 前已有至少一根 K 线即视为当时已上市。
  于是**新上市 ETF 会在上市日之后自动加入候选**，符合"根据调仓时间动态拉取
  当前池子"的语义。
- ``panel_existing_at`` 是池子无关的：无论基池来自 ``universe="etf"``（全市场）
  还是 ``universe="etf_core"`` / ``config:*``（config 精选池），都能按 asof 过滤。
- 另提供行业推断 ``infer_industry`` 与流动性统计 ``liquidity_stats``，供
  ETF 筛选类策略复用。

本模块只依赖 stdlib 与 pandas，不依赖 ``app.strategies.base``，避免循环导入。
"""

from __future__ import annotations

from typing import Any, Mapping

import pandas as pd


def panel_existing_at(
    panel: Mapping[str, Mapping[str, pd.DataFrame]],
    asof: str,
) -> dict[str, Mapping[str, pd.DataFrame]]:
    """返回 asof 时点已存在的 panel 子集（仅保留 asof 前已有 K 线的标的）。

    ``panel[symbol]["value"]`` 需含 ``date`` 列（横截面引擎 ``_load_panel`` 提供，
    格式 ``YYYY-MM-DD``）。这就是"按调仓日动态拉取当时存在的 ETF 池"。
    """
    asof_s = str(asof)[:10]
    out: dict[str, Mapping[str, pd.DataFrame]] = {}
    for symbol, payload in panel.items():
        value = payload.get("value")
        if value is None or value.empty:
            continue
        dates = value["date"].astype(str).str[:10]
        if (dates <= asof_s).any():
            out[symbol] = payload
    return out


# 行业关键词映射：按顺序首个命中即定行业，未命中兜底「其他」。
# 宽基/指数名放最前（如「创业板50」应归宽基而非科技），再按板块细分。
_INDUSTRY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("宽基", ("沪深300", "上证50", "中证500", "中证1000", "中证2000", "中证A50",
             "A500", "创业板", "科创50", "科创100", "双创", "上证综指", "深证",
             "中证全指", "综指", "上证")),
    ("半导体", ("半导体", "芯片", "集成电路")),
    ("科技", ("人工智能", "AI", "云计算", "计算机", "软件", "通信", "5G",
             "大数据", "信息技术", "电子", "数字经济", "科技")),
    ("医药", ("医药", "医疗", "生物医药", "创新药", "中药", "疫苗", "医疗器械")),
    ("消费", ("消费", "白酒", "酒", "食品", "养殖", "农业", "家电", "零售", "可选消费")),
    ("金融", ("银行", "证券", "券商", "非银", "保险", "金融", "地产")),
    ("新能源", ("新能源", "光伏", "电池", "锂电", "风电", "储能", "新能源车", "碳中和")),
    ("资源周期", ("煤炭", "有色", "稀土", "钢铁", "化工", "石油", "能源", "矿业", "建材")),
    ("军工", ("军工", "国防")),
    ("贵金属", ("黄金", "白银", "贵金属")),
    ("债券", ("国债", "政金债", "信用债", "可转债", "转债", "债券")),
    ("红利", ("红利",)),
    ("海外", ("纳指", "纳斯达克", "标普", "恒生", "港股", "中概", "海外",
             "美国", "日经", "亚太", "全球")),
    ("传媒", ("传媒",)),
    ("房地产", ("房地产",)),
)


def infer_industry(name: str | None) -> str:
    """由 ETF 名称关键词推断行业板块；名称为空或未命中返回「其他」。"""
    n = (name or "").upper()
    for industry, keywords in _INDUSTRY_RULES:
        for kw in keywords:
            if kw in n:
                return industry
    return "其他"


def liquidity_stats(
    value_df: pd.DataFrame,
    asof: str,
    days: int,
) -> tuple[float | None, float | None]:
    """取截至 asof 最近 ``days`` 个交易日的日均 (成交额, 成交量)。

    ``amount``（成交额）缺列时返回 None（不参与成交额过滤）；成交量缺列同理。
    序列为空返回 (None, None)。
    """
    if "date" not in value_df.columns:
        return None, None
    v = value_df[["date"]].copy()
    v["date"] = pd.to_datetime(v["date"], errors="coerce")
    v = v[v["date"] <= pd.Timestamp(str(asof)[:10])]
    v = v.dropna(subset=["date"])
    v = v.sort_values("date").drop_duplicates("date", keep="last")
    if v.empty:
        return None, None
    recent_dates = v.tail(days)["date"]

    def _daily_mean(col: str) -> float | None:
        if col not in value_df.columns:
            return None
        m = value_df[["date", col]].copy()
        m["date"] = pd.to_datetime(m["date"], errors="coerce")
        m[col] = pd.to_numeric(m[col], errors="coerce")
        m = m[m["date"].isin(recent_dates)]
        s = m[col].dropna().astype(float)
        return float(s.mean()) if not s.empty else None

    return _daily_mean("amount"), _daily_mean("volume")


def asof_pool_meta() -> dict[str, Any]:
    """返回本模块的池语义元数据，供文档/诊断用。"""
    return {
        "mode": "dynamic-asof",
        "existence": "首根 K 线 ≤ 决策日即视为当时已上市",
        "reused_by": ("etf_filter",),
        "industry_rules": len(_INDUSTRY_RULES),
    }
