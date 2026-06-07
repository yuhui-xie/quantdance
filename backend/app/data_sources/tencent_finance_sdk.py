"""腾讯财经（非官方）行情 SDK。

数据来源基于社区常用端点（如 qt.gtimg.cn / ifzq.gtimg.cn），字段可能随时间调整。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, TypedDict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class TencentFinanceError(Exception):
    """腾讯行情 SDK 统一异常。"""

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


class QuoteData(TypedDict):
    symbol: str
    name: str
    code: str
    price: float | None
    prev_close: float | None
    open: float | None
    high: float | None
    low: float | None
    volume: int | None
    amount: float | None
    timestamp: str | None
    raw_fields: list[str]


class StockSearchItem(TypedDict):
    symbol: str
    code: str
    name: str
    market: str
    type: str | None
    raw_fields: list[str]


class KlineBar(TypedDict):
    date: str
    open: float | None
    close: float | None
    high: float | None
    low: float | None
    volume: int | None


class FundFlowData(TypedDict):
    symbol: str
    main_inflow: float | None
    main_outflow: float | None
    net_inflow: float | None
    main_ratio: float | None
    retail_ratio: float | None
    raw_fields: list[str]


class ValuationData(TypedDict):
    symbol: str
    pe_ttm: float | None
    pb: float | None
    market_cap: float | None
    float_market_cap: float | None
    turnover_rate: float | None
    limit_up: float | None
    limit_down: float | None
    raw_fields: list[str]


_QUOTE_RE = re.compile(r'v_([^=]+)="(.*?)";')
_PERIOD_MAP: dict[str, str] = {"day": "day", "week": "week", "month": "month"}


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    s = value.strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    s = value.strip().replace(",", "")
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _safe_get(fields: list[str], idx: int) -> str | None:
    if idx < 0 or idx >= len(fields):
        return None
    return fields[idx]


class TencentFinanceSDK:
    """腾讯财经（非官方）SDK。

    特性：
    - 统一 requests Session、超时和重试策略
    - 统一 symbol 规范化
    - 统一异常与返回结构
    """

    quote_url = "https://qt.gtimg.cn/q="
    kline_url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    smartbox_url = "https://smartbox.gtimg.cn/s3/"

    def __init__(
        self,
        *,
        timeout: float = 8.0,
        max_retries: int = 2,
        backoff_factor: float = 0.3,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout = timeout
        self.session = session or requests.Session()
        retry = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.setdefault("User-Agent", "quantdance/tencent-finance-sdk")

    @staticmethod
    def normalize_symbol(raw: str) -> str:
        """统一 symbol 为 sh600519/sz000858/hk00700 形式。"""
        s = raw.strip().lower().replace(" ", "")
        if not s:
            raise TencentFinanceError("invalid_symbol", "symbol 不能为空")

        if re.fullmatch(r"(sh|sz)\d{6}", s):
            return s
        if re.fullmatch(r"hk\d{5}", s):
            return s

        m = re.fullmatch(r"(\d{6})\.(sh|sz|ss)", s)
        if m:
            code, market = m.groups()
            return ("sh" if market in ("sh", "ss") else "sz") + code

        m = re.fullmatch(r"(\d{5})\.hk", s)
        if m:
            return f"hk{m.group(1)}"

        m = re.fullmatch(r"(sh|sz|ss|hk)\.(\d+)", s)
        if m:
            market, code = m.groups()
            if market in ("sh", "ss") and re.fullmatch(r"\d{6}", code):
                return f"sh{code}"
            if market == "sz" and re.fullmatch(r"\d{6}", code):
                return f"sz{code}"
            if market == "hk" and re.fullmatch(r"\d{5}", code):
                return f"hk{code}"

        if re.fullmatch(r"\d{6}", s):
            if s.startswith(("5", "6", "9")):
                return f"sh{s}"
            return f"sz{s}"

        raise TencentFinanceError("invalid_symbol", f"不支持的 symbol 格式: {raw!r}")

    def _request_text(self, url: str, *, params: dict[str, Any] | None = None, symbol: str | None = None) -> str:
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:  # pragma: no cover - requests 自身分支
            raise TencentFinanceError("http_error", f"请求失败: {exc}", symbol=symbol, cause=exc) from exc
        if resp.status_code >= 400:
            raise TencentFinanceError(
                "http_status_error",
                f"HTTP 状态码异常: {resp.status_code}",
                symbol=symbol,
            )
        text = resp.text.strip()
        if not text:
            raise TencentFinanceError("empty_response", "响应为空", symbol=symbol)
        return text

    def _request_smartbox_text(self, keyword: str) -> str:
        try:
            resp = self.session.get(self.smartbox_url, params={"q": keyword, "t": "all"}, timeout=self.timeout)
        except requests.RequestException as exc:  # pragma: no cover - requests 自身分支
            raise TencentFinanceError("http_error", f"请求失败: {exc}", cause=exc) from exc
        if resp.status_code >= 400:
            raise TencentFinanceError("http_status_error", f"HTTP 状态码异常: {resp.status_code}")
        content = getattr(resp, "content", None)
        if content:
            text = bytes(content).decode("gbk", errors="ignore").strip()
        else:
            text = resp.text.strip()
        if not text:
            raise TencentFinanceError("empty_response", "响应为空")
        return text

    @staticmethod
    def _symbol_to_market(symbol: str) -> str:
        s = symbol.lower()
        if s.startswith("sh"):
            return "sh"
        if s.startswith("sz"):
            return "sz"
        if s.startswith("hk"):
            return "hk"
        return ""

    @staticmethod
    def _decode_search_value(value: str) -> str:
        s = value.strip()
        if "\\u" not in s:
            return s
        try:
            return s.encode("utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            return s

    @classmethod
    def _parse_search_fields(cls, fields: list[str]) -> StockSearchItem | None:
        if len(fields) < 2:
            return None

        first = fields[0].strip().lower()
        if not first:
            return None

        symbol = ""
        code = ""
        name = ""
        kind: str | None = None

        if first in ("sh", "sz", "hk") and len(fields) > 2:
            code = fields[1].strip()
            name = cls._decode_search_value(fields[2])
            if not re.fullmatch(r"\d{5,6}", code):
                return None
            symbol = f"{first}{code}"
            kind = fields[4].strip() if len(fields) > 4 and fields[4].strip() else None
        elif re.fullmatch(r"(sh|sz)\d{6}", first) or re.fullmatch(r"hk\d{5}", first):
            symbol = first
            code = first[2:]
            name = cls._decode_search_value(fields[1])
            kind = fields[3].strip() if len(fields) > 3 and fields[3].strip() else None
        elif re.fullmatch(r"\d{6}", first):
            code = first
            symbol = cls.normalize_symbol(code)
            name = cls._decode_search_value(fields[1])
            kind = fields[2].strip() if len(fields) > 2 and fields[2].strip() else None
        elif len(fields) > 2 and re.fullmatch(r"\d{6}", fields[2].strip()):
            code = fields[2].strip()
            name = cls._decode_search_value(fields[1])
            market = first if first in ("sh", "sz") else ""
            symbol = f"{market}{code}" if market else cls.normalize_symbol(code)
            kind = fields[3].strip() if len(fields) > 3 and fields[3].strip() else None
        else:
            return None

        if not name:
            return None

        return StockSearchItem(
            symbol=symbol,
            code=code,
            name=name,
            market=cls._symbol_to_market(symbol),
            type=kind,
            raw_fields=fields,
        )

    def _parse_search_text(self, text: str, *, limit: int) -> list[StockSearchItem]:
        match = re.search(r'v_hint="(.*?)";?', text, flags=re.S)
        payload = match.group(1) if match else text
        rows = [row for row in re.split(r"[\^\n\r]+", payload) if row.strip()]
        items: list[StockSearchItem] = []
        seen: set[str] = set()
        for row in rows:
            fields = [field.strip() for field in row.split("~")]
            item = self._parse_search_fields(fields)
            if item is None or item["symbol"] in seen:
                continue
            seen.add(item["symbol"])
            items.append(item)
            if len(items) >= limit:
                break
        return items

    def _parse_quote_text(self, text: str) -> dict[str, QuoteData]:
        out: dict[str, QuoteData] = {}
        for raw_symbol, payload in _QUOTE_RE.findall(text):
            fields = payload.split("~")
            symbol = raw_symbol.strip().lower()
            if not fields:
                continue
            out[symbol] = QuoteData(
                symbol=symbol,
                name=_safe_get(fields, 1) or "",
                code=_safe_get(fields, 2) or "",
                price=_to_float(_safe_get(fields, 3)),
                prev_close=_to_float(_safe_get(fields, 4)),
                open=_to_float(_safe_get(fields, 5)),
                high=_to_float(_safe_get(fields, 33)),
                low=_to_float(_safe_get(fields, 34)),
                volume=_to_int(_safe_get(fields, 36)),
                amount=_to_float(_safe_get(fields, 37)),
                timestamp=_safe_get(fields, 30),
                raw_fields=fields,
            )
        if not out:
            raise TencentFinanceError("parse_error", "未解析到任何行情字段")
        return out

    def _parse_valuation_fields(self, symbol: str, fields: list[str]) -> ValuationData:
        # 腾讯字段索引易变，但该索引组是项目既有约定：
        # 39=PE(TTM), 44=总市值, 45=流通市值, 46=PB, 47=涨停, 48=跌停
        return ValuationData(
            symbol=symbol,
            pe_ttm=_to_float(_safe_get(fields, 39)),
            pb=_to_float(_safe_get(fields, 46)),
            market_cap=_to_float(_safe_get(fields, 44)),
            float_market_cap=_to_float(_safe_get(fields, 45)),
            turnover_rate=_to_float(_safe_get(fields, 38)),
            limit_up=_to_float(_safe_get(fields, 47)),
            limit_down=_to_float(_safe_get(fields, 48)),
            raw_fields=fields,
        )

    def get_quote(self, symbol: str) -> QuoteData:
        norm = self.normalize_symbol(symbol)
        data = self.get_quotes([norm])
        item = data.get(norm)
        if item is None:
            raise TencentFinanceError("symbol_not_found", "未返回指定 symbol 行情", symbol=norm)
        return item

    def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        if not symbols:
            raise TencentFinanceError("invalid_argument", "symbols 不能为空")
        norm_symbols = [self.normalize_symbol(s) for s in symbols]
        q = ",".join(norm_symbols)
        text = self._request_text(self.quote_url, params={"q": q})
        parsed = self._parse_quote_text(text)
        return {symbol: parsed[symbol] for symbol in norm_symbols if symbol in parsed}

    def search_stocks(self, keyword: str, *, limit: int = 10) -> list[StockSearchItem]:
        q = keyword.strip()
        if not q:
            raise TencentFinanceError("invalid_argument", "搜索关键字不能为空")
        if limit < 1:
            raise TencentFinanceError("invalid_argument", "limit 必须大于 0")
        text = self._request_smartbox_text(q)
        return self._parse_search_text(text, limit=limit)

    def get_valuation(self, symbol: str) -> ValuationData:
        norm = self.normalize_symbol(symbol)
        data = self.get_valuations([norm])
        item = data.get(norm)
        if item is None:
            raise TencentFinanceError("symbol_not_found", "未返回指定 symbol 估值", symbol=norm)
        return item

    def get_valuations(self, symbols: list[str]) -> dict[str, ValuationData]:
        quotes = self.get_quotes(symbols)
        return {
            symbol: self._parse_valuation_fields(symbol, quote["raw_fields"])
            for symbol, quote in quotes.items()
        }

    def get_kline(
        self,
        symbol: str,
        period: Literal["day", "week", "month"] = "day",
        *,
        count: int = 320,
        fq: str = "qfq",
    ) -> list[KlineBar]:
        norm = self.normalize_symbol(symbol)
        api_period = _PERIOD_MAP.get(period)
        if api_period is None:
            raise TencentFinanceError("invalid_period", f"不支持 period: {period}", symbol=norm)
        param = f"{norm},{api_period},,,{count},{fq}"
        text = self._request_text(self.kline_url, params={"param": param}, symbol=norm)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TencentFinanceError("parse_error", "K 线响应不是合法 JSON", symbol=norm, cause=exc) from exc

        data = payload.get("data")
        if not isinstance(data, dict) or norm not in data:
            raise TencentFinanceError("parse_error", "K 线响应缺少 symbol 数据", symbol=norm)

        entry = data[norm]
        if not isinstance(entry, dict):
            raise TencentFinanceError("parse_error", "K 线数据结构异常", symbol=norm)

        candidates = [f"{fq}{api_period}", api_period]
        rows: list[list[str]] = []
        for key in candidates:
            value = entry.get(key)
            if isinstance(value, list):
                rows = value
                break
        if not rows:
            raise TencentFinanceError("empty_response", "K 线数据为空", symbol=norm)

        bars: list[KlineBar] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            bars.append(
                KlineBar(
                    date=str(row[0]),
                    open=_to_float(str(row[1])),
                    close=_to_float(str(row[2])),
                    high=_to_float(str(row[3])),
                    low=_to_float(str(row[4])),
                    volume=_to_int(str(row[5])),
                )
            )

        if not bars:
            raise TencentFinanceError("parse_error", "K 线解析后为空", symbol=norm)
        return bars

    def _parse_fund_flow_fields(self, symbol: str, fields: list[str]) -> FundFlowData:
        return FundFlowData(
            symbol=symbol,
            main_inflow=_to_float(_safe_get(fields, 5)),
            main_outflow=_to_float(_safe_get(fields, 6)),
            net_inflow=_to_float(_safe_get(fields, 7)),
            main_ratio=_to_float(_safe_get(fields, 8)),
            retail_ratio=_to_float(_safe_get(fields, 9)),
            raw_fields=fields,
        )

    def get_fund_flow(self, symbol: str) -> FundFlowData:
        norm = self.normalize_symbol(symbol)
        text = self._request_text(self.quote_url, params={"q": f"ff_{norm}"}, symbol=norm)
        records = _QUOTE_RE.findall(text)
        if not records:
            raise TencentFinanceError("parse_error", "资金流向响应解析失败", symbol=norm)
        _, payload = records[0]
        fields = payload.split("~")
        if len(fields) < 5:
            raise TencentFinanceError("parse_error", "资金流向字段不足", symbol=norm)
        return self._parse_fund_flow_fields(norm, fields)

