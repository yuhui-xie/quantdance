# Layer 1 行情 SDK（精简版）

本说明仅覆盖 Layer 1（实时行情与基础估值）能力，供 Agent/开发者在本仓库内直接使用。

## 能力边界

- 覆盖数据源：`mootdx + 腾讯财经`
- 覆盖数据类型：
  - K 线（`day/week/month/1m/5m/15m/30m/60m`）
  - 实时快照（最新价、涨跌、成交量、成交额）
  - 五档盘口
  - 逐笔成交
  - 估值与扩展字段（PE/PB/总市值/流通市值/换手率/涨跌停）
- 不包含 Layer 2+ 能力（盘口推流、因子回测、交易执行等）

## 统一入口

统一门面建议通过 `backend/app/data_sources/a_stock_data.py` 的 `AStockDataSDK` 调用：

- `get_snapshot(symbol)`：合并 mootdx 快照 + 腾讯估值
- `get_klines(symbol, period, count)`：获取统一格式 K 线
- `get_orderbook(symbol)`：获取五档盘口
- `get_trades(symbol, count)`：获取逐笔成交
- `get_valuation(symbols)`：批量获取腾讯估值字段

## Symbol 与周期规范

- 输入兼容：`600519` / `600519.SH` / `sh600519`
- 统一内部规范：
  - A 股：`sh600519` / `sz000858`
  - 港股：`hk00700`
- 周期映射：
  - `day/week/month` 对应日/周/月
  - `1m/5m/15m/30m/60m` 对应分钟线

## 关键字段定义

- `MarketQuote`
  - `symbol`, `name`, `price`, `prev_close`, `open`, `high`, `low`
  - `volume`, `amount`, `timestamp`
- `MarketKlineBar`
  - `datetime`, `open`, `high`, `low`, `close`, `volume`, `amount`
- `MarketTrade`
  - `time`, `price`, `volume`, `side`
- `MarketValuation`
  - `pe_ttm`, `pb`, `market_cap`, `float_market_cap`, `turnover_rate`
  - `limit_up`, `limit_down`
- `AStockSnapshot`
  - `symbol`, `quote`, `valuation`

## 腾讯字段索引注意事项（必须遵守）

腾讯 `qt.gtimg.cn` 的 `~` 分隔字段会变化，但以下索引在现有实现中约定如下：

- `39`：市盈率（PE，TTM）
- `44`：总市值
- `45`：流通市值
- `46`：市净率（PB）
- `47`：涨停价
- `48`：跌停价

常见误区：

- `43` 不是 PB，PB 使用 `46`
- 字段可能为空字符串，必须做安全转换，避免抛出数值转换异常

## 异常与容错约定

- 统一抛出带 `code/message/symbol/source` 上下文的异常
- 对外部接口超时、HTTP 错误、空响应、字段缺失分别使用不同错误码
- 映射层只做结构标准化，不吞掉上游异常语义
