"""
Agent 安全护栏

6 大防线：
    ① 最大迭代次数 — 防止工具调用死循环
    ② Cypher 安全校验 — 防止危险数据库操作
    ③ 全局护栏 — 所有路径的经营范围检查
    ④ Token/调用预算 — 防止成本爆炸
    ⑤ 幻觉检测闭环 — 检测到幻觉自动修正
    ⑥ 响应超时 — 防止请求卡死
"""

from .safety_guards import (
    MaxIterationGuard,
    CypherSafetyValidator,
    TimeoutGuard,
)
from .scope_guard import ScopeGuard
from .budget_guard import BudgetGuard
from .hallucination_guard import HallucinationGuard

__all__ = [
    "MaxIterationGuard",
    "CypherSafetyValidator",
    "TimeoutGuard",
    "ScopeGuard",
    "BudgetGuard",
    "HallucinationGuard",
]
