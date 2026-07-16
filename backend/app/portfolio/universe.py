"""组合策略股票池解析。"""

from __future__ import annotations

from app.data_sources.market_data import (
    fetch_a_share_universe,
    fetch_hs300_universe,
    fetch_zz399101_universe,
    normalize_a_share_symbol,
)
from app.schemas import PortfolioBacktestRequest


def resolve_universe(
    req: PortfolioBacktestRequest,
    *,
    default_universe: str | None = None,
) -> tuple[list[dict[str, str]], str]:
    if req.symbols:
        seen: set[str] = set()
        rows: list[dict[str, str]] = []
        for raw in req.symbols:
            code = normalize_a_share_symbol(raw)
            if code in seen:
                continue
            seen.add(code)
            rows.append({"symbol": code, "name": ""})
        return rows, f"使用请求传入股票池，共 {len(rows)} 只。"

    universe = (req.universe or default_universe or "all_a").strip()
    if universe == "hs300":
        return fetch_hs300_universe(req.max_universe, seed=req.seed)
    if universe in {"zz399101", "399101"}:
        return fetch_zz399101_universe(req.max_universe, seed=req.seed)
    return fetch_a_share_universe(req.max_universe, seed=req.seed)
