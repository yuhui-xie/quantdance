"""比对两台机器各自的回测产物，定位"结果对不上"到底出在哪一层。

用法（backend/ 目录下）：

    .venv/Scripts/python.exe out/verify_pack/tools/diff_check.py <A机目录> <B机目录>

两个目录可以是解压后的包根目录，也可以是各自的 ``out/_pack``（自动识别，见
``_load_run``）。检查顺序刻意由外到内：

1. **环境** git 版本 / 依赖版本 / 缓存口径 —— 不同就别看后面的数字了；
2. **数据** 逐标的 K 线根数、首末日期、收盘价哈希 —— 数据源/更新时点不同是头号原因；
3. **决策** 逐决策日候选与分数 —— 数据一致却在这里分歧，才是代码/参数问题；
4. **指标** 总收益/回撤/Sharpe 等。

任何一层出现差异都会单独列出来，不做"整体不一致"这种没用的结论。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 优先比钉死 end_date 的那组（跨机唯一能逐字节对齐的形式），其余作为参考
LABELS_PINNED = ["main_exit_off_pinned", "main_exit_on_pinned",
                 "dynamic_exit_off_pinned", "dynamic_exit_on_pinned"]
LABELS = ["main_exit_off", "main_exit_on", "dynamic_exit_off", "dynamic_exit_on",
          "dynamic_rsrsoff_exit_off", "dynamic_rsrsoff_exit_on"]


def _load_json(path: Path) -> dict | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  ! 读取失败 {path}: {exc}")
        return None


def _layout(root: Path) -> str:
    if (root / "decisions").is_dir():
        return "pack"
    if list(root.glob("result_*.json")):
        return "raw"
    return "unknown"


def _run_files(root: Path, kind: str, label: str, layout: str) -> Path:
    if layout == "pack":
        sub = {"result": "results", "pool": "pools", "decisions": "decisions", "log": "logs"}[kind]
        ext = "jsonl" if kind == "decisions" else ("log" if kind == "log" else "json")
        return root / sub / f"{label}.{ext}"
    if kind == "decisions":
        return root / f"_decisions_{label}.jsonl"
    ext = "txt" if kind == "log" else "json"
    return root / f"{kind}_{label}.{ext}"


def _decisions_of(root: Path, label: str, layout: str) -> dict[str, dict] | None:
    """返回 {decision_date: 该日记录}，直接读 result JSON 的 rebalances 摊平。"""
    if layout == "raw":
        result = _load_json(root / f"result_{label}.json")
    else:
        result = _load_json(root / "results" / f"{label}.json")
    if result is None:
        return None
    out: dict[str, dict] = {}
    for i, reb in enumerate(result.get("rebalances") or [], start=1):
        key = f"{reb.get('decision_date')}|{reb.get('exec_date')}|#{i}"
        out[key] = {
            "targets": reb.get("targets"),
            "n_candidates": len(reb.get("selection") or []),
            "candidates": reb.get("selection") or [],
        }
    return out


def _metrics_of(root: Path, label: str, layout: str) -> dict | None:
    path = (_run_files(root, "result", label, layout))
    result = _load_json(path)
    return (result or {}).get("metrics")


def _fingerprint_of(root: Path) -> dict | None:
    return _load_json(root / "data_fingerprint.json")


def compare_env(a: Path, b: Path) -> None:
    print("\n[1] 环境")
    ma, mb = _load_json(a / "manifest.json"), _load_json(b / "manifest.json")
    ea = (ma or {}).get("env") or (_fingerprint_of(a) or {}).get("env")
    eb = (mb or {}).get("env") or (_fingerprint_of(b) or {}).get("env")
    if not ea or not eb:
        print("  - 缺 manifest.json / data_fingerprint.json 的 env，跳过")
        return
    keys = ["git_rev", "git_branch", "python", "platform", "pandas", "numpy",
            "kline_cache_version"]
    same = True
    for key in keys:
        va, vb = ea.get(key), eb.get(key)
        if va != vb:
            same = False
            print(f"  ✗ {key}:\n      A = {va}\n      B = {vb}")
    for key in ("git_status_porcelain", "cache_env"):
        va, vb = ea.get(key), eb.get(key)
        if va != vb:
            print(f"  ~ {key} 不同（未必影响结果）:\n      A = {va}\n      B = {vb}")
    if same:
        print("  ✓ 代码版本与依赖一致")


def compare_data(a: Path, b: Path) -> None:
    print("\n[2] 行情数据指纹")
    fa, fb = _fingerprint_of(a), _fingerprint_of(b)
    if not fa or not fb:
        print("  - 缺 data_fingerprint.json，跳过（两台机器都跑 tools/data_fingerprint.py）")
        return
    for universe in sorted(set(fa.get("universes", {})) | set(fb.get("universes", {}))):
        ua = fa.get("universes", {}).get(universe) or {}
        ub = fb.get("universes", {}).get(universe) or {}
        ha, hb = ua.get("symbols_list_sha256_16"), ub.get("symbols_list_sha256_16")
        if ha != hb:
            print(f"  ✗ {universe}: **成员表本身不同**"
                  f"（A {ua.get('count')} 只 / B {ub.get('count')} 只）"
                  f"\n      A note = {str(ua.get('note'))[:100]}"
                  f"\n      B note = {str(ub.get('note'))[:100]}")
        sa = ua.get("symbols") or {}
        sb = ub.get("symbols") or {}
        only_a, only_b = sorted(set(sa) - set(sb)), sorted(set(sb) - set(sa))
        diffs: list[tuple[str, str]] = []
        for symbol in sorted(set(sa) & set(sb)):
            ra, rb = sa[symbol], sb[symbol]
            if ra == rb:
                continue
            fields = [
                f"{k}: {ra.get(k)} → {rb.get(k)}"
                for k in ("bars", "first_date", "last_date", "last_close",
                          "close_hash", "amount_hash", "volume_hash", "status")
                if ra.get(k) != rb.get(k)
            ]
            diffs.append((symbol, "; ".join(fields)))
        total = len(set(sa) | set(sb))
        if not (only_a or only_b or diffs) and ha == hb:
            print(f"  ✓ {universe}: {len(sa)} 只标的 K 线完全一致")
            continue
        print(f"  ✗ {universe}: {len(diffs)}/{total} 只不一致"
              + (f"，A 独有 {len(only_a)} 只、B 独有 {len(only_b)} 只" if only_a or only_b else ""))
        if only_a:
            print(f"      A 独有: {only_a[:20]}{' ...' if len(only_a) > 20 else ''}")
        if only_b:
            print(f"      B 独有: {only_b[:20]}{' ...' if len(only_b) > 20 else ''}")
        for symbol, desc in diffs[:30]:
            print(f"      {symbol}: {desc}")
        if len(diffs) > 30:
            print(f"      ... 另有 {len(diffs) - 30} 只")


# selection 里允许有末位浮点差异的字段（打印但不判为分歧）
_TOLERANT = {"close", "score", "slope_raw", "slope_short", "slope_score", "amount_score",
             "amount_log_ratio", "rsrs", "rsrs_beta", "rsrs_r2", "rsrs_z",
             "exit_slope", "exit_ma", "exit_ma_gap"}


def _close(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
    return a == b


def compare_decisions(a: Path, b: Path, la: str, lb: str, max_days: int = 5) -> None:
    print("\n[3] 逐决策日筛选过程")
    da = _decisions_of(a, la, _layout(a))
    db = _decisions_of(b, lb, _layout(b))
    if da is None or db is None:
        print(f"  - 缺 {la}/{lb} 的结果 JSON，跳过")
        return
    keys_a, keys_b = list(da), list(db)
    if keys_a == keys_b:
        print(f"  ✓ 决策日序列一致（{len(keys_a)} 个）")
    else:
        print(f"  ✗ 决策日序列不同：A {len(keys_a)} 个 / B {len(keys_b)} 个")
        for i, (x, y) in enumerate(zip(keys_a, keys_b)):
            if x != y:
                print(f"      首个不同出现在第 {i + 1} 个：\n        A = {x}\n        B = {y}")
                break
        sa, sb = set(keys_a), set(keys_b)
        if sa - sb:
            print(f"      A 独有 {len(sa - sb)} 个，前 5: {sorted(sa - sb)[:5]}")
        if sb - sa:
            print(f"      B 独有 {len(sb - sa)} 个，前 5: {sorted(sb - sa)[:5]}")

    shown = 0
    for key in keys_a:
        if key not in db:
            continue
        ra, rb = da[key], db[key]
        if ra["targets"] != rb["targets"] or ra["n_candidates"] != rb["n_candidates"]:
            print(f"  ✗ {key}: 目标不同\n      A = {ra['targets']} ({ra['n_candidates']} 候选)"
                  f"\n      B = {rb['targets']} ({rb['n_candidates']} 候选)")
            shown += 1
            if shown >= max_days:
                print("  ... 仅列前若干条")
                break
            continue
        ca = {row.get("symbol"): row for row in ra["candidates"]}
        cb = {row.get("symbol"): row for row in rb["candidates"]}
        if set(ca) != set(cb):
            print(f"  ✗ {key}: 候选集不同\n      A 独有 {sorted(set(ca) - set(cb))[:10]}"
                  f"\n      B 独有 {sorted(set(cb) - set(ca))[:10]}")
            shown += 1
            if shown >= max_days:
                break
            continue
        hard, soft = [], []
        for symbol in ca:
            for field in ca[symbol]:
                if field in _TOLERANT:
                    if not _close(ca[symbol][field], cb[symbol].get(field)):
                        soft.append(f"{symbol}.{field}: {ca[symbol][field]} → {cb[symbol].get(field)}")
                elif ca[symbol][field] != cb[symbol].get(field):
                    hard.append(f"{symbol}.{field}: {ca[symbol][field]} → {cb[symbol].get(field)}")
        if hard:
            print(f"  ✗ {key}: 候选字段不同\n      " + "\n      ".join(hard[:6]))
            shown += 1
            if shown >= max_days:
                print("  ... 仅列前若干条")
                break
        elif soft:
            print(f"  ~ {key}: 仅有末位浮点差异\n      " + "\n      ".join(soft[:4]))
    if shown == 0:
        print("  ✓ 目标、候选集与关键字段逐日一致")


def compare_windows(a: Path, b: Path) -> None:
    """最先比：两次运行**真实**用到的样本窗口。最后一天不同就不用往下比了。"""
    print("\n[0] 实际回测窗口")
    ma, mb = _load_json(a / "manifest.json"), _load_json(b / "manifest.json")
    if not ma or not mb:
        side = "A" if not ma else "B"
        print(f"  - {side} 侧缺 manifest.json，跳过这一步"
              "（退回直接比 results/*.json 里 equity 的首末日期）")
        return
    for label in LABELS_PINNED + LABELS:
        ra = ((ma.get("runs") or {}).get(label) or {}).get("effective_window")
        rb = ((mb.get("runs") or {}).get(label) or {}).get("effective_window")
        if not ra or not rb:
            continue
        if ra.get("last_equity_date") != rb.get("last_equity_date"):
            print(f"  ✗ {label}: **样本末尾不同** —— A 到 {ra.get('last_equity_date')}"
                  f"（{ra.get('equity_points')} 点），B 到 {rb.get('last_equity_date')}"
                  f"（{rb.get('equity_points')} 点）")
            print("      ⇒ 这就是收益数字对不上的原因：两边行情最新一根 K 线不同。"
                  "把 end_date 钉死到过去交易日再比。")
        else:
            print(f"  ✓ {label}: {ra.get('first_equity_date')} ~ {ra.get('last_equity_date')}"
                  f"（{ra.get('equity_points')} 点）")


def compare_metrics(a: Path, b: Path) -> None:
    print("\n[4] 绩效指标")
    for label in LABELS_PINNED + LABELS:
        ma = _metrics_of(a, label, _layout(a))
        mb = _metrics_of(b, label, _layout(b))
        if ma is None or mb is None:
            print(f"  - {label}: 缺结果，跳过")
            continue
        bad = [(k, ma.get(k), mb.get(k)) for k in ma if not _close(ma.get(k), mb.get(k))]
        if not bad:
            print(f"  ✓ {label}: 全部指标一致 "
                  f"(total_return={ma.get('total_return')}, sharpe={ma.get('sharpe')})")
        else:
            print(f"  ✗ {label}: {len(bad)} 项不同")
            for key, va, vb in bad:
                print(f"      {key}: A={va}  B={vb}")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    a, b = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    for root in (a, b):
        if not root.is_dir():
            raise SystemExit(f"目录不存在：{root}")
    print(f"A = {a}   [{_layout(a)}]")
    print(f"B = {b}   [{_layout(b)}]")
    compare_windows(a, b)
    compare_env(a, b)
    compare_data(a, b)
    for label in LABELS_PINNED + LABELS:
        if (a / f"result_{label}.json").exists() or (a / "results" / f"{label}.json").exists():
            compare_decisions(a, b, label, label)
    compare_metrics(a, b)


if __name__ == "__main__":
    main()
