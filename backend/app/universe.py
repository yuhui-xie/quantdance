"""供选股、发现和批量回测共用的 A 股股票池解析。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _load_config_pool(pool_name: str) -> tuple[list[dict[str, str]], str]:
    """从 backend/config 读取预设股票池 JSON（如 etf_core_pool.json）。

    协议：JSON 顶层含 ``names``（dict：代码→名称，键序即股票池顺序）与可选
    ``description``。``names`` 为唯一来源：代码即其键、名称即其值。文件不存在
    或缺少非空 ``names`` 时抛 ValueError。
    """
    path = _CONFIG_DIR / f"{pool_name}_pool.json"
    if not path.exists():
        raise ValueError(f"config 股票池文件不存在: {path}")
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    names = payload.get("names") or {}
    if not isinstance(names, dict) or not names:
        raise ValueError(f"config 股票池 {path} 缺少非空 names 映射（代码→名称）")
    rows = [{"symbol": code, "name": name} for code, name in names.items()]
    desc = payload.get("description", "")
    return rows, f"使用 config 股票池 {pool_name}（{len(rows)} 只）。{desc}".strip()


def _maybe_asof_filter_config(
    rows: list[dict[str, str]],
    note: str,
    asof_s: str | None,
    *,
    asof_filter_config: bool = True,
) -> tuple[list[dict[str, str]], str]:
    """config ETF 池在带 asof 时按 K 线覆盖过滤出"当时已存在"的子集。

    复用 ``filter_etf_symbols_at`` 的存在性判定（首根 K 线 ≤ asof 即视为已上市），
    使 ETF_CONFIG（如 ``etf_core``）也能走动态 as-of 池、避开幸存者偏差。只对
    纯 K 线存在的判定，普通股票 config 池同样安全（只剔除未上市/退市标的）。
    无 asof 时原样返回。

    ``asof_filter_config=False`` 时不做过滤、返回完整池：用于**多时点回测**——
    回测从 start_date 起步，若按 start_date 过滤会永久剔除之后才上市的标的。
    此时完整池由策略 select() 在每个决策日按可用 K 线根数自然门槛（首根 K 线
    晚于决策日 → asof_fundamental_row 返回 None）逐步纳入，避免幸存者偏差也
    避免误删新上市标的。单时点选股（pick/screen）应保持默认 True。
    """
    if not asof_s or not rows or not asof_filter_config:
        return rows, note
    existing, skipped = filter_etf_symbols_at(rows, asof_s)
    if not existing:
        raise ValueError(f"{asof_s} 时点该 config 股票池无可用标的")
    skip_note = (
        f"，{len(skipped)} 只 K 线首根晚于 {asof_s} 或无线剔除" if skipped else ""
    )
    return existing, (
        f"{note}（{asof_s} 时点按 K 线覆盖判定已存在 {len(existing)} 只{skip_note}）"
    )

from app.data_sources.market_data import (
    fetch_a_share_universe,
    fetch_etf_universe,
    fetch_etf_universe_at,
    filter_etf_symbols_at,
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
    asof_filter_config: bool = True,
) -> tuple[list[dict[str, str]], str]:
    """解析自定义代码或预设指数股票池，并统一代码格式、去重。

    asof 非空且命中中证指数时，用 index_constitution 拉取该历史时点的成分股
    （避开幸存者偏差）；否则回退当前成分股（见 fetch_*_universe_at）。

    ``asof_filter_config`` 仅影响 config 股票池（如 etf_core/etf_core_sub）：
    单时点选股（pick/screen）保持 True，按 asof 过滤"当时已存在"的子集；
    多时点回测置 False，用完整池并由 select() 逐决策日自然纳入（见
    ``_maybe_asof_filter_config``）。
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
    # 动态 as-of ETF 池：etf / etf_dynamic（全市场）在带 asof 时按 K 线覆盖
    # 重构"当时已存在"的池，避开幸存者偏差；新上市 ETF 在上市日之后自动纳入。
    if selected in {"etf", "etfs", "etf_dynamic", "etf_asof"}:
        if asof_s:
            return fetch_etf_universe_at(asof_s, max_universe, seed=seed)
        return fetch_etf_universe(max_universe, seed=seed)
    # 全市场"当前"清单别名：etf_market / etf_all 不做 as-of 过滤，返回当前全市场
    # ETF 快照。多时点回测/动态行业池发现用——面板一次性载入全市场全历史，由策略
    # select() 在每个决策日按 date<=asof 判定存在性、自然纳入新上市标的（与 config
    # 池 asof_filter_config=False 同构，避免 2020 起步被永久截断成"仅 start 前上市"）。
    if selected in {"etf_market", "etf_all"}:
        rows, _fetch_note = fetch_etf_universe(max_universe, seed=seed)
        return rows, f"{_fetch_note}（成员由策略逐决策日 as-of 发现）"
    if selected.startswith("config:"):
        rows, note = _load_config_pool(selected.split(":", 1)[1])
        return _maybe_asof_filter_config(
            rows, note, asof_s, asof_filter_config=asof_filter_config
        )
    if (_CONFIG_DIR / f"{selected}_pool.json").exists():
        rows, note = _load_config_pool(selected)
        return _maybe_asof_filter_config(
            rows, note, asof_s, asof_filter_config=asof_filter_config
        )
    return fetch_a_share_universe(max_universe, seed=seed)
