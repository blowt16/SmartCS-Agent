"""
相关性评分模块（Relevance Grading）

解决问题：
    检索返回的结果不一定都和用户问题相关。如果直接把不相关的结果
    喂给 LLM 生成回答，会产生幻觉或偏题的回答。

    相关性评分在检索之后、回答生成之前插入，用 LLM 对每条检索结果
    做二值评分（relevant / irrelevant），只有通过评分的结果才会被
    传递给下游。

工作流程：
    检索结果 → LLM 逐条评分 → 过滤掉 irrelevant → 返回相关文档
"""

from typing import List, Dict, Any

from pydantic import BaseModel, Field

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(service="relevance_grader")


# ==================== 数据模型 ====================


class RelevanceGrade(BaseModel):
    """单条检索结果的相关性评分"""
    document_index: int = Field(description="文档在列表中的索引")
    relevance: str = Field(description="relevant 或 irrelevant")
    reasoning: str = Field(description="判断理由（一句话）")


class RelevanceGradeOutput(BaseModel):
    """LLM 对所有文档的评分结果"""
    grades: List[RelevanceGrade] = Field(description="每条文档的相关性评分")


# ==================== 提示词 ====================


RELEVANCE_GRADING_PROMPT = """你是一个检索结果相关性评分器。

给定用户问题和一组检索到的文档片段，判断每个片段是否与问题相关。

评分标准：
- relevant：片段包含能直接或间接回答用户问题的信息
- irrelevant：片段与用户问题无关，或仅包含边缘信息

规则：
1. 严格评分，宁缺毋滥
2. 只要有实质性的相关信息就算 relevant
3. 如果片段只是提到了相同的实体但内容无关，算 irrelevant
"""


# ==================== 核心函数 ====================


async def grade_relevance(
    llm: BaseChatModel,
    query: str,
    documents: List[Dict[str, Any]],
    content_key: str = "text",
) -> List[Dict[str, Any]]:
    """
    对检索结果进行相关性评分，过滤掉不相关的文档。

    流程：
        1. 把 query 和每个文档片段拼接，让 LLM 逐条评分
        2. 只保留 relevant 的文档
        3. 如果 relevant 数量 < 阈值，记录日志

    Args:
        llm: 语言模型实例
        query: 用户查询
        documents: 检索结果列表
        content_key: 文档中用于评分的文本字段名

    Returns:
        过滤后的相关文档列表（带 relevance_score 字段）
    """
    if not documents:
        logger.info("无检索结果需要评分")
        return []

    if not settings.RELEVANCE_GRADING_ENABLED:
        logger.info("相关性评分已禁用，跳过评分")
        return documents

    logger.info(f"开始相关性评分: query='{query[:50]}...', 共 {len(documents)} 条结果")

    # 构造文档摘要（截断太长的内容，避免 prompt 超长）
    doc_summaries = []
    for i, doc in enumerate(documents):
        text = doc.get(content_key, str(doc))[:300] if isinstance(doc, dict) else str(doc)[:300]
        doc_summaries.append(f"[{i}] {text}")

    docs_text = "\n\n".join(doc_summaries)

    prompt = ChatPromptTemplate.from_messages([
        ("system", RELEVANCE_GRADING_PROMPT),
        ("human", "用户问题: {query}\n\n检索结果:\n{documents}\n\n请逐条评分。"),
    ])

    chain = prompt | llm.with_structured_output(RelevanceGradeOutput)

    try:
        result = await chain.ainvoke({
            "query": query,
            "documents": docs_text,
        })
    except Exception as e:
        logger.warning(f"相关性评分失败，返回全部结果: {e}")
        return documents

    # 构建评分索引
    grade_map = {}
    for grade in result.grades:
        grade_map[grade.document_index] = grade.relevance

    # 过滤：只保留 relevant 的文档
    relevant_docs = []
    for i, doc in enumerate(documents):
        relevance = grade_map.get(i, "irrelevant")
        if relevance == "relevant":
            scored_doc = dict(doc) if isinstance(doc, dict) else {"text": doc}
            scored_doc["relevance_score"] = "relevant"
            relevant_docs.append(scored_doc)
        else:
            logger.debug(f"  文档[{i}] 评为 irrelevant，已过滤")

    logger.info(
        f"相关性评分完成: {len(documents)} 条 -> {len(relevant_docs)} 条 relevant"
    )

    return relevant_docs
