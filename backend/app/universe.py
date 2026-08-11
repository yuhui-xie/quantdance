"""供选股、发现和批量回测共用的 A 股股票池解析。"""

from __future__ import annotations

from collections.abc import Sequence

from app.data_sources.market_data import (
    fetch_a_share_universe,
    fetch_etf_universe,
    fetch_gz2000_universe,
    fetch_hs300_universe,
    fetch_star50_universe,
    fetch_star_board_universe,
    fetch_zz1000_universe,
    fetch_zz399101_universe,
    fetch_zz500_universe,
    normalize_a_share_symbol,
)


def resolve_universe_rows(
    *,
    symbols: Sequence[str] | None,
    universe: str | None,
    max_universe: int,
    seed: int | None,
    default_universe: str = "all_a",
) -> tuple[list[dict[str, str]], str]:
    """解析自定义代码或预设指数股票池，并统一代码格式、去重。"""
    if symbols:
        seen: set[str] = set()
        rows: list[dict[str, str]] = []
        for raw in symbols:
            code = normalize_a_share_symbol(raw)
            if code in seen:
                continue
            seen.add(code)
            rows.append({"symbol": code, "name": ""})
        return rows, f"使用请求传入股票池，共 {len(rows)} 只。"

    selected = (universe or default_universe).strip().lower()
    if selected == "hs300":
        return fetch_hs300_universe(max_universe, seed=seed)
    if selected in {"zz500", "000905"}:
        return fetch_zz500_universe(max_universe, seed=seed)
    if selected in {"zz399101", "399101"}:
        return fetch_zz399101_universe(max_universe, seed=seed)
    if selected in {"zz1000", "000852"}:
        return fetch_zz1000_universe(max_universe, seed=seed)
    if selected in {"star50", "kc50", "000688"}:
        return fetch_star50_universe(max_universe, seed=seed)
    if selected in {"gz2000", "399303"}:
        return fetch_gz2000_universe(max_universe, seed=seed)
    if selected in {"star_board", "kcb"}:
        return fetch_star_board_universe(max_universe, seed=seed)
    if selected in {"etf", "etfs"}:
        return fetch_etf_universe(max_universe, seed=seed)
    return fetch_a_share_universe(max_universe, seed=seed)
