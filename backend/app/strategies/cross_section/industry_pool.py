"""ETF 动态行业池发现：按决策日 asof 从全市场 etf 面板重构"细分行业方向代表池"。

背景：静态细分池（如 config/etf_core_sub_pool.json）是用期末数据手工/脚本挑选的
固定清单，回测早期年份沿用"期末才知谁流动性最好"的代表，构成成员层面的前视/幸存者
偏差（单只打分数值本身用 asof 截断、无前视）。本模块把"逐决策日重新发现池成员"抽成
共享能力：在调仓日 asof，仅用 ≤ asof 的 K 线与成交额，把当时已上市、满足流动性门槛
的细分方向 ETF 选成"每个方向 ≤3 只、行业内做日收益相关性去冗余、再分层填充上限 90"
的候选子池。

方向 = 38 个有序方向（1-4 宽基 / 5-30 行业 / 31-33 策略 / 34-38 海外）。排除货币、
债券、现金及"无法细分"品种。作为池成员需：上市满 6 个月；近 120 个交易日中有 ≥60 个
成交日；行业内按历史日均成交额排序。

无前视保证：所有流动性/相关性/上市判定窗口只读取 ``value_df`` 中 ``date <= asof`` 的行，
且按 ``tail(days)`` 截断，绝不引用未来。发现阶段纯内存（复用 runner 一次性载入的全历史
``ctx.panel``），不逐月重拉行情。

本模块只依赖 stdlib 与 numpy/pandas，不依赖 ``app.strategies.base``，避免循环导入
（同 asof_pool.py 约定）。
"""

from __future__ import annotations

from typing import Mapping

import pandas as pd

# ---------------------------------------------------------------------------
# 38 个方向的有序列表（顺序即填充分层时同层内的优先级、以及报告顺序）。
# ---------------------------------------------------------------------------
DIRECTION_ORDER: tuple[str, ...] = (
    # 1-4 宽基
    "大盘宽基",
    "中小盘宽基",
    "成长宽基",
    "宽基",
    # 5-30 行业
    "半导体",
    "AI算力",
    "数字软件",
    "通信",
    "消费电子",
    "高端制造",
    "创新医药",
    "医疗健康",
    "食品饮料",
    "大众消费",
    "农业",
    "银行",
    "非银金融",
    "金融科技",
    "光伏",
    "电池汽车",
    "新能源",
    "有色矿业",
    "基础材料",
    "煤炭",
    "石油能源",
    "黄金",
    "国防航空",
    "传媒娱乐",
    "地产基建",
    "公用环保",
    # 31-33 策略
    "红利策略",
    "质量价值",
    "央企国企",
    # 34-38 海外/跨境
    "港股科技",
    "港股宽基",
    "美国市场",
    "亚太欧洲",
    "海外",
)

# 方向 -> 名称关键词。首个命中即定（见 classify_industry）。匹配顺序经过调整：
# 具体行业/策略/海外方向在前，四个宽基方向垫底作"未命中更细方向时"的兜底，从而避免
# "中证全指半导体 / 中证500信息"之类宽基前缀把行业 ETF 误并入宽基；四个宽基之间再按
# 大盘 → 中小盘 → 成长 → 一般宽基 的相对次序取唯一命中。
SUB_RULES: list[tuple[str, tuple[str, ...]]] = [
    # ---- 5-30 行业（细分优先）----
    ("半导体", ("半导体", "芯片", "集成电路", "晶圆", "半导体设备", "半导体材料")),
    ("AI算力", ("人工智能", "算力", "AIGC", "云计算")),  # 云计算归 AI/算力，与数字软件区隔存疑，见验证
    ("数字软件", ("软件", "计算机", "信息技术", "大数据", "信创", "信息安全", "网络安全", "数字经济")),
    ("通信", ("通信", "5G", "通信设备", "电信", "光通信", "光模块")),
    ("消费电子", ("消费电子", "电子")),
    ("高端制造", ("高端制造", "机器人", "智能制造", "高端装备", "机床", "数控机床", "工业母机", "工程机械", "通用设备", "装备产业")),
    ("创新医药", ("创新药", "医药创新", "生物创新", "CXO", "生物科技", "医药产业")),
    ("医疗健康", ("医疗", "医药", "生物", "疫苗", "中药", "血制品", "医疗器械", "医服", "制药", "卫生")),
    ("食品饮料", ("食品", "饮料", "白酒", "酒", "酿酒", "乳", "啤酒", "食饮")),
    ("大众消费", ("家电", "家用电器", "消费", "零售", "商贸", "旅游", "免税", "新零售", "日常消费", "主要消费", "饮食", "品牌")),
    ("农业", ("农业", "养殖", "畜牧", "农林牧渔", "农牧", "种植", "饲料", "粮", "猪", "渔")),
    ("银行", ("银行",)),
    ("金融科技", ("金融科技", "互联网金融", "支付")),
    ("非银金融", ("证券", "券商", "保险", "非银", "金融", "金融地产")),
    ("光伏", ("光伏",)),
    ("电池汽车", ("新能源车", "新能源汽车", "新能车", "智能汽车", "电池", "锂电", "锂", "储能", "动力电池", "汽车")),
    ("新能源", ("新能源", "风电", "碳中和", "绿色电力", "清洁能源", "光伏设备", "低碳")),
    ("有色矿业", ("有色金属", "有色", "矿业", "稀土", "铜", "稀有金属", "小金属")),
    ("基础材料", ("钢铁", "建材", "水泥", "化工", "基础化工", "新材料", "造纸", "玻璃")),
    ("煤炭", ("煤炭",)),
    ("石油能源", ("石油", "油气", "原油", "能源", "石化")),
    ("黄金", ("黄金", "贵金属", "白银", "上海金")),
    ("国防航空", ("军工", "国防", "航空", "航天", "船舶", "大飞机", "军民")),
    ("传媒娱乐", ("传媒", "游戏", "动漫", "影视", "娱乐", "文化", "体育", "文娱")),
    ("地产基建", ("房地产", "地产", "基建", "建筑", "一带一路", "城乡建设")),
    ("公用环保", ("电力", "公用", "环保", "水务", "燃气", "环境", "电网")),
    # ---- 31-33 策略（放在行业之后、宽基之前；红利/价值/央企含"低波"等策略词）----
    ("红利策略", ("红利", "高股息", "股息", "低波", "低波动")),
    ("质量价值", ("价值", "质量", "基本面", "估值", "回报", "现金流", "核心竞争力")),
    ("央企国企", ("央企", "国企", "国新", "中特估", "国资", "中央企业")),
    # ---- 34-38 海外/跨境（细目在前，恒生科技/港股科技 须先于 港股宽基）----
    ("港股科技", ("恒生科技", "恒科", "港股科技", "港股互联网", "恒生互联网", "中概互联", "中概互联网", "沪港深科技", "沪港深互联", "港股创新药")),
    ("港股宽基", ("恒生", "港股", "H股", "恒指", "香港", "港币", "沪港深", "大湾区")),
    ("美国市场", ("纳斯达克", "纳指", "标普", "道琼斯", "美国", "美股", "罗素", "MSCI美国")),
    ("亚太欧洲", ("日经", "日本", "德国", "法国", "亚太", "亚洲", "欧洲", "欧股", "越南", "印度", "韩国", "新加坡", "泰国", "泛欧")),
    ("海外", ("QDII", "全球", "海外", "跨境", "境外", "新兴市场", "国际")),
    # ---- 1-4 宽基（兜底，最后匹配；其中关键词互斥地定位具体宽基指数）----
    ("大盘宽基", ("沪深300", "上证50", "A50", "A500", "A100", "中证100", "上证180", "深证100", "深证50", "深100", "央视50", "超大")),
    ("中小盘宽基", ("中证500", "中证1000", "中证2000", "国证2000", "国证1000", "中小盘", "中盘")),
    ("成长宽基", ("创业板", "科创", "双创", "中证创业", "科创板", "科创创业", "成长")),
    ("宽基", ("中证800", "中证全指", "全指", "综指", "上证综指", "A股", "中证流通", "超大盘", "总市值", "价值指数")),
]

# 排除的品种关键词：货币、债券等非"细分权益行业方向"。发现时先命中即剔除。
# 注意：不含"现金"——"自由现金流/现金流"是权益质量因子，属"质量价值"方向，误加会把
# 整族现金流 ETF 排除；真正货币基金多以"货币/日盈/快线/财富宝"等命名，靠方向词不命中
# 天然剔除。故此处只列明确的固收/货币词。
_EXCLUDE_TOKENS: tuple[str, ...] = (
    "货币", "债", "国债", "政金债", "可转债", "转债", "城投",
    "短融", "存单", "同业存单", "理财", "逆回购", "央票", "ETF联接",
    "信用", "政府债", "基准做市", "国开",
)


def _exclude_name(name: str | None) -> bool:
    """命中货币/债券/现金等非权益工具关键词则 True（此类品种不入动态池）。"""
    n = (name or "").upper()
    for kw in _EXCLUDE_TOKENS:
        if kw.upper() in n:
            return True
    return False


def classify_industry(name: str | None) -> str | None:
    """由 ETF 名称关键词推断方向；返回 DIRECTION_ORDER 中之一，无法细分则 None。

    首个命中即定。未命中的（含不在 taxonomy 内的跨域/新品类）返回 None——发现阶段跳过，
    即"无法细分"品种被排除。货币/债券类请配合 :func:`_exclude_name` 先行剔除。
    """
    if _exclude_name(name):
        return None
    n = (name or "").upper()
    for cat, kws in SUB_RULES:
        for kw in kws:
            if kw.upper() in n:
                return cat
    return None


# ---------------------------------------------------------------------------
# as-of 辅助：一律只读 value_df 中 date <= asof 的行。
# ---------------------------------------------------------------------------


def _asof_rows(value_df: pd.DataFrame, asof: str) -> pd.DataFrame:
    """返回按日期升序、去重的 ≤ asof 行（date 取日期部分比较）。"""
    v = value_df.copy()
    if "date" not in v.columns:
        return v.iloc[0:0]
    v["_d"] = v["date"].astype(str).str[:10]
    v = v[v["_d"] <= str(asof)[:10]]
    v = v.sort_values("_d").drop_duplicates("_d", keep="last")
    return v


def _listed_months(value_df: pd.DataFrame, asof: str, min_listing_days: int) -> bool:
    """上市时长判定：首根 K 线距 asof 至少 min_listing_days 个自然日（近似满 N 个月）。

    "上市满 N 个月"用日历日近似；月数不整除时按 31 天保守化处理由调用方换算。
    """
    rows = _asof_rows(value_df, asof)
    if rows.empty:
        return False
    try:
        first = pd.Timestamp(rows["_d"].iloc[0])
        asof_ts = pd.Timestamp(str(asof)[:10])
    except Exception:
        return False
    return int((asof_ts - first).days) >= min_listing_days


def _turnover_stats(
    value_df: pd.DataFrame,
    asof: str,
    window_days: int,
) -> tuple[float | None, int]:
    """近 ``window_days`` 个交易日(≤asof)的成交情况。

    返回 ``(历史日均成交额, 有效成交日数)``。只保留有正成交额/量的行（停牌/缺失不计为
    成交日）。amount 缺失时回退 volume；两者都缺返回 (None, 0)。日均成交额仅对有效日
    求均值，避免停牌日把流动性均值拉低。作为行业内排序依据（同行业同量纲比较安全）。
    """
    cols = ["date"]
    col = None
    for cand in ("amount", "volume"):
        if cand in value_df.columns:
            cols.append(cand)
            col = cand
            break
    if col is None:
        return None, 0
    v = value_df[cols].copy()
    v["_d"] = v["date"].astype(str).str[:10]
    v = v[v["_d"] <= str(asof)[:10]]
    v = v.sort_values("_d").drop_duplicates("_d", keep="last")
    v[col] = pd.to_numeric(v[col], errors="coerce")
    v = v.tail(window_days)
    pos = v[v[col] > 0]
    valid = int(len(pos))
    if valid == 0:
        return None, 0
    avg = float(pos[col].mean())
    return avg, valid


def _asof_trailing_pct(value_df: pd.DataFrame, asof: str, days: int) -> pd.Series:
    """截至 asof 最近 ``days`` 个交易日的日收益序列（小数），日期索引、按日排序。

    复用面板现成 ``pct_change`` 列（单位 %，这里 /100），仅保留 ≤ asof。返回空 Series
    表示无足够数据。相关去冗余专用，绝对值量纲与方向无关。
    """
    if "pct_change" not in value_df.columns or "date" not in value_df.columns:
        return pd.Series(dtype=float)
    v = value_df[["date", "pct_change"]].copy()
    v["_d"] = v["date"].astype(str).str[:10]
    v = v[v["_d"] <= str(asof)[:10]]
    v = v.sort_values("_d").drop_duplicates("_d", keep="last")
    v["pct_change"] = pd.to_numeric(v["pct_change"], errors="coerce")
    v = v.dropna(subset=["pct_change"])
    if v.empty:
        return pd.Series(dtype=float)
    ret = (v["pct_change"].astype(float) / 100.0).set_axis(
        pd.to_datetime(v["_d"])
    )
    return ret.tail(days)


def _corr_ok(
    cand_pct: pd.Series,
    accepted_pct: dict[str, pd.Series],
    threshold: float,
    corr_days: int,
) -> bool:
    """候选与同方向每个已接受者做日收益相关，全部 < threshold 才接受。

    用 ``join='inner'`` 对齐（两标的上市日/停牌日历可能不同），截 ``tail(corr_days)``
    ——均只含 ≤ asof 数据。重叠样本不足视为不相关（防御性接受）。accepted 有每方向
    上限，naive O(accepted) 成本可控。
    """
    for acc_pct in accepted_pct.values():
        df = pd.concat([cand_pct, acc_pct], axis=1, join="inner").dropna().tail(corr_days)
        if len(df) < 2:
            continue  # 样本不足，按不相关接受
        c = float(df.corr().iloc[0, 1])
        if c >= threshold:
            return False
    return True


def discover_industry_pool(
    panel: Mapping[str, Mapping[str, pd.DataFrame]],
    names: Mapping[str, str],
    asof: str,
    *,
    min_listing_days: int = 182,
    window_days: int = 120,
    min_valid_days: int = 60,
    per_direction: int = 3,
    max_total: int = 90,
    corr_days: int = 60,
    corr_threshold: float = 0.7,
) -> list[str]:
    """在 asof 时点从全市场 ``panel`` 发现细分方向候选池，返回有序 symbol 列表。

    ``panel`` 形如 ``{symbol: {"value": DataFrame}}``（runner 一次性载入的全历史），
    本函数每调用只读 ``date <= asof`` 的行，保证无前视。流程：

    1. 分桶：遍历 panel，排除货币/债券/现金/无法细分(name 不命中任何方向)者；要求
       上市满 ``min_listing_days``、近 ``window_days`` 内有效成交日 ≥ ``min_valid_days``；
       同方向内记录历史日均成交额与 ``corr_days`` 日收益序列。
    2. 每方向按历史日均成交额降序排序，贪心接受日收益相关 < ``corr_threshold`` 者，
       最多 ``per_direction`` 只（保证代表性与彼此低相关）。
    3. 分层填充：按方向在 :data:`DIRECTION_ORDER` 中顺序，先取各方向第 1 名、再第 2 名、
       再第 3 名……直到总数达 ``max_total``（默认 90），保证"每个方向第 1 名优先进入"。

    无任何可分类/可交易候选时返回空列表。
    """
    by_dir: dict[str, list[tuple[str, float, pd.Series]]] = {}
    for symbol, payload in panel.items():
        value_df = payload.get("value") if isinstance(payload, Mapping) else None
        if value_df is None or getattr(value_df, "empty", True):
            continue
        name = names.get(symbol)
        direction = classify_industry(name)
        if direction is None:
            continue
        if not _listed_months(value_df, asof, min_listing_days):
            continue
        avg, valid = _turnover_stats(value_df, asof, window_days)
        if avg is None or valid < min_valid_days:
            continue
        pct = _asof_trailing_pct(value_df, asof, corr_days)
        by_dir.setdefault(direction, []).append((symbol, avg, pct))

    # 每方向：按历史日均成交额降序 + 贪心去冗余，至多 per_direction 只。
    picked: dict[str, list[str]] = {}
    for direction, bucket in by_dir.items():
        bucket.sort(key=lambda t: -t[1])
        accepted: list[str] = []
        accepted_pct: dict[str, pd.Series] = {}
        for symbol, _avg, pct in bucket:
            if len(accepted) >= per_direction:
                break
            if _corr_ok(pct, accepted_pct, corr_threshold, corr_days):
                accepted.append(symbol)
                accepted_pct[symbol] = pct
        if accepted:
            picked[direction] = accepted

    # 分层填充：先所有方向的第 1 名，再第 2 名……封顶 max_total，方向顺序取 DIRECTION_ORDER。
    out: list[str] = []
    max_rank = max((len(v) for v in picked.values()), default=0)
    for rank in range(max_rank):
        for direction in DIRECTION_ORDER:
            members = picked.get(direction)
            if members is None or rank >= len(members):
                continue
            out.append(members[rank])
            if len(out) >= max_total:
                return out
    return out


def industry_pool_meta() -> dict[str, object]:
    """返回本模块的池语义元数据，供文档/诊断用。"""
    return {
        "mode": "dynamic-industry-asof",
        "direction_count": len(DIRECTION_ORDER),
        "directions": list(DIRECTION_ORDER),
        "excluded": "money/bond/cash/unclassifiable",
        "listing_min": "6 months",
        "valid_days": ">=60 within recent 120 sessions",
        "selection": "per-direction liquidity sort + intra-direction corr prune, per-dir<=3",
        "fill": "layered by direction rank, direction order in DIRECTION_ORDER, total cap 90",
        "reused_by": ("etf_rotation (pool_discovery)",),
    }
