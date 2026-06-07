"""腾讯财经 SDK 最小示例（非官方接口，仅供研究/教学）。"""

from __future__ import annotations

import json
from pathlib import Path
import sys

try:
    from app.data_sources.tencent_finance_sdk import TencentFinanceError, TencentFinanceSDK
except ModuleNotFoundError:
    # 兼容直接运行该脚本（如 VSCode Code Runner），补充 backend 目录到导入路径
    backend_dir = Path(__file__).resolve().parents[2]
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from app.data_sources.tencent_finance_sdk import TencentFinanceError, TencentFinanceSDK


def main() -> int:
    # 风险提示：
    # 1) 该数据源为社区常见非官方接口，字段/可用性可能变更；
    # 2) 建议线上场景设置限频、重试、降级回退；
    # 3) 建议与其他数据源交叉校验关键字段。
    sdk = TencentFinanceSDK(timeout=8.0, max_retries=2)
    symbol = "600519"
    symbols = ["600519", "000858"]

    try:
        quote = sdk.get_quote(symbol)
        quotes = sdk.get_quotes(symbols)
        kline = sdk.get_kline(symbol, period="day", count=5)
        fund_flow = sdk.get_fund_flow(symbol)
    except TencentFinanceError as exc:
        print(f"调用失败: {exc}")
        return 1

    print("== 单标的实时行情 ==")
    print(json.dumps(quote, ensure_ascii=False, indent=2))
    print("\n== 批量行情 ==")
    print(json.dumps(quotes, ensure_ascii=False, indent=2))
    print("\n== K 线（最近 5 条） ==")
    print(json.dumps(kline, ensure_ascii=False, indent=2))
    print("\n== 资金流向 ==")
    print(json.dumps(fund_flow, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
