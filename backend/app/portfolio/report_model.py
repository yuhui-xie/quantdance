"""组合回测报告视图模型：从 PortfolioBacktestResponse JSON 派生交互报告数据。"""

from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_VALUE_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "em_fundamentals" / "value_em"


def _norm_date(d: Any) -> str:
    s = str(d or "").strip()
    return s[:10] if len(s) >= 10 else s


def _nf(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _symbol_name_map(rebalances: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for rb in rebalances:
        for item in rb.get("selection") or []:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").strip()
            name = str(item.get("name") or "").strip()
            if sym and name:
                names[sym] = name
    return names


def _equity_map(equity: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in equity:
        d = _norm_date(row.get("date"))
        v = _nf(row.get("equity"))
        if d and v is not None:
            out[d] = v
    return out


def _trade_row(
    trade: dict[str, Any],
    *,
    names: dict[str, str],
) -> dict[str, Any]:
    side = str(trade.get("side") or "").lower()
    price = _nf(trade.get("price")) or 0.0
    shares = _nf(trade.get("shares")) or 0.0
    cost = _nf(trade.get("cost")) or 0.0
    notional = abs(price * shares)
    symbol = str(trade.get("symbol") or "").strip()
    return {
        "date": _norm_date(trade.get("date")),
        "symbol": symbol,
        "name": names.get(symbol, ""),
        "side": side,
        "price": price,
        "shares": shares,
        "notional": notional,
        "cost": cost,
        "cash_after": _nf(trade.get("cash_after")),
        "reason": str(trade.get("reason") or "").strip(),
    }


def _fifo_round_trips(
    trades: list[dict[str, Any]],
    *,
    names: dict[str, str],
) -> list[dict[str, Any]]:
    """按代码 FIFO 匹配 buy→sell，估算单票回合盈亏。"""
    lots: dict[str, deque[dict[str, float]]] = defaultdict(deque)
    trips: list[dict[str, Any]] = []
    for trade in trades:
        side = str(trade.get("side") or "").lower()
        symbol = str(trade.get("symbol") or "").strip()
        if not symbol:
            continue
        price = _nf(trade.get("price")) or 0.0
        shares = _nf(trade.get("shares")) or 0.0
        if shares <= 0 or price <= 0:
            continue
        day = _norm_date(trade.get("date"))
        if side == "buy":
            lots[symbol].append({"shares": shares, "price": price, "date": day})
            continue
        if side != "sell":
            continue
        remain = shares
        buy_cost = 0.0
        buy_shares = 0.0
        buy_date = day
        while remain > 1e-9 and lots[symbol]:
            lot = lots[symbol][0]
            take = min(remain, lot["shares"])
            buy_cost += take * lot["price"]
            buy_shares += take
            buy_date = str(lot["date"])
            lot["shares"] -= take
            remain -= take
            if lot["shares"] <= 1e-9:
                lots[symbol].popleft()
        if buy_shares <= 0:
            continue
        sell_proceeds = buy_shares * price
        pnl = sell_proceeds - buy_cost
        pnl_pct = pnl / buy_cost if buy_cost > 0 else 0.0
        trips.append(
            {
                "symbol": symbol,
                "name": names.get(symbol, ""),
                "buy_date": buy_date,
                "sell_date": day,
                "shares": buy_shares,
                "buy_price": buy_cost / buy_shares,
                "sell_price": price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }
        )
    return trips


def _cache_only_closes(symbol: str, start: str, end: str) -> list[list[Any]]:
    """仅读本地估值缓存收盘价，缺文件或失败则返回空（不访问外网）。"""
    try:
        import pandas as pd

        from app.data_sources.em_fundamentals import fetch_stock_value_em
        from app.data_sources.market_data import normalize_a_share_symbol

        code = normalize_a_share_symbol(symbol)
        if not (_VALUE_CACHE_DIR / f"{code}.csv").is_file():
            return []
        df = fetch_stock_value_em(code, use_cache=True, force_refresh=False)
        if df is None or df.empty or "date" not in df.columns or "close" not in df.columns:
            return []
        dates = df["date"].map(_norm_date)
        closes = pd.to_numeric(df["close"], errors="coerce")
        out: list[list[Any]] = []
        for d, c in zip(dates, closes):
            if not d:
                continue
            if start and d < start:
                continue
            if end and d > end:
                continue
            if c is None or c != c:
                continue
            out.append([d, float(c)])
        return out
    except Exception:
        return []


def _ohlc_tuple(row: dict[str, Any]) -> tuple[float, float, float, float] | None:
    o = _nf(row.get("open"))
    h = _nf(row.get("high"))
    l = _nf(row.get("low"))
    c = _nf(row.get("close"))
    if None in (o, h, l, c):
        return None
    return float(o), float(h), float(l), float(c)


def _filter_ohlc_rows(
    rows: list[Any],
    start: str,
    end: str,
) -> list[list[Any]]:
    out: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        d = _norm_date(row.get("datetime") or row.get("date"))
        if not d:
            continue
        if start and d < start:
            continue
        if end and d > end:
            continue
        ohlc = _ohlc_tuple(row)
        if ohlc is None:
            continue
        o, h, l, c = ohlc
        out.append([d, o, h, l, c])
    return out


def _read_stale_day_klines(symbol: str) -> list[dict[str, Any]]:
    """读取 a_stock_data 日 K 本地缓存（允许过期），不访问外网。"""
    try:
        from app.data_sources.a_stock_data import AStockDataSDK

        sdk = AStockDataSDK()
        norm = sdk.normalize_symbol(symbol)
        payload = sdk._read_cache("klines", "day", f"{norm}.json")  # noqa: SLF001
        if not isinstance(payload, dict):
            return []
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]
    except Exception:
        return []


def _fetch_day_klines(symbol: str, *, count: int = 2000) -> list[dict[str, Any]]:
    """拉取日 K（优先缓存，必要时走数据源并回写缓存）。"""
    try:
        from app.data_sources.a_stock_data import AStockDataSDK

        rows = AStockDataSDK().get_klines(symbol, period="day", count=count)
        return [dict(r) for r in rows]
    except Exception:
        return []


def _closes_to_ohlc(closes: list[list[Any]]) -> list[list[Any]]:
    """把 [date, close] 扩成扁平 OHLC，便于与日 K 拼接。"""
    out: list[list[Any]] = []
    for row in closes:
        if not row:
            continue
        d = _norm_date(row[0])
        if not d:
            continue
        if len(row) >= 5:
            out.append([d, float(row[1]), float(row[2]), float(row[3]), float(row[4])])
            continue
        if len(row) < 2:
            continue
        c = _nf(row[1])
        if c is None:
            continue
        out.append([d, c, c, c, c])
    return out


def _merge_ohlc_bars(
    primary: list[list[Any]],
    fallback: list[list[Any]],
) -> list[list[Any]]:
    """按日期合并；primary（真 OHLC）覆盖 fallback。"""
    by_date: dict[str, list[Any]] = {}
    for row in fallback:
        if row:
            by_date[str(row[0])] = row
    for row in primary:
        if row:
            by_date[str(row[0])] = row
    return [by_date[d] for d in sorted(by_date)]


def _bars_cover_trade_dates(bars: list[list[Any]], cover_dates: list[str]) -> bool:
    """日 K 是否覆盖全部成交日（成交日可为非交易日，取 <= date 的最近一根）。"""
    if not cover_dates:
        return bool(bars)
    if not bars:
        return False
    first = str(bars[0][0])
    last = str(bars[-1][0])
    bar_dates = {str(b[0]) for b in bars}
    for d in cover_dates:
        if not d:
            continue
        if d < first or d > last:
            return False
        if d in bar_dates:
            continue
        if not any(x <= d for x in bar_dates):
            return False
    return True


def _load_symbol_bars(
    symbol: str,
    start: str,
    end: str,
    *,
    fetch_missing: bool = True,
    cover_dates: list[str] | None = None,
) -> tuple[list[list[Any]], bool]:
    """返回 (bars, has_ohlc)。OHLC 为 [date,o,h,l,c]；否则退回 [date,close]。

    本地日 K 缓存常常只有最近约 800 根，可能晚于首笔成交日；此时必须补拉或
    用估值收盘价回填，否则前端会把所有买卖点落到序列第 0 根。
    """
    need_dates = sorted({_norm_date(d) for d in (cover_dates or []) if _norm_date(d)})
    candidates = [d for d in [start, end, *need_dates] if d]
    need_start = min(candidates) if candidates else start
    need_end = max(candidates) if candidates else end

    cached = _filter_ohlc_rows(_read_stale_day_klines(symbol), need_start, need_end)
    bars = cached
    if not _bars_cover_trade_dates(bars, need_dates):
        if fetch_missing:
            fetched = _filter_ohlc_rows(
                _fetch_day_klines(symbol, count=5000),
                need_start,
                need_end,
            )
            bars = _merge_ohlc_bars(fetched, cached)

    has_real_ohlc = bool(bars)
    if not _bars_cover_trade_dates(bars, need_dates):
        closes = _closes_to_ohlc(_cache_only_closes(symbol, need_start, need_end))
        if closes:
            bars = _merge_ohlc_bars(bars, closes)
            if not has_real_ohlc:
                return [[d, c] for d, _o, _h, _l, c in bars], False

    if bars:
        return bars, has_real_ohlc or bool(bars)
    closes = _cache_only_closes(symbol, need_start, need_end)
    return closes, False


def _build_by_symbol(
    trades: list[dict[str, Any]],
    round_trips: list[dict[str, Any]],
    names: dict[str, str],
    *,
    start: str,
    end: str,
    load_prices: bool = True,
) -> dict[str, dict[str, Any]]:
    """按代码聚合成交/回合，并可选附上区间 K 线/收盘价序列。"""
    by: dict[str, dict[str, Any]] = {}
    for t in trades:
        sym = str(t.get("symbol") or "").strip()
        if not sym:
            continue
        slot = by.setdefault(
            sym,
            {
                "symbol": sym,
                "name": t.get("name") or names.get(sym, ""),
                "trades": [],
                "round_trips": [],
                "prices": [],
                "has_ohlc": False,
            },
        )
        if not slot["name"]:
            slot["name"] = names.get(sym, "")
        slot["trades"].append(
            {
                "date": _norm_date(t.get("date")),
                "side": t.get("side"),
                "price": t.get("price"),
                "shares": t.get("shares"),
                "notional": t.get("notional"),
                "cost": t.get("cost"),
            }
        )
    for trip in round_trips:
        sym = str(trip.get("symbol") or "").strip()
        if not sym:
            continue
        slot = by.setdefault(
            sym,
            {
                "symbol": sym,
                "name": trip.get("name") or names.get(sym, ""),
                "trades": [],
                "round_trips": [],
                "prices": [],
                "has_ohlc": False,
            },
        )
        slot["round_trips"].append(trip)

    if load_prices and by:
        def _one(sym: str) -> tuple[str, list[list[Any]], bool]:
            cover = [str(t.get("date") or "") for t in by[sym]["trades"]]
            bars, has_ohlc = _load_symbol_bars(
                sym,
                start,
                end,
                fetch_missing=True,
                cover_dates=cover,
            )
            return sym, bars, has_ohlc

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(_one, s) for s in by]
            for fut in as_completed(futs):
                sym, prices, has_ohlc = fut.result()
                by[sym]["prices"] = prices
                by[sym]["has_ohlc"] = has_ohlc
    return by


def build_portfolio_report_model(
    out: dict[str, Any],
    *,
    load_prices: bool = True,
) -> dict[str, Any]:
    """从组合回测 JSON 派生交互报告数据模型。"""
    # 旧 JSON 无基准时尝试补齐沪深300（失败则跳过）
    try:
        from app.portfolio.benchmarks import attach_hs300_benchmark

        out = attach_hs300_benchmark(dict(out))
    except Exception:
        pass

    equity = list(out.get("equity") or [])
    trades_raw = list(out.get("trades") or [])
    rebalances = list(out.get("rebalances") or [])
    metrics = dict(out.get("metrics") or {})
    names = _symbol_name_map(rebalances)
    eq_map = _equity_map(equity)

    trades = [_trade_row(t, names=names) for t in trades_raw if isinstance(t, dict)]
    trades_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        if t["date"]:
            trades_by_date[t["date"]].append(t)

    rb_dates = [_norm_date(rb.get("date")) for rb in rebalances if _norm_date(rb.get("date"))]
    equity_dates = [_norm_date(r.get("date")) for r in equity if _norm_date(r.get("date"))]
    end_date = equity_dates[-1] if equity_dates else (rb_dates[-1] if rb_dates else "")

    periods: list[dict[str, Any]] = []
    for i, rb in enumerate(rebalances):
        day = _norm_date(rb.get("date"))
        if not day:
            continue
        next_day = rb_dates[i + 1] if i + 1 < len(rb_dates) else end_date
        eq_start = eq_map.get(day)
        eq_end = eq_map.get(next_day) if next_day else None
        period_pnl = None
        period_ret = None
        if eq_start is not None and eq_end is not None and eq_start > 0:
            period_pnl = eq_end - eq_start
            period_ret = period_pnl / eq_start

        day_trades = trades_by_date.get(day, [])
        buys = [t for t in day_trades if t["side"] == "buy"]
        sells = [t for t in day_trades if t["side"] == "sell"]
        targets = [str(s) for s in (rb.get("targets") or [])]
        selection = []
        for item in rb.get("selection") or []:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").strip()
            selection.append(
                {
                    "symbol": sym,
                    "name": str(item.get("name") or names.get(sym, "")),
                    "close": _nf(item.get("close")),
                    "float_market_cap": _nf(item.get("float_market_cap")),
                    "rank_market_cap": _nf(item.get("rank_market_cap")),
                }
            )

        periods.append(
            {
                "index": i,
                "date": day,
                "next_date": next_day,
                "equity_start": eq_start,
                "equity_end": eq_end,
                "period_pnl": period_pnl,
                "period_return": period_ret,
                "cash": _nf(rb.get("cash")),
                "targets": targets,
                "selection": selection,
                "buys": buys,
                "sells": sells,
                "buy_count": len(buys),
                "sell_count": len(sells),
            }
        )

    equity_series = [
        {"date": _norm_date(r.get("date")), "equity": _nf(r.get("equity")) or 0.0}
        for r in equity
        if _norm_date(r.get("date"))
    ]
    initial_cash = _nf(metrics.get("initial_cash"))
    if initial_cash is None and equity_series:
        initial_cash = equity_series[0]["equity"] or 1.0
    if not initial_cash or initial_cash <= 0:
        initial_cash = 1.0

    for point in equity_series:
        point["nav"] = point["equity"] / initial_cash

    # 收益/回撤比（若缺失则补算）
    dd = _nf(metrics.get("max_drawdown")) or 0.0
    total_ret = _nf(metrics.get("total_return"))
    if metrics.get("return_drawdown_ratio") is None and total_ret is not None:
        metrics["return_drawdown_ratio"] = (
            float(total_ret / dd) if dd > 1e-12 else 0.0
        )

    buy_dates = sorted({t["date"] for t in trades if t["side"] == "buy" and t["date"]})
    sell_dates = sorted({t["date"] for t in trades if t["side"] == "sell" and t["date"]})

    bm_hs300 = (out.get("benchmarks") or {}).get("hs300") or {}
    bm_equity_raw = list(bm_hs300.get("equity") or [])
    bm_metrics = dict(bm_hs300.get("metrics") or {})
    bm_by_date = {
        _norm_date(r.get("date")): r
        for r in bm_equity_raw
        if _norm_date(r.get("date"))
    }
    benchmark_series: list[dict[str, Any]] = []
    for point in equity_series:
        row = bm_by_date.get(point["date"])
        if not row:
            continue
        nav = _nf(row.get("nav"))
        eqv = _nf(row.get("equity"))
        if nav is None and eqv is not None:
            bm_cash = _nf(bm_metrics.get("initial_cash")) or initial_cash
            nav = eqv / bm_cash if bm_cash and bm_cash > 0 else None
        if nav is None:
            continue
        benchmark_series.append({"date": point["date"], "nav": float(nav), "equity": eqv})

    round_trips = _fifo_round_trips(trades_raw, names=names)
    start = equity_series[0]["date"] if equity_series else ""
    end = equity_series[-1]["date"] if equity_series else ""
    by_symbol = _build_by_symbol(
        trades,
        round_trips,
        names,
        start=start,
        end=end,
        load_prices=load_prices,
    )
    symbol_index = sorted(
        (
            {
                "symbol": s,
                "name": info.get("name") or "",
                "trade_count": len(info.get("trades") or []),
                "trip_count": len(info.get("round_trips") or []),
                "pnl": float(
                    sum(_nf(t.get("pnl")) or 0.0 for t in (info.get("round_trips") or []))
                ),
            }
            for s, info in by_symbol.items()
        ),
        key=lambda x: (-abs(x["pnl"]), -x["trade_count"], x["symbol"]),
    )

    return {
        "strategy_id": out.get("strategy_id") or "",
        "mode": out.get("mode") or "",
        "asof": out.get("asof"),
        "universe_note": out.get("universe_note") or "",
        "warnings": list(out.get("warnings") or []),
        "disclaimer": out.get("disclaimer") or "",
        "metrics": metrics,
        "benchmark_metrics": bm_metrics,
        "initial_cash": initial_cash,
        "equity": equity_series,
        "benchmark": benchmark_series,
        "periods": periods,
        "trades": trades,
        "round_trips": round_trips,
        "by_symbol": by_symbol,
        "symbol_index": symbol_index,
        "rebalance_dates": rb_dates,
        "buy_dates": buy_dates,
        "sell_dates": sell_dates,
    }
