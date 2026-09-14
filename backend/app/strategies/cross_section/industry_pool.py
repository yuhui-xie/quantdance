"""ETF 动态行业池发现：按决策日 asof 从全市场 etf 面板重构"细分行业方向代表池"。

背景：静态细分池（如 config/etf_core_sub_pool.json）是用期末数据手工/脚本挑选的
固定清单，回测早期年份沿用"期末才知谁流动性最好"的代表，构成成员层面的前视/幸存者
偏差（单只打分数值本身用 asof 截断、无前视）。本模块把"逐决策日重新发现池成员"抽成
共享能力：在调仓日 asof，仅用 ≤ asof 的 K 线与成交额，把当时已上市、满足流动性门槛
的细分方向 ETF 选成"每个方向 ≤2 只、行业内做日收益相关性去冗余、再分层填充上限 90"
的候选子池。

方向 = 9 个大类、大类内再拆细分方向（细分为主、大类兜底）的候选桶。大类及顺序：
宽基 / 科技 / 医药 / 消费 / 金融 / 新能源 / 资源周期 / 其他行业 / 防御及跨境。宽基大类
只保留 沪深300/科创板/创业板 三只；科技、医药、消费、新能源、能源(资源周期) 各含一个
与大类同名的大类兜底桶。排除货币、同业存单/短融等现金、除 5年国债外的全部债券、无法
细分 ETF、以及 恒生(国企)/纳斯达克 之外的跨境品种。作为池成员需：上市满 6 个月；近
120 个交易日中有 ≥60 个成交日；行业内按历史日均成交额排序。

无前视保证：所有流动性/相关性/上市判定窗口只读取 ``value_df`` 中 ``date <= asof`` 的行，
且按 ``tail(days)`` 截断，绝不引用未来。发现阶段纯内存（复用 runner 一次性载入的全历史
``ctx.panel``），不逐月重拉行情。另见 :class:`IndustryPoolIndex`——把 panel 预计算成
按日切片的下标索引，避免每个决策日重扫全表。

本模块只依赖 stdlib 与 numpy/pandas，不依赖 ``app.strategies.base``，避免循环导入
（同 asof_pool.py 约定）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 大类（category）与其细分方向（direction/桶）。顺序即大类层级的填充分层优先级：
# 宽基 / 科技 / 医药 / 消费 / 金融 / 新能源 / 资源周期 / 其他行业 / 防御及跨境。
# 各细分方向里，若该大类含一个"与大类同名"的兜底桶（科技/医药/消费/新能源/能源，
# 见 CATEGORY_FALLBACKS），未命中更细方向、但确属该大类的 ETF（如"科技ETF"）落入其中。
# DIRECTION_ORDER 是全部细分方向桶的有序扁平列表（去兜底同名词），顺序即报告顺序、
# 也是填充分层"同层内先取谁"的依据。
# ---------------------------------------------------------------------------
CATEGORY_ORDER: tuple[str, ...] = (
    "宽基", "科技", "医药", "消费", "金融", "新能源", "资源周期", "其他行业", "防御及跨境",
)

# 大类 -> 该大类的细分方向（桶）。大类兜底桶由 CATEGORY_FALLBACKS 显式标注。
DIRECTIONS: dict[str, tuple[str, ...]] = {
    "宽基": ("沪深300", "科创板", "创业板"),
    "科技": ("半导体", "人工智能", "云计算", "通信", "计算机", "消费电子", "科技"),
    "医药": ("创新药", "中药", "医疗器械", "生物医药", "医药"),
    "消费": ("白酒", "食品饮料", "家电", "农业养殖", "消费"),
    "金融": ("银行", "证券", "保险"),
    "新能源": ("光伏", "电池", "新能源车", "风电", "新能源"),
    "资源周期": ("有色矿业", "稀土", "钢铁", "化工", "煤炭", "能源"),
    "其他行业": ("军工", "传媒", "游戏动漫", "房地产", "基建", "机器人", "电力", "环保"),
    "防御及跨境": ("黄金", "红利低波", "红利", "恒生国企", "纳斯达克", "5年国债"),
}

# 各大类中充当"大类兜底"的桶名（同名词收纳"仅能识别到大类、未命中更细细分"的 ETF）。
# 科技/医药/消费/新能源 各含一个；资源周期的大类兜底用其末位桶"能源"。
CATEGORY_FALLBACKS: dict[str, str] = {
    "科技": "科技",
    "医药": "医药",
    "消费": "消费",
    "新能源": "新能源",
    "资源周期": "能源",
}

# 全部细分方向桶的有序扁平列表 = 大类顺序展平（DIRECTION_ORDER 旧称）。
DIRECTION_ORDER: tuple[str, ...] = tuple(
    d for cat in CATEGORY_ORDER for d in DIRECTIONS[cat]
)

# 方向 -> 名称关键词。首个命中即定（见 classify_industry）。匹配顺序即遍历顺序，经设计：
# 一个细分方向可能命中多个关键词；顺序上须保证"更具体的细分词先于其大类兜底词/宽基板块词"，
# 以免 科创芯片 被 科创(宽基) 吞、创业板人工智能 被 创业板(宽基) 吞、消费电子 被 消费(兜底) 吞、
# 新能源车 被 新能源(兜底) 吞。故 SUB_RULES 按"先具体、后兜底/宽基"的两段式排列：
#   段1 = 各大类的"具体细分"方向（宽基 3 只也算"具体指数桶"，放段1内靠后，见下）
#   段2 = 各大类的"大类兜底"方向（科技/医药/消费/新能源/能源），放最末兜底
# 顺序按 CATEGORY_ORDER 展开；每个大类内细分方向按其 DIRECTIONS 相对次序。
# 实现沿用"循环两个并行有序列表 → 线性扫描"的简单逻辑，保持可读。
# ---------------------------------------------------------------------------
# 注意：分类结果允许返回"科技/医药/消费/新能源/能源"等兜底桶名（它们是合法桶），
# 也允许返回宽基 3 桶名；discover 阶段它们正常参与分层填充与去冗余。
# ---------------------------------------------------------------------------

# ---- 段1：具体细分方向（各大类按 CATEGORY_ORDER 展平，兜底桶除外）----
# 顺序要点：更具体的细分词须先于"会吞它的更泛词"。因此 消费电子 先于 消费(兜底)，
# 新能源车/风电 等先于 新能源(兜底)；宽基 3 桶放本段最末（科创/创业/沪深300 是被
# "科创芯片→半导体、创业板人工智能→人工智能" 之类更细行业词压制的板块词，故最后匹配）。
_SUB_SPECIFIC: list[tuple[str, tuple[str, ...]]] = [
    # 科技
    ("半导体", ("半导体", "芯片", "集成电路", "晶圆", "半导体设备", "半导体材料")),
    ("人工智能", ("人工智能", "AI", "AIGC", "算力")),
    ("云计算", ("云计算", "云服务", "云50", "数据中心")),
    ("通信", ("通信", "5G", "5g", "通信设备", "电信", "光通信", "光模块")),
    ("计算机", ("计算机", "软件", "信息技术", "信创", "网络安全", "信息安全", "数字经济", "信息")),
    ("消费电子", ("消费电子", "电子")),
    # 医药
    ("创新药", ("创新药", "CXO", "生物科技", "生物创新")),
    ("中药", ("中药", "中成药")),
    ("医疗器械", ("医疗器械", "医疗设备", "医疗保健")),
    ("生物医药", ("生物医药", "生物", "疫苗", "血制品", "基因")),
    # 消费（食品饮料/白酒 均含"酒/饮料"等子串；食品饮料 先匹配以免"啤酒ETF"被 白酒 吞）
    ("食品饮料", ("食品饮料", "食品", "饮料", "乳", "啤酒", "食饮", "主要消费")),
    ("白酒", ("白酒", "酒", "酿酒")),
    ("家电", ("家电", "家用电器", "电器")),
    ("农业养殖", ("农业", "养殖", "畜牧", "农林牧渔", "农牧", "种植", "饲料", "种业")),
    # 金融
    ("银行", ("银行",)),
    # 注意：不能用裸"证券"——大量基金公司的发行/托管名(中银证券、上证券商等)都含"证券"，
    # 会把 A500/中证500 等非金融 ETF 误归入证券桶。只认"证券/券商"等目标词。
    ("证券", ("证券", "券商")),
    ("保险", ("保险", "非银金融")),
    # 新能源（新能源车/风电 先于 新能源兜底）
    ("光伏", ("光伏", "太阳能")),
    ("电池", ("电池", "锂电", "锂", "储能", "动力电池")),
    ("新能源车", ("新能源车", "新能源汽车", "新能车", "智能汽车", "汽车")),
    ("风电", ("风电", "风能")),
    # 资源周期
    ("有色矿业", ("有色金属", "有色", "矿业", "铜", "稀有金属")),
    ("稀土", ("稀土",)),
    ("钢铁", ("钢铁", "钢材")),
    ("化工", ("化工", "基础化工", "化学")),
    ("煤炭", ("煤炭",)),
    # 其他行业
    ("军工", ("军工", "国防", "航天", "航空", "船舶", "军民", "大飞机")),
    ("传媒", ("传媒", "影视", "文化", "娱乐", "体育", "文娱")),
    ("游戏动漫", ("游戏", "动漫", "电竞")),
    ("房地产", ("房地产", "地产")),
    ("基建", ("基建", "建筑", "一带一路", "城乡建设")),
    ("机器人", ("机器人", "智能制造", "智能机器", "机床", "高端制造")),
    ("电力", ("电力", "电网", "公用事业", "水电")),
    ("环保", ("环保", "环境", "水务", "燃气")),
    # 防御及跨境（红利低波 先于 红利；跨境仅恒生国企与纳斯达克；黄金/红利/恒生/纳指互斥词）
    ("黄金", ("黄金", "贵金属", "白银", "上海金")),
    ("红利低波", ("红利低波", "低波红利", "低波动")),
    ("红利", ("红利", "股息", "高股息")),
    ("恒生国企", ("恒生国企", "恒生中国企业")),
    ("纳斯达克", ("纳斯达克", "纳指")),
    ("5年国债", ("5年期国债", "5年国债")),
]

# ---- 段2：大类兜底方向（消费电子/新能源车 等具体桶先命中后，剩纯大类名 ETF 落此）----
_SUB_FALLBACK: list[tuple[str, tuple[str, ...]]] = [
    ("科技", ("科技", "数字经济", "软件50")),  # 科技龙头ETF/科技ETF 等纯科技大类
    ("医药", ("医药", "医疗", "医服", "制药", "卫生", "健康")),
    ("消费", ("消费", "零售", "商贸", "可选消费", "日常消费", "饮食", "品牌")),
    ("新能源", ("新能源", "绿色电力", "清洁能源", "低碳", "碳中和")),
    ("能源", ("能源", "石油", "油气", "原油", "石化", "天然气")),
]

_SUB_BOARD:  list[tuple[str, tuple[str, ...]]] = [
    # ---- 宽基 3 桶（放段1最末：科创/创业 是"板块宽基"词，被上面行业细分词压制）----
    ("科创板", ("科创板", "科创50", "科创成长", "科创100", "科创芯片", "科创")),
    ("创业板", ("创业板", "创50", "创成长", "创业")),
    ("沪深300", ("沪深300",)),
]

SUB_RULES: list[tuple[str, tuple[str, ...]]] = _SUB_SPECIFIC + _SUB_FALLBACK + _SUB_BOARD

# 排除的品种关键词：现金管理/固收/除 5年国债外的债券 / 其它跨境(除恒生国企、纳斯达克)
# 等非目标资产。发现时先命中即剔除（见 _exclude_name）。注意不含"现金"——"自由现金流/
# 现金流"是权益质量因子，误加会把整族现金流 ETF 排除；货币类用"货币/短融/存单"等具体词。
_EXCLUDE_TOKENS: tuple[str, ...] = (
    # 现金/现金管理类
    "货币", "短融", "同业存单", "存单", "逆回购", "理财", "日盈", "快线", "现金管理",
    # 债券：除 5年国债 ETF 外（5年国债 由 _is_allowlisted 优先放行）
    "债", "国债", "政金债", "可转债", "转债", "城投", "信用", "政府债", "国开",
    "公司债", "企业债", "中短债", "金融债", "基准做市", "央票",
    # 其它跨境 / 无法归入恒生国企或纳斯达克的海外市场（德国医药/日经/标普/港股红利等）。
    # "港股"/"恒生" 是泛指词，须放行白名单里的 恒生国企/纳斯达克（_is_allowlisted 先行放行）
    "QDII", "德国", "日本", "日经", "标普", "美股", "美国", "道琼斯", "法国", "欧洲",
    "欧股", "越南", "印度", "韩国", "中韩", "新加坡", "泰国", "东南亚", "巴西", "沙特", 
    "全球", "海外", "跨境", "境外",
    "新兴市场", "亚太", "港股", "港股通", "恒生", "恒生科技", "恒生互联网", "中概", "恒指",
    "港币", "香港", "沪港深", "纳斯达克", "纳指",
)


# 白名单目标资产：名字可能含通用排除词的少数放行品种。5年国债含"债/国债"；
# 恒生国企 含"恒生"、纳斯达克 QDII 可能含"QDII"。这些须在 _exclude_name 命中通用排除词前
# 先行放行，之后再由细分关键词归桶。其余跨境一律排除。
_ALLOWLIST_TOKENS: tuple[str, ...] = (
    "5年期国债", "5年国债",           # 唯一放行的债券
    "恒生国企", "恒生中国企业",       # 香港目标：恒生国企
    # "纳斯达克", "纳指",              # 美国目标：纳斯达克
)

def _is_allowlisted(name: str | None) -> bool:
    """命中白名单的目标 ETF（须优先于通用排除词放行）才 True。"""
    n = _classification_text(name)
    return any(w in n for w in _ALLOWLIST_TOKENS) or _is_board_nasdaq(name)


def _exclude_name(name: str | None) -> bool:
    """命中货币/债券(除5年国债)/现金/其它跨境等非目标关键词则 True（此类品种不入动态池）。"""
    n = _classification_text(name)
    if _is_allowlisted(name):
        return False
    for kw in _EXCLUDE_TOKENS:
        if kw.upper() in n:
            return True
    return False

def _classification_text(name: str | None) -> str:
    normalized = re.sub(r"\s+", "", name or "").upper()
    if "ETF" in normalized:
        return normalized.split("ETF", 1)[0]
    return normalized

def _is_board_nasdaq(name: str) -> bool:
    theme = _classification_text(name)
    return re.fullmatch(r"(?:纳斯达克|纳指)(?:100)?(?:指数)?", theme) is not None

def _allowlisted_direction(name: str | None) -> str | None:
    """白名单放行的跨境/债券品种固定归其所属方向（不被其它 A 股细分词抢先）。

    例：一条"纳斯达克生物科技ETF"是允许的纳斯达克 QDII，须归 纳斯达克 而非 A 股 创新药/生物
    医药桶；恒生国企/5年国债同理。此短路在扫描 SUB_RULES 前执行，保证跨域放行品种永远落回
    防御及跨境/债券自身桶。
    """
    n = _classification_text(name)
    if "恒生国企" in n or "恒生中国企业" in n:
        return "恒生国企"
    if _is_board_nasdaq(name):
        return "纳斯达克"
    if "5年期国债" in n or "5年国债" in n:
        return "5年国债"
    return None


def classify_industry(name: str | None) -> str | None:
    """由 ETF 名称关键词推断方向；返回 DIRECTION_ORDER 中之一，无法细分则 None。

    判定顺序：货币/债券(除5年国债)/现金/其它跨境 先剔除（:func:`_exclude_name`）；白名单放行
    的 恒生国企/纳斯达克/5年国债 直接由其所属桶短路归位（:func:`_allowlisted_direction`），
    避免被先扫到的 A 股细分词吞走；否则按 SUB_RULES 首个命中即定。未命中者返回 None——发现
    阶段跳过，即"无法细分"品种被排除。
    """
    allowlisted = _allowlisted_direction(name)
    if allowlisted is not None:
        return allowlisted
    if _exclude_name(name):
        return None
    normalized = _classification_text(name)
    for cat, kws in SUB_RULES:
        if any(kw.upper() in normalized for kw in kws):
            return cat
    return None


# ---------------------------------------------------------------------------
# 面板索引：把"逐决策日重扫全表"降为 O(log n + window)。
#
# discover_industry_pool 原本每个决策日都对每只 ETF 的整张 value_df 做
# copy → astype(str).str[:10] → sort_values → drop_duplicates，而这三步的结果**只取决于
# panel 本身，与 asof 无关**——81 个决策日里同一只票被重算 81 次，单次发现即达 15s 量级。
#
# 索引把每只票的「升序去重交易日 / 成交额 / 日收益」预转成已排序 ndarray（构建一次），
# 于是每个 asof 只需一次 searchsorted 定位下标、再切片，语义与原实现逐点等价：
#   * date 比较按 YYYY-MM-DD 字典序 = 按时间序（与原实现的字符串比较一致）；
#   * 同日后取"最后一行"对应稳定排序后保留每组末位；
#   * 成交额回退链 amount→volume、只对 >0 的有效日求均值；
#   * 日收益 dropna 后再截尾，单位 % 转小数。
#
# panel 在一次回测内不变，故索引可跨决策日复用；调用方（etf_rotation）把它存进
# ``ctx.cache[POOL_INDEX_CACHE_KEY]``。不传 index 时 discover_industry_pool 会即时构建
# 一份，结果与旧实现逐点等价，只是每个决策日重建一次——批量回测务必传入复用。
# ---------------------------------------------------------------------------

# ctx.cache 中存放本索引的键；供策略跨决策日复用，避免每日期重建。
POOL_INDEX_CACHE_KEY = "industry_pool_index"


@dataclass
class IndustryPoolIndex:
    """``panel`` 的一次性预计算索引；只存 ndarray，不持有 DataFrame。

    ``directions`` 只收录**可分类**（``classify_industry`` 命中）的标的——不可分类者
    （货币/债券/其它跨境/无关键词）在发现阶段本就跳过，不进索引即省掉每日逐个判定。
    各数组按 symbol 对齐：``dates``/``amounts`` 长度相同且按交易日升序去重，
    ``pct_dates``/``pct_values`` 只含 ``pct_change`` 非缺失的那些交易日（已 /100 转小数）。
    """

    directions: dict[str, str] = field(default_factory=dict)
    dates: dict[str, np.ndarray] = field(default_factory=dict)
    amounts: dict[str, np.ndarray] = field(default_factory=dict)
    pct_dates: dict[str, np.ndarray] = field(default_factory=dict)
    pct_values: dict[str, np.ndarray] = field(default_factory=dict)


def _asof_days(asof: str) -> np.datetime64:
    """把 asof 解析成 ``datetime64[D]``；供 searchsorted 与日期差值使用。"""
    try:
        return np.datetime64(pd.Timestamp(str(asof)[:10]).date(), "D")
    except Exception as exc:
        raise ValueError(f"无效的 asof 日期: {asof!r}") from exc


def build_industry_pool_index(
    panel: Mapping[str, Mapping[str, pd.DataFrame]],
    names: Mapping[str, str],
) -> IndustryPoolIndex:
    """把 ``panel`` 预计算成 :class:`IndustryPoolIndex`（一次 O(总行数) 扫描）。

    只处理 ``classify_industry(name)`` 命中的标的；不可分类者直接跳过。每只票：
    解析 date（解析失败的 NaT 行丢弃，与原实现里 "NaT" 字符串大于任何 ISO 日期、
    被 ``<= asof`` 过滤掉等价）、按时间稳定排序、同交易日保留末行，再切出
    成交额（amount 缺失回退 volume，两者都缺则全 NaN）与有效日收益序列。
    """
    index = IndustryPoolIndex()
    for symbol, payload in panel.items():
        value_df = payload.get("value") if isinstance(payload, Mapping) else None
        if value_df is None or getattr(value_df, "empty", True):
            continue
        direction = classify_industry(names.get(symbol))
        if direction is None:
            continue
        if "date" not in value_df.columns:
            continue

        # 解析失败的 date 先剔除：原实现里它们会变成 "NaT" 字符串，因 "N" > "2" 而
        # 通不过 `_d <= asof` 的字符串比较，同样进不了任何统计。
        parsed = pd.to_datetime(value_df["date"], errors="coerce")
        valid = parsed.notna().to_numpy()
        if not valid.any():
            continue
        parsed = parsed[valid]
        row_count = len(parsed)
        order = np.argsort(parsed.to_numpy(), kind="stable")
        days = parsed.to_numpy()[order].astype("datetime64[D]")
        # 同交易日只保留排序后的末行（对应原 sort_values + drop_duplicates(keep="last")）。
        keep = np.ones(row_count, dtype=bool)
        if row_count > 1:
            keep[:-1] = days[1:] != days[:-1]
        days = days[keep]
        if len(days) == 0:
            continue
        # 与 days 对齐的「原始行号」：其他列用它一次索引即可对齐到同一批交易日。
        rows = np.flatnonzero(valid)[order][keep]

        amount_col = next(
            (c for c in ("amount", "volume") if c in value_df.columns), None
        )
        pct = _aligned_column(value_df, rows, "pct_change")
        pct_ok = ~np.isnan(pct)

        index.directions[symbol] = direction
        index.dates[symbol] = days
        index.amounts[symbol] = _aligned_column(value_df, rows, amount_col)
        index.pct_dates[symbol] = days[pct_ok]
        index.pct_values[symbol] = pct[pct_ok] / 100.0
    return index


def _aligned_column(
    value_df: pd.DataFrame,
    rows: np.ndarray,
    column: str | None,
) -> np.ndarray:
    """取 ``column`` 并按 ``rows``（已升序去重的原始行号）对齐；缺列返回全 NaN。

    缺列返回 NaN 而非报错，与旧实现等价：成交额列缺失时该票的有效成交日为 0，被流动性
    门槛挡掉；日收益列缺失时其日收益序列为空，相关判定按"样本不足"防御性接受。
    """
    if column is None or column not in value_df.columns:
        return np.full(len(rows), np.nan)
    return pd.to_numeric(value_df[column], errors="coerce").to_numpy()[rows]


def _asof_trailing_pct_series(
    index: IndustryPoolIndex,
    symbol: str,
    asof_d: np.datetime64,
    days: int,
) -> pd.Series:
    """由索引重建截至 asof、最近 ``days`` 个有日收益交易的序列（小数，日期索引）。

    语义与旧实现的逐日 ``pct_change`` 切片一致，只是数据来自预计算数组。**仍返回 pandas
    Series**：:func:`_corr_ok` 按 ``join='inner'`` 以日期对齐（不同标的停牌/上市日历不同），
    保留 Series 才能让相关性判定与旧实现逐位一致——改写成裸 ndarray 后若图省事按位置
    取尾对齐，真数据上会算错重叠窗口。
    """
    dates = index.pct_dates[symbol]
    cutoff = int(np.searchsorted(dates, asof_d, side="right"))
    if cutoff == 0:
        return pd.Series(dtype=float)
    tail_dates = dates[max(0, cutoff - days):cutoff]
    tail_values = index.pct_values[symbol][max(0, cutoff - days):cutoff]
    return pd.Series(tail_values, index=pd.to_datetime(tail_dates))


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
    per_direction: int = 2,
    max_total: int = 90,
    corr_days: int = 60,
    corr_threshold: float = 0.7,
    index: IndustryPoolIndex | None = None,
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

    ``index`` 为 :func:`build_industry_pool_index` 预计算的面板索引：panel 在一次回测内
    不变，跨决策日复用它可把每次发现从"重扫全表 + 逐票排序去重"降到按 asof 二分切片
    （实测单次 15.9s → ~0.03s，池结果不变）。**不传则在本函数内即时构建**，行为与旧实现
    逐点等价，只是每个决策日重建一次——批量回测应传入复用。
    """
    pool_index = (
        index if index is not None else build_industry_pool_index(panel, names)
    )
    asof_d = _asof_days(asof)

    by_dir: dict[str, list[tuple[str, float, pd.Series]]] = {}
    for symbol, direction in pool_index.directions.items():
        days = pool_index.dates[symbol]
        # ≤ asof 的行数；days 已升序去重，searchsorted 直接给出截至位。
        cutoff = int(np.searchsorted(days, asof_d, side="right"))
        if cutoff == 0:
            continue
        # 上市时长：首根 K 线距 asof 的自然日数（原用日历日近似"满 N 个月"）。
        if int((asof_d - days[0]).astype("int64")) < min_listing_days:
            continue
        window = pool_index.amounts[symbol][max(0, cutoff - window_days):cutoff]
        positive = window[window > 0]  # NaN 比较为 False，自动排除停牌/缺失日
        if len(positive) < min_valid_days:
            continue
        avg = float(positive.mean())
        pct = _asof_trailing_pct_series(pool_index, symbol, asof_d, corr_days)
        by_dir.setdefault(direction, []).append((symbol, avg, pct))

    # 每方向：按历史日均成交额降序 + 贪心去冗余，至多 per_direction 只。
    picked: dict[str, list[str]] = {}
    for direction, bucket in by_dir.items():
        bucket.sort(key=lambda item: (-item[1], item[0]))
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
        "category_order": list(CATEGORY_ORDER),
        "categories": {
            cat: [d for d in DIRECTIONS[cat]]
            for cat in CATEGORY_ORDER
        },
        "fallbacks": dict(CATEGORY_FALLBACKS),
        "direction_count": len(DIRECTION_ORDER),
        "directions": list(DIRECTION_ORDER),
        "excluded": "cash/money + all bonds except 5yTreasury + unclassifiable + non-HSCEI/Nasdaq cross-border",
        "listing_min": "6 months",
        "valid_days": ">=60 within recent 120 sessions",
        "selection": "per-direction liquidity sort + intra-direction corr prune, per-dir<=2",
        "fill": "layered by direction rank, direction order in DIRECTION_ORDER, total cap 90",
        "reused_by": ("etf_rotation (pool_discovery)",),
    }
