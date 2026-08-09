"""
查询改写模块：多查询生成 + HyDE（假设文档嵌入）

多查询生成（Multi-Query Generation）：
    从不同角度生成多个查询版本，提高检索召回率。
    原理：用户的表述方式可能和文档的表述方式不同，
    生成多个版本能覆盖更多语义空间。

HyDE（Hypothetical Document Embedding）：
    让 LLM 先生成一个"假想答案"，用假想答案去做向量检索。
    原理：假想答案中会包含专业术语和相关概念，
    语义上比用户的口语化问题更接近真实文档，检索效果更好。
"""

import asyncio
from typing import List

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.core.logger import get_logger

logger = get_logger(service="query_rewriting")


# ==================== 数据模型 ====================


class MultiQueryOutput(BaseModel):
    """多查询生成的结构化输出"""
    queries: List[str] = Field(
        description="从不同角度改写的查询列表，包含3个版本"
    )


class ContextRewrittenOutput(BaseModel):
    """上下文感知改写的结构化输出"""
    rewritten_query: str = Field(
        description="结合对话历史补全指代和省略后的完整问题"
    )
    is_context_dependent: bool = Field(
        description="当前问题是否依赖上下文（包含代词、省略等）"
    )


class RewrittenQuery(BaseModel):
    """查询改写的最终结果"""
    original_query: str          # 原始问题
    multi_queries: List[str]     # 多查询生成结果（含原始问题，共4个）
    hypothetical_answer: str     # HyDE 假设性答案
    enhanced_query: str          # 增强查询（原始问题 + HyDE 线索）


# ==================== 提示词 ====================

MULTI_QUERY_SYSTEM_PROMPT = """你是一个电商客服系统的查询改写助手。
你的任务是：从不同角度重新表述用户的问题，帮助系统检索到更多相关信息。

规则：
1. 生成3个不同角度的版本
2. 保持原始问题的核心意图不变
3. 第一个版本：用更专业/规范的术语重新表述
4. 第二个版本：从问题的对立面或相关场景角度提问
5. 第三个版本：把问题拆解为更具体的子问题

示例：
原始: "灯泡不亮了"
  版本1: "智能灯泡无法点亮，可能的原因和排查方法"
  版本2: "智能灯泡故障诊断，指示灯状态说明"
  版本3: "智能灯泡不亮的售后维修流程和退换货政策"
"""

HYDE_SYSTEM_PROMPT = """你是一个电商领域的专家客服。
请为用户的咨询问题写一段专业的回答。

注意：
- 你不需要知道确切答案，只需要写一段合理的、专业的回答
- 回答中要包含相关产品的专业术语和关键词
- 这段文字将用于在知识库中检索相关文档
- 回答控制在100-200字之间
"""

CONTEXT_REWRITE_SYSTEM_PROMPT = """你是一个多轮对话的指代消解专家。
你的任务是根据对话历史，把用户当前问题中的指代词（如"那个""它""这个""还有吗"）
和省略信息补全为完整、独立的问题。

规则：
1. 如果当前问题包含代词（他/她/它/那个/这个/那件/这件等），用历史中的实体替换
2. 如果当前问题是省略句（如"有货吗""多少钱""能退吗"），从历史中补全主语
3. 如果当前问题已经完整、不依赖上下文，直接返回原问题
4. 不要添加历史中没出现过的信息，只做补全，不做扩展

示例：
历史: 用户: "扫地机器人X1多少钱" → 助手: "扫地机器人X1售价2999元"
当前: "那个有货吗"
改写: "扫地机器人X1有货吗"
"""


# ==================== 核心函数 ====================


def _format_chat_history(messages: List[AnyMessage], max_turns: int = 5) -> str:
    """
    将消息列表格式化为 LLM 可读的对话历史文本。

    只取最近 max_turns 轮（1轮 = 1条用户 + 1条助手），
    避免历史过长导致 prompt 超长。

    Args:
        messages: LangGraph state.messages，完整的对话消息列表
        max_turns: 最多保留的对话轮数

    Returns:
        格式化的对话历史字符串，如 "用户: xxx\n助手: xxx\n用户: xxx\n助手: xxx"
    """
    # 筛选出用户和助手的消息，跳过 SystemMessage / ToolMessage 等
    chat_msgs = [m for m in messages if isinstance(m, (HumanMessage, AIMessage))]

    # 只保留最近 max_turns*2 条消息（每轮一问一答）
    chat_msgs = chat_msgs[-(max_turns * 2):]

    lines = []
    for msg in chat_msgs:
        role = "用户" if isinstance(msg, HumanMessage) else "助手"
        # 截断过长的消息，防止 prompt 爆掉
        content = msg.content[:200] if len(msg.content) > 200 else msg.content
        lines.append(f"{role}: {content}")

    return "\n".join(lines)


async def context_aware_rewrite(
    llm: BaseChatModel,
    messages: List[AnyMessage],
) -> str:
    """
    上下文感知改写：利用对话历史消解指代和省略。

    解决的核心问题：
        多轮对话中，用户常使用代词（"那个""它"）或省略主语（"有货吗"），
        但知识库检索需要完整、独立的问题才能匹配到相关文档。

        例如：
        第1轮: "扫地机器人X1多少钱" → 正常检索
        第2轮: "那个有货吗"         → 如果不补全，检索不到任何结果

    实现方式：
        1. 把对话历史格式化为文本（最近5轮）
        2. 让 LLM 判断当前问题是否依赖上下文
        3. 如果依赖，则补全为完整问题；否则原样返回

    Args:
        llm: 语言模型实例
        messages: 完整的对话消息列表（来自 state.messages）

    Returns:
        补全后的完整问题（如果不需要补全则返回原问题）
    """
    if not messages:
        return ""

    # 取当前用户问题（最后一条消息）
    current_query = messages[-1].content

    # 如果只有1条消息，不存在上下文依赖，直接返回
    if len(messages) <= 1:
        logger.info("首条消息，无需上下文感知改写")
        return current_query

    # 格式化对话历史（不包含当前消息，因为它是待改写的对象）
    history_text = _format_chat_history(messages[:-1])

    prompt = ChatPromptTemplate.from_messages([
        ("system", CONTEXT_REWRITE_SYSTEM_PROMPT),
        ("human", "对话历史:\n{history}\n\n当前问题: {current_query}\n\n请判断是否需要补全，并输出改写结果。"),
    ])

    chain = prompt | llm.with_structured_output(ContextRewrittenOutput)
    result = await chain.ainvoke({
        "history": history_text,
        "current_query": current_query,
    })

    if result.is_context_dependent:
        logger.info(f"上下文感知改写: '{current_query}' → '{result.rewritten_query}'")
    else:
        logger.info(f"当前问题不依赖上下文，保持原样: '{current_query}'")

    return result.rewritten_query


async def generate_multi_queries(
    llm: BaseChatModel,
    question: str,
) -> List[str]:
    """
    多查询生成：从不同角度生成多个查询版本

    Args:
        llm: 语言模型实例
        question: 用户的原始问题

    Returns:
        包含原始问题和3个改写版本的列表（共4个）
    """
    prompt = ChatPromptTemplate.from_messages([
        ("system", MULTI_QUERY_SYSTEM_PROMPT),
        ("human", "原始问题: {question}\n\n请生成3个不同角度的改写版本。"),
    ])

    chain = prompt | llm.with_structured_output(MultiQueryOutput)
    result = await chain.ainvoke({"question": question})

    # 原始问题放在头部，保证不会被丢掉
    all_queries = [question] + result.queries

    logger.info(f"多查询生成完成，共 {len(all_queries)} 个版本:")
    for i, q in enumerate(all_queries):
        logger.info(f"  Query[{i}]: {q}")

    return all_queries


async def generate_hypothetical_answer(
    llm: BaseChatModel,
    question: str,
) -> str:
    """
    HyDE：生成假设性答案

    原理说明：
        用户问题（口语化）     知识库文档（专业化）
             "灯不亮了"    vs   "LED智能灯泡故障排查指南：指示灯闪烁..."

        两者的向量在语义空间中距离较远，直接用"灯不亮了"检索可能匹配不到。

        解决方案：让 LLM 先生成一段"假想答案"。
        假想答案中会自然包含专业术语（如"LED驱动""恒流源"等），
        用假想答案做检索，语义上更接近真实文档，召回率更高。

    Args:
        llm: 语言模型实例
        question: 用户的原始问题

    Returns:
        LLM 生成的假设性答案文本
    """
    prompt = ChatPromptTemplate.from_messages([
        ("system", HYDE_SYSTEM_PROMPT),
        ("human", "问题: {question}"),
    ])

    chain = prompt | llm
    result = await chain.ainvoke({"question": question})
    hypothetical_answer = result.content

    logger.info(f"HyDE 假设性答案生成完成 ({len(hypothetical_answer)}字):")
    logger.info(f"  {hypothetical_answer[:100]}...")

    return hypothetical_answer


async def rewrite_query(
    llm: BaseChatModel,
    question: str,
) -> RewrittenQuery:
    """
    查询改写主入口：并行执行多查询生成 + HyDE

    流程：
        1. 多查询生成（Multi-Query）和 HyDE 并行执行（asyncio.gather）
        2. 合并结果，生成增强查询

    为什么并行？两个任务都只需调用 LLM，互不依赖。
    并行执行只耗时 max(多查询时间, HyDE时间)，而非两者之和。

    Args:
        llm: 语言模型实例
        question: 用户的原始问题

    Returns:
        RewrittenQuery 对象，包含所有改写结果
    """
    logger.info(f"开始查询改写，原始问题: {question}")

    # 并行执行，节省一轮 LLM 调用的时间
    multi_queries, hypothetical_answer = await asyncio.gather(
        generate_multi_queries(llm, question),
        generate_hypothetical_answer(llm, question),
    )

    # 增强查询 = 原始问题 + HyDE 线索
    # 这样下游的 Text2Cypher / GraphRAG 都能利用到 HyDE 的专业术语
    enhanced_query = f"{question}\n参考线索: {hypothetical_answer}"

    result = RewrittenQuery(
        original_query=question,
        multi_queries=multi_queries,
        hypothetical_answer=hypothetical_answer,
        enhanced_query=enhanced_query,
    )

    logger.info(f"查询改写完成")
    return result
