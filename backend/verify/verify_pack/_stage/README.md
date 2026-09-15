# etf_rotation 回测中间筛选过程 —— 跨机比对包

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
.venv/Scripts/python.exe -m app.script backtest     --request out/_pack/run_main_exit_on_pinned.json --json --output out/_pack/rerun.json
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
.venv/Scripts/python.exe -m app.script pick --strategy etf_rotation --asof 2024-05-06     --symbols 510300 510500 159915 --params '{}'
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

## 本包实测结果（自动生成）

| 运行 | 实际窗口 | 净值倍数 | 最大回撤 | Sharpe | 成交 | 周期行 |
| --- | --- | --- | --- | --- | --- | --- |
| `main_exit_off` | 2020-01-02 ~ 2026-09-15（1626 点） | 20.45× | 24.82% | 1.6915 | 143 | 81 |
| `main_exit_on` | 2020-01-02 ~ 2026-09-15（1626 点） | 18.23× | 24.82% | 1.6504 | 143 | 248 |
| `dynamic_exit_off` | 2020-01-02 ~ 2026-09-15（1626 点） | 13.82× | 19.11% | 1.6926 | 111 | 81 |
| `dynamic_exit_on` | 2020-01-02 ~ 2026-09-15（1626 点） | 14.33× | 17.66% | 1.7262 | 111 | 248 |
| `dynamic_rsrsoff_exit_off` | 2020-01-02 ~ 2026-09-15（1626 点） | 12.73× | 34.76% | 1.4645 | 147 | 81 |
| `dynamic_rsrsoff_exit_on` | 2020-01-02 ~ 2026-09-15（1626 点） | 11.54× | 31.19% | 1.4275 | 147 | 248 |
| `main_exit_off_pinned` | 2020-01-02 ~ 2026-09-11（1624 点） | 20.80× | 24.82% | 1.7017 | 143 | 81 |
| `main_exit_on_pinned` | 2020-01-02 ~ 2026-09-11（1624 点） | 18.54× | 24.82% | 1.6607 | 143 | 247 |
| `dynamic_exit_off_pinned` | 2020-01-02 ~ 2026-09-11（1624 点） | 14.06× | 19.11% | 1.7043 | 111 | 81 |
| `dynamic_exit_on_pinned` | 2020-01-02 ~ 2026-09-11（1624 点） | 14.58× | 17.66% | 1.7381 | 111 | 247 |

**同机复现自查**（`repro_check`）：`metrics` / `trades` / `equity` / 每个决策日的 `targets` 全部一致 = `True`；候选分数有 530 个字段存在末位差异，最大相对差 3.0e-13。判定：结果层完全可复现；仅候选分数有末位浮点噪声，未影响任何成交/持仓
