"""AStockDataSDK 最小示例（聚合行情 + 估值）。"""

from __future__ import annotations

import json
from pathlib import Path
import sys

try:
    from app.data_sources.a_stock_data import AStockDataError, AStockDataSDK
except ModuleNotFoundError:
    # 兼容直接运行该脚本（如 VSCode Code Runner），补充 backend 目录到导入路径
    backend_dir = Path(__file__).resolve().parents[2]
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from app.data_sources.a_stock_data import AStockDataError, AStockDataSDK


def main() -> int:
    sdk = AStockDataSDK()
    symbol = "600519"
    symbols = ["600519", "000858", "300750"]

    try:
        snapshot = sdk.get_snapshot(symbol)
        klines = sdk.get_klines(symbol, period="day", count=5)
        orderbook = sdk.get_orderbook(symbol)
        trades = sdk.get_trades(symbol, count=10)
        valuations = sdk.get_valuation(symbols)
    except AStockDataError as exc:
        print(f"调用失败: {exc}")
        return 1

    print("== snapshot（单标的快照 + 估值） ==")
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))

    print("\n== klines（日线最近 5 条） ==")
    print(json.dumps(klines, ensure_ascii=False, indent=2))

    print("\n== orderbook（买卖五档） ==")
    # 盘口字段较多，这里只打印前几档，避免输出太长
    compact_orderbook = {
        "symbol": orderbook.get("symbol"),
        "bids": orderbook.get("bids", [])[:5],
        "asks": orderbook.get("asks", [])[:5],
    }
    print(json.dumps(compact_orderbook, ensure_ascii=False, indent=2))

    print("\n== trades（最近 10 笔逐笔） ==")
    print(json.dumps(trades, ensure_ascii=False, indent=2))

    print("\n== valuations（多标的估值） ==")
    print(json.dumps(valuations, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
