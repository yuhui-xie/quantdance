# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

quantdance 是 A 股量化研究工具（纯脚本，无 HTTP 服务），提供**技术面选股**（`screen`）与**两类回测语义**（`backtest`）。业务文档与代码注释均以中文为主。

## 常用命令

所有命令在 `backend/` 目录下执行，使用 `backend/.venv`（Python 3.10+）：

```bash
cd backend
.venv/Scripts/python.exe -m app.script --help    # Windows；Linux/Mac 用 python -m app.script
pytest                                            # 全部测试
pytest tests/test_backtest_engine.py              # 单个文件
pytest tests/test_backtest_engine.py -k round_trip  # 单个用例（-k 过滤）
```

CLI 子命令与常用示例：

```bash
python -m app.script backtest --list-strategies
python -m app.script backtest --request examples/backtest_ma_crossover.json
python -m app.script backtest --strategy market_auntie --mode screen --max-universe 80 --seed 42 --json
python -m app.script screen --list-presets
python -m app.script screen --preset momentum --top-k 10 --json
python -m app.script stock-search 贵州茅台
```

回测请求由 `backend/examples/*.json` 驱动（`--request`），也支持全部 CLI 参数覆盖。结果默认打印摘要；追加 `--json` 打印完整结果，`--output out/x.json` 落盘，`--report path` 存交互 HTML（仅此一种可视化产物，无静态图）。`output_options.*` 可放进请求 JSON（见 `docs/strategy-guide.md` 的字段表）。

## 架构

### 两类回测语义（核心概念，务必区分）

统一入口 `python -m app.script backtest` 下有两种资金语义，模式相同但行为不同：

- **时序策略 `StrategySpec`**（`app/strategies/*.py`）：`mode=single` 单票独立资金；`mode=universe` 把同一策略对股票池**逐票独立运行**（每票用完整 `initial_cash`），再按归一化净值**等权汇总**（`aggregate` 是统计汇总，不模拟共享资金）。
- **横截面策略 `CrossSectionStrategySpec`**（`app/strategies/cross_section/*.py`）：所有标的**共享一个账户**，策略在自生成的决策日做截面选股，执行器据此换仓。`mode=universe` 回测、`mode=screen` 仅返回指定截面选股结果。策略通过 `spec.select(asof, ctx, params)` 返回目标持仓，通过 `spec.decision_dates(...)` 生成决策日。

`mode=universe` 的时序与横截面资金语义完全不同：前者 `run_backtest_universe_request`，后者 `run_cross_section_backtest`。判断走哪条路的依据是策略是否 `CrossSectionStrategySpec`（含 `select` 属性）。

### 主调用链

- `app/script.py`：argparse → 把 CLI 参数与请求 JSON 合并成 Pydantic 请求体 → `run_backtest_request(body)` / `run_screen(body)`。所有策略专属参数须放入 `strategy_params`（`BacktestRequest` 有顶层 `forbid_top_level_strategy_params` 校验）。
- `app/backtest_runner.py`：`run_backtest_request` 按策略类型 + mode 分发（时序单票 / 时序批量 / 横截面回测 / 横截面选股）。
- `app/backtest_engine.py`：**向量化全仓回测** `run_from_signals(df, signal, ...)`，输入买卖信号（1 买入 / -1 卖出 / 0 观望）输出 `BacktestResult`（equity/trades/metrics/price/signal）。`metrics_from_equity` 为统一绩效口径（单票与组合共用）。
- `app/backtest/shared_engine.py`：共享账户、多标的目标持仓引擎 `run_shared_backtest(close_panel, targets_by_date, ...)`，支持滑点、整手、最低佣金、止盈止损与 `position_management`。
- `app/backtest/cross_section_runner.py`：横截面策略的数据面板加载、决策日与选股预计算、执行编排（`run_cross_section_backtest` / `run_cross_section_screen`）。
- `app/backtest_aggregate.py`：批量回测的等权净值曲线汇总；`app/backtest/benchmarks.py` 挂沪深300基准。
- `app/cli_backtest_report.py`：HTML 交互报告渲染（`backtest --report`，无静态图输出）；报告模板在 `app/backtest/templates/`。

### 策略插件系统（自动注册）

策略模块导出 `STRATEGY` 常量即自动注册，无需手动登记：

- `app/strategies/registry.py` 递归扫描 `app.strategies` 包，跳过 `base`/`registry`，重复 `id` 会抛错。`StrategySpec` 与 `CrossSectionStrategySpec` 分别汇入 `STRATEGIES` / `CROSS_SECTION_STRATEGIES`。
- `StrategySpec` 字段：`id / name / description / params_model(Pydantic) / min_bars / run / signals(可选)`。新策略可直接提供 `signals` 回调返回事件信号；旧策略省略时由 `run().signal` 兼容读取（`StrategySpec.compute_signals`）。
- `CrossSectionStrategySpec` 额外字段：`select / decision_dates / default_universe / needs_fundamentals / needs_dividend / needs_financials / default_top_n / warnings`。内置策略用 `strategy_params.decision_interval`（默认 20 交易日）生成约月频决策日，这是策略自己的规则，共享资金引擎不强制调仓周期。

新增策略后需在 `docs/` 新增 `*-strategy.md` 专文并更新 `docs/strategy-guide.md` 索引（项目约定）。

### 数据源与缓存

- `app/data_sources/a_stock_data.py`：统一门面 `AStockDataSDK`，聚合 mootdx（K 线/快照/逐笔）+ 腾讯财经（估值/搜索）。日线回测与选股均经此。
- `app/data_sources/market_data.py`：`fetch_a_share_daily`（返回 `DatetimeIndex` 索引的 OHLCV DataFrame，前复权）、`fetch_*_universe`（指数成分股，用 akshare）。**行情失败会直接抛 `MarketDataError`，不会回退到合成/其他源。**
- `app/data_sources/em_fundamentals.py` / `financial_reports.py`：横截面策略的基本面面板与财报面板。
- 缓存：K 线与股票池按日新鲜度缓存到 `backend/data/a_stock_data/`，可用环境变量 `QUANTDANCE_A_STOCK_DATA_CACHE`（`0` 关闭）与 `QUANTDANCE_A_STOCK_DATA_CACHE_DIR` 控制。K 线缓存带 `version`/`adjust` 标记，前复权实现有破坏性变更时递增 `_KLINE_CACHE_VERSION` 以整体重建（见 `docs/data-adjustment.md`）。

### 股票池与选股

- `app/universe.py`：`resolve_universe_rows` 解析自定义 `symbols` 或预设 `universe`（`all_a` / `hs300` / `zz500` / `zz399101` / `zz1000` / `gz2000` / `star50` / `star_board`），统一代码格式并去重；超上限时按 `seed` 可复现抽样。横截面策略的默认池由 `spec.default_universe` 决定。
- `app/stock_screening.py`：技术面/基本面因子打分选股，`run_screen` 返回 `ScreenResponse`。
- `app/position_management.py`：`PositionManagementPolicy` 渐进建仓/加仓/止盈止损，仅横截面共享资金回测可用，且不能与旧版顶层止盈止损字段同时设置。

## 错误处理约定

- `ValueError`：参数/业务错误（CLI 退出码 1）。
- `MarketDataError`（`app/data_sources/market_data.py`）：行情源错误（CLI 退出码 3）。
- 其他未预期异常：CLI 退出码 2。
- 批量回测单票失败/跳过（`failed` / `skipped` 状态）不中断整批。

## 测试约定

`pytest`（`backend/pytest.ini` 设 `pythonpath=.`、`testpaths=tests`）。测试使用内存构造的 DataFrame，不访问网络。横截面/批量测试通过 mock 行情与基本面面板验证调度与执行逻辑。
