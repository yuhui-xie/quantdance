"""腾讯财经 SDK 测试（全部基于 mock，不依赖外网）。"""

from __future__ import annotations

import json

import pytest

from app.data_sources.tencent_finance_sdk import TencentFinanceError, TencentFinanceSDK


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, object]] = []
        self.headers: dict[str, str] = {}

    def mount(self, *_args, **_kwargs) -> None:  # requests.Session 兼容
        return None

    def get(self, url, params=None, timeout=None):  # noqa: ANN001
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if not self._responses:
            return _FakeResponse("", status_code=500)
        return self._responses.pop(0)


def _quote_line(symbol: str, name: str, code: str, price: str = "1888.88") -> str:
    fields = [
        "51",
        name,
        code,
        price,
        "1870.00",
        "1875.00",
    ]
    # 补齐到常见索引位，便于测试 high/low/volume/amount/timestamp 解析
    while len(fields) <= 37:
        fields.append("")
    fields[30] = "20260524150001"
    fields[33] = "1899.00"
    fields[34] = "1860.00"
    fields[36] = "123456"
    fields[37] = "99999999.0"
    return f'v_{symbol}="' + "~".join(fields) + '";'

def _valuation_quote_line(symbol: str, name: str, code: str) -> str:
    fields = ["51", name, code, "1888.88", "1870.00", "1875.00"]
    while len(fields) <= 48:
        fields.append("")
    fields[38] = "0.88"
    fields[39] = "21.5"
    fields[44] = "2400000000000"
    fields[45] = "1900000000000"
    fields[46] = "5.66"
    fields[47] = "2050.00"
    fields[48] = "1670.00"
    return f'v_{symbol}="' + "~".join(fields) + '";'


def test_normalize_symbol_variants():
    sdk = TencentFinanceSDK(session=_FakeSession([]))
    assert sdk.normalize_symbol("600519") == "sh600519"
    assert sdk.normalize_symbol("000858") == "sz000858"
    assert sdk.normalize_symbol("600519.SH") == "sh600519"
    assert sdk.normalize_symbol("hk00700") == "hk00700"
    assert sdk.normalize_symbol("00700.HK") == "hk00700"


def test_get_quote_parses_tilde_payload():
    fake_text = _quote_line("sh600519", "贵州茅台", "600519", "1888.88")
    sdk = TencentFinanceSDK(session=_FakeSession([_FakeResponse(fake_text)]))

    quote = sdk.get_quote("600519")

    assert quote["symbol"] == "sh600519"
    assert quote["name"] == "贵州茅台"
    assert quote["price"] == pytest.approx(1888.88)
    assert quote["high"] == pytest.approx(1899.0)
    assert quote["low"] == pytest.approx(1860.0)
    assert quote["volume"] == 123456
    assert quote["amount"] == pytest.approx(99999999.0)
    assert quote["timestamp"] == "20260524150001"


def test_get_quotes_returns_symbol_mapping():
    payload = _quote_line("sh600519", "贵州茅台", "600519") + _quote_line("sz000858", "五粮液", "000858", "130.25")
    sdk = TencentFinanceSDK(session=_FakeSession([_FakeResponse(payload)]))

    out = sdk.get_quotes(["600519", "000858"])

    assert list(out.keys()) == ["sh600519", "sz000858"]
    assert out["sh600519"]["name"] == "贵州茅台"
    assert out["sz000858"]["price"] == pytest.approx(130.25)


def test_search_stocks_parses_smartbox_payload():
    payload = 'v_hint="sh~600519~\\u8d35\\u5dde\\u8305\\u53f0~gzmt~GP-A^000858~五粮液~GP-A";'
    sdk = TencentFinanceSDK(session=_FakeSession([_FakeResponse(payload)]))

    out = sdk.search_stocks("茅台", limit=10)

    assert out[0]["symbol"] == "sh600519"
    assert out[0]["code"] == "600519"
    assert out[0]["name"] == "贵州茅台"
    assert out[0]["market"] == "sh"
    assert out[1]["symbol"] == "sz000858"
    assert out[1]["name"] == "五粮液"


def test_search_stocks_honors_limit():
    payload = 'v_hint="sh600519~贵州茅台~600519~GP-A^sz000858~五粮液~000858~GP-A";'
    sdk = TencentFinanceSDK(session=_FakeSession([_FakeResponse(payload)]))

    out = sdk.search_stocks("酒", limit=1)

    assert len(out) == 1
    assert out[0]["symbol"] == "sh600519"


def test_get_kline_parses_json_rows():
    payload = {
        "code": 0,
        "data": {
            "sh600519": {
                "qfqday": [
                    ["2026-05-20", "1800.0", "1812.0", "1820.0", "1790.0", "10000"],
                    ["2026-05-21", "1812.0", "1825.0", "1838.0", "1808.0", "12000"],
                ]
            }
        },
    }
    sdk = TencentFinanceSDK(session=_FakeSession([_FakeResponse(json.dumps(payload))]))

    bars = sdk.get_kline("600519", "day")

    assert len(bars) == 2
    assert bars[0]["date"] == "2026-05-20"
    assert bars[1]["close"] == pytest.approx(1825.0)
    assert bars[1]["volume"] == 12000


def test_get_fund_flow_parses_response():
    fields = ["", "", "", "", "", "88.8", "77.7", "11.1", "3.5", "-1.2"]
    payload = 'v_ff_sh600519="' + "~".join(fields) + '";'
    sdk = TencentFinanceSDK(session=_FakeSession([_FakeResponse(payload)]))

    flow = sdk.get_fund_flow("600519")

    assert flow["symbol"] == "sh600519"
    assert flow["main_inflow"] == pytest.approx(88.8)
    assert flow["main_outflow"] == pytest.approx(77.7)
    assert flow["net_inflow"] == pytest.approx(11.1)

def test_get_valuation_parses_expected_index_fields():
    payload = _valuation_quote_line("sh600519", "贵州茅台", "600519")
    sdk = TencentFinanceSDK(session=_FakeSession([_FakeResponse(payload)]))
    valuation = sdk.get_valuation("600519")

    assert valuation["symbol"] == "sh600519"
    assert valuation["pe_ttm"] == pytest.approx(21.5)
    assert valuation["pb"] == pytest.approx(5.66)  # 注意 PB 使用 46，不是 43
    assert valuation["market_cap"] == pytest.approx(2400000000000.0)
    assert valuation["float_market_cap"] == pytest.approx(1900000000000.0)
    assert valuation["turnover_rate"] == pytest.approx(0.88)
    assert valuation["limit_up"] == pytest.approx(2050.0)
    assert valuation["limit_down"] == pytest.approx(1670.0)


def test_error_on_empty_response():
    sdk = TencentFinanceSDK(session=_FakeSession([_FakeResponse("")]))
    with pytest.raises(TencentFinanceError, match="empty_response"):
        sdk.get_quote("600519")


def test_error_on_invalid_kline_json():
    sdk = TencentFinanceSDK(session=_FakeSession([_FakeResponse("not-json")]))
    with pytest.raises(TencentFinanceError, match="parse_error"):
        sdk.get_kline("600519", "day")
