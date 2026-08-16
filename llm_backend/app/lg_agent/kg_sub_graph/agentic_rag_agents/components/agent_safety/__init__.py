"""
Agent 安全护栏

5 大防线：
    ① 最大迭代次数 — 防止工具调用死循环
    ② 全局护栏 — 所有路径的经营范围检查
    ③ Token/调用预算 — 防止成本爆炸
    ④ 幻觉检测闭环 — 检测到幻觉自动修正
    ⑤ 响应超时 — 防止请求卡死
"""

from .safety_guards import (
    MaxIterationGuard,
    TimeoutGuard,
)
from .scope_guard import ScopeGuard
from .budget_guard import BudgetGuard
from .hallucination_guard import HallucinationGuard

__all__ = [
    "MaxIterationGuard",
    "TimeoutGuard",
    "ScopeGuard",
    "BudgetGuard",
    "HallucinationGuard",
]
