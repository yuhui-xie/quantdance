"""订单开工拐点：合同负债→毛利率→经营现金流→存货→综合评分，周期等权调仓。"""

from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import BaseModel, Field, model_validator

from app.data_sources.financial_reports import asof_financial_row
from app.portfolio.base import PortfolioSelectContext, PortfolioStrategySpec
from app.portfolio.common import asof_tradeable_row, is_st_stock


class OrderInflectionParams(BaseModel):
    top_n: int = Field(10, ge=1, le=50)
    # 第一步：合同负债 —— 明天有活干
    min_contract_liab: float = Field(0.0, ge=0, description="合同负债绝对值下限（元）")
    min_contract_liab_yoy: float = Field(
        0.0, description="合同负债同比增速下限（%）；0 表示至少不萎缩"
    )
    require_contract_liab: bool = Field(True, description="必须有合同负债科目")
    # 第二步：毛利率 —— 明天的活赚不赚钱
    min_gross_margin: float = Field(0.15, ge=-1.0, le=1.0, description="毛利率下限（小数）")
    min_gross_margin_yoy_delta: float = Field(
        -0.02, ge=-1.0, le=1.0, description="毛利率同比变动下限（小数，默认允许略降）"
    )
    require_gross_margin: bool = True
    # 第三步：经营现金流 —— 干活的钱够不够
    require_positive_ocf: bool = Field(True, description="要求经营现金流净额 > 0")
    min_ocf_yoy: float | None = Field(
        None, description="经营现金流同比下限（%）；None 表示不限制"
    )
    # 第四步：存货 —— 马上要开工（用存货同比近似备货，无明细结构时）
    min_inventory_yoy: float = Field(
        0.0, description="存货同比下限（%）；备货信号"
    )
    max_inventory_yoy: float = Field(
        80.0, description="存货同比上限（%）；过高疑似滞销"
    )
    require_inventory: bool = True
    # 第五步：综合研判 —— 拐点评分权重（东财 YOY 为百分数口径）
    w_contract: float = Field(1.0, ge=0)
    w_margin: float = Field(1.0, ge=0)
    w_ocf: float = Field(0.5, ge=0)
    w_inventory: float = Field(0.3, ge=0)
    max_notice_age_days: int = Field(
        200, ge=30, le=400, description="财报公告日距调仓日最大天数"
    )
    min_price: float = Field(2.0, ge=0)
    max_price: float = Field(100.0, gt=0)
    max_market_cap: float | None = Field(
        None, description="总市值上限（元）；None 不限制"
    )
    exclude_st: bool = True
    exclude_limit: bool = True
    exclude_suspended: bool = True
    limit_pct_threshold: float = Field(9.5, ge=1.0, le=30.0)

    @model_validator(mode="after")
    def check_ranges(self) -> OrderInflectionParams:
        if self.max_price < self.min_price:
            raise ValueError("max_price 不能小于 min_price")
        if self.max_inventory_yoy < self.min_inventory_yoy:
            raise ValueError("max_inventory_yoy 不能小于 min_inventory_yoy")
        if self.w_contract + self.w_margin + self.w_ocf + self.w_inventory <= 0:
            raise ValueError("评分权重之和须大于 0")
        return self


def _inflection_score(
    *,
    contract_liab_yoy: float,
    gross_margin_yoy_delta: float,
    netcash_operate_yoy: float | None,
    inventory_yoy: float,
    params: OrderInflectionParams,
) -> float:
    """
    综合拐点分：订单加速 + 毛利改善 + 现金流改善 + 适度备货。
    合同负债/存货/OCF 的 YOY 为东财百分数；毛利率变动为小数。
    """
    ocf_yoy = 0.0 if netcash_operate_yoy is None else float(netcash_operate_yoy)
    # 存货过高扣分：超过上限一半后边际贡献衰减
    inv = float(inventory_yoy)
    inv_soft = inv if inv <= params.max_inventory_yoy * 0.5 else params.max_inventory_yoy * 0.5
    return (
        params.w_contract * float(contract_liab_yoy)
        + params.w_margin * float(gross_margin_yoy_delta) * 100.0
        + params.w_ocf * ocf_yoy
        + params.w_inventory * inv_soft
    )


def select_order_inflection(
    asof: str,
    ctx: PortfolioSelectContext,
    params: OrderInflectionParams,
) -> tuple[list[str], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for symbol, payload in ctx.panel.items():
        value_df = payload.get("value")
        fin_df = payload.get("financials")
        if value_df is None or value_df.empty:
            continue
        if fin_df is None or (isinstance(fin_df, pd.DataFrame) and fin_df.empty):
            continue
        if params.exclude_st and is_st_stock(ctx.names.get(symbol)):
            continue

        row = asof_tradeable_row(
            value_df,
            asof,
            exclude_suspended=params.exclude_suspended,
            exclude_limit=params.exclude_limit,
            limit_pct_threshold=params.limit_pct_threshold,
        )
        if row is None:
            continue

        close = float(row["close"])
        market_cap = row.get("market_cap")
        if close < params.min_price or close > params.max_price:
            continue
        if market_cap is None:
            continue
        if params.max_market_cap is not None and float(market_cap) > params.max_market_cap:
            continue

        fin = asof_financial_row(
            fin_df,
            asof,
            max_notice_age_days=params.max_notice_age_days,
        )
        if fin is None:
            continue

        # 第一步：合同负债
        cl = fin.get("contract_liab")
        cl_yoy = fin.get("contract_liab_yoy")
        if params.require_contract_liab:
            if cl is None or cl <= params.min_contract_liab:
                continue
            if cl_yoy is None or float(cl_yoy) < params.min_contract_liab_yoy:
                continue

        # 第二步：毛利率
        gm = fin.get("gross_margin")
        gm_delta = fin.get("gross_margin_yoy_delta")
        if params.require_gross_margin:
            if gm is None or float(gm) < params.min_gross_margin:
                continue
            if gm_delta is None or float(gm_delta) < params.min_gross_margin_yoy_delta:
                continue

        # 第三步：经营现金流
        ocf = fin.get("netcash_operate")
        ocf_yoy = fin.get("netcash_operate_yoy")
        if params.require_positive_ocf and (ocf is None or float(ocf) <= 0):
            continue
        if params.min_ocf_yoy is not None:
            if ocf_yoy is None or float(ocf_yoy) < params.min_ocf_yoy:
                continue

        # 第四步：存货结构（存货同比近似备货）
        inv = fin.get("inventory")
        inv_yoy = fin.get("inventory_yoy")
        if params.require_inventory:
            if inv is None or float(inv) <= 0:
                continue
            if inv_yoy is None:
                continue
            if not (params.min_inventory_yoy <= float(inv_yoy) <= params.max_inventory_yoy):
                continue

        score = _inflection_score(
            contract_liab_yoy=float(cl_yoy or 0.0),
            gross_margin_yoy_delta=float(gm_delta or 0.0),
            netcash_operate_yoy=float(ocf_yoy) if ocf_yoy is not None else None,
            inventory_yoy=float(inv_yoy or 0.0),
            params=params,
        )

        candidates.append(
            {
                "symbol": symbol,
                "name": ctx.names.get(symbol, ""),
                "asof": asof,
                "fund_date": row.get("date"),
                "report_date": fin.get("report_date"),
                "notice_date": fin.get("notice_date"),
                "close": close,
                "market_cap": float(market_cap),
                "contract_liab": cl,
                "contract_liab_yoy": cl_yoy,
                "gross_margin": gm,
                "gross_margin_yoy_delta": gm_delta,
                "netcash_operate": ocf,
                "netcash_operate_yoy": ocf_yoy,
                "inventory": inv,
                "inventory_yoy": inv_yoy,
                "inflection_score": score,
                "pct_change": row.get("pct_change"),
            }
        )

    candidates.sort(
        key=lambda x: (
            -float(x["inflection_score"]),
            float(x["market_cap"]),
        )
    )
    picked = candidates[: params.top_n]
    return [c["symbol"] for c in picked], picked


STRATEGY = PortfolioStrategySpec(
    id="order_inflection",
    name="订单开工拐点",
    description="合同负债→毛利率→经营现金流→存货备货→综合拐点分，周期等权调仓",
    params_model=OrderInflectionParams,
    select=select_order_inflection,
    default_universe="all_a",
    needs_dividend=False,
    needs_financials=True,
    default_top_n=10,
    warnings=(
        "财报按公告日（NOTICE_DATE）对齐，避免报告期末日前视；季报披露滞后约 1~4 个月。",
        "存货无原材料/在产品/产成品明细时，用存货同比增速近似「备货开工」。",
        "拉取三大报表较慢，建议缩小 max_universe 并开启 use_cache。",
    ),
)
