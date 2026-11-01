"""ModelScope QuantDB 数据集接入：本地镜像 + 增量更新 + 按标的读取。

数据集 ``qusong0627/LightGBM_Alpha300`` 以**交易日分区**存放全市场数据
（``<table>/dt=YYYYMMDD/data.parquet``）。本模块把它当作一个可选数据源：

- 选该数据源时，先检查本地镜像是否覆盖所需的交易日区间，缺的分区从 ModelScope
  下载（按需、只下用到的那段，不整包拉 56GB）；
- 远端仓库每周更新一次。超过 ``QUANTDANCE_QUANTDB_CHECK_HOURS``（默认 24 小时）
  才会去问一次远端 revision，revision 变了才重新列分区清单，并按 sha256 增量补齐
  **新增**与**被改写**的分区；
- 远端不可达但本地已覆盖请求区间时继续用本地数据（打一行 stderr 告警），覆盖不到
  才报错——同一数据源读不到就是读不到，不会偷偷换源；
- 按标的读取走派生索引 ``derived/<table>/data.parquet``（按 symbol 排序、小 row group
  写入），这样 1668 只 ETF 的 as-of 扫描不必每次回扫 2623 个按日分区的文件。
  索引签名 = 当前镜像分区的 (dt, sha256) 集合，镜像或 revision 变化时整体重建。

口径（与数据集 README 一致，使用前务必知道）：

- 股票取 ``1_kline_data/daily_forward``，即**等比前复权**（锚定最新交易日）；
- ETF 只有 ``4_bond_etf/etf_kline``（**不复权**），数据集不含 ETF 复权因子。分红/
  折算日会与 a_stock_data 的前复权序列出现跳空，读取时打一行 stderr 告警；
- volume 单位股、amount 单位万元、价格单位元。

环境变量：
    QUANTDANCE_QUANTDB_DIR          本地镜像根目录（默认 backend/data/quantdb）
    QUANTDANCE_QUANTDB_CHECK_HOURS  远端 revision 检查间隔小时数（默认 24，0=每次都查）
    QUANTDANCE_QUANTDB_WORKERS      分区下载并发数（默认 8）
    QUANTDANCE_QUANTDB_REPO         覆盖数据集仓库（默认 qusong0627/LightGBM_Alpha300）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

DEFAULT_REPO = "qusong0627/LightGBM_Alpha300"

_WEB = "https://www.modelscope.cn"
_TREE_API = _WEB + "/api/v1/datasets/{repo}/repo/tree"
_FILE_API = _WEB + "/api/v1/datasets/{repo}/repo?Revision={rev}&FilePath="

_ENV_DIR = "QUANTDANCE_QUANTDB_DIR"
_ENV_CHECK_HOURS = "QUANTDANCE_QUANTDB_CHECK_HOURS"
_ENV_WORKERS = "QUANTDANCE_QUANTDB_WORKERS"
_ENV_REPO = "QUANTDANCE_QUANTDB_REPO"

# ModelScope 的 tree 接口单页上限 3000 条，超过要翻页。
_PAGE_SIZE = 3000
_INDEX_VERSION = 1
# dataset --update 自动补齐新分区的跨度上限：最新已镜像日离最新数据超过这么多天，
# 说明镜像是稀疏的（只读过个别日期），不自动补（否则会一下拉几 GB），只打提示。
_UPDATE_MAX_GAP_DAYS = 30
# 派生索引的小 row group：symbol 已排序，过滤单只标的是按 row group 的 min/max 统计跳过。
_INDEX_ROW_GROUP = 20000
_STATE_FILE = "_state.json"
_KLINE_COLUMNS = ["symbol", "time", "open", "high", "low", "close", "volume", "amount"]

@dataclass(frozen=True)
class TableSpec:
    """数据集内一张表的访问方式。

    ``layout`` 决定文件组织与同步粒度：

    - ``daily``：``<path>/dt=YYYYMMDD/data.parquet``，按交易日分区，可按日期区间
      只下载用到的分区（K 线、估值、技术指标、ML 宽表、融资融券等）；
    - ``per_symbol``：``<path>/<SYMBOL>.parquet``，逐股一个文件（财务数据），
      按标的下载；``symbol_from_file=True`` 表示文件里没有代码列，读取时补 ``Symbol``；
    - ``single``：``<path>/*.parquet``，整张表就是几个小文件（基础资料、成分、
      日历、ETF 申赎清单），同步时整目录拉取。

    ``date_col`` 是区间过滤用的列名（``m_timetag`` / ``TradingDate`` 这类是 int
    型 ``YYYYMMDD``，其余是 datetime）；``symbol_col`` 是行级代码列，``None`` 表示
    该表不支持按标的过滤。
    """

    name: str
    path: str
    layout: str
    note: str
    date_col: str | None = None
    symbol_col: str | None = None
    symbol_from_file: bool = False
    size_mb: float = 0.0


# 数据集内全部可访问的表。size_mb 是远端全量体积（2026-11 实测），供同步前判断代价。
TABLE_SPECS: dict[str, TableSpec] = {
    # ---- 1. K 线与指数日线（按日分区） ----
    "kline_stock": TableSpec(
        "kline_stock", "1_kline_data/daily_forward", "daily",
        "股票日线·等比前复权（锚定最新交易日）", "time", "symbol", size_mb=402.7,
    ),
    "kline_stock_raw": TableSpec(
        "kline_stock_raw", "1_kline_data/daily_unadjusted", "daily",
        "股票日线·不复权", "time", "symbol", size_mb=235.2,
    ),
    "kline_stock_hfq": TableSpec(
        "kline_stock_hfq", "1_kline_data/daily_backward", "daily",
        "股票日线·等比后复权", "time", "symbol", size_mb=414.7,
    ),
    "kline_etf": TableSpec(
        "kline_etf", "4_bond_etf/etf_kline", "daily",
        "ETF 日线·不复权（无复权因子）", "time", "symbol", size_mb=43.7,
    ),
    "index_daily": TableSpec(
        "index_daily", "1_kline_data/index_daily", "daily",
        "指数日线（宽基/风格/行业/主题，Category 列标注分类）", "time", "symbol",
        size_mb=8.5,
    ),
    # ---- 2. 基础与板块 ----
    "margin_trading": TableSpec(
        "margin_trading", "2_base_sector/margin_trading", "daily",
        "融资融券（融资余额/买入/偿还/净额，融券量）", "time", "symbol", size_mb=144.0,
    ),
    "instrument_detail": TableSpec(
        "instrument_detail", "2_base_sector/instrument_detail", "single",
        "合约基础资料（152 列；含 Name/BelongHS300/上市退市日等）", None, "Symbol", size_mb=3.2,
    ),
    "sector_concept": TableSpec(
        "sector_concept", "2_base_sector/sector_concept", "single",
        "行业与概念板块成分（板块代码/名称/类型/成分股）", None, "Symbol", size_mb=0.2,
    ),
    "trading_calendar": TableSpec(
        "trading_calendar", "2_base_sector/trading_calendar", "single",
        "交易日历（TradingDate + IsTradingDay）", "TradingDate", None, size_mb=0.0,
    ),
    "index_weights": TableSpec(
        "index_weights", "2_base_sector/index_weights", "single",
        "指数成分权重（每个指数一个文件，Weight 单位 %）", None, "Symbol", size_mb=0.0,
    ),
    # ---- 3. 财务数据（逐股文件） ----
    "fin_balance": TableSpec(
        "fin_balance", "3_financial_data/balance", "per_symbol",
        "资产负债表（报告期 m_timetag / 公告日 m_anntime）", "m_timetag", None,
        symbol_from_file=True, size_mb=332.8,
    ),
    "fin_income": TableSpec(
        "fin_income", "3_financial_data/income", "per_symbol",
        "利润表", "m_timetag", None, symbol_from_file=True, size_mb=188.0,
    ),
    "fin_cashflow": TableSpec(
        "fin_cashflow", "3_financial_data/cashflow", "per_symbol",
        "现金流量表", "m_timetag", None, symbol_from_file=True, size_mb=323.7,
    ),
    "fin_capital": TableSpec(
        "fin_capital", "3_financial_data/capital", "per_symbol",
        "股本结构", "m_timetag", "Symbol", size_mb=45.4,
    ),
    "fin_holder_num": TableSpec(
        "fin_holder_num", "3_financial_data/holder_num", "per_symbol",
        "股东户数与机构持仓", "m_timetag", "Symbol", size_mb=146.7,
    ),
    "fin_per_share": TableSpec(
        "fin_per_share", "3_financial_data/per_share", "per_symbol",
        "每股指标（TTM 净利润/营收/现金流等）", "m_timetag", "Symbol", size_mb=49.4,
    ),
    "fin_pershare_index": TableSpec(
        "fin_pershare_index", "3_financial_data/pershare_index", "per_symbol",
        "每股财务指标与比率（ROE/毛利率/周转率等 86 列）", "m_timetag", None,
        symbol_from_file=True, size_mb=436.7,
    ),
    "dividend_factors": TableSpec(
        "dividend_factors", "3_financial_data/dividend_factors", "per_symbol",
        "分红送配（时间/类型/派息/送股/转增/配股价），可用于自算复权因子", "time", "Symbol",
        size_mb=25.3,
    ),
    # ---- 4. 债券与 ETF ----
    "convertible_bond": TableSpec(
        "convertible_bond", "4_bond_etf/convertible_bond", "single",
        "可转债基础信息（转股价/溢价率/强赎价等）", None, "BondCode", size_mb=0.0,
    ),
    "etf_pcf": TableSpec(
        "etf_pcf", "4_bond_etf/etf_pcf", "single",
        "ETF 申赎清单（etf_pcf / etf_components / trackzs_etf_info 三个文件）", None, None,
        size_mb=1.1,
    ),
    # ---- 5. 衍生指标（按日分区） ----
    "valuation": TableSpec(
        "valuation", "5_technical_derived/valuation", "daily",
        "估值：PE/PB/PS/市值/股息率 + 净利润营收 TTM", "time", "symbol", size_mb=856.9,
    ),
    "technical_indicators": TableSpec(
        "technical_indicators", "5_technical_derived/technical_indicators", "daily",
        "技术指标（MA/RSI/KDJ/MACD/波动率 + 未来收益列）", "time", "symbol", size_mb=2629.2,
    ),
    "market_sentiment": TableSpec(
        "market_sentiment", "5_technical_derived/market_sentiment", "daily",
        "盘口情绪与流动性（上下影线/买卖压力/日内波动等）", "time", "symbol", size_mb=1023.4,
    ),
    # ---- 6. ML 宽表（按日分区，列数多、体积大） ----
    "features_daily": TableSpec(
        "features_daily", "6_ml_datasets/features_daily", "daily",
        "日线特征宽表（78 列）", "time", "symbol", size_mb=3597.1,
    ),
    "l1_factors": TableSpec(
        "l1_factors", "6_ml_datasets/l1_factors", "daily",
        "L1 日频因子（119 列，日期列名为 date）", "date", "symbol", size_mb=7465.4,
    ),
    "l1_l2_factors": TableSpec(
        "l1_l2_factors", "6_ml_datasets/l1_l2_factors", "daily",
        "L1+L2 因子宽表（329 列，2018 年起）", "date", "symbol", size_mb=25367.7,
    ),
    "factor_dictionary": TableSpec(
        "factor_dictionary", "6_ml_datasets/factor_dictionary", "daily",
        "动态因子宽表（571 列，含 gtja_* 与 risk_fac_*，2018 年起）", "date", "symbol",
        size_mb=34444.5,
    ),
}

# 兼容旧调用：name -> 仓库路径。
TABLES: dict[str, str] = {name: spec.path for name, spec in TABLE_SPECS.items()}

# 需要建派生索引（按 symbol 排序单文件）的表：只有回测高频读取的两张 K 线表。
# 其它日表体积大、用途以逐日扫描为主，直接读分区即可，不额外落一份索引。
_INDEXED_TABLES = frozenset({"kline_stock", "kline_etf"})

# 场内基金代码前缀：15/16/18（深市 ETF/LOF）、50/51/52/56/58（沪市 ETF）。
_ETF_PREFIXES = ("15", "16", "18", "50", "51", "52", "56", "58")

_ETF_WARNING = (
    "ETF 日线为不复权价（数据集不含 ETF 复权因子）：分红日有跳空，"
    "份额折算/拆分日（如 1 拆 2、缩股）会出现成倍的虚假涨跌，"
    "跨折算日的回测收益与回撤不可用；引用 ETF 的结果请先核对折算日。"
)

_ETF_WARNED = False
_ETF_WARN_LOCK = threading.Lock()


class QuantDBError(Exception):
    """QuantDB 数据集不可用：本地缺数据且下载失败、远端不可达、数据解析异常。"""


def _log(message: str) -> None:
    print(f"[quantdb] {message}", file=sys.stderr, flush=True)


def _warn_etf_once() -> None:
    global _ETF_WARNED
    with _ETF_WARN_LOCK:
        if _ETF_WARNED:
            return
        _ETF_WARNED = True
    _log(_ETF_WARNING)


def is_etf_symbol(code: str) -> bool:
    """按代码前缀判断是否场内基金（ETF/LOF）。"""
    return code[:2] in _ETF_PREFIXES


def dataset_symbol(code: str) -> str:
    """6 位代码 → 数据集内的 ``600519.SH`` / ``159915.SZ`` / ``920000.BJ``。"""
    text = str(code).strip().upper()
    if text.startswith("92"):
        return f"{text}.BJ"
    suffix = "SH" if text.startswith(("5", "6", "9")) else "SZ"
    return f"{text}.{suffix}"


def _dataset_symbol(code: str) -> str:
    """已经带后缀的原样返回，6 位代码补后缀。"""
    text = str(code).strip().upper()
    return text if "." in text else dataset_symbol(text)


def _default_root() -> Path:
    raw = os.getenv(_ENV_DIR)
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[2] / "data" / "quantdb"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _ymd(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    return str(value).strip()[:10].replace("-", "")


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _days_between(start: str, end: str) -> int:
    """``YYYYMMDD`` 之间的自然日差；解析失败返回 0。"""
    try:
        return (
            datetime.strptime(end, "%Y%m%d").date()
            - datetime.strptime(start, "%Y%m%d").date()
        ).days
    except (TypeError, ValueError):
        return 0


class QuantDBStore:
    """本地镜像 + 增量更新 + 按标的读取。进程内单例，见 :func:`get_store`。"""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        repo: str | None = None,
        workers: int | None = None,
        check_hours: int | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.root = Path(root).expanduser() if root is not None else _default_root()
        self.repo = repo or os.getenv(_ENV_REPO) or DEFAULT_REPO
        self.workers = max(1, min(16, workers or _env_int(_ENV_WORKERS, 8)))
        hours = check_hours if check_hours is not None else _env_int(_ENV_CHECK_HOURS, 24)
        self.check_hours = max(0, hours)
        self.timeout = timeout
        self._lock = threading.RLock()
        # 进程内状态缓存：state 含全部分区清单（数千条），每次读取都从磁盘
        # 反序列化/回写会白白吃掉几十毫秒，而 CLI 是单进程使用。
        self._state_cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # 本地路径与状态
    # ------------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.root / _STATE_FILE

    def spec(self, table: str) -> TableSpec:
        spec = TABLE_SPECS.get(table)
        if spec is None:
            raise ValueError(f"未知的数据表: {table}（可选: {', '.join(TABLE_SPECS)}）")
        return spec

    def table_dir(self, table: str) -> Path:
        return self.root / self.spec(table).path

    def partition_path(self, table: str, dt: str) -> Path:
        """按日分区表的单个分区路径（``daily`` 布局）。"""
        return self.table_dir(table) / f"dt={dt}" / "data.parquet"

    def file_path(self, table: str, name: str) -> Path:
        """``per_symbol`` / ``single`` 布局下相对表目录的文件路径。"""
        return self.table_dir(table) / name

    def _local_rel(self, table: str, key: str, info: dict[str, Any]) -> str:
        """远端条目 → 本地相对路径（兼容旧 state 里没有 ``local`` 字段的记录）。"""
        rel = info.get("local")
        if rel:
            return str(rel)
        if self.spec(table).layout == "daily":
            return f"dt={key}/data.parquet"
        return key

    def index_path(self, table: str) -> Path:
        return self.root / "derived" / table / "data.parquet"

    def index_meta_path(self, table: str) -> Path:
        return self.root / "derived" / table / "index.json"

    def _read_state(self) -> dict[str, Any]:
        if self._state_cache is not None:
            return self._state_cache
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        self._state_cache = payload if isinstance(payload, dict) else {}
        return self._state_cache

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(
            f".json.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.state_path)
        self._state_cache = state

    # ------------------------------------------------------------------
    # 远端
    # ------------------------------------------------------------------

    def _get_json(self, url: str, *, what: str) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if not isinstance(payload, dict):
                raise QuantDBError(f"{what} 返回格式异常")
            return payload
        raise QuantDBError(f"{what} 失败（已重试 3 次）: {last}")

    def _remote_revision(self) -> str:
        url = _TREE_API.format(repo=self.repo) + "?Revision=master&Root=%2F&Recursive=false"
        payload = self._get_json(url, what="ModelScope 仓库信息")
        files = (payload.get("Data") or {}).get("Files") or []
        for item in files:
            revision = item.get("Revision")
            if revision:
                return str(revision)
        raise QuantDBError("ModelScope 仓库信息里没有 revision")

    def _remote_files(self, table: str, revision: str) -> dict[str, dict[str, Any]]:
        """列出表内全部数据文件。

        返回 ``{key: {"path", "sha256", "size", "local"}}``：``daily`` 布局的 key 是
        ``YYYYMMDD``、local 是 ``dt=<key>/data.parquet``；另两种布局的 key 就是表目录
        下的一级文件名（如 ``000001.SZ.parquet``），local 与 key 相同。
        """
        spec = self.spec(table)
        root = spec.path
        base = _TREE_API.format(repo=self.repo) + "?Revision=" + urllib.parse.quote(revision)
        base += "&Recursive=true&Root=" + urllib.parse.quote(root, safe="")
        prefix = root + "/"

        out: dict[str, dict[str, Any]] = {}
        page = 1
        while True:
            url = f"{base}&PageNumber={page}&PageSize={_PAGE_SIZE}"
            payload = self._get_json(url, what=f"{table} 文件清单")
            data = payload.get("Data") or {}
            files = data.get("Files") or []
            if not files:
                break
            for item in files:
                if item.get("Type") != "blob":
                    continue
                path = str(item.get("Path") or "")
                if not path.startswith(prefix):
                    continue
                rel = path[len(prefix):]
                if spec.layout == "daily":
                    parent = rel.split("/")[0]
                    if not parent.startswith("dt="):
                        continue
                    key = parent[3:]
                    if len(key) != 8 or not key.isdigit():
                        continue
                    local = f"dt={key}/data.parquet"
                else:
                    # 只取表层文件：``per_symbol`` 一symbol一文件，``single`` 是几个小文件。
                    if "/" in rel or not rel.endswith(".parquet"):
                        continue
                    key = rel
                    local = rel
                out[key] = {
                    "path": path,
                    "sha256": str(item.get("Sha256") or ""),
                    "size": int(item.get("Size") or 0),
                    "local": local,
                }
            total = int(data.get("TotalCount") or 0)
            if total and page * _PAGE_SIZE >= total:
                break
            page += 1
            if page > 50:  # 防御：远端异常时不至于无限翻页
                break
        if not out:
            raise QuantDBError(f"{table} 远端文件清单为空")
        return out

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------

    def _refresh_remote(
        self,
        state: dict[str, Any],
        tables: Sequence[str],
        *,
        force: bool,
    ) -> bool:
        """按需刷新远端 revision 与分区清单（就地改 state），返回 state 是否变化。"""
        tables_state: dict[str, Any] = state.setdefault("tables", {})
        checked_at = state.get("checked_at")
        fresh = False
        if checked_at:
            try:
                age = datetime.now() - datetime.fromisoformat(str(checked_at))
                fresh = age < timedelta(hours=self.check_hours)
            except ValueError:
                fresh = False
        missing = [t for t in tables if t not in tables_state]
        if fresh and not missing and not force:
            return False

        try:
            revision = self._remote_revision()
        except QuantDBError as exc:
            covered = [t for t in tables if tables_state.get(t)]
            if covered and len(covered) == len(tables) and not force:
                _log(f"无法检查远端更新（{exc}），继续使用本地镜像")
                return False
            raise

        if revision != state.get("revision"):
            # revision 变了：旧的清单与 sha 全部作废，按当前表重新列。
            state["revision"] = revision
            state["tables"] = {}
            tables_state = state["tables"]

        for table in tables:
            cached = tables_state.get(table)
            if cached and cached.get("revision") == revision and not force:
                continue
            _log(f"获取 {table} 远端分区清单…")
            files = self._remote_files(table, revision)
            tables_state[table] = {
                "revision": revision,
                "files": files,
                "listed_at": _now_iso(),
                "local": (cached or {}).get("local", {}),
                "mirrored": (cached or {}).get("mirrored", []),
            }
        state["checked_at"] = _now_iso()
        return True

    def _select_dates(
        self,
        files: dict[str, dict[str, Any]],
        *,
        start: str | date | datetime | None,
        end: str | date | datetime | None,
        limit: int | None,
        all_partitions: bool,
    ) -> list[str]:
        dts = sorted(files)
        if all_partitions:
            return dts
        # 日期区间优先于 limit：同时给出时按区间取，limit 不再截断。
        if start is not None and end is not None:
            lo, hi = _ymd(start), _ymd(end)
            return [d for d in dts if lo <= d <= hi]
        if start is not None:
            lo = _ymd(start)
            return [d for d in dts if d >= lo]
        if end is not None:
            hi = _ymd(end)
            return [d for d in dts if d <= hi]
        if limit is not None:
            return dts[-limit:]
        return dts

    def _download_file(self, remote_path: str, dest: Path, revision: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
        url = _FILE_API.format(repo=self.repo, rev=urllib.parse.quote(revision))
        url += urllib.parse.quote(remote_path)
        last: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    with tmp.open("wb") as fh:
                        shutil.copyfileobj(resp, fh)
                os.replace(tmp, dest)
                return
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                time.sleep(1.5 * (attempt + 1))
        raise QuantDBError(f"下载 {remote_path} 失败（已重试 3 次）: {last}")

    def _download_partitions(
        self,
        table: str,
        keys: Sequence[str],
        table_state: dict[str, Any],
        revision: str,
    ) -> int:
        """下载表内指定 key 的文件（``daily`` 是分区，其它布局是文件名）。"""
        files = table_state["files"]
        local: dict[str, str] = table_state.setdefault("local", {})
        spec = self.spec(table)
        unit = "分区" if spec.layout == "daily" else "文件"

        def needs_download(key: str) -> bool:
            # 只比对记录，不逐文件 stat：2628 次 exists()/stat() 在每次读取路径上
            # 会累积出可观的开销。记录只在原子替换成功后写入；进程中途崩溃时该
            # 分区没有记录，下一轮会重下（幂等）。
            return local.get(key) != files[key]["sha256"]

        todo = [(key, files[key]) for key in keys if key in files and needs_download(key)]
        if not todo:
            return 0

        _log(f"{table} 需要下载 {len(todo)} 个{unit}（共请求 {len(keys)} 个）")
        done = 0
        with ThreadPoolExecutor(max_workers=min(self.workers, len(todo))) as pool:
            futures = {
                pool.submit(
                    self._download_file,
                    info["path"],
                    self.file_path(table, self._local_rel(table, key, info)),
                    revision,
                ): (key, info)
                for key, info in todo
            }
            try:
                for future in as_completed(futures):
                    key, info = futures[future]
                    future.result()
                    local[key] = info["sha256"]
                    done += 1
                    if done % 100 == 0 or done == len(todo):
                        _log(f"{table} 下载进度 {done}/{len(todo)}")
            except Exception:
                # 已完成的记录到 local 不代表已落盘（state 尚未保存），但分区文件本身
                # 已原子替换，下一轮按 sha/size 判定会跳过它们。
                raise
        return done

    def _mirrored_keys(self, table: str, table_state: dict[str, Any]) -> list[str]:
        """已下载且 sha 与远端一致的分区/文件（不碰磁盘，记录只在原子写成功后写入）。"""
        files = table_state.get("files") or {}
        local = table_state.get("local") or {}
        return [key for key in sorted(files) if local.get(key) == files[key].get("sha256")]

    # 旧名保留：daily 布局下 key 就是日期。
    _mirrored_dates = _mirrored_keys

    @staticmethod
    def _tail_size_mb(table_state: dict[str, Any], keys: Sequence[str]) -> float:
        files = table_state.get("files") or {}
        return sum(int(files[k].get("size") or 0) for k in keys if k in files) / 1024 / 1024

    def _ensure_index(self, table: str, table_state: dict[str, Any], revision: str) -> None:
        """按当前本地镜像重建派生索引（symbol 排序的单文件 parquet）。"""
        files = table_state["files"]
        dts = self._mirrored_dates(table, table_state)
        if not dts:
            raise QuantDBError(f"{table} 本地没有任何分区，无法建索引")
        signature = hashlib.sha1(
            (
                f"v{_INDEX_VERSION}|{revision}|"
                + "\n".join(f"{dt}:{files[dt]['sha256']}" for dt in dts)
            ).encode("utf-8")
        ).hexdigest()

        meta_path = self.index_meta_path(table)
        data_path = self.index_path(table)
        if data_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
            if isinstance(meta, dict) and meta.get("signature") == signature:
                return

        # 只有真要重建时才逐分区 stat/读取：签名命中的常规读取路径不碰磁盘。
        dts = [dt for dt in dts if self.partition_path(table, dt).exists()]
        if not dts:
            raise QuantDBError(f"{table} 本地分区文件缺失，请重新同步")
        _log(f"构建 {table} 派生索引（{len(dts)} 个分区）…")
        frames: list[pd.DataFrame] = []
        for dt in dts:
            part = pd.read_parquet(self.partition_path(table, dt), columns=_KLINE_COLUMNS)
            frames.append(part)
        frame = pd.concat(frames, ignore_index=True)
        del frames
        frame = frame.sort_values(["symbol", "time"], kind="mergesort")

        data_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = data_path.with_suffix(f".parquet.{os.getpid()}.{threading.get_ident()}.tmp")
        frame.to_parquet(tmp, index=False, row_group_size=_INDEX_ROW_GROUP)
        os.replace(tmp, data_path)
        meta_tmp = meta_path.with_suffix(
            f".json.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        meta_tmp.write_text(
            json.dumps(
                {
                    "signature": signature,
                    "revision": revision,
                    "version": _INDEX_VERSION,
                    "partitions": len(dts),
                    "first": dts[0],
                    "last": dts[-1],
                    "rows": int(len(frame)),
                    "built_at": _now_iso(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(meta_tmp, meta_path)
        _log(f"{table} 派生索引完成：{len(frame)} 行，{dts[0]}~{dts[-1]}")

    def _select_symbols(
        self,
        table: str,
        files: dict[str, dict[str, Any]],
        *,
        symbols: Sequence[str] | None,
        all_files: bool,
    ) -> list[str]:
        """``per_symbol`` 布局：把代码列表映射成文件名。"""
        if not symbols:
            if not all_files:
                raise ValueError(
                    f"{table} 是逐股文件表，需用 symbols 指定标的（或 --all 同步全部 "
                    f"{len(files)} 个文件，约 {self.spec(table).size_mb:.0f} MB）"
                )
            return sorted(files)
        keys: list[str] = []
        missing: list[str] = []
        for raw in symbols:
            name = f"{_dataset_symbol(str(raw))}.parquet"
            if name in files:
                keys.append(name)
            else:
                missing.append(str(raw))
        if missing:
            raise QuantDBError(f"{table} 远端没有这些标的: {', '.join(missing)}")
        return keys

    def sync(
        self,
        table: str,
        *,
        start: str | date | datetime | None = None,
        end: str | date | datetime | None = None,
        limit: int | None = None,
        update: bool = False,
        all_partitions: bool = False,
        symbols: Sequence[str] | None = None,
        keys: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """确保 ``table`` 的本地镜像覆盖请求范围。

        ``daily`` 按日期区间/条数选分区，``per_symbol`` 按代码选文件（或 ``--all``），
        ``single`` 整目录拉取（一个表只有几个小文件）。

        返回 {"table", "layout", "requested", "downloaded", "first", "last", "revision"}。
        """
        spec = self.spec(table)
        if limit is not None and limit < 1:
            raise ValueError("limit 至少为 1")
        with self._lock:
            state = self._read_state()
            changed = self._refresh_remote(state, [table], force=update)
            table_state = state["tables"][table]
            revision = str(table_state.get("revision") or state.get("revision") or "")
            files = table_state["files"]
            if spec.layout == "daily":
                if keys is not None:
                    # 显式指定分区（update 用：已镜像的 + 最新之后的），不再按日期推导。
                    keys = [k for k in keys if k in files]
                else:
                    keys = self._select_dates(
                        files,
                        start=start,
                        end=end,
                        limit=limit,
                        all_partitions=all_partitions,
                    )
                if not keys:
                    raise QuantDBError("请求区间内远端没有可用分区")
            elif spec.layout == "per_symbol":
                keys = self._select_symbols(
                    table, files, symbols=symbols, all_files=all_partitions
                )
            else:
                keys = sorted(files)
            downloaded = self._download_partitions(table, keys, table_state, revision)
            if table in _INDEXED_TABLES:
                self._ensure_index(table, table_state, revision)
            mirrored = self._mirrored_keys(table, table_state)
            if changed or downloaded or table_state.get("mirrored") != mirrored:
                table_state["mirrored"] = mirrored
                self._write_state(state)
            return {
                "table": table,
                "layout": spec.layout,
                "requested": len(keys),
                "downloaded": downloaded,
                "first": keys[0] if spec.layout == "daily" and keys else None,
                "last": keys[-1] if spec.layout == "daily" and keys else None,
                "revision": revision,
            }

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def _read_symbol(
        self,
        table: str,
        symbol: str,
        *,
        start: str | date | datetime | None,
        end: str | date | datetime | None,
        limit: int | None,
    ) -> pd.DataFrame:
        path = self.index_path(table)
        if not path.exists():
            raise QuantDBError(f"{table} 派生索引不存在")
        frame = pd.read_parquet(
            path,
            columns=_KLINE_COLUMNS,
            filters=[("symbol", "==", symbol)],
        )
        if frame.empty:
            return frame
        frame = frame.sort_values("time").drop_duplicates("time", keep="last")
        if start is not None:
            lo = pd.Timestamp(_ymd(start))
            frame = frame[frame["time"] >= lo]
        if end is not None:
            hi = pd.Timestamp(_ymd(end))
            frame = frame[frame["time"] <= hi]
        frame = frame.dropna(subset=["open", "high", "low", "close"])
        if limit is not None:
            frame = frame.tail(limit)
        out = frame.set_index("time")[_KLINE_COLUMNS[2:]]
        out.index = pd.DatetimeIndex(out.index)
        return out

    def read_daily(
        self,
        code: str,
        *,
        start: str | date | datetime | None = None,
        end: str | date | datetime | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """读取单只标的日线，返回 DatetimeIndex 索引的 OHLCV(+amount) DataFrame。"""
        if start is not None and end is not None:
            limit = None
        elif limit is not None:
            if limit < 50:
                raise ValueError("limit 至少为 50")
            start = end = None
        else:
            raise ValueError("请同时提供 start 与 end，或提供 limit")

        etf = is_etf_symbol(code)
        table = "kline_etf" if etf else "kline_stock"
        if etf:
            _warn_etf_once()
        symbol = _dataset_symbol(code)

        if limit is None:
            with self._lock:
                self.sync(table, start=start, end=end)
                out = self._read_symbol(table, symbol, start=start, end=end, limit=None)
            if out.empty:
                raise QuantDBError(f"未获取到 {code} 的日线数据")
            return out

        # limit 语义是「该标的最近 N 根 K 线」。分区是按交易日切的，停牌会让最近
        # N 个交易日内的行数不足，因此按 2x 递增回溯，直到行数够或已到历史起点。
        want = limit
        attempts = 0
        while True:
            with self._lock:
                self.sync(table, limit=want)
                out = self._read_symbol(table, symbol, start=None, end=None, limit=limit)
                remote_total = len(self._remote_dates(table))
            if len(out) >= limit or attempts >= 3 or want >= remote_total:
                break
            want *= 2
            attempts += 1
        if out.empty:
            raise QuantDBError(f"未获取到 {code} 的日线数据")
        return out

    def _remote_dates(self, table: str) -> list[str]:
        state = self._read_state()
        table_state = (state.get("tables") or {}).get(table) or {}
        return sorted(table_state.get("files") or {})

    # ------------------------------------------------------------------
    # 通用表读取（数据集里除两张 K 线表之外的全部表）
    # ------------------------------------------------------------------

    @staticmethod
    def _read_parquet(path: Path, columns: Sequence[str] | None) -> pd.DataFrame:
        try:
            return pd.read_parquet(path, columns=list(columns) if columns else None)
        except KeyError as exc:
            raise ValueError(
                f"{path.name} 里没有这些列: {exc}；用 dataset --describe 查看可用列"
            ) from exc

    @staticmethod
    def _filter_dates(
        frame: pd.DataFrame,
        spec: TableSpec,
        *,
        start: str | date | datetime | None,
        end: str | date | datetime | None,
    ) -> pd.DataFrame:
        col = spec.date_col
        if start is None and end is None:
            return frame
        if not col or col not in frame.columns:
            raise ValueError(f"{spec.name} 没有日期列，不支持 start/end 过滤")
        series = frame[col]
        if pd.api.types.is_numeric_dtype(series):
            lo = int(_ymd(start)) if start is not None else None
            hi = int(_ymd(end)) if end is not None else None
            mask = pd.Series(True, index=frame.index)
            if lo is not None:
                mask &= series >= lo
            if hi is not None:
                mask &= series <= hi
        else:
            values = pd.to_datetime(series, errors="coerce")
            mask = values.notna()
            if start is not None:
                mask &= values >= pd.Timestamp(_ymd(start))
            if end is not None:
                mask &= values <= pd.Timestamp(_ymd(end))
        return frame[mask]

    def _read_daily_table(
        self,
        table: str,
        spec: TableSpec,
        *,
        start: str | date | datetime | None,
        end: str | date | datetime | None,
        symbols: Sequence[str] | None,
        columns: Sequence[str] | None,
        limit: int | None,
    ) -> pd.DataFrame:
        codes = [_dataset_symbol(str(s)) for s in symbols] if symbols else None
        with self._lock:
            self.sync(table, start=start, end=end, limit=limit)
            table_state = self._read_state()["tables"][table]
            available = self._mirrored_keys(table, table_state)
        if not available:
            raise QuantDBError(f"{table} 本地没有可用分区")
        # 只读本次请求范围内的已镜像分区（区间外的旧镜像不参与本次读取）。
        dts = self._select_dates(
            {k: {} for k in available}, start=start, end=end, limit=limit,
            all_partitions=False,
        )
        if not dts:
            raise QuantDBError(f"{table} 请求范围内没有已镜像的分区")

        if table in _INDEXED_TABLES:
            # 索引是「全部已镜像分区」的合并视图，这里再按本次选中的分区区间裁一次，
            # 否则 --limit（最近 N 个交易日）会退回全历史。
            frame = self._read_index(table, codes, columns)
            col = spec.date_col
            if dts and col in frame.columns:
                lo = pd.Timestamp(dts[0])
                hi = pd.Timestamp(dts[-1])
                values = pd.to_datetime(frame[col])
                frame = frame[(values >= lo) & (values <= hi)]
            return self._filter_dates(frame, spec, start=start, end=end)

        frames: list[pd.DataFrame] = []
        for dt in dts:
            path = self.partition_path(table, dt)
            if not path.exists():
                continue
            part = self._read_parquet(path, columns)
            if codes and spec.symbol_col in part.columns:
                part = part[part[spec.symbol_col].isin(codes)]
            if not part.empty:
                frames.append(part)
        if not frames:
            raise QuantDBError(f"{table} 在请求范围内没有数据")
        frame = pd.concat(frames, ignore_index=True)
        return self._filter_dates(frame, spec, start=start, end=end)

    def _read_index(
        self,
        table: str,
        codes: Sequence[str] | None,
        columns: Sequence[str] | None,
    ) -> pd.DataFrame:
        path = self.index_path(table)
        if not path.exists():
            raise QuantDBError(f"{table} 派生索引不存在")
        cols = list(columns) if columns else None
        if codes:
            frames = []
            for code in codes:
                try:
                    part = pd.read_parquet(
                        path, columns=cols, filters=[("symbol", "==", code)]
                    )
                except KeyError as exc:
                    raise ValueError(f"{table} 里没有这些列: {exc}") from exc
                if not part.empty:
                    frames.append(part)
            if not frames:
                return pd.DataFrame(columns=cols or _KLINE_COLUMNS)
            return pd.concat(frames, ignore_index=True)
        return self._read_parquet(path, cols)

    def _read_per_symbol_table(
        self,
        table: str,
        spec: TableSpec,
        *,
        start: str | date | datetime | None,
        end: str | date | datetime | None,
        symbols: Sequence[str] | None,
        columns: Sequence[str] | None,
        limit: int | None,
    ) -> pd.DataFrame:
        if not symbols:
            raise ValueError(
                f"{table} 是逐股文件表，请用 symbols 指定标的（如 --symbols 600519 000001）"
            )
        with self._lock:
            self.sync(table, symbols=symbols)
        frames: list[pd.DataFrame] = []
        for raw in symbols:
            name = f"{_dataset_symbol(str(raw))}.parquet"
            path = self.file_path(table, name)
            if not path.exists():
                continue
            part = self._read_parquet(path, columns)
            if spec.symbol_from_file and spec.symbol_col and spec.symbol_col not in part.columns:
                part[spec.symbol_col] = name[: -len(".parquet")]
            part = self._filter_dates(part, spec, start=start, end=end)
            if limit is not None and spec.date_col in part.columns:
                part = part.sort_values(spec.date_col).tail(limit)
            if not part.empty:
                frames.append(part)
        if not frames:
            raise QuantDBError(f"{table} 未获取到这些标的的数据: {', '.join(map(str, symbols))}")
        return pd.concat(frames, ignore_index=True, sort=False)

    def _read_single_table(
        self,
        table: str,
        spec: TableSpec,
        *,
        start: str | date | datetime | None,
        end: str | date | datetime | None,
        symbols: Sequence[str] | None,
        columns: Sequence[str] | None,
        limit: int | None,
    ) -> pd.DataFrame:
        with self._lock:
            self.sync(table)
        frames: list[pd.DataFrame] = []
        for path in sorted(self.table_dir(table).glob("*.parquet")):
            part = self._read_parquet(path, columns)
            if symbols and spec.symbol_col and spec.symbol_col in part.columns:
                codes = [_dataset_symbol(str(s)) for s in symbols]
                part = part[part[spec.symbol_col].isin(codes)]
            part = self._filter_dates(part, spec, start=start, end=end)
            if not part.empty:
                frames.append(part)
        if not frames:
            raise QuantDBError(f"{table} 没有匹配的数据")
        frame = pd.concat(frames, ignore_index=True, sort=False)
        if limit is not None:
            frame = frame.tail(limit)
        return frame

    def read_table(
        self,
        table: str,
        *,
        start: str | date | datetime | None = None,
        end: str | date | datetime | None = None,
        symbols: Sequence[str] | None = None,
        columns: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """读取数据集内任意表，返回扁平 DataFrame。

        - ``daily`` 表：``limit`` 表示「最近 N 个交易日分区」，返回这些分区的全部行；
        - ``per_symbol`` 表：必须给 ``symbols``，``limit`` 表示每只标的最近 N 行；
        - ``single`` 表：``limit`` 表示整体末尾 N 行。

        ``columns`` 指定列子集（先按它读 parquet，再按同样顺序返回）。
        """
        spec = self.spec(table)
        requested = list(columns) if columns else None
        # 过滤/排序需要的列即使没被请求也要读出来，返回前再丢掉。
        read_cols = requested
        if requested is not None:
            needed = [
                col
                for col in (spec.date_col, spec.symbol_col)
                if col and col not in requested
            ]
            read_cols = requested + needed
        if spec.layout == "daily":
            frame = self._read_daily_table(
                table, spec, start=start, end=end, symbols=symbols,
                columns=read_cols, limit=limit,
            )
        elif spec.layout == "per_symbol":
            frame = self._read_per_symbol_table(
                table, spec, start=start, end=end, symbols=symbols,
                columns=read_cols, limit=limit,
            )
        else:
            frame = self._read_single_table(
                table, spec, start=start, end=end, symbols=symbols,
                columns=read_cols, limit=limit,
            )
        sort_keys = [
            col for col in (spec.symbol_col, spec.date_col) if col and col in frame.columns
        ]
        if sort_keys:
            frame = frame.sort_values(sort_keys, kind="mergesort")
        frame = frame.reset_index(drop=True)
        if requested is not None:
            return frame[[c for c in requested if c in frame.columns]]
        return frame

    def list_tables(self) -> list[dict[str, Any]]:
        """列出数据集内全部可访问的表与本地镜像情况。"""
        state = self._read_state()
        items: list[dict[str, Any]] = []
        for name, spec in TABLE_SPECS.items():
            tstate = (state.get("tables") or {}).get(name) or {}
            files = tstate.get("files") or {}
            mirrored = self._mirrored_keys(name, tstate)
            items.append(
                {
                    "table": name,
                    "path": spec.path,
                    "layout": spec.layout,
                    "note": spec.note,
                    "date_col": spec.date_col,
                    "symbol_col": spec.symbol_col,
                    "checked": bool(tstate),
                    "remote_files": len(files),
                    "mirrored_files": len(mirrored),
                    "first": mirrored[0] if mirrored and spec.layout == "daily" else None,
                    "last": mirrored[-1] if mirrored and spec.layout == "daily" else None,
                    "remote_mb": spec.size_mb,
                }
            )
        return items

    def describe_table(self, table: str, *, symbol: str | None = None) -> dict[str, Any]:
        """下载一个样本文件并返回 schema 与前几行（不读整张表）。"""
        import pyarrow.parquet as pq

        spec = self.spec(table)
        with self._lock:
            if spec.layout == "daily":
                report = self.sync(table, limit=1)
                sample = self.partition_path(table, str(report["last"]))
            elif spec.layout == "per_symbol":
                state = self._read_state()
                files = ((state.get("tables") or {}).get(table) or {}).get("files") or {}
                if symbol:
                    name = f"{_dataset_symbol(symbol)}.parquet"
                    if name not in files:
                        raise QuantDBError(f"{table} 远端没有 {symbol}")
                else:
                    name = sorted(files)[0] if files else None
                if not name:
                    raise QuantDBError(f"{table} 远端文件清单为空")
                self.sync(table, symbols=[name[: -len(".parquet")]])
                sample = self.file_path(table, name)
            else:
                self.sync(table)
                candidates = sorted(self.table_dir(table).glob("*.parquet"))
                if not candidates:
                    raise QuantDBError(f"{table} 本地没有文件")
                sample = candidates[0]

        pf = pq.ParquetFile(sample)
        schema = pf.schema_arrow
        head = pf.read_row_group(0).slice(0, 3).to_pandas()
        if spec.symbol_from_file and spec.symbol_col and spec.symbol_col not in head.columns:
            head[spec.symbol_col] = sample.stem
        return {
            "table": table,
            "path": spec.path,
            "layout": spec.layout,
            "note": spec.note,
            "sample_file": str(sample),
            "rows_in_sample": int(pf.metadata.num_rows),
            "columns": [
                {"name": name, "type": str(schema.field(name).type)}
                for name in schema.names
            ],
            "preview": json.loads(head.to_json(orient="records", force_ascii=False, date_format="iso")),
        }

    # ------------------------------------------------------------------
    # 管理命令
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        tables: dict[str, Any] = {}
        for table, spec in TABLE_SPECS.items():
            tstate = (state.get("tables") or {}).get(table) or {}
            files = tstate.get("files") or {}
            mirrored = self._mirrored_keys(table, tstate)
            meta: dict[str, Any] = {}
            if table in _INDEXED_TABLES:
                try:
                    meta = json.loads(self.index_meta_path(table).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    meta = {}
            size = 0
            for key in mirrored:
                try:
                    size += self.file_path(table, self._local_rel(table, key, files[key])).stat().st_size
                except (OSError, KeyError):
                    continue
            tables[table] = {
                "layout": spec.layout,
                "checked": bool(files),
                "remote_partitions": len(files),
                "mirrored_partitions": len(mirrored),
                "first": mirrored[0] if mirrored and spec.layout == "daily" else None,
                "last": mirrored[-1] if mirrored and spec.layout == "daily" else None,
                "mirror_files": len(mirrored),
                "mirror_bytes": size,
                "index": {
                    "exists": table in _INDEXED_TABLES and self.index_path(table).exists(),
                    "partitions": meta.get("partitions"),
                    "first": meta.get("first"),
                    "last": meta.get("last"),
                    "rows": meta.get("rows"),
                },
            }
        total = 0
        for path in self.root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return {
            "repo": self.repo,
            "root": str(self.root),
            "revision": state.get("revision"),
            "checked_at": state.get("checked_at"),
            "check_hours": self.check_hours,
            "disk_bytes": total,
            "tables": tables,
        }

    def update(self, *, all_partitions: bool = False) -> list[dict[str, Any]]:
        """强制检查远端更新，并补齐已镜像区间（或全量）在远端新增/改写的分区。

        只处理已经镜像过的表：没镜像过的表不主动拉全量（除 ``all_partitions``）。
        """
        state = self._read_state()
        tables_state = state.get("tables") or {}
        candidates = [
            table
            for table in TABLE_SPECS
            if all_partitions or self._mirrored_keys(table, tables_state.get(table) or {})
        ]
        if not candidates:
            return []

        # 先把候选表的远端清单刷新一遍（一次 revision 检查），再据此规划下载量。
        with self._lock:
            changed = self._refresh_remote(state, candidates, force=True)
            if changed:
                self._write_state(state)

        plan: list[tuple[str, dict[str, Any]]] = []
        for table in candidates:
            spec = self.spec(table)
            tstate = (state.get("tables") or {}).get(table) or {}
            mirrored = self._mirrored_keys(table, tstate)
            kwargs: dict[str, Any] = {"all_partitions": all_partitions}
            if not all_partitions:
                if spec.layout == "daily":
                    # 只复查已镜像的分区，外加「最新已镜像日之后」的新分区。
                    # 不从最早镜像日往后全拉：稀疏镜像（只读过几天）会因此把整段
                    # 历史都下下来（l1_l2_factors 一年多就是几 GB）。
                    remote = sorted(tstate.get("files") or {})
                    tail = [d for d in remote if d > mirrored[-1]]
                    gap = _days_between(mirrored[-1], remote[-1]) if remote else 0
                    if gap > _UPDATE_MAX_GAP_DAYS and tail:
                        _log(
                            f"{table} 最新已镜像 {mirrored[-1]}，距最新 {remote[-1]} 已 {gap} 天，"
                            f"跳过自动补齐（{len(tail)} 个分区，约 "
                            f"{self._tail_size_mb(tstate, tail):.0f} MB）；"
                            f"需要时执行 dataset --sync --tables {table} "
                            f"--start-date {mirrored[-1][:4]}-{mirrored[-1][4:6]}-{mirrored[-1][6:]}"
                        )
                        tail = []
                    kwargs["keys"] = sorted(set(mirrored) | set(tail))
                elif spec.layout == "per_symbol":
                    # 只复查已镜像的标的文件，新增标的需显式 sync。
                    kwargs["symbols"] = [k[: -len(".parquet")] for k in mirrored]
                # single 布局文件少，整目录复查
            plan.append((table, kwargs))
        if not plan:
            return []

        # 清单已刷新，下面每张表的 sync 不会再问远端 revision。
        return [self.sync(table, **kwargs) for table, kwargs in plan]


_STORE: QuantDBStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> QuantDBStore:
    """进程内复用同一个 store（下载锁、索引状态共享）。"""
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = QuantDBStore()
    return _STORE


__all__ = [
    "DEFAULT_REPO",
    "QuantDBError",
    "QuantDBStore",
    "TABLES",
    "dataset_symbol",
    "get_store",
    "is_etf_symbol",
]
