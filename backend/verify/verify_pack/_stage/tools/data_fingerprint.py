"""跨机比对用：导出行情数据指纹 + 环境信息。

**在 backend/ 目录下运行，两台机器各跑一次**：

    .venv/Scripts/python.exe out/verify_pack/tools/data_fingerprint.py > out/_pack/data_fingerprint.json

（在 `backend/` 目录下跑即可，脚本自己会把 `backend/` 加进 `sys.path`。）

输出 JSON 两部分：

- ``env``      git 版本、Python / 关键依赖版本、K 线缓存版本、缓存目录与开关；
- ``universes`` 每个股票池的成员表哈希（``symbols_list_sha256_16``）与逐标的 K 线指纹
  （根数 / 首末交易日 / 末值 / 收盘价序列哈希）。

⚠️ ``etf_market`` 的成员表来自 akshare **按日缓存**：两台机器抓取日期不同，成员表本身就不同，
这会直接改变动态配方的候选范围。所以 ``symbols_list_sha256_16`` 要一起比。

回测"两台机器对不上"通常只有两类原因：**行情数据不同**（数据源节点、复权口径、
缓存新旧）或**代码/参数不同**。把两台的 ``env`` 与 ``symbols`` 逐条 diff 即可定位：
``env.git_rev``/依赖不同 ⇒ 代码环境问题；某些标的 ``close_hash`` 或 ``bars`` 不同
⇒ 数据问题，且能直接看出是哪几只、差多少根。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import numpy as np

# 直接以脚本路径运行时 `app` 不在 sys.path 上（只有 `python -m app.script` 才在），
# 这里显式补上 backend/，让本脚本在哪台机器上都能原样跑。
BACKEND = Path(__file__).resolve().parents[3]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _git_rev() -> dict[str, str]:
    def run(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", *args], cwd=BACKEND, stderr=subprocess.DEVNULL
            ).decode(errors="replace").strip()
        except Exception:
            return ""

    return {
        "git_rev": run("rev-parse", "HEAD"),
        "git_branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": run("status", "--porcelain"),
    }


def _versions() -> dict[str, str]:
    out = {"python": sys.version.split()[0], "platform": platform.platform()}
    for mod in ("pandas", "numpy", "mootdx", "akshare", "pydantic"):
        try:
            out[mod] = getattr(__import__(mod), "__version__", "unknown")
        except Exception as exc:  # pragma: no cover - 依赖缺失时如实记录
            out[mod] = f"unavailable ({exc.__class__.__name__})"
    return out


def _cache_info() -> dict[str, object]:
    from app.data_sources import a_stock_data as adm
    from app.data_sources.a_stock_data import AStockDataSDK

    cache_dir = AStockDataSDK._default_cache_dir()
    return {
        # _KLINE_CACHE_VERSION 是模块级常量（不在 SDK 类上）
        "kline_cache_version": str(getattr(adm, "_KLINE_CACHE_VERSION", "unknown")),
        "cache_enabled": os.environ.get("QUANTDANCE_A_STOCK_DATA_CACHE", "(unset)"),
        "cache_dir": str(cache_dir),
        "cache_dir_exists": cache_dir.is_dir(),
        # 缓存目录里已落盘的 K 线文件数与最新 mtime，便于看出两台机器的缓存新旧
        "cache_files": sum(1 for _ in cache_dir.rglob("*")) if cache_dir.is_dir() else 0,
    }


def _fingerprint_symbol(symbol: str, data_source: str) -> dict[str, object]:
    from app.data_sources.market_data import fetch_a_share_daily

    try:
        daily = fetch_a_share_daily(symbol, limit=5000, data_source=data_source)
    except Exception as exc:
        return {"status": f"error: {exc.__class__.__name__}: {exc}"}
    if daily is None or daily.empty:
        return {"status": "empty"}
    closes = daily["close"].astype(float).to_numpy()
    digest = hashlib.sha1(np.round(closes, 4).tobytes()).hexdigest()[:16]
    index = [str(i)[:10] for i in daily.index]
    out: dict[str, object] = {
        "status": "ok",
        "bars": int(len(closes)),
        "first_date": index[0] if index else "",
        "last_date": index[-1] if index else "",
        "last_close": round(float(closes[-1]), 4),
        "close_hash": digest,
    }
    # amount/volume 不参与选股排序的主键，但 amount_score 直接由 amount 算出。
    # 实测同一台机器两次跑，amount_score 会出现 ~1e-16 的差（未影响任何成交），
    # 单列哈希便于确认跨机的这点噪声是否来自行情本身。
    for column in ("amount", "volume"):
        if column in daily.columns:
            values = daily[column].astype(float).to_numpy()
            out[f"{column}_hash"] = hashlib.sha1(
                np.round(values, 6).tobytes()).hexdigest()[:16]
    return out


def main() -> None:
    from app.universe import resolve_universe_rows

    # 与两个主配方一致的口径：精选子池（max_universe 用 schema 默认 80）+ 全市场动态池 2000。
    # 注意 etf_market 的成员来自 akshare 且按日缓存，两台机器抓取日期不同就会得到不同成员表。
    targets = [
        ("etf_core_sub", "etf_core_sub", 80),
        ("etf_market", "etf_market", 2000),
    ]
    payload: dict[str, object] = {
        "env": {**_git_rev(), **_versions(), **_cache_info()},
        "universes": {},
    }
    for name, universe, max_universe in targets:
        rows, note = resolve_universe_rows(
            symbols=None, universe=universe, max_universe=max_universe, seed=None)
        symbols = sorted({str(r["symbol"]) for r in rows})
        entry: dict[str, object] = {
            "note": note,
            "count": len(symbols),
            "symbols_list_sha256_16": hashlib.sha256(
                "\n".join(symbols).encode()).hexdigest()[:16],
            "symbols": {},
        }
        for i, symbol in enumerate(symbols, start=1):
            entry["symbols"][symbol] = _fingerprint_symbol(symbol, "a_stock_data")
            if i % 200 == 0:
                print(f"[{name}] {i}/{len(symbols)}", file=sys.stderr)
        payload["universes"][name] = entry
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
