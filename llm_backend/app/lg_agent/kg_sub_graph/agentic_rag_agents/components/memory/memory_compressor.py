"""
对话历史压缩器

为什么需要压缩？
    30 轮对话 = 60 条消息，全部发给 LLM 会消耗大量 token。
    但直接丢弃老消息会丢失关键信息。

    解决方案：用 LLM 把老对话压缩成摘要，保留关键信息，大幅减少 token。

两层压缩：
    中等摘要（最近 6-15 轮）：保留主要内容和结论，省略寒暄和重复
    高层摘要（16 轮以前）：进一步压缩为极简总结，只保留最关键信息
"""

from typing import List, Any, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.core.logger import get_logger

logger = get_logger(service="memory_compressor")


class ConversationSummary(BaseModel):
    """对话摘要的结构化输出"""
    summary: str = Field(description="压缩后的对话摘要")
    key_entities: List[str] = Field(
        default_factory=list,
        description="对话中提到的关键实体（产品名、品牌等）"
    )
    user_intents: List[str] = Field(
        default_factory=list,
        description="用户的主要意图列表"
    )


MEDIUM_COMPRESS_PROMPT = """你是一个对话摘要专家。
请将以下电商客服对话压缩为简洁的摘要。

要求：
1. 保留关键信息：产品名、价格、库存状态、政策结论
2. 保留用户的决策和偏好（买了什么、没买什么、满意什么）
3. 省略寒暄、重复、无关内容
4. 控制在 200 字以内
5. 列出提到的关键实体和用户意图

对话内容：
{conversation}
"""


HIGH_COMPRESS_PROMPT = """你是一个对话摘要专家。
请将以下对话摘要进一步压缩为一段极简总结。

要求：
1. 只保留最关键的信息：用户身份、购买历史、核心需求
2. 控制在 100 字以内
3. 列出最重要的实体

摘要内容：
{conversation}
"""


def _format_messages(messages: List[Any]) -> str:
    """将消息列表格式化为可读的对话文本。"""
    lines = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            role = "用户"
        elif isinstance(msg, AIMessage):
            role = "助手"
        elif isinstance(msg, dict):
            role = msg.get("role", "unknown")
        else:
            role = "unknown"
        content = msg.content if hasattr(msg, "content") else str(msg)
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def compress_medium(
    llm: BaseChatModel,
    messages: List[Any],
) -> ConversationSummary:
    """
    中等压缩：适合最近 6-15 轮的对话。
    保留主要内容，省略寒暄和重复，输出约 200 字。
    """
    if not messages:
        return ConversationSummary(summary="", key_entities=[], user_intents=[])

    conversation = _format_messages(messages)

    prompt = ChatPromptTemplate.from_messages([
        ("system", MEDIUM_COMPRESS_PROMPT),
    ])

    chain = prompt | llm.with_structured_output(ConversationSummary)
    result = await chain.ainvoke({"conversation": conversation})

    logger.info("中等压缩: {} 条消息 -> {} 字, 实体: {}", len(messages), len(result.summary), result.key_entities)
    return result


async def compress_high(
    llm: BaseChatModel,
    previous_summary: str,
    new_messages: Optional[List[Any]] = None,
) -> ConversationSummary:
    """
    高层压缩：适合 16 轮以前的对话。
    进一步压缩为极简总结，只保留最关键信息，输出约 100 字。
    """
    if not previous_summary and not new_messages:
        return ConversationSummary(summary="", key_entities=[], user_intents=[])

    content = previous_summary
    if new_messages:
        content += "\n\n新增对话:\n" + _format_messages(new_messages)

    prompt = ChatPromptTemplate.from_messages([
        ("system", HIGH_COMPRESS_PROMPT),
    ])

    chain = prompt | llm.with_structured_output(ConversationSummary)
    result = await chain.ainvoke({"conversation": content})

    logger.info("高层压缩: -> {} 字, 实体: {}", len(result.summary), result.key_entities)
    return result
