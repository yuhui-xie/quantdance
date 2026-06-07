# quantdance

## 环境要求

- Python 3.10+

## 安装

在 `backend` 目录执行：

```bash
python -m venv .venv
pip install -r requirements.txt
```

## 统一脚本入口

在 `backend` 目录执行：

```bash
python -m app.script --help
```

支持子命令：

- `backtest`：运行回测
- `screen`：运行选股
- `discover`：批量回测驱动的股票发掘

## 常用示例

```bash
# 回测
python -m app.script backtest --list-strategies
# 使用 examples：默认打印简短摘要，并保存图表
python -m app.script backtest --request examples/backtest_ma_crossover.json
python -m app.script backtest --request examples/backtest_macd.json
python -m app.script backtest --request examples/backtest_volume_ma_pulse.json
# 如需完整 JSON，追加 --json；如需保存完整结果，追加 --output out/backtest.json

# 选股
python -m app.script screen --list-presets
python -m app.script screen --preset momentum --top-k 10 --max-universe 80 --seed 42 --json
python -m app.script screen --request examples/screen_custom_factor.json --json

# 股票发掘：先做基本面过滤，再批量回测排序
python -m app.script discover --max-pe-ttm 30 --max-pb 3 --min-market-cap 10000000000 --top-k 10 --json
# 沪深300股票池：同时输出沪深300成分股等权基准和候选超额收益
python -m app.script discover --universe hs300 --strategy ma_crossover --top-k 10
# 保存策略等权净值与沪深300等权基准的对比图（默认 SVG）
python -m app.script discover --universe hs300 --strategy ma_crossover --plot out/discover_hs300_ma_crossover.svg
# 使用配置文件运行股票发掘，并按 output_options 保存结果
python -m app.script discover --request examples/discover_value_ma_crossover.json
```

## 数据与存储

- 回测行情：`backtest` 仅使用 `a_stock_data` 拉取 A 股日线；行情失败时会直接返回错误，不会自动回退到其他源或合成数据
- 选股行情、估值与全 A 股票池列表：均通过 `a_stock_data` 获取；行情失败时会直接返回错误，不会自动回退到其他源或合成数据
- 沪深300股票池：`discover --universe hs300` 通过 `akshare` 获取当前沪深300成分股，并额外合成沪深300成分股等权基准；严格历史验证仍需历史成分股数据以避免幸存者偏差

## a_stock_data SDK（mootdx + 腾讯财经）

在 `backend` 目录可直接使用统一门面：

```python
from app.data_sources.a_stock_data import AStockDataSDK

sdk = AStockDataSDK()

# 1) 快照：mootdx 实时行情 + 腾讯估值
snapshot = sdk.get_snapshot("600519")
print(snapshot["quote"]["price"], snapshot["valuation"]["pb"] if snapshot["valuation"] else None)

# 2) K 线：支持 day/week/month/1m/5m/15m/30m/60m
bars = sdk.get_klines("600519", period="day", count=20)
print(bars[-1]["datetime"], bars[-1]["close"])

# 3) 五档与逐笔
orderbook = sdk.get_orderbook("600519")
trades = sdk.get_trades("600519", count=50)
print(orderbook["bids"][0], trades[0] if trades else None)

# 4) 批量估值
vals = sdk.get_valuation(["600519", "000858"])
print(vals["sh600519"]["pe_ttm"], vals["sz000858"]["market_cap"])
```

## 策略文档

内置回测策略的说明见 `docs/`：

- 总览与索引：[docs/strategy-guide.md](docs/strategy-guide.md)
- 各策略专文：`docs/*-strategy.md`（如 [量比脉冲](docs/volume-ma-pulse-strategy.md)、[双均线](docs/ma-crossover-strategy.md)）

新增策略时请同步新增对应专文，并更新总览索引表。

## 测试

在 `backend` 目录执行：

```bash
pytest
```
