"""
Agent 安全护栏

2 大防线：
    ① 全局护栏 — 所有路径的经营范围检查（ScopeGuard）
    ② 响应超时 — 防止请求卡死（TimeoutGuard）
"""

from .safety_guards import TimeoutGuard
from .scope_guard import ScopeGuard

__all__ = [
    "TimeoutGuard",
    "ScopeGuard",
]
