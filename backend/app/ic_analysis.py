"""因子 IC（信息系数）分析：横截面评价因子对未来收益的预测能力。

IC（Information Coefficient）：在每个时点 t，对股票池横截面地计算
「因子值 × 未来 h 期收益」的秩相关系数（Spearman），得到逐期 RankIC 序列，
再汇总为均值 RankIC、RankIC 标准差、ICIR（均值/标准差）、t 值、IC>0 占比。

设计约定：
- 因子定义与选股打分（``app/stock_screening.py``）完全一致：注册表内每个因子
  在单只股票最后一根 K 线的值，等于对应 ``compute_*_breakdown`` 标量 /
  ``app/factors/momentum.py`` 标量，保证「IC 因子 == screen 因子」。
- 环境无 scipy，Spearman 用 numpy 手动实现：先平均秩（并列取平均），再对秩算 Pearson。
- 未来收益用前复权收盘价 ``close[t+h]/close[t] - 1``；因子在 t 取值、收益在 t..t+h，
  属标准前视窗口，报告只汇总预测力结论，不做任何交易模拟。

演示用途，IC 分析不构成投资建议。
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from app.data_sources.market_data import MarketDataError, fetch_a_share_daily
from app.factors.registry import FACTOR_MIN_BARS, FACTOR_REGISTRY, factor_source

DISCLAIMER = "演示用途，IC 分析不构成投资建议。"


# ---------------------------------------------------------------------------
# 统计工具：平均秩 Spearman
# ---------------------------------------------------------------------------

def _rankdata(x: np.ndarray) -> np.ndarray:
    """scipy.stats.rankdata 的平均秩（并列取平均）实现，返回 1 基秩。"""
    arr = np.asarray(x, dtype=float)
    n = len(arr)
    order = np.argsort(arr, kind="mergesort")
    ranks = np.empty(n, dtype=float)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and arr[order[j + 1]] == arr[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        ranks[order[i : j + 1]] = avg
        i = j + 1
    return ranks


def spearman_rank(a: np.ndarray, b: np.ndarray) -> float | None:
    """Spearman 秩相关系数；任一方零方差返回 None（无意义）。"""
    ra = _rankdata(a)
    rb = _rankdata(b)
    if np.std(ra) < 1e-15 or np.std(rb) < 1e-15:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


# ---------------------------------------------------------------------------
# 因子校验
# ---------------------------------------------------------------------------

def _validate_factors(factors: list[str] | None) -> list[str]:
    # 容错：去掉名字两端的空白（JSON 里可能带多余空格），空串忽略
    names = [name.strip() for name in (factors or []) if name and name.strip()]
    names = names or list(FACTOR_REGISTRY)
    unknown = sorted(set(names) - set(FACTOR_REGISTRY))
    if unknown:
        raise ValueError(
            f"未知因子: {', '.join(unknown)}；可选: {', '.join(sorted(FACTOR_REGISTRY))}"
        )
    return names


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def _load_panel(
    symbols: list[str],
    *,
    start: str | None,
    end: str | None,
    limit: int | None,
    max_workers: int,
) -> dict[str, pd.DataFrame]:
    """并行拉取每只股票日线，返回 {symbol: DatetimeIndex 索引的 OHLCV DataFrame}。"""
    panel: dict[str, pd.DataFrame] = {}

    def load_one(symbol: str) -> tuple[str, pd.DataFrame] | None:
        try:
            df = fetch_a_share_daily(symbol, start=start, end=end, limit=limit)
        except (MarketDataError, ValueError):
            return None
        if df is None or df.empty:
            return None
        return symbol, df

    with ThreadPoolExecutor(max_workers=min(max_workers, len(symbols))) as pool:
        futures = [pool.submit(load_one, symbol) for symbol in symbols]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception:
                continue
            if result is not None:
                panel[result[0]] = result[1]
    if not panel:
        raise MarketDataError("未能加载任何日线行情数据")
    return panel


# ---------------------------------------------------------------------------
# IC 计算与聚合
# ---------------------------------------------------------------------------

def _aggregate_horizon(horizon: int, ic_series: list[dict[str, float]]) -> dict[str, object]:
    """把某因子在某持有期的逐期 RankIC 汇总为统计量。"""
    if not ic_series:
        return {
            "horizon": horizon,
            "n_dates": 0,
            "mean_ic": None,
            "std_ic": None,
            "icir": None,
            "t_stat": None,
            "ic_positive_ratio": None,
            "ic_series": [],
        }
    ics = np.asarray([item["ic"] for item in ic_series], dtype=float)
    n = len(ics)
    mean = float(np.mean(ics))
    std = float(np.std(ics, ddof=1)) if n > 1 else 0.0
    icir = mean / std if std > 0 else None
    t_stat = mean / (std / math.sqrt(n)) if std > 0 else None
    positive = float(np.sum(ics > 0) / n)
    return {
        "horizon": horizon,
        "n_dates": n,
        "mean_ic": mean,
        "std_ic": std,
        "icir": icir,
        "t_stat": t_stat,
        "ic_positive_ratio": positive,
        "ic_series": ic_series,
    }


def compute_factor_ic(
    symbols: list[str],
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    horizons: tuple[int, ...] = (5, 10, 20),
    min_cs: int = 10,
    factors: list[str] | None = None,
    max_workers: int = 8,
) -> dict:
    """计算一组因子在多个持有期下的 RankIC 及汇总统计。

    返回 plain dict（供 CLI 直接序列化）：
    ``{symbols, count, start_date, end_date, horizons, min_cs, results, warnings}``
    """
    if min_cs < 1:
        raise ValueError("min_cs 至少为 1")
    if not symbols:
        raise ValueError("股票池为空")
    factor_names = _validate_factors(factors)
    panel = _load_panel(
        symbols, start=start, end=end, limit=limit, max_workers=max_workers
    )
    loaded_symbols = list(panel)

    # 统一交易日历 = 各股日期的并集
    all_dates: list[pd.Timestamp] = []
    for df in panel.values():
        all_dates.extend(df.index.tolist())
    cal = pd.DatetimeIndex(sorted(set(all_dates)))

    # 未来收益面板：fwd[h][symbol] 为 close[t+h]/close[t]-1（重索引到日历，含尾部 NaN）
    fwd: dict[int, pd.DataFrame] = {
        h: pd.DataFrame(index=cal, columns=loaded_symbols, dtype=float) for h in horizons
    }
    # 因子面板：fpanel[name][symbol] 为因子时间序列
    fpanel: dict[str, pd.DataFrame] = {
        name: pd.DataFrame(index=cal, columns=loaded_symbols, dtype=float)
        for name in factor_names
    }
    for symbol, df in panel.items():
        close = df["close"].astype(float)
        for h in horizons:
            fwd[h][symbol] = (close.shift(-h) / close - 1.0).reindex(cal)
        for name in factor_names:
            fpanel[name][symbol] = FACTOR_REGISTRY[name](df).reindex(cal)

    warnings: list[str] = []
    results: list[dict[str, object]] = []
    for name in factor_names:
        need = FACTOR_MIN_BARS.get(name, 1)
        valid = [
            sym for sym in loaded_symbols if len(panel[sym]) >= need
        ]
        if not valid:
            warnings.append(f"因子 {name} 需 ≥{need} 根 K 线，无有效标的，跳过。")
            continue

        horizons_out: list[dict[str, object]] = []
        for h in horizons:
            ic_series: list[dict[str, float]] = []
            for date in cal:
                vf = fpanel[name].loc[date].to_numpy(dtype=float)
                vr = fwd[h].loc[date].to_numpy(dtype=float)
                mask = np.isfinite(vf) & np.isfinite(vr)
                if int(mask.sum()) < min_cs:
                    continue
                ic = spearman_rank(vf[mask], vr[mask])
                if ic is None:
                    continue
                ic_series.append({"date": str(date)[:10], "ic": ic})
            horizons_out.append(_aggregate_horizon(h, ic_series))
        results.append(
            {"factor": name, "source": factor_source(name), "horizons": horizons_out}
        )

    return {
        "symbols": loaded_symbols,
        "count": len(loaded_symbols),
        "start_date": str(cal[0])[:10] if len(cal) else None,
        "end_date": str(cal[-1])[:10] if len(cal) else None,
        "horizons": list(horizons),
        "min_cs": min_cs,
        "results": results,
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }
