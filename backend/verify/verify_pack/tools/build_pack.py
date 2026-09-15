"""把本地回测的中间筛选过程打包成可跨机比对的一个 zip。

用法（backend/ 目录下，先跑完 out/_pack/run_queue.sh）：

    .venv/Scripts/python.exe out/verify_pack/tools/build_pack.py

产出 ``out/verify_pack/etf_rotation_verify_pack.zip``，结构见包内 README.md。

打包内容刻意做成**逐决策日可对齐**的形式：``decisions/<label>.jsonl`` 每个决策日一行，
两台机器各跑一次后，用 ``tools/diff_check.py`` 逐行 diff，第一个对不上的决策日会直接暴露
出来（连同当天的候选、分数、选中标的），而不用去啃几十万行的结果 JSON。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]          # .../backend
# 以脚本路径运行时 `app` 不在 sys.path 上（只有 `python -m app.script` 才在），显式补上
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

OUT = BACKEND / "out"
PACK_SRC = OUT / "_pack"
PACK_ROOT = OUT / "verify_pack"
STAGE = PACK_ROOT / "_stage"
ZIP_PATH = PACK_ROOT / "etf_rotation_verify_pack.zip"

# 前三个用配方原样的 end_date（未来日期，"跑到最新一根 K 线"，跨机不可比）；
# 后三个把 end_date 钉死在过去交易日，**这才是两台机器能逐字节对齐的形式**。
LABELS = [
    "main_exit_off", "main_exit_on",
    "dynamic_exit_off", "dynamic_exit_on",
    "dynamic_rsrsoff_exit_off", "dynamic_rsrsoff_exit_on",   # §2.3 RSRS 门控消融
    "main_exit_off_pinned", "main_exit_on_pinned",
    "dynamic_exit_off_pinned", "dynamic_exit_on_pinned",
]

# 结果 JSON 里 selection 中需要对齐的数值字段（缺的键按 None 记，不影响比对）
SELECTION_FIELDS = [
    "symbol", "name", "selected", "rank", "score",
    "slope_raw", "slope_short", "slope_score", "amount_score", "amount_log_ratio",
    "rsrs", "rsrs_beta", "rsrs_r2", "rsrs_z",
    "close", "exit_slope", "exit_ma", "exit_ma_gap", "exit_triggered",
]

METRIC_KEYS = [
    "final_equity", "total_return", "annualized_return", "max_drawdown", "sharpe",
    "num_trades", "closed_trades", "win_rate", "profit_factor", "return_drawdown_ratio",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _env() -> dict[str, object]:
    def git(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", *args], cwd=BACKEND, stderr=subprocess.DEVNULL
            ).decode(errors="replace").strip()
        except Exception:
            return ""

    env: dict[str, object] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "git_rev": git("rev-parse", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_status_porcelain": git("status", "--porcelain"),
    }
    for mod in ("pandas", "numpy", "pydantic"):
        try:
            env[mod] = getattr(__import__(mod), "__version__", "unknown")
        except Exception as exc:
            env[mod] = f"unavailable ({exc.__class__.__name__})"
    try:
        from app.data_sources import a_stock_data as adm

        # _KLINE_CACHE_VERSION 是模块级常量，不在 SDK 类上
        env["kline_cache_version"] = str(getattr(adm, "_KLINE_CACHE_VERSION", "unknown"))
    except Exception as exc:
        env["kline_cache_version"] = f"unavailable ({exc.__class__.__name__}: {exc})"
    env["cache_env"] = {
        k: os.environ.get(k, "(unset)")
        for k in ("QUANTDANCE_A_STOCK_DATA_CACHE", "QUANTDANCE_A_STOCK_DATA_CACHE_DIR")
    }
    return env


def _decisions(label: str, result: dict) -> list[dict]:
    """把 rebalances[] 摊平成每决策日一行，供跨机逐行 diff。"""
    rows: list[dict] = []
    for i, reb in enumerate(result.get("rebalances") or [], start=1):
        selection = reb.get("selection") or []
        rows.append({
            "seq": i,
            "exec_date": reb.get("date"),
            "decision_date": reb.get("decision_date"),
            "targets": reb.get("targets"),
            "cash": reb.get("cash"),
            "n_candidates": len(selection),
            "candidates": [
                {k: row.get(k) for k in SELECTION_FIELDS if k in row}
                for row in selection
            ],
        })
    return rows


def _summary(result: dict, request: dict) -> dict:
    metrics = result.get("metrics") or {}
    selectors = result.get("rebalances") or []
    equity = result.get("equity") or []
    return {
        "strategy_id": result.get("strategy_id"),
        "rebalances": len(selectors),
        "trades": len(result.get("trades") or []),
        # ★ 实际回测窗口：配方里 end_date 写的是未来日期（如 2026-09-31），
        #   实际到哪一天完全取决于本机行情的最新一根 K 线。两台机器只要差一根，
        #   头条收益数字就会不同——所以必须把这个真实窗口记下来对齐。
        "effective_window": {
            "requested_start": request.get("start_date"),
            "requested_end": request.get("end_date"),
            "first_equity_date": (equity[0] or {}).get("date") if equity else None,
            "last_equity_date": (equity[-1] or {}).get("date") if equity else None,
            "equity_points": len(equity),
        },
        "execution_timing": request.get("execution_timing"),
        "rebalance_mode": request.get("rebalance_mode"),
        "metrics": {k: metrics.get(k) for k in METRIC_KEYS},
    }


README = """# etf_rotation 回测中间筛选过程 —— 跨机比对包

本包用于定位「两台机器跑同一个配方结果对不上」。里面是**本机**跑出的完整中间产物，
另一台机器按同样方式跑一遍后，用 `tools/diff_check.py` 逐项比对即可定位分歧点。

## ⚠️ 先说最可能的原因：配方里的 `end_date` 是未来日期

两个配方的 `end_date` 都写着 `2026-09-31`（未来），所以**实际回测到哪一天，取决于本机行情
里最新的一根 K 线**。同一台机器、同一份代码、相隔 1.5 小时跑两次主配方，就复现了"对不上"：

```
22:57 跑：净值 18.2696x，equity 1625 点，最后一天 2026-09-14
00:30 跑：净值 18.2265x，equity 1626 点，最后一天 2026-09-15   ← 期间行情多了一根
```

逐点对比：**1625 个重叠交易日的 equity 完全一致（0 个点不同）**，成交 143 笔完全一致，
两个配方的 targets 逐日一致。唯一差别就是样本末尾多了一天（那天 −0.24%）。

**结论：只要两台机器的最新 K 线不是同一根，头条收益数字就必然不同，这跟策略/代码无关。**

要做可复现比对，请把 `end_date` 钉在一个**双方都已收盘、且都不会再变化的过去交易日**
（例如 `2026-09-11`），两台机器都用这个日期跑，才能逐字节对齐。
`out/_pack/req_*_pinned.json` 就是这种钉死日期的请求，优先用它比对。

`manifest.json` 的 `runs.<label>.effective_window` 记录了每次运行**真实**用到的
起止日期与点数，先比这个字段，不一致就不用比数字了。

## 内容

| 路径 | 说明 |
| --- | --- |
| `requests/committed_*.json` | 仓库里提交的两个配方**原样**副本（另一台机器应直接用这两个文件跑） |
| `requests/run_*.json` | 本机**实际执行**的请求（与 committed 的唯一差别：`output_options.pool_dump` 指向包内路径，避免两个配方同名互相覆盖；另 `dynamic_exit_off` 是消融对照，关闭了 `trend_exit_check`） |
| `results/<label>.json` | 完整回测结果（含 `rebalances[].selection` 逐决策日候选与分数） |
| `pools/<label>.json` | 每个决策日的池成员（`source` 标明是 `eligibility` 还是 `trend_exit`） |
| `decisions/<label>.jsonl` | **每决策日一行**的摊平记录——最方便 diff 的形态 |
| `logs/<label>.log` | stderr：逐决策日进度行（`[ N/M] 日期 选中 X | 通过筛选 K 只`） |
| `data_fingerprint.json` | 本机行情数据指纹：每个标的的 K 线根数/首末日期/收盘序列哈希 |
| `spotcheck/pick_*.json` | 单日截面选股结果（很小、人能直接读）——最省事的对账方式，见下 |
| `tools/` | `data_fingerprint.py`（另一台机器也跑一次）、`diff_check.py`（比对两份包） |

`<label>` 命名规则：

- `main_*` = 主配方（`etf_core_sub` 精选池）；`dynamic_*` = 动态配方（全市场池 + `pool_discovery`）；
- `*_exit_on` / `*_exit_off` = 双周趋势退出 `trend_exit_check` 的开 / 关；
- `dynamic_rsrsoff_*` = 在动态配方上**额外关掉 `rsrs_gate`**（§2.3 的门控消融）；其余
  `dynamic_*` 都保持 `rsrs_gate=true`；
- `*_pinned` = 把 `end_date` 钉死在 **2026-09-11** 的那一组 —— **跨机比对请用这四个**，
  其余不带后缀的跑的是配方原样的未来 `end_date`（窗口随本机最新 K 线浮动，天然不可比）。

## 另一台机器怎么跑

```bash
cd backend
git checkout <本包 manifest.json 里的 env.git_rev>   # 代码必须同版本，否则参数不认识

# 0) 把包解压到 backend/out/_pack_check/，再把 requests/ 拷成 out/_pack/（请求里的落盘路径
#    就指向 out/_pack/，与对方保持一致）
mkdir -p out/_pack && cp out/_pack_check/requests/run_*.json out/_pack/

# 1) 用钉死 end_date 的三个请求跑（跨机可比的那组）
for f in main_exit_on_pinned dynamic_exit_off_pinned dynamic_exit_on_pinned; do
  .venv/Scripts/python.exe -m app.script backtest --request out/_pack/run_$f.json --json
done
# 2) 本机行情指纹
mkdir -p out/verify_pack/tools
cp out/_pack_check/tools/data_fingerprint.py out/verify_pack/tools/
.venv/Scripts/python.exe out/verify_pack/tools/data_fingerprint.py > out/_pack/data_fingerprint.json
# 3) 先自查：同一份请求跑两次，结果必须完全一致（否则本机就不稳定，先查这个）
.venv/Scripts/python.exe -m app.script backtest \
    --request out/_pack/run_main_exit_on_pinned.json --json --output out/_pack/rerun.json
cmp out/_pack/result_main_exit_on_pinned.json out/_pack/rerun.json && echo "本机可复现 ✓"
# 4) 把两边的 out/_pack 喂给 diff_check
.venv/Scripts/python.exe out/_pack_check/tools/diff_check.py <本机 out/_pack> <对方 out/_pack>
```

第 3 步很关键：**先确认自己这台机器可复现，再谈跨机**。本机钉死日期后跑两次必须逐字节一致
（`cmp` 通不过说明这台机器本身就不稳定，先查这个再谈别的）。

## 怎么判断是谁的问题

按由外到内的顺序，`diff_check.py` 也是这个顺序：

1. **窗口**：`manifest.json` 的 `effective_window`。**最后一天不同 ⇒ 直接结案**，就是上一节
   说的最新 K 线差异，后面的数字不用比。
2. **环境**：`env.git_rev` 不同 ⇒ 代码不同，对照数字没有意义；`kline_cache_version` /
   `cache_env` 不同 ⇒ 缓存口径不同；`pandas`/`numpy` 版本不同 ⇒ 浮点末位可能有差异。
3. **数据**：`data_fingerprint.json` 里逐标的比 `bars` / `first_date` / `last_date` /
   `close_hash` / `amount_hash`。数据源节点或更新时点不同会让**前复权价整体改变**，
   足以让斜率和轮动顺序变样。注意 `etf_market` 的成员表来自 akshare **按日缓存**，
   两台机器抓取日期不同，成员表本身就不同（比 `symbols_list_sha256_16`）。
4. **决策**：`decisions/*.jsonl` 逐行 diff，报出**第一个分歧的决策日**连同当天候选与分数。
   数据完全一致却在这里分歧 ⇒ 代码/参数问题。

## 最省事的对账：单日截面

不想跑完整回测时，先比 `spotcheck/` 里那个单日选股结果——它只有几十行，全池候选与得分
一目了然。另一台机器用同一条命令复现（`asof` 是过去日期，结果完全确定）：

```bash
.venv/Scripts/python.exe -m app.script pick --strategy etf_rotation --asof 2024-05-06 \
    --symbols 510300 510500 159915 --params '{}'
```

两边 `out/pick_etf_rotation_2024-05-06.json` 完全一致 ⇒ 代码与这三只标的的行情都一致。
不一致 ⇒ 差异必然出在这个小样本里，逐字段看即可，比啃结果 JSON 快得多。

## 已知的浮点噪声（当前不影响结果，但要知道）

同一台机器、同一个钉死日期的请求跑两次，**结果层完全一致**：`metrics`、`trades`、`equity`
以及每个决策日的 `targets` 全部逐字段相同。唯一有差异的是候选分数本身——本包实测
**530 个字段**存在相对差，**最大 3e-13**（例：`slope_score` 0.00043961792369705 →
0.00043961792369719）。

`manifest.json` 的 `repro_check` 记录了这次自查的结论。含义：

- **不必担心**：这种量级的噪声不会改变成交，也不影响任何已记录的回测数字。
- **要知道**：若两个候选的分数差小于 ~1e-13，排序有可能翻转（本次没有发生）。
  `diff_check.py` 因此把 `score` / `slope_*` / `amount_*` / `rsrs_*` / `close` 这类字段
  单独归类为「末位浮点差异」打印，而不判为分歧——真正需要警惕的是 `targets`、
  `symbol`、`selected`、`rank` 这几个**离散**字段（一票之差会直接改变持仓）。
- `amount_hash` / `volume_hash` 用来确认这点噪声是否来自行情数据本身。

## 已知口径差异（不构成 bug）

- `main_exit_on` 与 `dynamic_exit_on` 的检查日（`source=trend_exit`）只对**当前持仓**做退出判定，
  不补位；因此 `n_candidates` 等于当时持仓数，而不是月度决策日那样的全池候选数。
- 检查日即使什么都没成交也会在 `rebalances` 里留一行（区间收益仍由 equity 正确计算），
  所以 `main_exit_on` 的 `rebalances` 数（248）远多于不开启退出的版本。
"""


def _repro_check() -> dict | None:
    """同机自查：同一个钉死日期的请求跑两次，结果层必须完全一致。

    两次运行的完整 JSON 都留在 stdout 里（`--json` 会把结果整份打印），
    这里直接比，把结论写进 manifest —— 万一跨机对不上，先看这一段就知道
    问题是不是出在"本机自己就不稳定"。
    """
    first = PACK_SRC / "stdout_main_exit_on_pinned.txt"
    second = PACK_SRC / "stdout_main_exit_on_pinned_rerun.txt"
    if not (first.exists() and second.exists()):
        return None

    def load(path: Path) -> dict:
        text = path.read_text(encoding="utf-8")
        obj, _ = json.JSONDecoder().raw_decode(text)
        return obj

    a, b = load(first), load(second)
    ra, rb = a.get("rebalances") or [], b.get("rebalances") or []
    field_diffs = 0
    max_rel = 0.0
    for x, y in zip(ra, rb):
        ca = {c.get("symbol"): c for c in x.get("selection") or []}
        cb = {c.get("symbol"): c for c in y.get("selection") or []}
        for symbol in set(ca) & set(cb):
            for key, va in (ca[symbol] or {}).items():
                vb = (cb[symbol] or {}).get(key)
                if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and va != vb:
                    field_diffs += 1
                    max_rel = max(max_rel, abs(va - vb) / max(1e-12, abs(va)))
    return {
        "request": "req_main_exit_on_pinned.json",
        "metrics_equal": a.get("metrics") == b.get("metrics"),
        "trades_equal": a.get("trades") == b.get("trades"),
        "equity_equal": a.get("equity") == b.get("equity"),
        "targets_equal": [r.get("targets") for r in ra] == [r.get("targets") for r in rb],
        "decision_days_equal": len(ra) == len(rb),
        "score_fields_differing": field_diffs,
        "max_relative_score_diff": max_rel,
        "verdict": ("结果层完全可复现；仅候选分数有末位浮点噪声，未影响任何成交/持仓"
                    if a.get("metrics") == b.get("metrics")
                    and [r.get("targets") for r in ra] == [r.get("targets") for r in rb]
                    else "★ 同机重复运行结果不一致，先查本机（并发/缓存），别急着比跨机"),
    }


def main() -> None:
    missing = [
        f"{kind}_{label}.json"
        for label in LABELS
        for kind in ("result", "pool")
        if not (PACK_SRC / f"{kind}_{label}.json").exists()
    ]
    if missing:
        raise SystemExit(f"缺少产物：{missing}，先跑完 out/_pack/run_queue.sh")

    if STAGE.exists():
        shutil.rmtree(STAGE)
    for sub in ("requests", "results", "pools", "decisions", "logs", "tools"):
        (STAGE / sub).mkdir(parents=True, exist_ok=True)

    manifest: dict[str, object] = {"env": _env(), "runs": {}, "files": {}}

    # 仓库里提交的配方原样副本（另一台机器直接用这两个）
    committed = {
        "committed_etf_rotation.json":
            BACKEND / "examples/cross_section/backtest_shared_etf_rotation.json",
        "committed_etf_rotation_dynamic.json":
            BACKEND / "examples/cross_section/backtest_shared_etf_rotation_dynamic.json",
    }
    for name, src in committed.items():
        shutil.copy2(src, STAGE / "requests" / name)
        manifest["files"][f"requests/{name}"] = {"sha256_16": sha256(src)}

    for label in LABELS:
        req = PACK_SRC / f"req_{label}.json"
        res = PACK_SRC / f"result_{label}.json"
        pool = PACK_SRC / f"pool_{label}.json"
        log = PACK_SRC / f"log_{label}.txt"

        shutil.copy2(req, STAGE / "requests" / f"run_{label}.json")
        shutil.copy2(res, STAGE / "results" / f"{label}.json")
        shutil.copy2(pool, STAGE / "pools" / f"{label}.json")
        if log.exists():
            shutil.copy2(log, STAGE / "logs" / f"{label}.log")

        request = json.loads(req.read_text(encoding="utf-8"))
        result = json.loads(res.read_text(encoding="utf-8"))
        rows = _decisions(label, result)
        with (STAGE / "decisions" / f"{label}.jsonl").open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

        manifest["runs"][label] = {
            **_summary(result, request),
            "request": request,
            "artifacts": {
                "result_sha256_16": sha256(res),
                "pool_sha256_16": sha256(pool),
                "decisions_sha256_16": sha256(STAGE / "decisions" / f"{label}.jsonl"),
            },
        }
        manifest["files"].update({
            f"requests/run_{label}.json": {"sha256_16": sha256(req)},
            f"results/{label}.json": {"sha256_16": sha256(res)},
            f"pools/{label}.json": {"sha256_16": sha256(pool)},
        })

    # 单日截面抽查：pick 的产物很小、人能直接读，两台机器对一眼就能判断选股是否一致。
    spot_dir = STAGE / "spotcheck"
    spot_dir.mkdir(exist_ok=True)
    for src in sorted(OUT.glob("pick_*.json")):
        shutil.copy2(src, spot_dir / src.name)
        manifest["files"][f"spotcheck/{src.name}"] = {"sha256_16": sha256(src)}

    fp = PACK_SRC / "data_fingerprint.json"
    if fp.exists() and fp.stat().st_size > 0:
        shutil.copy2(fp, STAGE / "data_fingerprint.json")
        manifest["files"]["data_fingerprint.json"] = {"sha256_16": sha256(fp)}

    for tool in ("data_fingerprint.py", "diff_check.py"):
        src = Path(__file__).resolve().parent / tool
        if src.exists():
            shutil.copy2(src, STAGE / "tools" / tool)

    repro = _repro_check()
    if repro:
        manifest["repro_check"] = repro

    # 本包实测结果一览（自动生成，避免手抄错）
    lines = ["\n## 本包实测结果（自动生成）\n",
             "| 运行 | 实际窗口 | 净值倍数 | 最大回撤 | Sharpe | 成交 | 周期行 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for label in LABELS:
        run = manifest["runs"][label]
        win = run["effective_window"]
        met = run["metrics"]
        initial = (run.get("request") or {}).get("initial_cash") or met.get("initial_cash") or 1.0
        net = (met.get("final_equity") or 0.0) / float(initial)
        sharpe = met.get("sharpe")
        lines.append(
            f"| `{label}` | {win['first_equity_date']} ~ {win['last_equity_date']}"
            f"（{win['equity_points']} 点） | {net:.2f}× | "
            f"{(met.get('max_drawdown') or 0) * 100:.2f}% | "
            f"{sharpe:.4f} | {(met.get('num_trades') or 0):.0f} | {run['rebalances']} |"
            if sharpe is not None else
            f"| `{label}` | {win['first_equity_date']} ~ {win['last_equity_date']}"
            f"（{win['equity_points']} 点） | {net:.2f}× | - | - | - | {run['rebalances']} |")
    if repro:
        lines += [
            "\n**同机复现自查**（`repro_check`）：`metrics` / `trades` / `equity` / 每个决策日的 "
            f"`targets` 全部一致 = `{repro['metrics_equal']}`；候选分数有 "
            f"{repro['score_fields_differing']} 个字段存在末位差异，最大相对差 "
            f"{repro['max_relative_score_diff']:.1e}。判定：{repro['verdict']}\n",
        ]

    (STAGE / "README.md").write_text(README + "\n".join(lines), encoding="utf-8")
    (STAGE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(STAGE.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(STAGE))

    print(f"打包完成：{ZIP_PATH}  ({ZIP_PATH.stat().st_size / 1e6:.1f} MB)")
    for label in LABELS:
        print(f"  {label}: {json.dumps(manifest['runs'][label]['metrics'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()
