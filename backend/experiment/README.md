# experiment/ —— 实验脚本与实验结论

存放**一次性/探索性**的回测实验脚本、网格输入与实验结论。与正式示例（
`examples/cross_section/*.json` 的基线配方）分开，正式策略文档仍见仓库 `docs/`。

> 从 `backend/` 下运行（复用 `app.*` 项目代码）。

## 脚本

| 脚本 | 作用 | 运行 |
| --- | --- | --- |
| `tune_params.py` | **通用参数整定**：基线请求 + 网格 JSON → 笛卡尔积扫描排名。已被内建于调度器的 weekly/biweekly/monthly 支持，无需改策略代码 | `.venv/Scripts/python.exe -m experiment.tune_params examples/cross_section/backtest_shared_etf_rotation.json experiment/grid_etf_rotation_slope.json` |
| `experiment_hold_month_start.py` | 一次性：对比"月度首/末/月中"持有时点（decision_anchor）的差异 | `.venv/Scripts/python.exe -m experiment.experiment_hold_month_start` |
| `experiment_month_nth.py` | 一次性：月度 Nth 交易日调仓（decision_month_nth）的扫描 | `.venv/Scripts/python.exe -m experiment.experiment_month_nth` |

## 网格输入（喂给 tune_params.py）

- `grid_etf_rotation_freq.json` —— 调仓频率 × top_n 扫描（weekly/biweekly/monthly × top_n 1/3/5）
- `grid_etf_rotation_slope.json` —— 斜率窗 × min_score 扫描（slope_days 20/40/60/90 × min_score 0/0.1）

## 实验结论

- `etf-rotation-findings.md` —— ETF 轮动策略 2020-2026 etf_core_sub 上多次迭代的**结论汇总**
  （成交时点、短期过热压制、近期涨幅阈值、长窗涨幅反噬、配方锁定值），由本地记忆收敛而来。

## 约定

- `tune_params.py` 是收敛后的可复用工具，取代已删除的一次性 `experiment_*.py` 硬编码网格。
- 纯实验结论不入正式 `docs/*-strategy.md`，统一沉淀在本目录。
