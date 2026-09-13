"""mootdx 行情适配层。

负责：
- 统一 symbol/period 规范
- 隔离 mootdx 原始返回结构差异
- 输出项目内部统一字段
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, TypedDict

import pandas as pd

# ---------------------------------------------------------------------------
# pandas 3.0 移除了 DataFrame/Series.fillna(method=...)，而 mootdx 0.11.x 的
# 前复权计算（tools/reversion.py、utils/adjust.py）仍依赖它。这里安装一个兼容
# 垫片，把 method='ffill'/'bfill' 转译为 ffill()/bfill()，仅恢复被移除的关键字
# 语义，不改变其他行为；无 method 时原样走原实现。仅在数据层导入时生效。
# ---------------------------------------------------------------------------
def _install_fillna_method_shim() -> None:
    try:
        import pandas as pd
    except Exception:  # pragma: no cover - 无 pandas 时不需垫片
        return
    if getattr(pd.DataFrame.fillna, "_qd_fillna_shim", False):
        return
    _orig_df_fillna = pd.DataFrame.fillna
    _orig_s_fillna = pd.Series.fillna

    def _shim(orig, frame, value=None, *, method=None, axis=None, inplace=False, limit=None, **kw):
        if method is None:
            return orig(frame, value=value, axis=axis, inplace=inplace, limit=limit, **kw)
        if value is not None:
            raise TypeError("fillna(): value 与 method 不能同时指定")
        if method not in ("ffill", "bfill"):
            return orig(frame, value=value, axis=axis, inplace=inplace, limit=limit, **kw)
        if inplace:
            getattr(frame, method)(axis=axis, limit=limit, inplace=True)
            return None
        return getattr(frame, method)(axis=axis, limit=limit)

    def _df_shim(self, *args, **kw):
        return _shim(_orig_df_fillna, self, *args, **kw)

    def _s_shim(self, *args, **kw):
        return _shim(_orig_s_fillna, self, *args, **kw)

    _df_shim._qd_fillna_shim = True  # type: ignore[attr-defined]
    _s_shim._qd_fillna_shim = True  # type: ignore[attr-defined]
    pd.DataFrame.fillna = _df_shim  # type: ignore[method-assign]
    pd.Series.fillna = _s_shim  # type: ignore[method-assign]


_install_fillna_method_shim()


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

# TDX 协议单次 K 线请求最多约 800 根；超过时须按 start 偏移向前翻页拼接。
_KLINE_PAGE_SIZE = 800

# K 线复权口径：qfq=前复权。mootdx 自带 adjust='qfq' 有除权断层缺陷，
# 本项目改为拉原始价 + 本地按除权记录折算（详见 docs/data-adjustment.md）。
# 该常量仅作为缓存中的复权语义标记使用。
_KLINE_ADJUST = "qfq"

# ---------------------------------------------------------------------------
# TDX 行情节点选择
#
# mootdx 的 bestip 只测 **TCP 连接耗时**，不验证节点是否真的回 K 线（`valid_server`
# 更是只校验元组格式）。实测内置 104 个节点：8 个能回 K 线、23 个属于「TCP 能连但
# K 线只回 2 字节空包」的僵尸、71 个完全不通；僵尸的 TCP 延迟中位数 28ms，而可用
# 节点是 24~40ms —— 按延迟升序挑，僵尸稳定排在前列。于是 `~/.mootdx/config.json`
# 里缓存的 BESTIP 落到 `180.153.18.172:80`（列表里的 "上海双线主站Z80"，只开 80
# 端口、不服务 K 线的入口），而 `StdQuotes.__init__` 无条件读 BESTIP，所有 K 线请求
# 都打在这台僵尸上：现象是「网络正常、股票列表/股票数都有响应，但一只票都取不到
# K 线」，最终由上层报 `未能加载任何日线行情数据`。
#
# 这里改为**用数据探活**选点：候选节点必须真的返回一根 K 线才算可用，选中的节点
# 落盘缓存（带 TTL），并在进程内把它写回 mootdx 的 BESTIP（见 `_publish_node`）。
#
# 另有一个坑：探活和真正建连是**两次独立连接**。探活刚通过的节点，紧接着
# `Quotes.factory` 就可能建连超时（节点抖动 / 限制连入），所以选点不是一个「选出
# 唯一答案」的函数，而是一串按优先级排好的候选（见 `_candidate_nodes`），由
# `_connect_node` 一路试到建连成功为止。
# ---------------------------------------------------------------------------
_PROBE_MARKET = 1  # 探活标的：长历史个股，任何可用节点都必有数据
_PROBE_CODE = "600519"
_PROBE_CATEGORY = 9  # 日线
_PROBE_COUNT = 10
_DEFAULT_NODE_PORT = 7709
_NODE_ENV = "QUANTDANCE_TDX_NODE"  # 显式指定 "ip:port"，跳过探测
_NODE_CACHE_NAME = "tdx_node.json"  # 选中节点缓存，落在行情缓存同目录
_NODE_TTL_SECONDS = 6 * 3600  # 缓存节点的复用时长
_NODE_TCP_TIMEOUT = 1.2  # 候选排序用的 TCP 探测超时
_NODE_TCP_WORKERS = 32
_NODE_DATA_TIMEOUT = 5.0  # 数据探活的连接超时
_NODE_SCAN_LIMIT = 40  # 单次扫描最多数据探活多少个候选
_NODE_RECHECK_SECONDS = 60.0  # 运行中复检节点的最小间隔
_NODE_CONNECT_TIMEOUT = 5  # 建连超时；失败要尽快换下一个候选
_NODE_READ_TIMEOUT = 15  # 建连后的收发超时（mootdx StdQuotes 的默认值）


def _node_cache_path() -> Path:
    """选中节点的缓存路径（与 `AStockDataSDK` 的行情缓存同目录）。

    这里不反向 import `a_stock_data`（它会 import 本模块，成环），故复用同一套路径
    规则：`QUANTDANCE_A_STOCK_DATA_CACHE_DIR` 覆盖，否则 `backend/data/a_stock_data`。
    与行情缓存不同，节点缓存不受 `QUANTDANCE_A_STOCK_DATA_CACHE=0` 影响——关掉行情
    缓存不该让每次运行都全量重扫节点。
    """
    raw = os.getenv("QUANTDANCE_A_STOCK_DATA_CACHE_DIR")
    base = (
        Path(raw).expanduser()
        if raw
        else Path(__file__).resolve().parents[2] / "data" / "a_stock_data"
    )
    return base / _NODE_CACHE_NAME


def _parse_node(raw: str) -> tuple[str, int]:
    """把 "ip:port" / "ip" 解析为 `(ip, port)`（缺省端口 7709）。"""
    text = str(raw).strip()
    if not text:
        raise ValueError("节点地址不能为空")
    host, _, port_text = text.partition(":")
    if not host:
        raise ValueError(f"无效节点地址: {raw!r}")
    port = int(port_text) if port_text else _DEFAULT_NODE_PORT
    if not 0 < port < 65536:
        raise ValueError(f"无效端口: {port}")
    return host, port


def _read_cached_node() -> tuple[str, int] | None:
    """读取仍在 TTL 内的选中节点；缺失/过期/损坏一律返回 None。"""
    try:
        payload = json.loads(_node_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        cached_at = float(payload.get("validated_at", 0))
    except (TypeError, ValueError):
        return None
    if time.time() - cached_at > _NODE_TTL_SECONDS:
        return None
    try:
        return str(payload["ip"]), int(payload["port"])
    except (KeyError, TypeError, ValueError):
        return None


def _write_cached_node(ip: str, port: int, *, rtt_ms: float | None = None) -> None:
    path = _node_cache_path()
    payload = {
        "type": "tdx_node",
        "ip": ip,
        "port": int(port),
        "rtt_ms": rtt_ms,
        "validated_at": time.time(),
        "validated_on": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tmp.replace(path)
    except OSError:
        return


def _publish_node(ip: str, port: int) -> None:
    """把选中节点写到 mootdx 的 BESTIP（内存 + 配置文件）。

    必须写：mootdx 内部还有别的入口自己读配置建连——`get_xdxr` 就是
    `Quotes.factory('std').xdxr(...)`，只 pin 我们自己的 client 不够，它仍会连僵尸。
    僵尸节点上 `xdxr` 返回空表，`_get_xdxr_info` 会把它当成「无除权记录」，于是原始
    未复权价被当作 qfq 静默写进缓存（正是该函数注释里警告的失效模式）。写 BESTIP
    等价于 mootdx `bestip -w` 的落盘动作，只是判据从「TCP 通」换成了「真的能回 K 线」。
    """
    try:
        from mootdx import config as mootdx_config
    except Exception:  # pragma: no cover - mootdx 缺失时不阻断（本模块其它路径会给错）
        return
    try:
        bestip = dict(mootdx_config.get("BESTIP") or {})
        if list(bestip.get("HQ") or []) == [ip, int(port)]:
            return
        bestip["HQ"] = [ip, int(port)]
        mootdx_config.set("BESTIP", bestip)
    except Exception:  # pragma: no cover - 内存写入失败不该影响取数
        return

    # 落盘：让其它进程/其它 mootdx 用法也脱离僵尸节点。
    try:
        path = Path(mootdx_config.CONF)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        payload.setdefault("BESTIP", {})["HQ"] = [ip, int(port)]
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception:  # pragma: no cover - 配置落盘失败不影响本次运行
        return


def _builtin_nodes() -> list[tuple[str, int]]:
    """内置候选节点池：tdxpy 的 hq_hosts + mootdx 自带的 HQ_HOSTS / 配置文件，去重。"""
    rows: list[Any] = []
    try:
        from tdxpy.constants import hq_hosts

        rows.extend(hq_hosts)
    except Exception:  # pragma: no cover - 依赖缺失时不阻断
        pass
    try:
        from mootdx.consts import HQ_HOSTS

        rows.extend(HQ_HOSTS)
    except Exception:  # pragma: no cover
        pass
    try:
        from mootdx import config as mootdx_config

        rows.extend(mootdx_config.get("SERVER", {}).get("HQ") or [])
    except Exception:  # pragma: no cover
        pass

    nodes: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        try:
            ip, port = str(row[1]), int(row[2])
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if (ip, port) in seen:
            continue
        seen.add((ip, port))
        nodes.append((ip, port))
    return nodes


def _tcp_rtt(ip: str, port: int, timeout: float = _NODE_TCP_TIMEOUT) -> float | None:
    """TCP 连接耗时（毫秒）；连不上返回 None。

    只用于给扫描排序，**不作为可用判据**：僵尸节点的 TCP 延迟比可用节点还低。
    """
    started = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return (time.monotonic() - started) * 1000.0
    except OSError:
        return None


def _node_has_data(ip: str, port: int, *, timeout: float = _NODE_DATA_TIMEOUT) -> bool:
    """节点是否真的能回 K 线：发一次日线探活请求，拿到非空记录才算可用。

    僵尸节点在这一步露馅——`get_security_count` / `get_security_list` 都有响应，
    唯独 `get_security_bars` 回 2 字节空包（记录数被写成 800 却没有记录体），
    `tdxpy` 解析时抛 `struct.error` 并吞成 None。
    """
    try:
        from tdxpy.hq import TdxHq_API
    except Exception:  # pragma: no cover - 依赖缺失
        return False
    api = TdxHq_API()
    try:
        if not api.connect(ip, int(port), time_out=timeout):
            return False
        bars = api.get_security_bars(
            _PROBE_CATEGORY, _PROBE_MARKET, _PROBE_CODE, 0, _PROBE_COUNT
        )
    except Exception:
        return False
    finally:
        try:
            api.disconnect()
        except Exception:  # pragma: no cover
            pass
    return bool(bars)


_announced_node: tuple[str, int] | None = None


def _announce_node(node: tuple[str, int], *, quiet: bool) -> None:
    """节点变化时才往 stderr 打一行。

    股票池加载会给每只标的建一个 SDK，若每次都打就成了刷屏——几百行会把
    「加载行情」的原地进度条冲得七零八落。节点没变就没什么可说的。
    """
    global _announced_node
    if quiet or node == _announced_node:
        return
    _announced_node = node
    print(f"[tdx] 使用节点 {node[0]}:{node[1]}", file=sys.stderr, flush=True)


def _scan_nodes(*, quiet: bool) -> Iterator[tuple[tuple[str, int], float | None]]:
    """按 TCP 延迟升序逐个数据探活，**逐个产出**真的能回 K 线的节点（惰性）。

    惰性很关键：探活只证明「刚才能取到数」，调用方还得自己建连。第一个探活通过的
    节点可能偏偏建连超时，此时应当继续消费下一个，而不是让整次运行失败。
    """
    candidates = _builtin_nodes()
    if not candidates:
        return

    with ThreadPoolExecutor(max_workers=min(_NODE_TCP_WORKERS, len(candidates))) as pool:
        rtts = list(pool.map(lambda n: _tcp_rtt(*n), candidates))
    reachable = sorted(
        ((rtt, node) for rtt, node in zip(rtts, candidates) if rtt is not None),
        key=lambda item: item[0],
    )
    if not reachable:
        return

    if not quiet:
        print(
            f"[tdx] 节点探活：{len(candidates)} 个候选、{len(reachable)} 个 TCP 可达，"
            f"逐个验证能否真的回 K 线…",
            file=sys.stderr,
            flush=True,
        )
    for tried, (rtt, node) in enumerate(reachable[:_NODE_SCAN_LIMIT], start=1):
        if _node_has_data(*node):
            if not quiet:
                print(
                    f"[tdx] 探活通过 {node[0]}:{node[1]}（第 {tried} 个候选，"
                    f"TCP {rtt:.0f}ms）",
                    file=sys.stderr,
                    flush=True,
                )
            yield node, rtt


def _candidate_nodes(
    explicit: str | None = None,
    *,
    force: bool = False,
    quiet: bool = False,
    skip: tuple[tuple[str, int], ...] = (),
) -> Iterator[tuple[str, int]]:
    """按优先级**逐个产出**数据探活通过的候选节点，并写回 mootdx BESTIP。

    顺序：显式指定 → 环境变量 `QUANTDANCE_TDX_NODE` → 未过期的缓存 → 全量扫描。
    `force=True` 跳过缓存（换点场景：缓存里大概率就是刚失效的那个）。`skip` 里的
    节点直接略过，用于「换一个，别再给我当前这个」。

    注意探活与建连是**两次独立连接**：探活通过不代表建连一定成功，调用方必须对
    `_create_client` 的失败保持容忍并继续取下一个候选。
    """
    skipped = set(skip)
    seen: set[tuple[str, int]] = set()

    for source, raw in (
        ("node=", explicit),
        (_NODE_ENV, os.getenv(_NODE_ENV)),
    ):
        if not raw:
            continue
        try:
            node = _parse_node(raw)
        except ValueError as exc:
            raise MootdxMarketError("invalid_node", f"{source} 指定的节点无效: {exc}") from exc
        if node in seen or node in skipped:
            continue
        seen.add(node)
        if _node_has_data(*node):
            _publish_node(*node)
            _write_cached_node(*node)
            yield node
        elif not quiet:
            print(
                f"[tdx] {source} 指定的节点 {node[0]}:{node[1]} 取不到 K 线，"
                f"忽略并自动选点",
                file=sys.stderr,
                flush=True,
            )

    if not force:
        cached = _read_cached_node()
        if cached is not None and cached not in seen and cached not in skipped:
            seen.add(cached)
            if _node_has_data(*cached):
                _publish_node(*cached)
                yield cached

    for node, rtt in _scan_nodes(quiet=quiet):
        if node in seen or node in skipped:
            continue
        seen.add(node)
        _publish_node(*node)
        _write_cached_node(*node, rtt_ms=rtt)
        yield node


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        # 复权或当日未成交等情况下可能产生 NaN，统一视为缺值。
        if f != f:  # NaN 判定
            return None
        return f
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

    def __init__(
        self,
        *,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
        node: str | None = None,
        node_quiet: bool = False,
    ) -> None:
        """初始化 SDK。

        默认走**数据探活选点**（见模块顶部「TDX 行情节点选择」）：先取显式 `node` /
        环境变量 / 缓存节点，都不行才全量扫描，并把选中的节点写回 mootdx BESTIP。
        注入 `client` / `client_factory` 时完全跳过选点（测试或无网场景）。
        """
        self._node: tuple[str, int] | None = None
        self._node_ok_until = 0.0
        self.client: Any = None
        if client is not None:
            self.client = client
            return
        if client_factory is not None:
            self.client = client_factory()
            return
        self._connect_node(node, quiet=node_quiet)

    @property
    def node(self) -> tuple[str, int] | None:
        """当前连接的 TDX 节点；注入外部 client 时为 None。"""
        return self._node

    def _connect_node(
        self,
        explicit: str | None = None,
        *,
        force: bool = False,
        quiet: bool = False,
        skip: bool = False,
    ) -> None:
        """选点并建连：沿候选序列一路试到**建连成功**为止。

        探活只管「能不能回 K 线」，建连可能单独失败（节点抖动/限制连入），所以不能
        在第 1 个候选上一次性押注——否则就是 `init_error` 直接终止整轮取数。
        """
        blocked = (self._node,) if (skip and self._node) else ()
        last_error: MootdxMarketError | None = None
        for candidate in _candidate_nodes(
            explicit, force=force, quiet=quiet, skip=blocked
        ):
            try:
                client = self._create_client(candidate)
            except MootdxMarketError as exc:
                last_error = exc
                if not quiet:
                    print(
                        f"[tdx] 节点 {candidate[0]}:{candidate[1]} 探活通过但建连失败"
                        f"（{exc}），换下一个",
                        file=sys.stderr,
                        flush=True,
                    )
                continue
            self._swap_client(candidate, client)
            _announce_node(candidate, quiet=quiet)
            return

        if last_error is not None:
            raise MootdxMarketError(
                "init_error",
                f"mootdx 客户端初始化失败: {last_error}",
                cause=last_error,
            )
        raise MootdxMarketError(
            "no_node",
            "未找到任何能返回 K 线的 TDX 节点（候选池内的节点均不可用）",
        )

    def _swap_client(self, node: tuple[str, int], client: Any) -> None:
        """换到新节点：关掉旧连接，避免换点/重扫时泄漏 socket。"""
        previous = self.client
        self._node = node
        self.client = client
        self._node_ok_until = time.monotonic() + _NODE_RECHECK_SECONDS
        close = getattr(previous, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - 关旧连接失败无所谓
                pass

    def _node_seems_dead(self) -> bool:
        """空结果是否该归咎于节点：用探活标的复检（带最小间隔，避免逐票都去探）。

        返回 False 表示「节点没问题，空结果就是这个标的本身没数据」。
        """
        if self._node is None:
            return False
        now = time.monotonic()
        if now < self._node_ok_until:
            return False
        if _node_has_data(*self._node):
            self._node_ok_until = now + _NODE_RECHECK_SECONDS
            return False
        return True

    def _reseat_node(self) -> bool:
        """当前节点失效时换一个：重新选点并重建客户端；失败返回 False。"""
        try:
            self._connect_node(force=True, skip=True)
        except MootdxMarketError:
            return False
        return True

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
    def _create_client(node: tuple[str, int]) -> Any:
        """按节点建 mootdx 客户端。

        必须显式传 `server=`：`StdQuotes.__init__` 的 `ip`/`port` kwargs 根本没人接
        （只认 `server=(ip, port)`），不传就落回配置里的 BESTIP —— 也就是僵尸节点。

        同时把建连超时压到 5s（tdxpy 自己的默认值，mootdx 把它放宽到了 15s）：建连
        失败要尽快换下一个候选。`connect()` 的 `time_out` 会被 `settimeout` 一直沿用到
        后续收发，所以连上之后再调回常规值，避免大页取数被误判成超时。
        """
        try:
            from mootdx.quotes import Quotes  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - 依赖缺失分支
            raise MootdxMarketError("dependency_error", "未安装或无法导入 mootdx", cause=exc) from exc

        ip, port = node
        try:
            client = Quotes.factory(
                market="std", server=(ip, int(port)), timeout=_NODE_CONNECT_TIMEOUT
            )
        except Exception as exc:
            raise MootdxMarketError(
                "init_error",
                f"mootdx 客户端初始化失败: {exc}",
                symbol=f"{ip}:{port}",
                cause=exc,
            ) from exc

        # mootdx 默认 `raise_exception=False`：connect 超时只是在 tdxpy 内部 `return
        # False`，而 `StdQuotes.__init__` 又把这个返回值丢掉，于是「没连上」会被当成
        # 建连成功，直到第一次取数才暴露。这里补一次显式确认。
        if getattr(getattr(client, "client", None), "closed", False):
            raise MootdxMarketError(
                "init_error", f"连接 {ip}:{port} 未建立", symbol=f"{ip}:{port}"
            )

        # 建连成功，把刚才为「快速换点」压短的超时调回常规值：tdxpy 的 `connect()` 用
        # `settimeout()` 设超时，会一直沿用到后续所有收发，5s 对大页取数偏紧。内部结构
        # 变了就跳过（退化成 tdxpy 自己的默认 5s，不会更糟）。
        try:
            client.client.client.settimeout(_NODE_READ_TIMEOUT)
        except Exception:  # pragma: no cover - 仅在 tdxpy 内部结构变化时触发
            pass
        return client

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
        """获取 **前复权** 日线；超过单页上限（约 800 根）时按 start 偏移向前翻页拼接。

        mootdx/TDX 协议单次请求最多约 800 根，`count` 更大时若不翻页会被截断到
        约 3 年历史。这里从最新页向前逐页取，按 datetime 去重，返回最近 ``count``
        根（时间升序）。

        注意：mootdx 自带的 `adjust='qfq'` 依赖新浪预计算因子表且按**乘**因子处理，
        会在分红除权日留下虚假价格断层（见 docs/data-adjustment.md）。因此这里拉取**原始价**，
        再用 TDX 本地除权记录按教科书前复权公式自行折算，保证序列连续。

        取到空结果时会先用探活标的复检**节点**是否还能回数据（僵尸节点的表现就是
        「TCP 通、K 线回空包」），确认节点失效则换点重试一次。
        """
        bars = self._load_klines(symbol, period, count)
        if bars or not self._node_seems_dead():
            return bars
        if self._reseat_node():
            return self._load_klines(symbol, period, count)
        return bars

    def _load_klines(
        self,
        symbol: str,
        period: Literal["day", "week", "month", "1m", "5m", "15m", "30m", "60m"],
        count: int,
    ) -> list[MootdxKlineBar]:
        """按当前 client 拉取并前复权；不做节点复检（由 :meth:`get_klines` 负责）。"""
        norm = self._parse_symbol(symbol)
        category = self.map_period(period)

        def fetch_page(start_offset: int) -> list[MootdxKlineBar]:
            try:
                method = getattr(self.client, "bars", None)
                if callable(method):
                    try:
                        raw = method(
                            norm.code,
                            frequency=category,
                            start=start_offset,
                            offset=_KLINE_PAGE_SIZE,
                        )
                    except TypeError:
                        raw = method(
                            category, norm.market, norm.code, start_offset, _KLINE_PAGE_SIZE
                        )
                else:
                    raw = self._call_first(
                        ("get_security_bars", "get_kline"),
                        category,
                        norm.market,
                        norm.code,
                        start_offset,
                        _KLINE_PAGE_SIZE,
                    )
            except MootdxMarketError:
                raise
            except Exception as exc:
                raise MootdxMarketError(
                    "source_error", f"获取 K 线失败: {exc}", symbol=norm.symbol, cause=exc
                ) from exc

            bars: list[MootdxKlineBar] = []
            for item in _to_records(raw, label="K 线", symbol=norm.symbol):
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

        # start 递增得到的是更新的历史，需先取完再从旧到新拼接。
        pages: list[list[MootdxKlineBar]] = []
        start_offset = 0
        fetched = 0
        while fetched < count:
            page = fetch_page(start_offset)
            if not page:
                break
            pages.append(page)
            fetched += len(page)
            if len(page) < _KLINE_PAGE_SIZE:
                break  # 已到历史起点，无更早数据
            start_offset += _KLINE_PAGE_SIZE

        all_bars: list[MootdxKlineBar] = []
        seen: set[str] = set()
        for page in reversed(pages):  # 旧页在前
            for bar in page:
                key = bar["datetime"] or ""
                if not key or key in seen:
                    continue
                seen.add(key)
                all_bars.append(bar)
        raw_bars = all_bars[-count:]
        return self._forward_adjust_bars(norm.symbol, raw_bars)

    def _get_xdxr_info(self, symbol: str) -> pd.DataFrame | None:
        """获取除权除息记录，列为 fenhong/peigu/peigujia/songzhuangu。

        category==1 为分红/送转；category==11 为 ETF 份额折算（拆细/合并），字段 suogu
        为份额比例（1 份变 suogu 份，价格 ×1/suogu）：suogu>1 即拆细（价格下调，如 512930
        的 4.0），suogu<1 即合并（价格上调，如 512200 的 0.358）。折算数学上等价于送转股，
        这里把 suogu 折算为 songzhuangu=10*(suogu-1)，使其与分红送转共用同一条前复权公式，
        否则 ETF 折算日在行情里留下虚假断层。

        数据来自 TDX 本地服务器（与原始 K 线同一来源），带 24h 缓存。

        返回 None 表示**确认无除权除息记录**；取数本身失败则抛 MootdxMarketError。
        两者必须区分：调用方拿不到记录时会按"无需复权"放行原始价，若把取数失败也
        当成"无记录"，原始未复权价会被当作 qfq 一路写进缓存，且不报任何错。
        """
        code = symbol[2:] if str(symbol)[:2].lower() in ("sh", "sz", "bj") else symbol
        try:
            from mootdx.utils.adjust import get_xdxr  # 延迟导入，避免 import 副作用

            xdxr = get_xdxr(code)  # xdxr 接口只接受裸六位代码，带市场前缀会取不到数据
        except Exception as exc:
            raise MootdxMarketError(
                "source_error", f"获取除权除息记录失败: {exc}", symbol=symbol, cause=exc
            ) from exc
        if xdxr is None:
            # mootdx 的 file_cache 在"无本地缓存 + 取数失败"时会落到 None，
            # 这不是"没有除权记录"（那会返回空 DataFrame），必须按取数失败处理。
            raise MootdxMarketError(
                "source_error", "获取除权除息记录返回空对象（疑似取数失败）", symbol=symbol
            )
        if getattr(xdxr, "empty", True) or "category" not in getattr(xdxr, "columns", ()):
            return None
        info = xdxr[xdxr["category"].isin((1, 11))].copy()
        cols = [c for c in ("category", "fenhong", "peigu", "peigujia", "songzhuangu", "suogu") if c in info.columns]
        if not cols:
            return None
        info = info[cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
        # ETF 份额折算：1 份变 suogu 份、价格 ×1/suogu；等价于每 10 份送 (suogu-1)*10 份，
        # songzhuangu 可为负（合并时 suogu<1）。仅跳过无有效比例的记录（suogu<=0 会导致除零）。
        conversion = info["category"] == 11
        info.loc[conversion & (info["suogu"] > 0), "songzhuangu"] = (info["suogu"] - 1) * 10
        info.loc[conversion, ["fenhong", "peigu", "peigujia"]] = 0.0
        info = info.drop(columns=[c for c in ("category", "suogu") if c in info.columns])
        info.index = pd.to_datetime(info.index).normalize()
        info = info[~info.index.duplicated(keep="last")]
        return info

    @staticmethod
    def _align_xdxr_to_bars(info: pd.DataFrame, trading_days: pd.DatetimeIndex) -> pd.DataFrame:
        """把除权记录的日期对齐到「该日或之后的第一个交易日」（仅保留落在 K 线窗口内的记录）。

        TDX 的除权日**未必是交易日**：可能落在周末/节假日（如 ETF 159922 的份额折算记录
        日期为周日 2024-12-01），也可能是标的当日停牌。而前复权因子只能挂在真实的 K 线
        日期上 —— 原实现直接按日期 join，这类记录会被静默丢弃，该次除权完全不参与折算，
        在"前复权"序列里留下一个虚假断层（159922 在 2024-12-02 留下 -60% 断层，
        且 2024-12-02 之前的全部历史价格被高估 2.5 倍）。

        对齐规则：
        - 早于首根 K 线的记录丢弃 —— 前复权因子以最新一根为 1 向前累乘，窗口外的除权
          不影响窗口内的**相对**因子；若把它们也顺延到首根 K 线，反而会污染整段窗口。
        - 晚于末根 K 线的记录丢弃（尚无对应行情，无价可折）。
        - 多条记录顺延到同一交易日时合并（159922 即为此例：周日 12-01 的折算记录与
          12-02 的分红记录都落到 12-02）。可加字段求和，配股价取最大（配股不会撞车，
          只有一条 peigu>0 时 max 即该条自身）。
        """
        if info.empty or len(trading_days) == 0:
            return info.iloc[0:0]
        pos = trading_days.searchsorted(info.index, side="left")
        inside = (info.index >= trading_days[0]) & (pos < len(trading_days))
        if not inside.any():
            return info.iloc[0:0]
        aligned = info.loc[inside].copy()
        aligned.index = trading_days[pos[inside]]
        if aligned.index.has_duplicates:
            aligned = aligned.groupby(level=0).agg(
                {c: ("max" if c == "peigujia" else "sum") for c in aligned.columns}
            )
        return aligned

    def _forward_adjust_bars(
        self, symbol: str, bars: list[MootdxKlineBar]
    ) -> list[MootdxKlineBar]:
        """把原始价按前复权折算，消除分红除权日的价格断层。

        公式与 mootdx `reversion._reversion` 的前复权分支一致：
        preclose = (close[前]*10 - fenhong + peigu*peigujia) / (10 + peigu + songzhuangu)
        adj      = (preclose.next / close).fillna(1)[::-1].cumprod()   # 最新根因子=1
        复权价   = 原始价 * adj

        除权记录先经 `_align_xdxr_to_bars` 对齐到交易日，避免除权日非交易日的记录被丢弃。
        """
        if not bars:
            return bars
        info = self._get_xdxr_info(symbol)
        if info is None or info.empty:
            return bars  # 无除权记录，无需调整

        def _d(b: MootdxKlineBar) -> datetime:
            return pd.to_datetime(b["datetime"]).normalize()

        df = pd.DataFrame({"close": [b["close"] for b in bars]}, index=pd.DatetimeIndex([_d(b) for b in bars]))
        df = df[~df.index.duplicated(keep="last")].sort_index()
        info = self._align_xdxr_to_bars(info, df.index)
        data = df.join(info, how="left").fillna(0.0)
        data["preclose"] = (
            data["close"].shift(1) * 10 - data["fenhong"] + data["peigu"] * data["peigujia"]
        ) / (10 + data["peigu"] + data["songzhuangu"])
        # 单日因子 = 理论前收 / 该日收盘。缺失价（TDX 给 0 或空，`_to_float` 置 None）会被
        # 上面的 fillna(0.0) 变成 0，一旦紧邻除权日就得到 ±inf。adj 是**反向 cumprod**，
        # inf 会乘进它前面所有更早的 bar，把整段历史价格变成 ±inf 且不报错；`.fillna(1.0)`
        # 只捞 NaN、捞不住 inf。这里先把非有限的单日因子降级为 1（即该步不复权），再累乘。
        ratio = (data["preclose"].shift(-1) / data["close"]).replace(
            [float("inf"), float("-inf")], 1.0
        )
        data["adj"] = ratio.fillna(1.0)[::-1].cumprod()
        adj_map = {d: a for d, a in zip(data.index, data["adj"])}

        out: list[MootdxKlineBar] = []
        for b in bars:
            f = adj_map.get(_d(b), 1.0)
            nb = dict(b)
            for col in ("open", "high", "low", "close"):
                if nb.get(col) is not None:
                    nb[col] = nb[col] * f
            out.append(nb)
        return out

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

