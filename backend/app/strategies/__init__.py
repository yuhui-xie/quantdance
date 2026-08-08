"""可插拔策略包：在子模块中导出 STRATEGY，由 registry 自动注册。"""

from __future__ import annotations

from app.strategies.registry import (
    ALL_STRATEGIES,
    CROSS_SECTION_STRATEGIES,
    STRATEGIES,
    get_cross_section_strategy,
    get_registered_strategy,
    get_strategy,
    list_cross_section_strategies,
)

__all__ = [
    "ALL_STRATEGIES",
    "CROSS_SECTION_STRATEGIES",
    "STRATEGIES",
    "get_cross_section_strategy",
    "get_registered_strategy",
    "get_strategy",
    "list_cross_section_strategies",
]
