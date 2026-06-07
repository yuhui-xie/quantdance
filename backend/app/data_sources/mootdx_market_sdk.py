"""mootdx 行情适配层。

负责：
- 统一 symbol/period 规范
- 隔离 mootdx 原始返回结构差异
- 输出项目内部统一字段
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal, TypedDict


class MootdxMarketError(Exception):
    """mootdx 适配层统一异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        symbol: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.symbol = symbol
        self.cause = cause
        super().__init__(self.__str__())

    def __str__(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        if self.symbol:
            parts.append(f"(symbol={self.symbol})")
        return " ".join(parts)


class MootdxQuote(TypedDict):
    symbol: str
    name: str
    price: float | None
    prev_close: float | None
    open: float | None
    high: float | None
    low: float | None
    volume: int | None
    amount: float | None
    timestamp: str | None


class MootdxStockInfo(TypedDict):
    symbol: str
    code: str
    name: str


class MootdxKlineBar(TypedDict):
    datetime: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    amount: float | None


class MootdxOrderBookLevel(TypedDict):
    price: float | None
    volume: int | None


class MootdxOrderBook(TypedDict):
    symbol: str
    bids: list[MootdxOrderBookLevel]
    asks: list[MootdxOrderBookLevel]


class MootdxTrade(TypedDict):
    time: str
    price: float | None
    volume: int | None
    side: str | None


@dataclass(frozen=True)
class _NormSymbol:
    symbol: str
    market: int
    code: str


_PERIOD_TO_CATEGORY: dict[str, int] = {
    "5m": 0,
    "15m": 1,
    "30m": 2,
    "60m": 3,
    "day": 9,
    "week": 5,
    "month": 6,
    "1m": 8,
}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    n = _to_float(value)
    if n is None:
        return None
    return int(n)


def _pick(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def _to_records(raw: Any, *, label: str, symbol: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [dict(item) for item in raw]
    if isinstance(raw, dict):
        return [raw]
    if hasattr(raw, "to_dict"):
        return list(raw.to_dict("records"))  # type: ignore[call-arg]
    raise MootdxMarketError("parse_error", f"{label}返回结构不支持", symbol=symbol)


class MootdxMarketSDK:
    """mootdx 行情 SDK（标准化包装）。"""

    def __init__(self, *, client: Any | None = None, client_factory: Callable[[], Any] | None = None) -> None:
        if client is not None:
            self.client = client
            return
        if client_factory is not None:
            self.client = client_factory()
            return
        self.client = self._create_default_client()

    @staticmethod
    def normalize_symbol(raw: str) -> str:
        s = raw.strip().lower().replace(" ", "")
        if not s:
            raise MootdxMarketError("invalid_symbol", "symbol 不能为空")

        if re.fullmatch(r"(sh|sz)\d{6}", s):
            return s

        m = re.fullmatch(r"(\d{6})\.(sh|sz|ss)", s)
        if m:
            code, market = m.groups()
            return ("sh" if market in ("sh", "ss") else "sz") + code

        m = re.fullmatch(r"(sh|sz|ss)\.(\d{6})", s)
        if m:
            market, code = m.groups()
            return ("sh" if market in ("sh", "ss") else "sz") + code

        if re.fullmatch(r"\d{6}", s):
            if s.startswith(("5", "6", "9")):
                return f"sh{s}"
            return f"sz{s}"

        raise MootdxMarketError("invalid_symbol", f"不支持的 symbol 格式: {raw!r}")

    @staticmethod
    def _parse_symbol(raw: str) -> _NormSymbol:
        symbol = MootdxMarketSDK.normalize_symbol(raw)
        market = 1 if symbol.startswith("sh") else 0
        return _NormSymbol(symbol=symbol, market=market, code=symbol[2:])

    @staticmethod
    def _parse_datetime(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    @staticmethod
    def map_period(period: Literal["day", "week", "month", "1m", "5m", "15m", "30m", "60m"]) -> int:
        category = _PERIOD_TO_CATEGORY.get(period)
        if category is None:
            raise MootdxMarketError("invalid_period", f"不支持 period: {period}")
        return category

    @staticmethod
    def _create_default_client() -> Any:
        try:
            from mootdx.quotes import Quotes  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - 依赖缺失分支
            raise MootdxMarketError("dependency_error", "未安装或无法导入 mootdx", cause=exc) from exc

        for ctor in (
            lambda: Quotes.factory(market="std"),
            lambda: Quotes.factory(),
            lambda: Quotes(),
        ):
            try:
                return ctor()
            except Exception:
                continue
        raise MootdxMarketError("init_error", "mootdx 客户端初始化失败")

    def _call_first(self, names: tuple[str, ...], *args: Any, **kwargs: Any) -> Any:
        for name in names:
            method = getattr(self.client, name, None)
            if callable(method):
                try:
                    return method(*args, **kwargs)
                except TypeError:
                    continue
        raise MootdxMarketError("method_not_found", f"客户端不支持方法: {', '.join(names)}")

    def get_quote(self, symbol: str) -> MootdxQuote:
        norm = self._parse_symbol(symbol)
        try:
            method = getattr(self.client, "quotes", None)
            if callable(method):
                raw = method(norm.symbol)
            else:
                raw = self._call_first(
                    ("get_security_quotes", "get_quote"),
                    [(norm.market, norm.code)],
                )
        except MootdxMarketError:
            raise
        except Exception as exc:
            raise MootdxMarketError("source_error", f"获取实时行情失败: {exc}", symbol=norm.symbol, cause=exc) from exc

        rows = _to_records(raw, label="实时行情", symbol=norm.symbol)
        if not rows:
            raise MootdxMarketError("empty_response", "实时行情为空", symbol=norm.symbol)
        row = rows[0]

        return MootdxQuote(
            symbol=norm.symbol,
            name=str(_pick(row, ("name", "stock_name")) or ""),
            price=_to_float(_pick(row, ("price", "last_close", "lastPrice"))),
            prev_close=_to_float(_pick(row, ("last_close", "yclose", "pre_close"))),
            open=_to_float(_pick(row, ("open", "open_price"))),
            high=_to_float(_pick(row, ("high", "high_price"))),
            low=_to_float(_pick(row, ("low", "low_price"))),
            volume=_to_int(_pick(row, ("vol", "volume"))),
            amount=_to_float(_pick(row, ("amount", "turnover"))),
            timestamp=str(_pick(row, ("servertime", "time")) or "") or None,
        )

    def _stock_records_for_market(self, market: int) -> list[dict[str, Any]]:
        try:
            method = getattr(self.client, "stocks", None)
            if callable(method):
                for kwargs in ({"market": market}, {}):
                    try:
                        raw = method(**kwargs)
                        return _to_records(raw, label="股票列表", symbol="")
                    except TypeError:
                        continue

            method = getattr(self.client, "get_security_list", None)
            if callable(method):
                for args in ((market, 0), (market,)):
                    try:
                        raw = method(*args)
                        return _to_records(raw, label="股票列表", symbol="")
                    except TypeError:
                        continue

            raw = self._call_first(("stocks", "get_security_list"), market)
            return _to_records(raw, label="股票列表", symbol="")
        except MootdxMarketError:
            raise
        except Exception as exc:
            raise MootdxMarketError("source_error", f"获取股票列表失败: {exc}", cause=exc) from exc

    def get_stocks(self) -> list[MootdxStockInfo]:
        """获取沪深 A 股代码与名称列表。"""
        out: list[MootdxStockInfo] = []
        seen: set[str] = set()
        for market in (1, 0):
            rows = self._stock_records_for_market(market)
            prefix = "sh" if market == 1 else "sz"
            for row in rows:
                code_raw = _pick(row, ("code", "symbol", "stock_code"))
                if code_raw is None:
                    continue
                code = re.sub(r"\D", "", str(code_raw))
                if not re.fullmatch(r"\d{6}", code):
                    continue
                if market == 1 and not code.startswith("6"):
                    continue
                if market == 0 and not code.startswith(("0", "3")):
                    continue
                symbol = f"{prefix}{code}"
                if symbol in seen:
                    continue
                seen.add(symbol)
                name = str(_pick(row, ("name", "stock_name", "volunit")) or "").strip()
                out.append(MootdxStockInfo(symbol=symbol, code=code, name=name))
        if not out:
            raise MootdxMarketError("empty_response", "股票列表为空")
        return out

    def get_klines(
        self,
        symbol: str,
        period: Literal["day", "week", "month", "1m", "5m", "15m", "30m", "60m"] = "day",
        *,
        count: int = 200,
    ) -> list[MootdxKlineBar]:
        norm = self._parse_symbol(symbol)
        category = self.map_period(period)
        try:
            method = getattr(self.client, "bars", None)
            if callable(method):
                try:
                    raw = method(norm.code, frequency=category, start=0, offset=count)
                except TypeError:
                    raw = method(category, norm.market, norm.code, 0, count)
            else:
                raw = self._call_first(
                    ("get_security_bars", "get_kline"),
                    category,
                    norm.market,
                    norm.code,
                    0,
                    count,
                )
        except MootdxMarketError:
            raise
        except Exception as exc:
            raise MootdxMarketError("source_error", f"获取 K 线失败: {exc}", symbol=norm.symbol, cause=exc) from exc

        rows = _to_records(raw, label="K 线", symbol=norm.symbol)
        bars: list[MootdxKlineBar] = []
        for item in rows:
            bars.append(
                MootdxKlineBar(
                    datetime=self._parse_datetime(_pick(item, ("datetime", "date"))),
                    open=_to_float(_pick(item, ("open",))),
                    high=_to_float(_pick(item, ("high",))),
                    low=_to_float(_pick(item, ("low",))),
                    close=_to_float(_pick(item, ("close",))),
                    volume=_to_int(_pick(item, ("vol", "volume"))),
                    amount=_to_float(_pick(item, ("amount",))),
                )
            )
        return bars

    def get_orderbook(self, symbol: str) -> MootdxOrderBook:
        norm = self._parse_symbol(symbol)
        try:
            method = getattr(self.client, "quotes", None)
            if callable(method):
                raw = method(norm.symbol)
            else:
                raw = self._call_first(("get_security_quotes",), [(norm.market, norm.code)])
        except MootdxMarketError:
            raise
        except Exception as exc:
            raise MootdxMarketError("source_error", f"获取盘口失败: {exc}", symbol=norm.symbol, cause=exc) from exc

        rows = _to_records(raw, label="盘口", symbol=norm.symbol)
        row = rows[0] if rows else {}

        bids: list[MootdxOrderBookLevel] = []
        asks: list[MootdxOrderBookLevel] = []
        for i in range(1, 6):
            bids.append(
                MootdxOrderBookLevel(
                    price=_to_float(_pick(row, (f"bid{i}", f"buy{i}", f"b{i}_p"))),
                    volume=_to_int(_pick(row, (f"bid_vol{i}", f"buy{i}_vol", f"b{i}_v"))),
                )
            )
            asks.append(
                MootdxOrderBookLevel(
                    price=_to_float(_pick(row, (f"ask{i}", f"sell{i}", f"a{i}_p"))),
                    volume=_to_int(_pick(row, (f"ask_vol{i}", f"sell{i}_vol", f"a{i}_v"))),
                )
            )

        return MootdxOrderBook(symbol=norm.symbol, bids=bids, asks=asks)

    def get_trades(self, symbol: str, *, count: int = 200) -> list[MootdxTrade]:
        norm = self._parse_symbol(symbol)
        try:
            method = getattr(self.client, "transaction", None)
            if callable(method):
                raw = method(norm.code, start=0, offset=count)
            else:
                raw = self._call_first(
                    ("get_transaction_data", "get_trades"),
                    norm.market,
                    norm.code,
                    0,
                    count,
                )
        except MootdxMarketError:
            raise
        except Exception as exc:
            raise MootdxMarketError("source_error", f"获取逐笔失败: {exc}", symbol=norm.symbol, cause=exc) from exc

        rows = _to_records(raw, label="逐笔", symbol=norm.symbol)
        trades: list[MootdxTrade] = []
        for item in rows:
            trades.append(
                MootdxTrade(
                    time=str(_pick(item, ("time", "datetime")) or ""),
                    price=_to_float(_pick(item, ("price",))),
                    volume=_to_int(_pick(item, ("vol", "volume"))),
                    side=str(_pick(item, ("side", "bsflag", "type", "buyorsell")) or "") or None,
                )
            )
        return trades

