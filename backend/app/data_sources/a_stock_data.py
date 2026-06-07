"""A 股数据源 SDK 统一入口（a_stock_data）。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict

from app.data_sources.mootdx_market_sdk import (
    MootdxKlineBar,
    MootdxMarketError,
    MootdxMarketSDK,
    MootdxOrderBook,
    MootdxQuote,
    MootdxStockInfo,
    MootdxTrade,
)
from app.data_sources.tencent_finance_sdk import (
    TencentFinanceError,
    TencentFinanceSDK,
    ValuationData,
)


class AStockDataError(Exception):
    """a_stock_data 聚合门面统一异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        symbol: str | None = None,
        source: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.symbol = symbol
        self.source = source
        self.cause = cause
        super().__init__(self.__str__())

    def __str__(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        if self.source:
            parts.append(f"(source={self.source})")
        if self.symbol:
            parts.append(f"(symbol={self.symbol})")
        return " ".join(parts)


class MarketQuote(TypedDict):
    symbol: str  # 股票代码，例如 sh600519。
    name: str  # 股票名称。
    price: float | None  # 最新成交价。
    prev_close: float | None  # 前一交易日收盘价。
    open: float | None  # 当日开盘价。
    high: float | None  # 当日最高价。
    low: float | None  # 当日最低价。
    volume: int | None  # 当日成交量。
    amount: float | None  # 当日成交额。
    timestamp: str | None  # 行情更新时间。


class MarketKlineBar(TypedDict):
    datetime: str  # K 线对应的时间。
    open: float | None  # 当前周期的开盘价。
    high: float | None  # 当前周期的最高价。
    low: float | None  # 当前周期的最低价。
    close: float | None  # 当前周期的收盘价。
    volume: int | None  # 当前周期的成交量。
    amount: float | None  # 当前周期的成交额。


class MarketTrade(TypedDict):
    time: str  # 成交时间。
    price: float | None  # 成交价格。
    volume: int | None  # 成交数量。
    side: str | None  # 买卖方向。


class MarketValuation(TypedDict):
    symbol: str  # 股票代码，例如 sh600519。
    pe_ttm: float | None  # 滚动市盈率。看盈利估值贵不贵
    pb: float | None  # 市净率。看资产估值贵不贵
    market_cap: float | None  # 总市值。看公司整体市值规模
    float_market_cap: float | None  # 流通市值。看真实可交易盘子大小
    turnover_rate: float | None  # 换手率。看交易活跃程度
    limit_up: float | None  # 涨停价。
    limit_down: float | None  # 跌停价。


class AStockUniverseItem(TypedDict):
    symbol: str  # 6 位股票代码。
    name: str  # 股票名称。


class AStockSnapshot(TypedDict):
    symbol: str  # 股票代码，例如 sh600519。
    quote: MarketQuote  # 实时行情快照。
    valuation: MarketValuation | None  # 估值与涨跌停信息，获取失败时为空。


_CACHE_DIR_ENV = "QUANTDANCE_A_STOCK_DATA_CACHE_DIR"
_CACHE_ENABLED_ENV = "QUANTDANCE_A_STOCK_DATA_CACHE"


class AStockDataSDK:
    """统一 A 股数据聚合入口。"""

    def __init__(
        self,
        *,
        mootdx_sdk: MootdxMarketSDK | None = None,
        tencent_sdk: TencentFinanceSDK | None = None,
        cache_dir: str | Path | None = None,
        cache_enabled: bool | None = None,
    ) -> None:
        """初始化聚合 SDK，可传入自定义底层 SDK 便于测试或替换数据源。"""
        self.mootdx = mootdx_sdk or MootdxMarketSDK()
        self.tencent = tencent_sdk or TencentFinanceSDK()
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir is not None else self._default_cache_dir()
        self.cache_enabled = cache_enabled if cache_enabled is not None else os.getenv(_CACHE_ENABLED_ENV, "1") != "0"

    @staticmethod
    def normalize_symbol(raw: str) -> str:
        """标准化股票代码格式，例如把 600519 统一转换为 sh600519。"""
        return TencentFinanceSDK.normalize_symbol(raw)

    @staticmethod
    def _default_cache_dir() -> Path:
        raw = os.getenv(_CACHE_DIR_ENV)
        if raw:
            return Path(raw).expanduser()
        return Path(__file__).resolve().parents[2] / "data" / "a_stock_data"

    @staticmethod
    def _today() -> str:
        return datetime.now().date().isoformat()

    @staticmethod
    def _cache_is_fresh(payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        cached_at = payload.get("cached_at")
        return isinstance(cached_at, str) and cached_at[:10] == AStockDataSDK._today()

    @staticmethod
    def _normalize_cache_rows(rows: object) -> list[dict[str, object]]:
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _merge_rows_by_key(
        old_rows: list[dict[str, object]],
        new_rows: list[dict[str, object]],
        key: str,
    ) -> list[dict[str, object]]:
        merged: dict[str, dict[str, object]] = {}
        for row in (*old_rows, *new_rows):
            value = row.get(key)
            if value is None:
                continue
            merged[str(value)] = row
        return [merged[k] for k in sorted(merged)]

    def _cache_path(self, *parts: str) -> Path:
        return self.cache_dir.joinpath(*parts)

    def _read_cache(self, *parts: str) -> dict[str, object] | None:
        if not self.cache_enabled:
            return None
        path = self._cache_path(*parts)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_cache(self, payload: dict[str, object], *parts: str) -> None:
        if not self.cache_enabled:
            return
        path = self._cache_path(*parts)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError:
            return

    def _cached_klines(
        self,
        symbol: str,
        period: str,
        count: int,
        *,
        allow_stale: bool = False,
    ) -> list[MarketKlineBar] | None:
        payload = self._read_cache("klines", period, f"{symbol}.json")
        if payload is None or (not allow_stale and not self._cache_is_fresh(payload)):
            return None
        try:
            requested_count = int(payload.get("requested_count", 0))
        except (TypeError, ValueError):
            requested_count = 0
        rows = self._normalize_cache_rows(payload.get("rows"))
        if requested_count < count or not rows:
            return None
        return [self._kline_cache_to_market(row) for row in rows[-count:]]

    def _write_klines_cache(
        self,
        symbol: str,
        period: str,
        count: int,
        rows: list[MarketKlineBar],
    ) -> None:
        old_payload = self._read_cache("klines", period, f"{symbol}.json") or {}
        old_rows = self._normalize_cache_rows(old_payload.get("rows"))
        old_count = old_payload.get("requested_count", 0)
        try:
            requested_count = max(count, int(old_count))
        except (TypeError, ValueError):
            requested_count = count
        merged_rows = self._merge_rows_by_key(old_rows, list(rows), "datetime")
        self._write_cache(
            {
                "type": "klines",
                "source": "a_stock_data",
                "symbol": symbol,
                "period": period,
                "requested_count": requested_count,
                "cached_at": self._today(),
                "rows": merged_rows,
            },
            "klines",
            period,
            f"{symbol}.json",
        )

    def _cached_universe(self, *, allow_stale: bool = False) -> list[AStockUniverseItem] | None:
        payload = self._read_cache("universe", "all_a.json")
        if payload is None or (not allow_stale and not self._cache_is_fresh(payload)):
            return None
        rows = self._normalize_cache_rows(payload.get("rows"))
        if not rows:
            return None
        return [
            AStockUniverseItem(symbol=str(row.get("symbol", "")), name=str(row.get("name", "")))
            for row in rows
            if row.get("symbol")
        ]

    def _write_universe_cache(self, rows: list[AStockUniverseItem]) -> None:
        self._write_cache(
            {
                "type": "universe",
                "source": "a_stock_data",
                "cached_at": self._today(),
                "rows": list(rows),
            },
            "universe",
            "all_a.json",
        )

    @staticmethod
    def _kline_cache_to_market(row: dict[str, object]) -> MarketKlineBar:
        return MarketKlineBar(
            datetime=str(row.get("datetime", "")),
            open=row.get("open"),  # type: ignore[typeddict-item]
            high=row.get("high"),  # type: ignore[typeddict-item]
            low=row.get("low"),  # type: ignore[typeddict-item]
            close=row.get("close"),  # type: ignore[typeddict-item]
            volume=row.get("volume"),  # type: ignore[typeddict-item]
            amount=row.get("amount"),  # type: ignore[typeddict-item]
        )

    @staticmethod
    def _quote_to_market(quote: MootdxQuote) -> MarketQuote:
        """把 mootdx 的实时行情结构转换为统一的 MarketQuote。"""
        return MarketQuote(
            symbol=quote["symbol"],
            name=quote["name"],
            price=quote["price"],
            prev_close=quote["prev_close"],
            open=quote["open"],
            high=quote["high"],
            low=quote["low"],
            volume=quote["volume"],
            amount=quote["amount"],
            timestamp=quote["timestamp"],
        )

    @staticmethod
    def _kline_to_market(bar: MootdxKlineBar) -> MarketKlineBar:
        """把 mootdx 的 K 线结构转换为统一的 MarketKlineBar。"""
        return MarketKlineBar(
            datetime=bar["datetime"],
            open=bar["open"],
            high=bar["high"],
            low=bar["low"],
            close=bar["close"],
            volume=bar["volume"],
            amount=bar["amount"],
        )

    @staticmethod
    def _trade_to_market(trade: MootdxTrade) -> MarketTrade:
        """把 mootdx 的逐笔成交结构转换为统一的 MarketTrade。"""
        return MarketTrade(
            time=trade["time"],
            price=trade["price"],
            volume=trade["volume"],
            side=trade["side"],
        )

    @staticmethod
    def _valuation_to_market(valuation: ValuationData) -> MarketValuation:
        """把腾讯财经估值结构转换为统一的 MarketValuation。"""
        return MarketValuation(
            symbol=valuation["symbol"],
            pe_ttm=valuation["pe_ttm"],
            pb=valuation["pb"],
            market_cap=valuation["market_cap"],
            float_market_cap=valuation["float_market_cap"],
            turnover_rate=valuation["turnover_rate"],
            limit_up=valuation["limit_up"],
            limit_down=valuation["limit_down"],
        )

    def get_snapshot(self, symbol: str) -> AStockSnapshot:
        """获取单只股票的行情快照。

        参数:
            symbol: 股票代码，支持 600519、sh600519 等格式。

        返回:
            包含标准化股票代码、实时行情和估值信息的快照。
        """
        norm = self.normalize_symbol(symbol)
        try:
            quote = self._quote_to_market(self.mootdx.get_quote(norm))
        except MootdxMarketError as exc:
            raise AStockDataError(exc.code, exc.message, symbol=norm, source="mootdx", cause=exc) from exc

        valuation: MarketValuation | None
        try:
            valuation = self._valuation_to_market(self.tencent.get_valuation(norm))
        except TencentFinanceError:
            valuation = None

        return AStockSnapshot(symbol=norm, quote=quote, valuation=valuation)

    def get_klines(
        self,
        symbol: str,
        period: Literal["day", "week", "month", "1m", "5m", "15m", "30m", "60m"] = "day",
        *,
        count: int = 200,
    ) -> list[MarketKlineBar]:
        """获取单只股票的 K 线数据。

        参数:
            symbol: 股票代码，支持 600519、sh600519 等格式。
            period: K 线周期，支持 day/week/month 和 1m/5m/15m/30m/60m。
            count: 返回的 K 线条数。

        返回:
            按底层数据源顺序返回的 K 线列表。
        """
        norm = self.normalize_symbol(symbol)
        cached = self._cached_klines(norm, period, count)
        if cached is not None:
            return cached
        try:
            rows = self.mootdx.get_klines(norm, period=period, count=count)
        except MootdxMarketError as exc:
            cached = self._cached_klines(norm, period, count, allow_stale=True)
            if cached is not None:
                return cached
            raise AStockDataError(exc.code, exc.message, symbol=norm, source="mootdx", cause=exc) from exc
        out = [self._kline_to_market(row) for row in rows]
        self._write_klines_cache(norm, period, count, out)
        return out

    def get_universe(self) -> list[AStockUniverseItem]:
        """获取沪深 A 股股票池列表。"""
        cached = self._cached_universe()
        if cached is not None:
            return cached
        try:
            rows: list[MootdxStockInfo] = self.mootdx.get_stocks()
        except MootdxMarketError as exc:
            cached = self._cached_universe(allow_stale=True)
            if cached is not None:
                return cached
            raise AStockDataError(exc.code, exc.message, source="mootdx", cause=exc) from exc
        out = [
            AStockUniverseItem(symbol=row["code"], name=row["name"])
            for row in rows
        ]
        self._write_universe_cache(out)
        return out

    def get_orderbook(self, symbol: str) -> MootdxOrderBook:
        """获取单只股票的盘口五档/委买委卖数据。

        参数:
            symbol: 股票代码，支持 600519、sh600519 等格式。

        返回:
            mootdx 返回的盘口结构。
        """
        norm = self.normalize_symbol(symbol)
        try:
            out = self.mootdx.get_orderbook(norm)
        except MootdxMarketError as exc:
            raise AStockDataError(exc.code, exc.message, symbol=norm, source="mootdx", cause=exc) from exc
        return out

    def get_trades(self, symbol: str, *, count: int = 200) -> list[MarketTrade]:
        """获取单只股票的逐笔成交数据。

        参数:
            symbol: 股票代码，支持 600519、sh600519 等格式。
            count: 返回的成交记录条数。

        返回:
            统一格式的逐笔成交列表。
        """
        norm = self.normalize_symbol(symbol)
        try:
            rows = self.mootdx.get_trades(norm, count=count)
        except MootdxMarketError as exc:
            raise AStockDataError(exc.code, exc.message, symbol=norm, source="mootdx", cause=exc) from exc
        return [self._trade_to_market(row) for row in rows]

    def get_valuation(self, symbols: list[str]) -> dict[str, MarketValuation]:
        """批量获取股票估值与涨跌停信息。

        参数:
            symbols: 股票代码列表，支持 600519、sh600519 等格式。

        返回:
            以标准化股票代码为键、估值信息为值的字典。
        """
        norm_symbols = [self.normalize_symbol(symbol) for symbol in symbols]
        try:
            data = self.tencent.get_valuations(norm_symbols)
        except TencentFinanceError as exc:
            raise AStockDataError(exc.code, exc.message, source="tencent", cause=exc) from exc
        return {symbol: self._valuation_to_market(item) for symbol, item in data.items()}


__all__ = [
    "AStockDataSDK",
    "AStockDataError",
    "AStockUniverseItem",
    "AStockSnapshot",
    "MarketQuote",
    "MarketKlineBar",
    "MarketTrade",
    "MarketValuation",
]

