"""可插拔策略包：在子模块中导出 STRATEGY，由 registry 自动注册。"""

from __future__ import annotations

from app.strategies.registry import STRATEGIES, get_strategy

__all__ = ["STRATEGIES", "get_strategy"]
