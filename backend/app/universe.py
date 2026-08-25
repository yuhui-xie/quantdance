"""供选股、发现和批量回测共用的 A 股股票池解析。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _attach_config_pool_names(symbols: list[str]) -> dict[str, str]:
    """用 akshare 场内 ETF 列表补 config 池（如 etf_core）的名称。

    与 fetch_etf_universe 的数据源链一致：东财实时 → 同花顺。仅用于纯 ETF 池
    （config 池当前只有 ETF）。失败或缺码时对应 name 留空，不影响成分正确性。
    """
    name_map: dict[str, str] = {}
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - 依赖环境分支
        return name_map
    providers = (
        ("fund_etf_spot_em", (), ("代码", "code", "symbol"), ("名称", "name"), False),
        ("fund_etf_spot_ths", (), ("基金代码", "代码", "code", "symbol"), ("基金简称", "基金名称", "名称", "name"), False),
    )
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
        except Exception:  # pragma: no cover - 网络/源异常，继续尝试下一个
            continue
        if parsed:
            for r in parsed:
                if r["name"]:
                    name_map[r["symbol"]] = r["name"]
            break
    return name_map


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
    # 优先用 JSON 里的静态 names 映射（离线可靠），缺名代码再走 akshare 补。
    static_names = payload.get("names") or {}
    missing = [c for c in symbols if not static_names.get(c)]
    name_map = dict(static_names)
    if missing:
        name_map.update(_attach_config_pool_names(missing))
    rows = [{"symbol": code, "name": name_map.get(code, "")} for code in symbols]
    desc = payload.get("description", "")
    return rows, f"使用 config 股票池 {pool_name}（{len(rows)} 只）。{desc}".strip()


def _maybe_asof_filter_config(
    rows: list[dict[str, str]],
    note: str,
    asof_s: str | None,
) -> tuple[list[dict[str, str]], str]:
    """config ETF 池在带 asof 时按 K 线覆盖过滤出"当时已存在"的子集。

    复用 ``filter_etf_symbols_at`` 的存在性判定（首根 K 线 ≤ asof 即视为已上市），
    使 ETF_CONFIG（如 ``etf_core``）也能走动态 as-of 池、避开幸存者偏差。只对
    纯 K 线存在的判定，普通股票 config 池同样安全（只剔除未上市/退市标的）。
    无 asof 时原样返回。
    """
    if not asof_s or not rows:
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
    _parse_etf_df,
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
    # 动态 as-of ETF 池：etf / etf_dynamic（全市场）在带 asof 时按 K 线覆盖
    # 重构"当时已存在"的池，避开幸存者偏差；新上市 ETF 在上市日之后自动纳入。
    if selected in {"etf", "etfs", "etf_dynamic", "etf_asof"}:
        if asof_s:
            return fetch_etf_universe_at(asof_s, max_universe, seed=seed)
        return fetch_etf_universe(max_universe, seed=seed)
    if selected.startswith("config:"):
        rows, note = _load_config_pool(selected.split(":", 1)[1])
        return _maybe_asof_filter_config(rows, note, asof_s)
    if (_CONFIG_DIR / f"{selected}_pool.json").exists():
        rows, note = _load_config_pool(selected)
        return _maybe_asof_filter_config(rows, note, asof_s)
    return fetch_a_share_universe(max_universe, seed=seed)
