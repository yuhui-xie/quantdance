"""交易时段口径：判定「今天这根日线 bar 是否已经定稿」。

**为什么需要它**：TDX 在交易时段内会把**当日尚未收盘**的日线 bar（datetime 记为该日
15:00、OHLC 是此刻的快照）当作最后一根返回。它一旦被当作「今日收盘价」写进 K 线缓存，
盘中/盘后的打分、选股、回测就会用一根随时在变的半日 bar 当收盘价——同一天早上和晚上
跑同一次回测会得到不同结果，而缓存的新鲜度判据只看「是否今天拉的」，不会察觉这件事。

**时间口径**：本项目所有时间判据一律为**本地墙钟**，与
`AStockDataSDK._today()`（`datetime.now().date()`）、K 线缓存 `cached_at` 同一口钟。
换时区会让「今天」与「bar 日期」互相矛盾，故不引入任何时区转换。

**不引入交易日历**：判据只依赖「bar 日期 == 今天」，与「今天是不是交易日」无关——非
交易日 TDX 根本不会给出今天的 bar，自然不命中。仓库里现有的日历一律从面板 `date` 列
反推，不另建表。
"""

from __future__ import annotations

from datetime import date, datetime, time

# 当日日线定稿时刻（本地墙钟）。沪深收盘集合竞价 14:57-15:00，TDX 侧定稿还要一两分钟，
# 取 15:05 留出余量。取数侧（丢弃未收盘 bar）与缓存新鲜度判据共用这一个常量。
SESSION_BAR_FINAL = time(15, 5)


def is_before_bar_final(now: datetime | None = None) -> bool:
    """此刻是否早于当日日线定稿时刻。``now`` 仅供测试注入。"""
    if now is None:
        now = datetime.now()
    return now.time() < SESSION_BAR_FINAL


def bar_date(value: object) -> str | None:
    """从 bar 的 datetime 取 ``YYYY-MM-DD``；取不到日期返回 None。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else None


def is_incomplete_bar(value: object, now: datetime | None = None) -> bool:
    """``value`` 是否为「今天、且此刻尚未定稿」的未收盘 bar。

    只在「bar 日期 == 今天」且「早于 :data:`SESSION_BAR_FINAL`」时成立。历史 bar 一律
    返回 False——它们早已定稿，与本判据无关。
    """
    day = bar_date(value)
    if day is None:
        return False
    if now is None:
        now = datetime.now()
    if day != now.date().isoformat():
        return False
    return is_before_bar_final(now)
