"""
知识库查询提示词模块

提供统一的提示词管理功能，简化prompts的导入和使用
"""

from .kg_prompts import PLANNER_SYSTEM_PROMPT

__all__ = [
    "PLANNER_SYSTEM_PROMPT",
]
