"""
RAG 检索工具（langchain @tool 薄封装）

将整个 RAG 检索管线（HNSW ∥ BM25 → RRF → Reranker 精排）封装为 agent 可调用的工具。
核心逻辑全部在 RAGRetrieverService，此处仅为薄适配层。

用法（后续 agent 接入）：
    from app.tools.rag_tool import rag_retrieval
    from app.tools.product_stock_tool import product_stock_lookup
    llm.bind_tools([rag_retrieval, product_stock_lookup])
"""

import asyncio
import json
from pathlib import Path

import aiohttp
from langchain_core.tools import tool
from psycopg.errors import InvalidPassword  # psycopg3 驱动层认证异常（SQLAlchemy 不暴露）
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.core.config import settings
from app.core.logger import get_logger
from app.services.rag_retriever_service import get_rag_retriever_service

logger = get_logger(service="rag_tool")

# 超时与重试配置统一入 env（settings.TOOL_*，与 product_stock_lookup 共用）


def _classify_error(e: Exception) -> tuple[str, bool]:
    """异常 → (error_type, retryable)。瞬时类（超时/连接/API）可自动重试；永久类不重试。"""
    if isinstance(e, asyncio.TimeoutError):
        return "db_timeout", True
    if isinstance(e, OperationalError):
        return "db_connection", True
    if isinstance(e, (ProgrammingError, InvalidPassword)):
        return "db_config", False
    if isinstance(e, aiohttp.ClientError):          # Embedding API/HTTP 调用异常（网络抖动/限流）
        return "api_unavailable", True
    return "unknown", False


async def _search_with_retry(query: str) -> list[dict]:
    """超时保护 + 瞬时错误自动重试（与 product_stock_lookup._query_with_retry 同模式）。"""
    for attempt in range(settings.TOOL_RETRY_TIMES + 1):
        try:
            return await asyncio.wait_for(
                get_rag_retriever_service().search(query),
                timeout=settings.TOOL_DB_TIMEOUT_SECONDS,
            )
        except Exception as e:
            _, retryable = _classify_error(e)
            if attempt < settings.TOOL_RETRY_TIMES and retryable:
                await asyncio.sleep(settings.TOOL_RETRY_INTERVAL)
                continue
            raise


def _tool_error(error_type: str, retryable: bool, message: str) -> str:
    """错误信息（统一 JSON 协议，与 product_stock_lookup 一致）。

    瞬时类错误 tool 内部已自动重试过 → retryable=false（LLM 不再盲目重试）；
    仅 invalid_argument（入参错误）为 true——修正参数后重试有意义。
    """
    return json.dumps(
        {"status": "error", "error_type": error_type, "retryable": retryable, "message": message},
        ensure_ascii=False,
    )


@tool
async def rag_retrieval(query: str) -> str:
    """
    从企业知识库中检索与用户问题相关的知识文档片段。

    知识库内容（商品静态信息）：商品参数与规格、功能特点、使用指导与故障排查、
    保修与售后政策（含《京东自营售后政策》独立文档）等。
    【动态数据不在本工具】商品价格与库存存储在数据库，请使用 product_stock_lookup 工具查询。

    何时使用本工具：
    - 用户询问商品参数/规格/功能特点（如"这款灯的亮度调节范围是多少"）
    - 用户询问使用方法或故障解决（如"扫地机器人一直报错怎么办"）
    - 用户询问保修、退换货、售后政策条款（如"这个锁保修多久"）
    - 用户询问某类商品的功能/推荐/对比（如"电动沙发和普通沙发有什么区别"）

    何时不要使用本工具：
    - 询问商品价格或是否有货 → 使用 product_stock_lookup（数据库动态数据）
    - 与业务无关的闲聊 → 直接回答，无需检索
    - 违规/高风险内容 → 按风险规则处理，不检索

    Args:
        query: 用户的问题（建议为补全指代后的完整问题）

    Returns:
        成功：相关文档片段列表（每段以换行分隔，含【来源:文件名】前缀）；
        空结果：提示未检索到 + 可执行建议；
        失败：统一错误 JSON（status=error，含 error_type/retryable/message）。
    """
    # 入参校验：query 为空 → 明确错误，引导 LLM 提供问题（retryable=true）
    if not query or not query.strip():
        return _tool_error(
            "invalid_argument", True,
            "参数错误：query 不能为空。请根据用户消息生成完整检索问题（可补全指代）后重试，"
            "若确实无法生成检索问题，请直接回答或向用户澄清。",
        )

    try:
        # 超时保护（10s）+ 瞬时错误自动重试 1 次；重试后仍失败 → 返回 error
        docs = await _search_with_retry(query.strip())
    except Exception as e:
        error_type, _ = _classify_error(e)
        logger.warning("RAG 检索异常: {} ({})", type(e).__name__, error_type)
        return _tool_error(
            error_type, False,
            f"知识检索失败（{error_type}），已自动重试仍未恢复。"
            "请告知用户当前知识检索暂不可用，稍后重试或转人工处理，不要编造知识库中不存在的内容。",
        )

    if not docs:
        return (
            f"未检索到与「{query}」相关的知识内容。建议："
            "1) 更换措辞或补充商品名称后重试；"
            "2) 若用户询问的是商品价格或库存，请使用 product_stock_lookup 工具；"
            "3) 可向用户说明该信息暂未收录。"
        )

    return "\n\n".join(
        f"【来源:{Path(doc.get('file_path') or '未知').name}】\n{doc.get('text', '')}"
        for doc in docs
    )
