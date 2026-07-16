"""CLI 回测结果静态图：matplotlib 多子图（权益、回撤、K 线+成交、指标）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
import warnings

warnings.simplefilter("ignore", ResourceWarning)

SubplotKind = Literal["macd", "vol_ratio", "stoch", "rsi", "bollinger", "donchian", "ma"]


def _detect_subplot_kind(price: list[dict[str, Any]]) -> SubplotKind:
    if any(p.get("macd") is not None for p in price):
        return "macd"
    if any(p.get("vol_ratio") is not None for p in price):
        return "vol_ratio"
    if any(p.get("stoch_k") is not None for p in price):
        return "stoch"
    if any(p.get("rsi") is not None for p in price):
        return "rsi"
    if any(p.get("bb_middle") is not None for p in price):
        return "bollinger"
    if any(p.get("donchian_high") is not None for p in price):
        return "donchian"
    return "ma"


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


def _norm_date(d: str) -> str:
    s = str(d).strip()
    return s[:10] if len(s) >= 10 else s


# 栅格图（PNG 等）导出分辨率；矢量格式（SVG/PDF）本身可无限缩放。
_RASTER_DPI = 600
_VECTOR_SUFFIXES = {".svg", ".pdf", ".eps"}


def _configure_chinese_fonts() -> None:
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    preferred = [
        "Microsoft YaHei",
        "SimHei",
        "SimSun",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "Arial Unicode MS",
    ]
    installed = {f.name for f in font_manager.fontManager.ttflist}
    available = [name for name in preferred if name in installed]
    if available:
        plt.rcParams["font.sans-serif"] = available + ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def _plot_output_paths(dest: Path) -> list[Path]:
    """主路径外始终同步写出同名 `.svg` 与 `.png`。"""
    dest = dest.expanduser().resolve()
    if not dest.suffix:
        dest = dest.with_suffix(".svg")
    paths = {dest.with_suffix(".svg"), dest.with_suffix(".png")}
    suffix = dest.suffix.lower()
    if suffix not in {".svg", ".png"}:
        paths.add(dest)
    return sorted(paths, key=lambda p: (p.suffix.lower() != ".svg", p.suffix.lower()))


def _savefig(fig, dest: Path) -> list[Path]:
    """保存图表：默认 SVG，并同步写出同名高 DPI PNG。"""
    targets = _plot_output_paths(dest)
    targets[0].parent.mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r"Glyph .* missing from font.*")
        for path in targets:
            suffix = path.suffix.lower()
            kwargs: dict[str, Any] = {
                "facecolor": "#0f1115",
                "edgecolor": "none",
                "bbox_inches": "tight",
                "dpi": _RASTER_DPI,
            }
            if suffix in _VECTOR_SUFFIXES:
                kwargs["format"] = suffix.lstrip(".")
            fig.savefig(path, **kwargs)
    return targets


def _fseries(rows: list[dict[str, Any]], key: str) -> list[float]:
    """与 numpy 数组对齐的序列；缺失为 nan；保留 0。"""

    out: list[float] = []
    for p in rows:
        v = _nf(p.get(key))
        out.append(float("nan") if v is None else v)
    return out


def _plot_candles(
    ax,
    xs: range,
    price_rows: list[dict[str, Any]],
    *,
    width: float = 0.55,
) -> None:
    for i in xs:
        p = price_rows[i]
        o = _nf(p.get("open"))
        h = _nf(p.get("high"))
        l = _nf(p.get("low"))
        c = _nf(p.get("close"))
        if o is None or h is None or l is None or c is None:
            continue
        # A 股习惯：红涨绿跌
        color = "#ef5350" if c >= o else "#26a69a"
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, solid_capstyle="round")
        lo = min(o, c)
        hi = max(o, c)
        ax.bar(
            i,
            hi - lo,
            width,
            bottom=lo,
            color=color,
            edgecolor=color,
            linewidth=0.5,
        )


def render_backtest_figure(out: dict[str, Any], dest: Path) -> list[Path]:
    """将回测结果写入图片。默认 SVG，并同步保存同名高 DPI PNG。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import gridspec

    _configure_chinese_fonts()

    metrics: dict[str, Any] = out["metrics"]
    equity: list[dict[str, Any]] = out["equity"]
    trades: list[dict[str, Any]] = out["trades"]
    price: list[dict[str, Any]] = out["price"]

    if not equity or not price:
        raise ValueError("权益或价格数据为空，无法绘图")

    ic = float(metrics["initial_cash"])
    c0 = _nf(price[0].get("close"))
    if c0 is None or c0 <= 0:
        raise ValueError("首日收盘价无效")

    dates = [e["date"] for e in equity]
    n_eq = len(dates)
    nx = min(n_eq, len(price))
    if nx <= 0:
        raise ValueError("数据长度无效")

    dates = dates[:nx]
    eq_vals = [float(e["equity"]) for e in equity[:nx]]
    buy_hold = []
    for i in range(nx):
        c = _nf(price[i].get("close"))
        buy_hold.append(ic * (c / c0) if c is not None and c > 0 else float("nan"))

    peak = float("-inf")
    dd_vals: list[float] = []
    for e in eq_vals:
        peak = max(peak, e)
        dd_vals.append((peak - e) / peak if peak > 0 else 0.0)

    x_idx = range(nx)
    price = price[:nx]
    kind = _detect_subplot_kind(price)

    fig = plt.figure(figsize=(12, 10), facecolor="#0f1115")
    gs = gridspec.GridSpec(4, 1, height_ratios=[1.0, 0.75, 1.25, 0.95], hspace=0.35)

    ax0 = fig.add_subplot(gs[0])
    ax1 = fig.add_subplot(gs[1], sharex=ax0)
    ax2 = fig.add_subplot(gs[2], sharex=ax0)
    ax3 = fig.add_subplot(gs[3], sharex=ax0)

    for ax in (ax0, ax1, ax2, ax3):
        ax.set_facecolor("#1a1d24")
        ax.tick_params(colors="#9aa0a6", labelsize=8)
        ax.grid(True, color="#2a2f3a", linestyle="--", linewidth=0.5, alpha=0.8)
        for spine in ax.spines.values():
            spine.set_color("#2a2f3a")

    ax0.plot(x_idx, eq_vals, color="#ce93d8", linewidth=1.2, label="策略权益")
    ax0.plot(x_idx, buy_hold, color="#90caf9", linewidth=1.0, label="买入持有", alpha=0.95)
    ax0.set_ylabel("净值", color="#e8eaed", fontsize=9)
    ax0.legend(loc="upper left", fontsize=8, facecolor="#1a1d24", edgecolor="#2a2f3a", labelcolor="#e8eaed")
    ax0.set_title("权益 vs 买入持有", color="#e8eaed", fontsize=10)

    ax1.fill_between(x_idx, dd_vals, 0, color="#8b4545", alpha=0.35, linewidth=0)
    ax1.plot(x_idx, dd_vals, color="#e57373", linewidth=1.0)
    ax1.set_ylabel("回撤", color="#e8eaed", fontsize=9)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y * 100:.0f}%"))
    ax1.set_title("回撤（自峰值）", color="#e8eaed", fontsize=10)

    has_ohlc = all(
        _nf(price[i].get("open")) is not None
        and _nf(price[i].get("high")) is not None
        and _nf(price[i].get("low")) is not None
        for i in range(nx)
    )
    if has_ohlc:
        _plot_candles(ax2, range(nx), price)
    else:
        closes = [_nf(price[i].get("close")) for i in range(nx)]
        ax2.plot(x_idx, [c if c is not None else float("nan") for c in closes], color="#7cb7ff", linewidth=1.2)

    pr = price

    def add_price_line(key: str, color: str, label: str) -> None:
        ys = [_nf(p.get(key)) for p in pr]
        if not any(v is not None for v in ys):
            return
        ax2.plot(
            x_idx,
            [v if v is not None else float("nan") for v in ys],
            color=color,
            linewidth=0.9,
            label=label,
            alpha=0.9,
        )

    if kind == "ma":
        add_price_line("fast_ma", "#ffb74d", "快线")
        add_price_line("slow_ma", "#81c784", "慢线")
    if kind == "bollinger":
        add_price_line("bb_upper", "#9575cd", "布林上")
        add_price_line("bb_middle", "#b39ddb", "布林中")
        add_price_line("bb_lower", "#7986cb", "布林下")
    if kind == "donchian":
        add_price_line("donchian_high", "#ffab91", "唐奇安上")
        add_price_line("donchian_low", "#4fc3f7", "唐奇安下")

    trade_points: dict[str, list[tuple[int, float]]] = {"buy": [], "sell": []}
    date_to_i = {_norm_date(d): j for j, d in enumerate(dates)}
    for t in trades:
        d = _norm_date(str(t.get("date", "")))
        side = str(t.get("side", ""))
        if d in date_to_i and side in trade_points:
            i = date_to_i[d]
            y = _nf(t.get("price"))
            if y is None and i < len(price):
                y = _nf(price[i].get("close"))
            if y is not None:
                trade_points[side].append((i, y))

    for points, color, marker, label in (
        (trade_points["buy"], "#00e676", "^", "买入点"),
        (trade_points["sell"], "#ff5252", "v", "卖出点"),
    ):
        if not points:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        ax2.scatter(
            xs,
            ys,
            c=color,
            marker=marker,
            s=82,
            zorder=6,
            edgecolors="white",
            linewidths=0.7,
            label=label,
        )
        for x, y in points:
            ax2.annotate(
                "买" if label == "买入点" else "卖",
                (x, y),
                xytext=(0, 10 if label == "买入点" else -14),
                textcoords="offset points",
                ha="center",
                va="center",
                fontsize=7,
                color=color,
                zorder=7,
            )

    ax2.set_ylabel("价格", color="#e8eaed", fontsize=9)
    ax2.set_title("K 线 / 价格与成交标记", color="#e8eaed", fontsize=10)
    if len(ax2.get_legend_handles_labels()[0]) > 0:
        ax2.legend(loc="upper left", fontsize=7, facecolor="#1a1d24", edgecolor="#2a2f3a", labelcolor="#e8eaed")

    if kind == "macd":
        ax3.bar(
            x_idx,
            _fseries(pr, "macd_hist"),
            color="#5c6bc0",
            alpha=0.4,
            width=0.7,
            label="柱",
        )
        ax3.plot(x_idx, _fseries(pr, "macd"), color="#ffb74d", linewidth=1, label="MACD")
        ax3.plot(x_idx, _fseries(pr, "macd_signal"), color="#81c784", linewidth=1, label="Signal")
    elif kind == "vol_ratio":
        ax3.axhline(y=1.0, color="#9aa0a6", linestyle="--", linewidth=0.6, alpha=0.7)
        ax3.plot(x_idx, _fseries(pr, "vol_ratio"), color="#ffb74d", linewidth=1, label="量比")
        if any(p.get("high_th") is not None for p in pr):
            ax3.plot(
                x_idx,
                _fseries(pr, "high_th"),
                color="#ef5350",
                linewidth=0.9,
                linestyle="--",
                label="放量阈值",
            )
        if any(p.get("low_th") is not None for p in pr):
            ax3.plot(
                x_idx,
                _fseries(pr, "low_th"),
                color="#66bb6a",
                linewidth=0.9,
                linestyle="--",
                label="缩量阈值",
            )
    elif kind == "stoch":
        ax3.axhline(20, color="#66bb6a", linestyle="--", linewidth=0.6, alpha=0.7)
        ax3.axhline(80, color="#ff7043", linestyle="--", linewidth=0.6, alpha=0.7)
        ax3.plot(x_idx, _fseries(pr, "stoch_k"), color="#ffb74d", label="%K")
        ax3.plot(x_idx, _fseries(pr, "stoch_d"), color="#81c784", label="%D")
        ax3.set_ylim(0, 100)
    elif kind == "rsi":
        ax3.axhline(30, color="#66bb6a", linestyle="--", linewidth=0.6, alpha=0.7)
        ax3.axhline(70, color="#ff7043", linestyle="--", linewidth=0.6, alpha=0.7)
        ax3.plot(x_idx, _fseries(pr, "rsi"), color="#ce93d8", linewidth=1.2, label="RSI")
        ax3.set_ylim(0, 100)
    elif kind == "bollinger":
        ax3.plot(x_idx, _fseries(pr, "bb_upper"), color="#9575cd", label="上轨")
        ax3.plot(x_idx, _fseries(pr, "bb_middle"), color="#b39ddb", label="中轨")
        ax3.plot(x_idx, _fseries(pr, "bb_lower"), color="#7986cb", label="下轨")
    elif kind == "donchian":
        ax3.plot(x_idx, _fseries(pr, "donchian_high"), color="#ffab91", label="上轨")
        ax3.plot(x_idx, _fseries(pr, "donchian_low"), color="#4fc3f7", label="下轨")
    else:
        ax3.plot(x_idx, _fseries(pr, "fast_ma"), color="#ffb74d", linewidth=1, label="快线")
        ax3.plot(x_idx, _fseries(pr, "slow_ma"), color="#81c784", linewidth=1, label="慢线")

    ax3.set_ylabel("指标", color="#e8eaed", fontsize=9)
    ax3.set_title(f"指标：{kind}", color="#e8eaed", fontsize=10)
    h3, l3 = ax3.get_legend_handles_labels()
    if h3:
        ax3.legend(h3, l3, loc="upper left", fontsize=7, facecolor="#1a1d24", edgecolor="#2a2f3a", labelcolor="#e8eaed")

    tick_step = max(1, nx // 12)
    tick_pos = list(range(0, nx, tick_step))
    tick_lbl = [dates[i][:10] if len(dates[i]) >= 10 else dates[i] for i in tick_pos]
    ax3.set_xticks(tick_pos)
    ax3.set_xticklabels(tick_lbl, rotation=35, ha="right")

    plt.setp(ax0.get_xticklabels(), visible=False)
    plt.setp(ax1.get_xticklabels(), visible=False)
    plt.setp(ax2.get_xticklabels(), visible=False)

    fig.patch.set_facecolor("#0f1115")
    saved = _savefig(fig, dest)
    plt.close(fig)
    return saved


def _equity_by_date(rows: list[dict[str, Any]], initial_cash: float | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        date = _norm_date(str(row.get("date", "")))
        value = _nf(row.get("equity"))
        if not date or value is None or value <= 0:
            continue
        base = initial_cash if initial_cash and initial_cash > 0 else value
        out[date] = value / base
    return out


def render_discovery_figure(out: dict[str, Any], dest: Path) -> list[Path]:
    """将批量策略发现结果写成策略 vs 基准对比图（保存规则同 render_backtest_figure）。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import gridspec

    _configure_chinese_fonts()

    benchmarks = out.get("benchmarks", {})
    strategy = benchmarks.get("strategy_equal_weight")
    baseline = benchmarks.get("hs300_equal_weight")
    if not strategy:
        raise ValueError("缺少 strategy_equal_weight 净值曲线，无法绘制策略对比图")

    strategy_metrics = strategy.get("metrics", {})
    baseline_metrics = baseline.get("metrics", {}) if baseline else {}
    strategy_rows = strategy.get("equity", [])
    baseline_rows = baseline.get("equity", []) if baseline else []
    if not strategy_rows:
        raise ValueError("策略净值曲线为空，无法绘图")

    strategy_initial = _nf(strategy_metrics.get("initial_cash"))
    baseline_initial = _nf(baseline_metrics.get("initial_cash"))
    strategy_map = _equity_by_date(strategy_rows, strategy_initial)
    baseline_map = _equity_by_date(baseline_rows, baseline_initial)
    dates = sorted(set(strategy_map) | set(baseline_map))
    if not dates:
        raise ValueError("净值日期为空，无法绘图")

    strategy_vals = [strategy_map.get(date, float("nan")) for date in dates]
    baseline_vals = [baseline_map.get(date, float("nan")) for date in dates]
    excess_dates = [date for date in dates if date in strategy_map and date in baseline_map and baseline_map[date] > 0]
    excess_vals = [strategy_map[date] / baseline_map[date] - 1.0 for date in excess_dates]
    excess_x = [dates.index(date) for date in excess_dates]

    fig = plt.figure(figsize=(12, 7), facecolor="#0f1115")
    gs = gridspec.GridSpec(2, 1, height_ratios=[1.35, 0.85], hspace=0.28)
    ax0 = fig.add_subplot(gs[0])
    ax1 = fig.add_subplot(gs[1], sharex=ax0)

    for ax in (ax0, ax1):
        ax.set_facecolor("#1a1d24")
        ax.tick_params(colors="#9aa0a6", labelsize=8)
        ax.grid(True, color="#2a2f3a", linestyle="--", linewidth=0.5, alpha=0.8)
        for spine in ax.spines.values():
            spine.set_color("#2a2f3a")

    x_idx = range(len(dates))
    ax0.plot(x_idx, strategy_vals, color="#ce93d8", linewidth=1.4, label="策略等权")
    if baseline_map:
        ax0.plot(x_idx, baseline_vals, color="#90caf9", linewidth=1.2, label="沪深300等权基准")
    ax0.axhline(1.0, color="#9aa0a6", linestyle="--", linewidth=0.7, alpha=0.7)
    ax0.set_ylabel("净值", color="#e8eaed", fontsize=9)
    ax0.set_title("策略 vs 基准净值对比", color="#e8eaed", fontsize=11)
    ax0.legend(loc="upper left", fontsize=8, facecolor="#1a1d24", edgecolor="#2a2f3a", labelcolor="#e8eaed")

    summary = out.get("summary", {})
    text = (
        f"策略平均收益 {_fmt_percent(summary.get('average_total_return'))} | "
        f"平均超额 {_fmt_percent(summary.get('average_excess_total_return'))} | "
        f"基准收益 {_fmt_percent(baseline_metrics.get('total_return'))}"
    )
    ax0.text(
        0.01,
        0.03,
        text,
        transform=ax0.transAxes,
        color="#e8eaed",
        fontsize=8,
        bbox={"facecolor": "#0f1115", "edgecolor": "#2a2f3a", "alpha": 0.85, "pad": 5},
    )

    if excess_vals:
        colors = ["#66bb6a" if value >= 0 else "#ef5350" for value in excess_vals]
        ax1.bar(excess_x, excess_vals, color=colors, alpha=0.55, width=0.8)
        ax1.plot(excess_x, excess_vals, color="#ffb74d", linewidth=1.0, alpha=0.95)
    ax1.axhline(0.0, color="#9aa0a6", linestyle="--", linewidth=0.7, alpha=0.7)
    ax1.set_ylabel("超额", color="#e8eaed", fontsize=9)
    ax1.set_title("策略相对沪深300等权基准的超额净值", color="#e8eaed", fontsize=10)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y * 100:.0f}%"))

    tick_step = max(1, len(dates) // 12)
    tick_pos = list(range(0, len(dates), tick_step))
    tick_lbl = [dates[i] for i in tick_pos]
    ax1.set_xticks(tick_pos)
    ax1.set_xticklabels(tick_lbl, rotation=35, ha="right")
    plt.setp(ax0.get_xticklabels(), visible=False)

    saved = _savefig(fig, dest)
    plt.close(fig)
    return saved


def _fmt_percent(value: Any) -> str:
    v = _nf(value)
    if v is None:
        return "-"
    return f"{v * 100:.2f}%"


def render_portfolio_figure(out: dict[str, Any], dest: Path) -> list[Path]:
    """组合回测权益与回撤图。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import gridspec
    import numpy as np

    _configure_chinese_fonts()

    equity = out.get("equity") or []
    metrics = out.get("metrics") or {}
    if not equity:
        raise ValueError("权益曲线为空，无法绘图")

    dates = [_norm_date(str(row.get("date", ""))) for row in equity]
    eq_vals = [float(row.get("equity", 0.0)) for row in equity]
    ic = _nf(metrics.get("initial_cash")) or (eq_vals[0] if eq_vals else 1.0)
    if ic <= 0:
        ic = 1.0
    norm = [v / ic for v in eq_vals]
    peak = np.maximum.accumulate(np.asarray(norm, dtype=float))
    dd = (peak - np.asarray(norm, dtype=float)) / np.where(peak > 0, peak, np.nan)

    fig = plt.figure(figsize=(12, 7), facecolor="#0f1115")
    gs = gridspec.GridSpec(2, 1, height_ratios=[1.4, 0.8], hspace=0.28)
    ax0 = fig.add_subplot(gs[0])
    ax1 = fig.add_subplot(gs[1], sharex=ax0)
    for ax in (ax0, ax1):
        ax.set_facecolor("#0f1115")
        ax.tick_params(colors="#c5c8ce")
        for spine in ax.spines.values():
            spine.set_color("#2a2f3a")

    xs = range(len(dates))
    ax0.plot(xs, norm, color="#4fc3f7", linewidth=1.4, label="组合净值")
    ax0.axhline(1.0, color="#9aa0a6", linestyle="--", linewidth=0.7, alpha=0.7)
    ax0.set_ylabel("净值", color="#e8eaed")
    title = f"组合回测 {out.get('strategy_id', '')} | asof={out.get('asof') or '-'}"
    ax0.set_title(title, color="#e8eaed", fontsize=11)
    ax0.legend(facecolor="#1a1d24", edgecolor="#2a2f3a", labelcolor="#e8eaed")
    ax0.text(
        0.01,
        0.03,
        (
            f"总收益 {_fmt_percent(metrics.get('total_return'))} | "
            f"年化 {_fmt_percent(metrics.get('annualized_return'))} | "
            f"回撤 {_fmt_percent(metrics.get('max_drawdown'))} | "
            f"Sharpe {_nf(metrics.get('sharpe')) or 0:.2f}"
        ),
        transform=ax0.transAxes,
        color="#e8eaed",
        fontsize=8,
        bbox={"facecolor": "#0f1115", "edgecolor": "#2a2f3a", "alpha": 0.85, "pad": 5},
    )

    ax1.fill_between(list(xs), dd.tolist(), 0.0, color="#ef5350", alpha=0.35)
    ax1.plot(xs, dd.tolist(), color="#ef5350", linewidth=1.0)
    ax1.set_ylabel("回撤", color="#e8eaed")
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y * 100:.0f}%"))

    tick_step = max(1, len(dates) // 12)
    tick_pos = list(range(0, len(dates), tick_step))
    ax1.set_xticks(tick_pos)
    ax1.set_xticklabels([dates[i] for i in tick_pos], rotation=35, ha="right")
    plt.setp(ax0.get_xticklabels(), visible=False)

    saved = _savefig(fig, dest)
    plt.close(fig)
    return saved
