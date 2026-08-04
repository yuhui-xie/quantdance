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

- `backtest`：运行单票回测
- `screen`：运行选股
- `portfolio`：低频组合（截面选股 + 按交易日间隔调仓；如菜场大妈、中小综指微盘、涨停回落埋伏、订单开工拐点）

## 常用示例

```bash
# 回测
python -m app.script backtest --list-strategies
# 使用 examples：默认打印简短摘要，并保存图表
python -m app.script backtest --request examples/backtest_ma_crossover.json
python -m app.script backtest --request examples/backtest_macd.json
python -m app.script backtest --request examples/backtest_volume_ma_pulse.json
python -m app.script backtest --request examples/backtest_signal_composite.json
# 如需完整 JSON，追加 --json；如需保存完整结果，追加 --output out/backtest.json

# 选股
python -m app.script screen --list-presets
python -m app.script screen --preset momentum --top-k 10 --max-universe 80 --seed 42 --json
python -m app.script screen --request examples/screen_custom_factor.json --json

# 低频组合：列出策略 / 截面选股 / 周期调仓回测
python -m app.script portfolio --list-strategies
python -m app.script portfolio --strategy market_auntie --mode screen --max-universe 80 --seed 42 --json
python -m app.script portfolio --request examples/portfolio_market_auntie.json
python -m app.script portfolio --request examples/portfolio_small_cap_zz399101.json
python -m app.script portfolio --request examples/portfolio_etf_rotation.json
python -m app.script portfolio --request examples/portfolio_limit_up_pullback.json
python -m app.script portfolio --request examples/portfolio_order_inflection.json
```

## 数据与存储

- 回测行情：`backtest` 仅使用 `a_stock_data` 拉取 A 股日线；行情失败时会直接返回错误，不会自动回退到其他源或合成数据
- 选股行情、估值与全 A 股票池列表：均通过 `a_stock_data` 获取；行情失败时会直接返回错误，不会自动回退到其他源或合成数据

## 数据源

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
- 低频组合：[菜场大妈](docs/market-auntie-strategy.md)、[中小综指微盘](docs/small-cap-zz399101-strategy.md)、[涨停回落埋伏](docs/limit-up-pullback-strategy.md)

新增策略时请同步新增对应专文，并更新总览索引表。

## 测试

在 `backend` 目录执行：

```bash
pytest
```
