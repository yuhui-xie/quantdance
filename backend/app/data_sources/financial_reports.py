"""东财三大报表关键科目（经 akshare），按公告日做时点对齐，带本地缓存。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from app.data_sources.market_data import MarketDataError, normalize_a_share_symbol

_CACHE_ROOT = Path(__file__).resolve().parents[2] / "data" / "financial_reports"
_CACHE_DIR = _CACHE_ROOT / "merged"

_FINANCIAL_COLUMNS = (
    "report_date",
    "notice_date",
    "contract_liab",
    "contract_liab_yoy",
    "inventory",
    "inventory_yoy",
    "operate_income",
    "operate_cost",
    "gross_margin",
    "gross_margin_yoy_delta",
    "netcash_operate",
    "netcash_operate_yoy",
)


def _em_prefixed_symbol(symbol: str) -> str:
    """东财报表接口需要 SH/SZ/BJ 前缀。"""
    code = normalize_a_share_symbol(symbol)
    if code.startswith(("5", "6", "9")):
        return f"SH{code}"
    if code.startswith(("0", "3")):
        return f"SZ{code}"
    if code.startswith(("4", "8")):
        return f"BJ{code}"
    return f"SZ{code}"


def _to_date(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date()


def _cache_path(symbol: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{normalize_a_share_symbol(symbol)}.csv"


def _load_csv(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    try:
        df = pd.read_csv(path)
    except Exception:
        return None
    if df.empty:
        return None
    return df


def _save_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")


def _num(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(series, errors="coerce")


def _fetch_sheet(func_name: str, em_symbol: str) -> pd.DataFrame:
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except Exception as e:  # pragma: no cover
        raise MarketDataError(f"akshare 不可用: {e}") from e

    func = getattr(ak, func_name, None)
    if not callable(func):
        raise MarketDataError(f"当前 akshare 无 {func_name} 接口")
    try:
        raw = func(symbol=em_symbol)
    except Exception as e:
        raise MarketDataError(f"{func_name} 获取失败 ({em_symbol}): {e}") from e
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        raise MarketDataError(f"{func_name} 返回为空 ({em_symbol})")
    return raw


def _sheet_base(raw: pd.DataFrame) -> pd.DataFrame:
    report = pd.to_datetime(raw.get("REPORT_DATE"), errors="coerce")
    notice = pd.to_datetime(raw.get("NOTICE_DATE"), errors="coerce")
    if notice.isna().all():
        notice = report
    out = pd.DataFrame(
        {
            "report_date": report.dt.strftime("%Y-%m-%d"),
            "notice_date": notice.dt.strftime("%Y-%m-%d"),
        }
    )
    return out


def _normalize_merged(
    balance: pd.DataFrame,
    profit: pd.DataFrame,
    cashflow: pd.DataFrame,
) -> pd.DataFrame:
    b = _sheet_base(balance)
    b["contract_liab"] = _num(balance.get("CONTRACT_LIAB"))
    b["contract_liab_yoy"] = _num(balance.get("CONTRACT_LIAB_YOY"))
    b["inventory"] = _num(balance.get("INVENTORY"))
    b["inventory_yoy"] = _num(balance.get("INVENTORY_YOY"))

    p = _sheet_base(profit)
    income = _num(profit.get("OPERATE_INCOME"))
    if income.isna().all():
        income = _num(profit.get("TOTAL_OPERATE_INCOME"))
    cost = _num(profit.get("OPERATE_COST"))
    p["operate_income"] = income
    p["operate_cost"] = cost
    gm = (income - cost) / income.replace(0, pd.NA)
    p["gross_margin"] = gm.where(income > 0).replace([float("inf"), float("-inf")], pd.NA)

    c = _sheet_base(cashflow)
    c["netcash_operate"] = _num(cashflow.get("NETCASH_OPERATE"))
    c["netcash_operate_yoy"] = _num(cashflow.get("NETCASH_OPERATE_YOY"))

    merged = b.merge(
        p[["report_date", "operate_income", "operate_cost", "gross_margin"]],
        on="report_date",
        how="outer",
    ).merge(
        c[["report_date", "netcash_operate", "netcash_operate_yoy"]],
        on="report_date",
        how="outer",
    )
    # 公告日：三表取非空最大值（最晚披露），避免过早用到未公告数据
    notice_map = (
        pd.concat(
            [
                b[["report_date", "notice_date"]],
                p[["report_date", "notice_date"]],
                c[["report_date", "notice_date"]],
            ]
        )
        .dropna(subset=["report_date", "notice_date"])
        .groupby("report_date", as_index=False)["notice_date"]
        .max()
    )
    merged = merged.drop(columns=["notice_date"], errors="ignore").merge(
        notice_map, on="report_date", how="left"
    )
    merged = merged.dropna(subset=["report_date", "notice_date"])
    merged = merged.sort_values(["report_date", "notice_date"]).drop_duplicates(
        "report_date", keep="last"
    )

    # 毛利率同比变动：同报告期末月日、上一年度
    gm_by_report = {
        str(r.report_date)[:10]: float(r.gross_margin)
        for r in merged.itertuples()
        if r.gross_margin is not None and pd.notna(r.gross_margin)
    }
    deltas: list[float | None] = []
    for rd, gm in zip(merged["report_date"], merged["gross_margin"]):
        if gm is None or not pd.notna(gm):
            deltas.append(None)
            continue
        try:
            d = datetime.strptime(str(rd)[:10], "%Y-%m-%d").date()
            prev = date(d.year - 1, d.month, d.day).strftime("%Y-%m-%d")
        except ValueError:
            deltas.append(None)
            continue
        prev_gm = gm_by_report.get(prev)
        deltas.append(float(gm) - prev_gm if prev_gm is not None else None)
    merged["gross_margin_yoy_delta"] = deltas

    for col in _FINANCIAL_COLUMNS:
        if col not in merged.columns:
            merged[col] = float("nan")
    out = merged[list(_FINANCIAL_COLUMNS)].copy()
    return out.sort_values("report_date").reset_index(drop=True)


def _fetch_financials_remote(symbol: str) -> pd.DataFrame:
    em = _em_prefixed_symbol(symbol)
    balance = _fetch_sheet("stock_balance_sheet_by_report_em", em)
    profit = _fetch_sheet("stock_profit_sheet_by_report_em", em)
    cashflow = _fetch_sheet("stock_cash_flow_sheet_by_report_em", em)
    return _normalize_merged(balance, profit, cashflow)


def fetch_stock_financials(
    symbol: str,
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """拉取并规范化合同负债/毛利率/经营现金流/存货等报告期序列。"""
    code = normalize_a_share_symbol(symbol)
    path = _cache_path(code)
    if use_cache and not force_refresh:
        cached = _load_csv(path)
        if cached is not None:
            return _ensure_columns(cached)

    df = _fetch_financials_remote(code)
    if use_cache:
        _save_csv(path, df)
    return df


def _ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in _FINANCIAL_COLUMNS:
        if col not in out.columns:
            out[col] = float("nan")
    out["report_date"] = pd.to_datetime(out["report_date"], errors="coerce").dt.strftime(
        "%Y-%m-%d"
    )
    out["notice_date"] = pd.to_datetime(out["notice_date"], errors="coerce").dt.strftime(
        "%Y-%m-%d"
    )
    out = out.dropna(subset=["report_date", "notice_date"])
    return out[list(_FINANCIAL_COLUMNS)].sort_values("report_date").reset_index(drop=True)


def asof_financial_row(
    financials: pd.DataFrame,
    asof: str | date,
    *,
    max_notice_age_days: int | None = 200,
) -> dict[str, Any] | None:
    """
    取 asof 当日或之前已公告的最近一期财报行（按 notice_date，避免前视）。
    """
    asof_d = _to_date(asof)
    if asof_d is None or financials is None or financials.empty:
        return None
    notices = pd.to_datetime(financials["notice_date"], errors="coerce")
    mask = notices.notna() & (notices.dt.date <= asof_d)
    if not mask.any():
        return None
    sub = financials.loc[mask].copy()
    sub["_notice"] = pd.to_datetime(sub["notice_date"], errors="coerce")
    row = sub.sort_values(["_notice", "report_date"]).iloc[-1]
    notice_d = _to_date(row.get("notice_date"))
    if (
        max_notice_age_days is not None
        and notice_d is not None
        and (asof_d - notice_d).days > max_notice_age_days
    ):
        return None

    def _f(key: str) -> float | None:
        val = row.get(key)
        if val is None or not pd.notna(val):
            return None
        return float(val)

    return {
        "report_date": str(row["report_date"])[:10],
        "notice_date": str(row["notice_date"])[:10],
        "contract_liab": _f("contract_liab"),
        "contract_liab_yoy": _f("contract_liab_yoy"),
        "inventory": _f("inventory"),
        "inventory_yoy": _f("inventory_yoy"),
        "operate_income": _f("operate_income"),
        "operate_cost": _f("operate_cost"),
        "gross_margin": _f("gross_margin"),
        "gross_margin_yoy_delta": _f("gross_margin_yoy_delta"),
        "netcash_operate": _f("netcash_operate"),
        "netcash_operate_yoy": _f("netcash_operate_yoy"),
        "notice_age_days": (asof_d - notice_d).days if notice_d else None,
    }


def load_financials_panel(
    symbols: list[str],
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
    max_workers: int = 4,
    progress: Callable[[str], None] | None = None,
) -> dict[str, pd.DataFrame]:
    """批量加载财报面板；单票失败时跳过。"""
    codes = [normalize_a_share_symbol(s) for s in symbols]
    result: dict[str, pd.DataFrame] = {}

    def _one(code: str) -> tuple[str, pd.DataFrame | None, str | None]:
        try:
            df = fetch_stock_financials(
                code, use_cache=use_cache, force_refresh=force_refresh
            )
        except MarketDataError as e:
            return code, None, str(e)
        return code, df, None

    workers = max(1, min(max_workers, len(codes) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, code): code for code in codes}
        for fut in as_completed(futures):
            code, df, err = fut.result()
            if progress is not None:
                progress(code if err is None else f"{code}: {err}")
            if df is None or df.empty:
                continue
            result[code] = df
    return result


# 供测试直接规范化已缓存/构造的三表
normalize_financial_reports = _normalize_merged
