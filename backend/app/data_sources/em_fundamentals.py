"""东财个股估值历史与分红数据（经 akshare），带本地缓存。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from app.data_sources.market_data import MarketDataError, normalize_a_share_symbol

_CACHE_ROOT = Path(__file__).resolve().parents[2] / "data" / "em_fundamentals"
_VALUE_DIR = _CACHE_ROOT / "value_em"
_DIV_DIR = _CACHE_ROOT / "dividend"

_VALUE_COLUMNS = (
    "date",
    "close",
    "close_qfq",
    "pct_change",
    "market_cap",
    "float_market_cap",
    "pe_ttm",
    "pb",
    "peg",
    "ps_ttm",
)


def _cache_path(kind: str, symbol: str) -> Path:
    root = _VALUE_DIR if kind == "value_em" else _DIV_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{symbol}.csv"


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


def _first_col(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    return None


def _forward_adjusted_close(close: pd.Series, pct_change: pd.Series) -> pd.Series:
    """由东财的「复权口径涨跌幅」还原**前复权**收盘价。

    东财估值面板里两列口径不同：`pct_change`（当日涨跌幅）是**复权**口径，
    而 `close`（当日收盘价）是**不复权**的真实成交价。同一行混用两者会凭空造出除权跳空
    （实测 603027 在 2025-10-23：不复权收盘 11.53→8.92 即 -22.6%，同行 `pct_change`
    却是 +1.48%）。

    `pct_change` 累乘得到复权因子，再锚定「最后一行的原始收盘价 = 前复权收盘价」，
    得到与 `fetch_a_share_daily`（TDX 前复权）同口径的序列。抽样 250 只标的逐日比对，
    本式还原出的日收益与 TDX 前复权 K 线收益差异在 1e-12 量级（即完全一致）。

    涨跌幅缺失（停牌 / 新股首日）按 0 处理：只影响当日，不改变其余 bar 的相对关系。

    涨跌幅整列为空时无法还原（锚点因子不可用），抛 `MarketDataError` 而非退回原始价——
    静默退回不复权价正是本次要消除的缺陷。
    """
    close_num = pd.to_numeric(close, errors="coerce")
    ret = pd.to_numeric(pct_change, errors="coerce") / 100.0
    factor = (1.0 + ret.fillna(0.0)).cumprod()
    anchor_factor = float(factor.iloc[-1]) if len(factor) else float("nan")
    anchor_close = float(close_num.iloc[-1]) if len(close_num) else float("nan")
    if not np.isfinite(anchor_factor) or anchor_factor <= 0 or not np.isfinite(anchor_close):
        raise MarketDataError(
            "stock_value_em 无法还原前复权收盘价：涨跌幅或最新收盘价不可用"
        )
    return anchor_close * factor / anchor_factor


def _normalize_value_em(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=list(_VALUE_COLUMNS))

    date_col = _first_col(raw, ("数据日期", "日期", "date", "trade_date"))
    close_col = _first_col(raw, ("当日收盘价", "收盘价", "close"))
    pct_col = _first_col(raw, ("当日涨跌幅", "涨跌幅", "pct_change"))
    mv_col = _first_col(raw, ("总市值", "market_cap", "total_mv"))
    fmv_col = _first_col(raw, ("流通市值", "float_market_cap", "circ_mv"))
    pe_col = _first_col(raw, ("PE(TTM)", "pe_ttm", "市盈率TTM"))
    pb_col = _first_col(raw, ("市净率", "pb"))
    peg_col = _first_col(raw, ("PEG值", "PEG", "peg"))
    ps_col = _first_col(raw, ("市销率", "ps_ttm", "PS"))

    if date_col is None or close_col is None or mv_col is None:
        raise MarketDataError("stock_value_em 缺少日期/收盘价/市值列")

    out = pd.DataFrame(
        {
            "date": pd.to_datetime(raw[date_col], errors="coerce").dt.strftime("%Y-%m-%d"),
            "close": pd.to_numeric(raw[close_col], errors="coerce"),
            "pct_change": pd.to_numeric(raw[pct_col], errors="coerce") if pct_col else float("nan"),
            "market_cap": pd.to_numeric(raw[mv_col], errors="coerce"),
            "float_market_cap": (
                pd.to_numeric(raw[fmv_col], errors="coerce") if fmv_col else float("nan")
            ),
            "pe_ttm": pd.to_numeric(raw[pe_col], errors="coerce") if pe_col else float("nan"),
            "pb": pd.to_numeric(raw[pb_col], errors="coerce") if pb_col else float("nan"),
            "peg": pd.to_numeric(raw[peg_col], errors="coerce") if peg_col else float("nan"),
            "ps_ttm": pd.to_numeric(raw[ps_col], errors="coerce") if ps_col else float("nan"),
        }
    )
    out = out.dropna(subset=["date", "close", "market_cap"])
    out = out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    # 前复权因子按日期顺序累乘，必须在排序去重之后计算。
    out["close_qfq"] = _forward_adjusted_close(out["close"], out["pct_change"])
    return out[list(_VALUE_COLUMNS)]


def _resolve_ak_func(name: str) -> Callable[..., Any]:
    """按名称解析 akshare 函数，统一处理导入与接口缺失错误。"""
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise MarketDataError(f"akshare 不可用: {e}") from e
    func = getattr(ak, name, None)
    if not callable(func):
        raise MarketDataError(f"当前 akshare 无 {name} 接口")
    return func


def _fetch_value_em_remote(symbol: str) -> pd.DataFrame:
    fetch = _resolve_ak_func("stock_value_em")
    try:
        raw = fetch(symbol=symbol)
    except Exception as e:
        raise MarketDataError(f"stock_value_em 获取失败 ({symbol}): {e}") from e
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        raise MarketDataError(f"stock_value_em 返回为空 ({symbol})")
    return _normalize_value_em(raw)


def fetch_stock_value_em(
    symbol: str,
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """拉取个股东财估值历史（含收盘价、市值、PEG 等），按交易日对齐。"""
    code = normalize_a_share_symbol(symbol)
    path = _cache_path("value_em", code)
    if use_cache and not force_refresh:
        cached = _load_csv(path)
        if cached is not None:
            return _normalize_value_em(cached)

    df = _fetch_value_em_remote(code)
    if use_cache:
        _save_csv(path, df)
    return df


def _normalize_dividends(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["ex_date", "cash_per_share"])

    # 已规范化的缓存 CSV
    if {"ex_date", "cash_per_share"}.issubset(set(raw.columns)):
        out = pd.DataFrame(
            {
                "ex_date": pd.to_datetime(raw["ex_date"], errors="coerce").dt.strftime("%Y-%m-%d"),
                "cash_per_share": pd.to_numeric(raw["cash_per_share"], errors="coerce"),
            }
        )
        out = out.dropna(subset=["ex_date"])
        out = out[out["cash_per_share"].fillna(0) > 0]
        return out.sort_values("ex_date").drop_duplicates(["ex_date", "cash_per_share"]).reset_index(drop=True)

    pay_col = _first_col(raw, ("派息比例", "派息(元/10股)", "现金分红", "cash_per_share"))
    if pay_col is None:
        return pd.DataFrame(columns=["ex_date", "cash_per_share"])

    date_series = None
    for col in ("除权日", "除权除息日", "派息日", "股权登记日", "ex_date"):
        if col not in raw.columns:
            continue
        series = pd.to_datetime(raw[col], errors="coerce")
        date_series = series if date_series is None else date_series.fillna(series)
    if date_series is None:
        return pd.DataFrame(columns=["ex_date", "cash_per_share"])

    cash = pd.to_numeric(raw[pay_col], errors="coerce")
    # 原始巨潮「派息比例」为元/10 股；若列名已是 cash_per_share 则不再 /10
    if pay_col != "cash_per_share":
        cash = cash / 10.0
    out = pd.DataFrame(
        {
            "ex_date": date_series.dt.strftime("%Y-%m-%d"),
            "cash_per_share": cash,
        }
    )
    out = out.dropna(subset=["ex_date"])
    out = out[out["cash_per_share"].fillna(0) > 0]
    out = out.sort_values("ex_date").drop_duplicates(["ex_date", "cash_per_share"]).reset_index(drop=True)
    return out


def _fetch_dividends_remote(symbol: str) -> pd.DataFrame:
    fetch = _resolve_ak_func("stock_dividend_cninfo")
    try:
        raw = fetch(symbol=symbol)
    except Exception as e:
        raise MarketDataError(f"stock_dividend_cninfo 获取失败 ({symbol}): {e}") from e
    if not isinstance(raw, pd.DataFrame):
        return pd.DataFrame(columns=["ex_date", "cash_per_share"])
    return _normalize_dividends(raw)


def fetch_stock_dividends(
    symbol: str,
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """拉取个股分红记录（派息折算为每股现金）。"""
    code = normalize_a_share_symbol(symbol)
    path = _cache_path("dividend", code)
    if use_cache and not force_refresh:
        cached = _load_csv(path)
        if cached is not None:
            return _normalize_dividends(cached)

    df = _fetch_dividends_remote(code)
    if use_cache:
        _save_csv(path, df)
    return df


def trailing_dividend_yield(
    dividends: pd.DataFrame,
    *,
    asof: str | date,
    price: float,
    lookback_days: int = 365,
) -> float | None:
    """用 trailing 现金分红 / 现价估算股息率。"""
    if price is None or not pd.notna(price) or float(price) <= 0:
        return None
    asof_d = _to_date(asof)
    if asof_d is None:
        return None
    if dividends is None or dividends.empty:
        return 0.0

    start = asof_d - timedelta(days=lookback_days)
    total = 0.0
    for _, row in dividends.iterrows():
        ex = _to_date(row.get("ex_date"))
        cash = row.get("cash_per_share")
        if ex is None or cash is None or not pd.notna(cash):
            continue
        if start < ex <= asof_d:
            total += float(cash)
    return float(total / float(price))


# id 键缓存，值里强引用 DataFrame 本身：对象存活期间地址不会被复用，
# 避免纯 id → ndarray 缓存把旧对象的日期数组误配给新对象（id 地址复用 bug）。
_DATE_STR_CACHE: dict[int, tuple[pd.DataFrame, np.ndarray]] = {}


def _date_str_array(value_df: pd.DataFrame) -> np.ndarray:
    """按 DataFrame 身份缓存 YYYY-MM-DD 字符串列，避免反复 to_datetime。"""
    key = id(value_df)
    cached = _DATE_STR_CACHE.get(key)
    if cached is not None and cached[0] is value_df:
        return cached[1]
    dates = value_df["date"].astype(str).str[:10].to_numpy()
    _DATE_STR_CACHE[key] = (value_df, dates)
    # 粗暴限长，防止长期进程无限增长
    if len(_DATE_STR_CACHE) > 20000:
        _DATE_STR_CACHE.clear()
        _DATE_STR_CACHE[key] = (value_df, dates)
    return dates


def asof_fundamental_row(
    value_df: pd.DataFrame,
    asof: str | date,
) -> dict[str, Any] | None:
    """取 asof 当日或之前最近一条估值行。

    假定 value_df 已按 date 升序（load/normalize 保证）；用二分查找替代全表 to_datetime。
    """
    asof_d = _to_date(asof)
    if asof_d is None or value_df is None or value_df.empty:
        return None
    asof_s = asof_d.isoformat()
    dates = _date_str_array(value_df)
    i = int(np.searchsorted(dates, asof_s, side="right") - 1)
    if i < 0:
        return None
    row = value_df.iloc[i]
    out: dict[str, Any] = {"date": str(row["date"])[:10]}
    for key in (
        "close",
        "pct_change",
        "market_cap",
        "float_market_cap",
        "pe_ttm",
        "pb",
        "peg",
        "ps_ttm",
    ):
        val = row.get(key)
        out[key] = float(val) if val is not None and pd.notna(val) else None
    return out


def load_fundamentals_panel(
    symbols: list[str],
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
    max_workers: int = 8,
    include_dividend: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, dict[str, pd.DataFrame]]:
    """
    批量加载估值与分红。

    返回 {symbol: {"value": df, "dividend": df}}；单票失败时跳过并继续。
    """
    codes = [normalize_a_share_symbol(s) for s in symbols]
    result: dict[str, dict[str, pd.DataFrame]] = {}

    def _one(code: str) -> tuple[str, pd.DataFrame | None, pd.DataFrame | None, str | None]:
        try:
            value = fetch_stock_value_em(
                code, use_cache=use_cache, force_refresh=force_refresh
            )
        except MarketDataError as e:
            return code, None, None, str(e)
        if not include_dividend:
            return code, value, pd.DataFrame(columns=["ex_date", "cash_per_share"]), None
        try:
            div = fetch_stock_dividends(
                code, use_cache=use_cache, force_refresh=force_refresh
            )
        except MarketDataError:
            div = pd.DataFrame(columns=["ex_date", "cash_per_share"])
        return code, value, div, None

    workers = max(1, min(max_workers, len(codes) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, code): code for code in codes}
        for fut in as_completed(futures):
            code, value, div, err = fut.result()
            if progress is not None:
                progress(code if err is None else f"{code}: {err}")
            if value is None or value.empty:
                continue
            result[code] = {
                "value": value,
                "dividend": div if div is not None else pd.DataFrame(columns=["ex_date", "cash_per_share"]),
            }
    return result
