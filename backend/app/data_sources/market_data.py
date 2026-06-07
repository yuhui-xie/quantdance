"""A 股行情数据：日线经 a_stock_data 拉取，其余截面数据按需规范化。"""

from __future__ import annotations

import random
import re
from datetime import date, datetime
from typing import Any, Literal

import pandas as pd

from app.data_sources.a_stock_data import AStockDataError, AStockDataSDK, MarketKlineBar

class MarketDataError(Exception):
    """行情不可用：网络、数据源为空或参数错误。"""


def normalize_a_share_symbol(raw: str) -> str:
    """接受 600000、600000.SH、sh600000、sz000001 等，返回 6 位数字代码。"""
    s = raw.strip().upper().replace(" ", "")
    if not s:
        raise ValueError("股票代码不能为空")
    if "." in s:
        code, suf = s.split(".", 1)
        if suf in ("SH", "SZ", "SS"):
            s = code
    if s.startswith("SH"):
        s = s[2:]
    elif s.startswith("SZ"):
        s = s[2:]
    if not re.fullmatch(r"\d{6}", s):
        raise ValueError(f"无效 A 股代码: {raw!r}，需为 6 位数字")
    return s


def _to_yyyymmdd(d: date | datetime | str) -> str:
    if isinstance(d, str):
        dt = datetime.strptime(d.strip()[:10], "%Y-%m-%d")
        return dt.strftime("%Y%m%d")
    if isinstance(d, datetime):
        return d.date().strftime("%Y%m%d")
    return d.strftime("%Y%m%d")


def _a_stock_data_bars_to_ohlcv(rows: list[MarketKlineBar]) -> pd.DataFrame:
    if not rows:
        raise MarketDataError("a_stock_data 未获取到 K 线数据")

    raw = pd.DataFrame(rows)
    for c in ("datetime", "open", "high", "low", "close", "volume"):
        if c not in raw.columns:
            raise MarketDataError(f"a_stock_data K 线列缺失: {c}")

    out = pd.DataFrame(
        {
            "open": pd.to_numeric(raw["open"], errors="coerce"),
            "high": pd.to_numeric(raw["high"], errors="coerce"),
            "low": pd.to_numeric(raw["low"], errors="coerce"),
            "close": pd.to_numeric(raw["close"], errors="coerce"),
            "volume": pd.to_numeric(raw["volume"], errors="coerce").fillna(0),
        }
    )
    if "amount" in raw.columns:
        out["amount"] = pd.to_numeric(raw["amount"], errors="coerce")
    dates = pd.to_datetime(raw["datetime"], errors="coerce")
    out.index = dates
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[~out.index.isna()]
    if out.empty:
        raise MarketDataError("a_stock_data K 线解析后为空")

    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def _a_stock_data_hist(symbol: str, count: int) -> pd.DataFrame:
    try:
        rows = AStockDataSDK().get_klines(symbol, period="day", count=count)
    except AStockDataError as e:
        raise MarketDataError(f"a_stock_data 行情不可用: {e}") from e
    return _a_stock_data_bars_to_ohlcv(rows)


def _filter_ohlcv_date_range(
    df: pd.DataFrame,
    start: str | date | datetime,
    end: str | date | datetime,
) -> pd.DataFrame:
    start_ts = pd.to_datetime(_to_yyyymmdd(start), format="%Y%m%d")
    end_ts = pd.to_datetime(_to_yyyymmdd(end), format="%Y%m%d")
    out = df.loc[(df.index >= start_ts) & (df.index <= end_ts)]
    if out.empty:
        raise MarketDataError("指定日期区间内未获取到行情数据")
    return out


def fetch_a_share_daily(
    symbol: str,
    *,
    start: str | date | datetime | None = None,
    end: str | date | datetime | None = None,
    limit: int | None = None,
    data_source: Literal["a_stock_data"] = "a_stock_data",
) -> pd.DataFrame:
    """
    拉取 A 股前复权日线，索引为 DatetimeIndex，列为 open/high/low/close/volume，
    若源数据含成交额则额外带 amount。

    若同时提供 start 与 end，则取该区间全部 K 线；否则使用 limit 取最近 limit 条交易日。
    """
    code = normalize_a_share_symbol(symbol)
    if data_source != "a_stock_data":
        raise ValueError(f"不支持的数据源: {data_source}")

    if start is not None and end is not None:
        return _filter_ohlcv_date_range(_a_stock_data_hist(code, count=5000), start, end)

    if limit is not None:
        if limit < 50:
            raise ValueError("limit 至少为 50")
        return _a_stock_data_hist(code, count=limit)

    raise ValueError("请同时提供 start 与 end，或提供 limit")


def fetch_a_share_universe(
    max_universe: int = 200,
    *,
    universe_cap: int = 500,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """
    通过 a_stock_data 拉取 A 股代码与名称列表；超过 max_universe 时按代码升序截取，或使用 seed 做可复现随机抽样。

    返回 (股票列表, universe_note)，每项为 {"symbol": 六位代码, "name": 名称}。
    """
    if max_universe < 1:
        raise ValueError("max_universe 至少为 1")
    if universe_cap < 1:
        raise ValueError("universe_cap 至少为 1")
    n_take = min(max_universe, universe_cap)

    try:
        rows = AStockDataSDK().get_universe()
    except AStockDataError as e:
        raise MarketDataError(f"a_stock_data A 股列表不可用: {e}") from e

    total = len(rows)
    if total == 0:
        raise MarketDataError("a_stock_data A 股列表为空")

    if total <= n_take:
        note = f"a_stock_data 股票池共 {total} 只，已全部纳入本次选股。"
        return list(rows), note

    if seed is not None:
        rng = random.Random(seed)
        shuffled = list(rows)
        rng.shuffle(shuffled)
        out = shuffled[:n_take]
        note = (
            f"a_stock_data 股票池共 {total} 只，已使用随机种子 {seed} 抽样 {n_take} 只（可复现）。"
        )
        return out, note

    rows_sorted = sorted(rows, key=lambda x: x["symbol"])
    out = rows_sorted[:n_take]
    note = f"a_stock_data 股票池共 {total} 只，已按代码升序截取前 {n_take} 只。"
    return out, note


def _first_existing_column(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    return None


def _load_hs300_constituents_from_akshare() -> pd.DataFrame:
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except Exception as e:  # pragma: no cover - 依赖环境分支
        raise MarketDataError(f"akshare 不可用，无法获取沪深300成分股: {e}") from e

    errors: list[str] = []
    calls: tuple[tuple[str, dict[str, Any]], ...] = (
        ("index_stock_cons_csindex", {"symbol": "000300"}),
        ("index_stock_cons", {"symbol": "000300"}),
    )
    for func_name, kwargs in calls:
        func = getattr(ak, func_name, None)
        if not callable(func):
            continue
        try:
            df = func(**kwargs)
        except Exception as e:
            errors.append(f"{func_name}: {e}")
            continue
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df

    detail = "；".join(errors) if errors else "未找到可用的 akshare 成分股接口"
    raise MarketDataError(f"沪深300成分股获取失败: {detail}")


def fetch_hs300_universe(
    max_universe: int = 300,
    *,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """
    通过 akshare 拉取沪深300当前成分股。

    注意：该接口返回当前成分股，严格历史回测仍会有成分股幸存者偏差。
    """
    if max_universe < 1:
        raise ValueError("max_universe 至少为 1")

    df = _load_hs300_constituents_from_akshare()
    code_col = _first_existing_column(
        df,
        ("成分券代码", "品种代码", "证券代码", "代码", "stock_code", "code", "symbol"),
    )
    if code_col is None:
        raise MarketDataError("沪深300成分股缺少代码列")
    name_col = _first_existing_column(df, ("成分券名称", "证券简称", "名称", "name"))

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        raw_code = str(row.get(code_col, "")).strip()
        code = re.sub(r"\D", "", raw_code)[-6:]
        if not re.fullmatch(r"\d{6}", code):
            continue
        if code in seen:
            continue
        seen.add(code)
        name = str(row.get(name_col, "")).strip() if name_col is not None else ""
        rows.append({"symbol": code, "name": name})

    total = len(rows)
    if total == 0:
        raise MarketDataError("沪深300成分股解析后为空")

    n_take = min(max_universe, total)
    if total <= n_take:
        note = f"沪深300当前成分股共 {total} 只，已全部纳入本次回测；注意存在当前成分股幸存者偏差。"
        return rows, note

    if seed is not None:
        rng = random.Random(seed)
        sampled = list(rows)
        rng.shuffle(sampled)
        out = sampled[:n_take]
        note = (
            f"沪深300当前成分股共 {total} 只，已使用随机种子 {seed} 抽样 {n_take} 只；"
            "注意存在当前成分股幸存者偏差。"
        )
        return out, note

    out = sorted(rows, key=lambda x: x["symbol"])[:n_take]
    note = f"沪深300当前成分股共 {total} 只，已按代码升序截取前 {n_take} 只；注意存在当前成分股幸存者偏差。"
    return out, note


def fetch_a_share_valuation_snapshot(symbol: str) -> dict[str, float] | None:
    """
    通过 a_stock_data 拉取个股最新估值截面（PE TTM、PB 等）。
    网络或解析失败时返回 None；数据源异常时抛出 MarketDataError。
    """
    code = normalize_a_share_symbol(symbol)
    try:
        valuation = AStockDataSDK().get_snapshot(code).get("valuation")
    except AStockDataError as e:
        raise MarketDataError(f"a_stock_data 估值不可用: {e}") from e
    except (ValueError, KeyError, TypeError) as e:
        raise MarketDataError(f"a_stock_data 估值数据解析失败: {e}") from e
    if valuation is None:
        return None

    out: dict[str, float] = {}
    for key in ("pe_ttm", "pb", "market_cap", "float_market_cap", "turnover_rate"):
        value = valuation.get(key)
        if value is not None and value > 0:
            out[key] = float(value)
    if not out:
        return None
    return out

