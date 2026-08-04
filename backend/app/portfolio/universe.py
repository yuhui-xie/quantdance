"""组合策略股票池解析。"""

from __future__ import annotations

from app.schemas import PortfolioBacktestRequest
from app.universe import resolve_universe_rows


def resolve_universe(
    req: PortfolioBacktestRequest,
    *,
    default_universe: str | None = None,
) -> tuple[list[dict[str, str]], str]:
    return resolve_universe_rows(
        symbols=req.symbols,
        universe=req.universe,
        max_universe=req.max_universe,
        seed=req.seed,
        default_universe=default_universe or "all_a",
    )
