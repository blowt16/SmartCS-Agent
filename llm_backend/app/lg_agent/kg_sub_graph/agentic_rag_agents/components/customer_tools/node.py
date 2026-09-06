from typing import Any, Callable, Coroutine, Dict, List

from pydantic import BaseModel, Field

from app.core.logger import get_logger
from app.services.product_dynamic_service import fetch_by_skus
from app.services.rag_retriever_service import get_rag_retriever_service
from app.tools.doc_block_renderer import render_doc_blocks, render_dynamic_rows

logger = get_logger(service="customer_tools")


# ==================== 数据模型 ====================

class VectorSearchInputState(BaseModel):
    task: str
    query: str
    steps: List[str]


class VectorSearchOutputState(BaseModel):
    task: str
    query: str
    errors: List[str]
    records: Dict[str, Any]
    steps: List[str]


# ==================== LangGraph 节点工厂 ====================

def create_vector_search_query_node() -> Callable[
    [Dict[str, Any]],
    Coroutine[Any, Any, Dict[str, List[VectorSearchOutputState] | List[str]]],
]:
    """
    创建向量检索查询节点，用于 LangGraph 工作流。

    检索链路收敛于 RAGRetrieverService（HNSW ∥ BM25 并行 → RRF → Reranker 精排），
    应用层不再持有语料与索引。

    返回
    -------
    Callable
        名为 vector_search_query 的 LangGraph 节点。
    """

    async def vector_search_query(
        state: Dict[str, Any],
    ) -> Dict[str, List[VectorSearchOutputState] | List[str]]:
        """
        执行混合检索（向量 ∥ BM25 → RRF → 精排），返回检索结果供 summarize 消费。
        """
        errors = list()
        docs = []
        dynamic_rows: dict = {}

        query = state.get("task", "")
        if not query:
            errors.append("未提供查询文本")
        else:
            retriever = get_rag_retriever_service()
            docs = await retriever.search(query)
            logger.info("检索节点返回 {} 条文档", len(docs))

            # RAG 门控动态补全(方案 A,2026-09-06):检索准确性由 RAG 负责——
            # 仅对命中块的 sku 候选集取动态行;零命中/无 sku 块不查动态库
            skus: list[str] = []
            for d in docs:
                for s in d.get("sku_codes") or []:
                    if s and s not in skus:
                        skus.append(s)
            if skus:
                # 节点层独立兜底(与 service 内兜底双保险):任何动态异常 → 降级仅静态,
                # 防子任务整体崩溃(一致性=失败减数据不减错,口径由 summarize 规则承接)
                try:
                    dynamic_rows = await fetch_by_skus(skus)
                except Exception as e:
                    logger.warning("动态补全异常,降级为仅静态: {}", e)
                    errors.append("dynamic_fetch_failed")
                    dynamic_rows = {}
                logger.info("动态补全: 候选 {} 个 sku,命中 {} 行", len(skus), len(dynamic_rows))

        # 构建 LLM 可用的文本上下文——公共渲染与 rag_retrieval @tool 输出同格式
        # (SPEC_RAG_SKU_METADATA D5:双通道共用 render_doc_blocks,防格式漂移)
        # 方案 A:动态区(按 sku 与静态块前缀同键配对)置于静态块之前,LLM 免 join 原子消费
        static_text = render_doc_blocks(docs)
        dynamic_text = render_dynamic_rows(dynamic_rows)
        response_text = (
            f"{dynamic_text}\n\n{static_text}" if dynamic_text else static_text
        )

        return {
            "searches": [
                VectorSearchOutputState(
                    **{
                        "task": state.get("task", ""),
                        "query": query,
                        "errors": errors,
                        "records": {
                            "result": response_text,
                            "hybrid_docs": docs,
                            "dynamic_rows": dynamic_rows,  # {sku: 动态行}(程序化消费,非 LLM 通道)
                        },
                        "steps": ["execute_vector_search"],
                    }
                )
            ],
            "steps": ["execute_vector_search"],
        }

    return vector_search_query
