"""A 股行情数据：日线经 a_stock_data 拉取，其余截面数据按需规范化。"""

from __future__ import annotations

import random
import re
from datetime import date, datetime
from typing import Any, Literal

import pandas as pd

from app.data_sources.a_stock_data import AStockDataError, AStockDataSDK, MarketKlineBar
from app.data_sources.tencent_finance_sdk import TencentFinanceError, TencentFinanceSDK

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
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """
    通过 a_stock_data 拉取 A 股代码与名称列表；超过 max_universe 时按代码升序截取，或使用 seed 做可复现随机抽样。

    返回 (股票列表, universe_note)，每项为 {"symbol": 六位代码, "name": 名称}。
    """
    if max_universe < 1:
        raise ValueError("max_universe 至少为 1")
    n_take = max_universe

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


def fetch_star_board_universe(
    max_universe: int = 500,
    *,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """
    通过 a_stock_data 全 A 列表按 688/689 代码前缀过滤出科创板股票。

    返回 (股票列表, universe_note)，每项为 {"symbol": 六位代码, "name": 名称}。
    """
    if max_universe < 1:
        raise ValueError("max_universe 至少为 1")

    try:
        rows = AStockDataSDK().get_universe()
    except AStockDataError as e:
        raise MarketDataError(f"a_stock_data A 股列表不可用: {e}") from e

    stars = [row for row in rows if row["symbol"].startswith(("688", "689"))]
    total = len(stars)
    if total == 0:
        raise MarketDataError("a_stock_data 科创板列表为空")

    n_take = min(max_universe, total)
    if total <= n_take:
        note = f"a_stock_data 科创板股票池共 {total} 只，已全部纳入本次选股。"
        return list(stars), note

    if seed is not None:
        rng = random.Random(seed)
        shuffled = list(stars)
        rng.shuffle(shuffled)
        out = shuffled[:n_take]
        note = (
            f"a_stock_data 科创板股票池共 {total} 只，已使用随机种子 {seed} 抽样 {n_take} 只（可复现）。"
        )
        return out, note

    stars_sorted = sorted(stars, key=lambda x: x["symbol"])
    out = stars_sorted[:n_take]
    note = f"a_stock_data 科创板股票池共 {total} 只，已按代码升序截取前 {n_take} 只。"
    return out, note


def _first_existing_column(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    return None


def fetch_hs300_index_daily(
    start: str | date | datetime,
    end: str | date | datetime,
) -> pd.DataFrame:
    """拉取沪深300指数日线，返回列 date/close（date 为 YYYY-MM-DD）。"""
    start_s = _to_yyyymmdd(start)
    end_s = _to_yyyymmdd(end)
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except Exception as e:  # pragma: no cover - 依赖环境分支
        raise MarketDataError(f"akshare 不可用，无法获取沪深300指数: {e}") from e

    errors: list[str] = []
    raw: pd.DataFrame | None = None
    calls: tuple[tuple[str, dict[str, Any]], ...] = (
        (
            "index_zh_a_hist",
            {
                "symbol": "000300",
                "period": "daily",
                "start_date": start_s,
                "end_date": end_s,
            },
        ),
        ("stock_zh_index_daily_em", {"symbol": "sh000300"}),
        ("stock_zh_index_daily", {"symbol": "sh000300"}),
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
            raw = df
            break

    if raw is None:
        detail = "；".join(errors) if errors else "未找到可用的 akshare 指数接口"
        raise MarketDataError(f"沪深300指数行情获取失败: {detail}")

    date_col = _first_existing_column(raw, ("日期", "date", "time", "datetime"))
    close_col = _first_existing_column(raw, ("收盘", "close", "Close"))
    if date_col is None or close_col is None:
        raise MarketDataError("沪深300指数行情缺少日期或收盘列")

    out = pd.DataFrame(
        {
            "date": pd.to_datetime(raw[date_col], errors="coerce"),
            "close": pd.to_numeric(raw[close_col], errors="coerce"),
        }
    )
    out = out.dropna(subset=["date", "close"])
    out = out[out["close"] > 0]
    start_ts = pd.to_datetime(start_s, format="%Y%m%d")
    end_ts = pd.to_datetime(end_s, format="%Y%m%d")
    out = out[(out["date"] >= start_ts) & (out["date"] <= end_ts)]
    out = out.sort_values("date").drop_duplicates("date", keep="last")
    if out.empty:
        raise MarketDataError("指定区间内沪深300指数行情为空")
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out.reset_index(drop=True)


def _load_index_constituents_from_akshare(index_code: str) -> pd.DataFrame:
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except Exception as e:  # pragma: no cover - 依赖环境分支
        raise MarketDataError(f"akshare 不可用，无法获取指数成分股: {e}") from e

    code = str(index_code).strip()
    errors: list[str] = []
    calls: tuple[tuple[str, dict[str, Any]], ...] = (
        ("index_stock_cons_csindex", {"symbol": code}),
        ("index_stock_cons", {"symbol": code}),
        ("index_stock_cons_sina", {"symbol": f"sz{code}" if code.startswith("399") else f"sh{code}"}),
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
    raise MarketDataError(f"指数 {code} 成分股获取失败: {detail}")


def fetch_index_universe(
    index_code: str,
    *,
    index_name: str,
    max_universe: int = 300,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """
    通过 akshare 拉取指数当前成分股。

    注意：该接口返回当前成分股，严格历史回测仍会有成分股幸存者偏差。
    """
    if max_universe < 1:
        raise ValueError("max_universe 至少为 1")

    df = _load_index_constituents_from_akshare(index_code)
    code_col = _first_existing_column(
        df,
        ("成分券代码", "品种代码", "证券代码", "代码", "stock_code", "code", "symbol"),
    )
    if code_col is None:
        raise MarketDataError(f"{index_name}成分股缺少代码列")
    name_col = _first_existing_column(df, ("成分券名称", "品种名称", "证券简称", "名称", "name"))

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
        raise MarketDataError(f"{index_name}成分股解析后为空")

    n_take = min(max_universe, total)
    if total <= n_take:
        note = (
            f"{index_name}当前成分股共 {total} 只，已全部纳入本次回测；"
            "注意存在当前成分股幸存者偏差。"
        )
        return rows, note

    if seed is not None:
        rng = random.Random(seed)
        sampled = list(rows)
        rng.shuffle(sampled)
        out = sampled[:n_take]
        note = (
            f"{index_name}当前成分股共 {total} 只，已使用随机种子 {seed} 抽样 {n_take} 只；"
            "注意存在当前成分股幸存者偏差。"
        )
        return out, note

    out = sorted(rows, key=lambda x: x["symbol"])[:n_take]
    note = (
        f"{index_name}当前成分股共 {total} 只，已按代码升序截取前 {n_take} 只；"
        "注意存在当前成分股幸存者偏差。"
    )
    return out, note


def _finalize_index_rows(
    rows: list[dict[str, str]],
    *,
    index_name: str,
    max_universe: int,
    seed: int | None,
    source_desc: str,
    bias_warning: str,
) -> tuple[list[dict[str, str]], str]:
    """对成分股 rows 按 max_universe/seed 抽样去重，返回 (out, note)。

    source_desc 描述来源（如"当前成分股"/"2024-06-14 时点成分股"），bias_warning
    追加在 note 末尾；历史时点路径无幸存者偏差，bias_warning 传空串。
    """
    if max_universe < 1:
        raise ValueError("max_universe 至少为 1")
    total = len(rows)
    if total == 0:
        raise MarketDataError(f"{index_name}成分股解析后为空")
    n_take = min(max_universe, total)
    if total <= n_take:
        return rows, f"{index_name}{source_desc}共 {total} 只，已全部纳入本次回测。{bias_warning}"
    if seed is not None:
        rng = random.Random(seed)
        sampled = list(rows)
        rng.shuffle(sampled)
        return (
            sampled[:n_take],
            f"{index_name}{source_desc}共 {total} 只，已使用随机种子 {seed} 抽样 {n_take} 只。{bias_warning}",
        )
    out = sorted(rows, key=lambda x: x["symbol"])[:n_take]
    return (
        out,
        f"{index_name}{source_desc}共 {total} 只，已按代码升序截取前 {n_take} 只。{bias_warning}",
    )


_CONSTITUTION_INDEX_MAP: dict[str, str] = {
    "000300": "csi300",
    "000905": "csi500",
}


def _attach_names_from_spot(
    symbols: list[str],
) -> tuple[dict[str, str], bool]:
    """用 akshare 全市场快照补干净名称，返回 (symbol->name 映射, 是否成功)。

    index_constitution 包内的 name 列乱码不可用，故用 akshare 的
    stock_zh_a_spot_em（一次拉全市场代码->名称）补名。失败或缺码时 name 为空。
    """
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - 依赖环境分支
        return {}, False
    try:
        spot = ak.stock_zh_a_spot_em()
    except Exception:  # pragma: no cover - 网络/源异常
        return {}, False
    code_col = _first_existing_column(spot, ("代码", "code", "证券代码"))
    name_col = _first_existing_column(spot, ("名称", "name", "证券简称"))
    if code_col is None or name_col is None:
        return {}, False
    name_map: dict[str, str] = {}
    for _, r in spot.iterrows():
        code = re.sub(r"\D", "", str(r.get(code_col, "")))[-6:]
        if re.fullmatch(r"\d{6}", code):
            name_map[code] = str(r.get(name_col, "")).strip()
    return name_map, True


def fetch_index_universe_at(
    index_code: str,
    asof: str | date | datetime,
    *,
    index_name: str,
    max_universe: int = 300,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """拉取指数在某历史时点 asof 的成分股（index_constitution），避开幸存者偏差。

    支持 csi300/csi500；其余指数或包不可用时回退 fetch_index_universe（当前成分，
    仍有幸存者偏差，note 注明）。symbol 统一归一为裸 6 位码，名称用 akshare 补。
    """
    asof_s = str(asof)[:10]
    key = _CONSTITUTION_INDEX_MAP.get(index_code)
    if key is not None:
        try:
            import index_constitution as ic  # type: ignore[import-not-found]
        except Exception:  # pragma: no cover - 依赖环境分支
            ic = None
        if ic is not None:
            try:
                df = ic.constituents_at(key, asof_s)
                rows: list[dict[str, str]] = []
                seen: set[str] = set()
                for _, r in df.iterrows():
                    code = re.sub(r"\D", "", str(r.get("symbol", "")))[-6:]
                    if not re.fullmatch(r"\d{6}", code) or code in seen:
                        continue
                    seen.add(code)
                    rows.append({"symbol": code, "name": ""})
                if rows:
                    name_map, _ = _attach_names_from_spot([x["symbol"] for x in rows])
                    for row in rows:
                        row["name"] = name_map.get(row["symbol"], "")
                    # 历史时点成分股本身无幸存者偏差；名称补全失败不影响成分正确性
                    return _finalize_index_rows(
                        rows,
                        index_name=index_name,
                        max_universe=max_universe,
                        seed=seed,
                        source_desc=f"{asof_s} 时点成分股",
                        bias_warning="",
                    )
            except Exception:  # pragma: no cover - 数据/解析异常
                ic = None  # 落回 akshare 当前成分
    rows, note = fetch_index_universe(
        index_code, index_name=index_name, max_universe=max_universe, seed=seed
    )
    return rows, note


def fetch_hs300_universe_at(
    asof: str | date | datetime,
    max_universe: int = 300,
    *,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """拉取沪深300在历史时点 asof 的成分股。"""
    return fetch_index_universe_at(
        "000300", asof, index_name="沪深300", max_universe=max_universe, seed=seed
    )


def fetch_zz500_universe_at(
    asof: str | date | datetime,
    max_universe: int = 500,
    *,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """拉取中证500在历史时点 asof 的成分股。"""
    return fetch_index_universe_at(
        "000905", asof, index_name="中证500", max_universe=max_universe, seed=seed
    )


def fetch_zz1000_universe_at(
    asof: str | date | datetime,
    max_universe: int = 1000,
    *,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """中证1000在历史时点的成分股：index_constitution 未覆盖，回退 akshare 当前成分。"""
    return fetch_index_universe_at(
        "000852", asof, index_name="中证1000", max_universe=max_universe, seed=seed
    )


def fetch_hs300_universe(
    max_universe: int = 300,
    *,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """通过 akshare 拉取沪深300当前成分股。"""
    return fetch_index_universe(
        "000300",
        index_name="沪深300",
        max_universe=max_universe,
        seed=seed,
    )


def fetch_zz500_universe(
    max_universe: int = 500,
    *,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """通过 akshare 拉取中证500当前成分股。"""
    return fetch_index_universe(
        "000905",
        index_name="中证500",
        max_universe=max_universe,
        seed=seed,
    )


def fetch_zz399101_universe(
    max_universe: int = 500,
    *,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """通过 akshare 拉取中小综指（399101）当前成分股。"""
    return fetch_index_universe(
        "399101",
        index_name="中小综指(399101)",
        max_universe=max_universe,
        seed=seed,
    )


def fetch_zz1000_universe(
    max_universe: int = 1000,
    *,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """通过 akshare 拉取中证1000当前成分股。"""
    return fetch_index_universe(
        "000852",
        index_name="中证1000",
        max_universe=max_universe,
        seed=seed,
    )


def fetch_star50_universe(
    max_universe: int = 50,
    *,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """通过 akshare 拉取科创50当前成分股。"""
    return fetch_index_universe(
        "000688",
        index_name="科创50",
        max_universe=max_universe,
        seed=seed,
    )


def fetch_gz2000_universe(
    max_universe: int = 2000,
    *,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """通过 akshare 拉取国证2000当前成分股。"""
    return fetch_index_universe(
        "399303",
        index_name="国证2000",
        max_universe=max_universe,
        seed=seed,
    )


def _parse_etf_df(
    ak: Any,
    func_name: str,
    *,
    func_args: tuple[str, ...] = (),
    code_cols: tuple[str, ...],
    name_cols: tuple[str, ...],
    strip_prefix: bool = False,
) -> list[dict[str, str]]:
    """调用某个 akshare ETF 接口并解析为 [{"symbol","name"}]。

    symbol 统一为 6 位数字代码；sina 的代码形如 "sz159998"，通过 strip_prefix 剥掉市场前缀。
    接口缺失、返回空或解析后无有效代码时返回空列表（由调用方决定是否继续尝试下一数据源）。
    """
    func = getattr(ak, func_name, None)
    if not callable(func):
        return []
    df = func(*func_args)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []

    code_col = _first_existing_column(df, code_cols)
    name_col = _first_existing_column(df, name_cols)
    if code_col is None:
        return []

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        code = str(row.get(code_col, "")).strip()
        if strip_prefix:
            code = code[-6:]  # "sz159998" -> "159998"
        if not re.fullmatch(r"\d{6}", code):
            continue
        if code in seen:
            continue
        seen.add(code)
        name = str(row.get(name_col, "")).strip() if name_col is not None else ""
        rows.append({"symbol": code, "name": name})
    return rows


def fetch_etf_universe(
    max_universe: int = 500,
    *,
    seed: int | None = None,
) -> tuple[list[dict[str, str]], str]:
    """通过 akshare 拉取场内 ETF 列表（fund_etf_spot_em）。

    返回 (ETF 列表, universe_note)，每项为 {"symbol": 六位代码, "name": 名称}。
    ETF 代码为 6 位数字（沪 5xxxxx、深 15xxxx），行情走 a_stock_data/腾讯时由
    normalize_symbol 按 5→SH、其余→SZ 自动映射市场。
    """
    if max_universe < 1:
        raise ValueError("max_universe 至少为 1")

    try:
        import akshare as ak  # type: ignore[import-not-found]
    except Exception as e:  # pragma: no cover - 依赖环境分支
        raise MarketDataError(f"akshare 不可用，无法获取 ETF 列表: {e}") from e

    # 多数据源备用链：东财实时 → 同花顺 → 新浪分类。单个失败自动尝试下一个，
    # 全部失败才抛 MarketDataError（东财 fund_etf_spot_em 常被风控/限流）。
    # 每项为 (接口名, 调用位置参数, 代码列候选, 名称列候选, 代码是否带 sh/sz 前缀)。
    providers: tuple[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], bool], ...] = (
        ("fund_etf_spot_em", (), ("代码", "code", "symbol"), ("名称", "name"), False),
        ("fund_etf_spot_ths", (), ("基金代码", "代码", "code", "symbol"), ("基金简称", "基金名称", "名称", "name"), False),
        ("fund_etf_category_sina", ("ETF基金",), ("代码", "code"), ("名称", "name"), True),
    )

    errors: list[str] = []
    rows: list[dict[str, str]] = []
    for func_name, func_args, code_cols, name_cols, strip_prefix in providers:
        try:
            parsed = _parse_etf_df(
                ak,
                func_name,
                func_args=func_args,
                code_cols=code_cols,
                name_cols=name_cols,
                strip_prefix=strip_prefix,
            )
        except Exception as e:  # 单个数据源失败，记录后继续尝试下一个
            errors.append(f"{func_name}: {e}")
            continue
        if parsed:
            rows = parsed
            break

    total = len(rows)
    if total == 0:
        detail = "；".join(errors) if errors else "所有 akshare ETF 接口均不可用"
        raise MarketDataError(f"akshare ETF 列表获取失败: {detail}")

    n_take = min(max_universe, total)
    if total <= n_take:
        note = f"akshare 场内 ETF 共 {total} 只，已全部纳入本次回测。"
        return rows, note

    if seed is not None:
        rng = random.Random(seed)
        sampled = list(rows)
        rng.shuffle(sampled)
        out = sampled[:n_take]
        note = (
            f"akshare 场内 ETF 共 {total} 只，已使用随机种子 {seed} 抽样 {n_take} 只（可复现）。"
        )
        return out, note

    out = sorted(rows, key=lambda x: x["symbol"])[:n_take]
    note = f"akshare 场内 ETF 共 {total} 只，已按代码升序截取前 {n_take} 只。"
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


def _to_iso_date(d: date | datetime | str) -> str:
    if isinstance(d, str):
        return d.strip()[:10]
    if isinstance(d, datetime):
        return d.date().isoformat()
    return d.isoformat()


def _fetch_tencent_turnover_page(
    sdk: TencentFinanceSDK,
    symbol: str,
    end: str | None,
) -> list[dict[str, Any]]:
    try:
        return sdk.get_kline_with_turnover(symbol, count=_PAGE_SIZE, end=end)
    except TencentFinanceError as e:
        raise MarketDataError(f"腾讯日线获取失败: {e}") from e


_PAGE_SIZE = 640


def fetch_a_share_daily_turnover(
    symbol: str,
    *,
    start: str | date | datetime | None = None,
    end: str | date | datetime | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """获取 A 股日线（含换手率），数据源为腾讯财经 newfqkline。

    返回**前复权**日线，换手率为腾讯公布的**真实历史换手率**（非估算），
    DatetimeIndex 索引，列为 open/high/low/close/volume/amount/turnover_rate；
    turnover_rate 为小数（0.01 = 1%）。单次接口上限约 640 条，超过时自动逐页向前翻取。

    调用方式二选一：
    - 同时提供 start 与 end：取该区间全部 K 线；
    - 仅提供 limit：取最近 limit 条交易日。
    """
    code = normalize_a_share_symbol(symbol)
    if start is not None and end is not None:
        iso_start = _to_iso_date(start)
        iso_end = _to_iso_date(end)
    elif limit is not None:
        if limit < 50:
            raise ValueError("limit 至少为 50")
        iso_start = None
        iso_end = None
    else:
        raise ValueError("请同时提供 start 与 end，或提供 limit")

    sdk = TencentFinanceSDK()
    pages: list[list[dict[str, Any]]] = []
    page_end: str | None = iso_end
    while True:
        page = _fetch_tencent_turnover_page(sdk, symbol, page_end)
        if not page:
            break
        pages.append(page)
        if len(page) < _PAGE_SIZE:
            break
        if limit is not None and sum(len(p) for p in pages) >= limit:
            break
        if iso_start is not None and page[0]["date"] <= iso_start:
            break
        page_end = page[0]["date"]

    # 各页内部升序、页间按时间先后拼接（旧页在前），跨页边界日期去重。
    bars: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in reversed(pages):
        for bar in page:
            d = bar["date"]
            if d in seen:
                continue
            seen.add(d)
            bars.append(bar)

    if limit is not None:
        bars = bars[-limit:]
    elif iso_start is not None:
        bars = [b for b in bars if iso_start <= b["date"] <= iso_end]

    if not bars:
        raise MarketDataError(f"未获取到 {code} 的日线数据")

    raw = pd.DataFrame(bars)
    out = pd.DataFrame(
        {
            "open": pd.to_numeric(raw["open"], errors="coerce"),
            "high": pd.to_numeric(raw["high"], errors="coerce"),
            "low": pd.to_numeric(raw["low"], errors="coerce"),
            "close": pd.to_numeric(raw["close"], errors="coerce"),
            "volume": pd.to_numeric(raw["volume"], errors="coerce").fillna(0),
            "amount": pd.to_numeric(raw["amount"], errors="coerce").fillna(0),
            "turnover_rate": pd.to_numeric(raw["turnover_rate"], errors="coerce"),
        }
    )
    dates = pd.to_datetime(raw["date"], errors="coerce")
    out.index = dates
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[~out.index.isna()]
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    if out.empty:
        raise MarketDataError(f"指定范围内未获取到 {code} 的日线数据")
    return out

