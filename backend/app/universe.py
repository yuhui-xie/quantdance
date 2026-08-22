"""供选股、发现和批量回测共用的 A 股股票池解析。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _load_config_pool(pool_name: str) -> tuple[list[dict[str, str]], str]:
    """从 backend/config 读取预设股票池 JSON（如 etf_core_pool.json）。

    返回 (rows, note)。文件不存在或格式不符时抛 ValueError。
    """
    path = _CONFIG_DIR / f"{pool_name}_pool.json"
    if not path.exists():
        raise ValueError(f"config 股票池文件不存在: {path}")
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ValueError(f"config 股票池 {path} 缺少非空 symbols 列表")
    rows = [{"symbol": code, "name": ""} for code in symbols]
    desc = payload.get("description", "")
    return rows, f"使用 config 股票池 {pool_name}（{len(rows)} 只）。{desc}".strip()

from app.data_sources.market_data import (
    fetch_a_share_universe,
    fetch_etf_universe,
    fetch_gz2000_universe,
    fetch_hs300_universe,
    fetch_hs300_universe_at,
    fetch_star50_universe,
    fetch_star_board_universe,
    fetch_zz1000_universe,
    fetch_zz1000_universe_at,
    fetch_zz399101_universe,
    fetch_zz500_universe,
    fetch_zz500_universe_at,
    normalize_a_share_symbol,
)


def resolve_universe_rows(
    *,
    symbols: Sequence[str] | None,
    universe: str | None,
    max_universe: int,
    seed: int | None,
    default_universe: str = "all_a",
    asof: str | date | None = None,
) -> tuple[list[dict[str, str]], str]:
    """解析自定义代码或预设指数股票池，并统一代码格式、去重。

    asof 非空且命中中证指数时，用 index_constitution 拉取该历史时点的成分股
    （避开幸存者偏差）；否则回退当前成分股（见 fetch_*_universe_at）。
    """
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
    asof_s = str(asof)[:10] if asof is not None else None
    if selected == "hs300":
        if asof_s:
            return fetch_hs300_universe_at(asof_s, max_universe, seed=seed)
        return fetch_hs300_universe(max_universe, seed=seed)
    if selected in {"zz500", "000905"}:
        if asof_s:
            return fetch_zz500_universe_at(asof_s, max_universe, seed=seed)
        return fetch_zz500_universe(max_universe, seed=seed)
    if selected in {"zz399101", "399101"}:
        return fetch_zz399101_universe(max_universe, seed=seed)
    if selected in {"zz1000", "000852"}:
        if asof_s:
            return fetch_zz1000_universe_at(asof_s, max_universe, seed=seed)
        return fetch_zz1000_universe(max_universe, seed=seed)
    if selected in {"star50", "kc50", "000688"}:
        return fetch_star50_universe(max_universe, seed=seed)
    if selected in {"gz2000", "399303"}:
        return fetch_gz2000_universe(max_universe, seed=seed)
    if selected in {"star_board", "kcb"}:
        return fetch_star_board_universe(max_universe, seed=seed)
    if selected in {"etf", "etfs"}:
        return fetch_etf_universe(max_universe, seed=seed)
    if selected.startswith("config:"):
        return _load_config_pool(selected.split(":", 1)[1])
    if (_CONFIG_DIR / f"{selected}_pool.json").exists():
        return _load_config_pool(selected)
    return fetch_a_share_universe(max_universe, seed=seed)
