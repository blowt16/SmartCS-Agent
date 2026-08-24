"""子图评测执行器：指代消解入口复刻 → 子图直调 → EvaluationDataset → 四指标。

评测流程与真实系统一致（spec §2.3）：
    真实系统进入 RAG 子图的 query 是"入口指代消解后"的 query（main.py:389-395），
    本模块 apply_entry_resolution 复刻同一入口；single-turn 合成题无历史，
    与生产单轮行为一致（不消解、原样传递）。
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ragas.dataset_schema import EvaluationDataset, SingleTurnSample

from app.core.config import settings
from app.core.logger import get_logger
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.agent_safety import TimeoutGuard

logger = get_logger(service="evaluation.runner")


# ==================== 指代消解（评测入口与生产一致） ====================

async def apply_entry_resolution(
    question: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> Tuple[str, bool]:
    """复刻生产入口的指代消解（main.py:389-395），保证评测流程与真实系统一致。

    生产行为：仅当「有会话历史 且 detect == NEED_RESOLVE」才消解，消解 LLM 与生产
    同款（LLMFactory.create_chat_service，main.py:48-53）。
    MVP 单轮评测（history=None）恒原样通过——与生产对单轮问题的行为一致。
    未来评测集含多轮指代问题时传 history 即走同款消解链路。

    Returns:
        (进入子图的 query, 是否发生过消解)
    """
    from app.services.llm_factory import LLMFactory
    from app.services.pronoun_detector import DetectionDecision, detect_pronoun
    from app.services.pronoun_resolver import resolve_pronouns

    decision = detect_pronoun(question, skip_filler=settings.RESOLVE_SKIP_FILLER)
    if not history or decision != DetectionDecision.NEED_RESOLVE:
        return question, False

    resolved = await resolve_pronouns(
        LLMFactory.create_chat_service(),
        history + [{"role": "user", "content": question}],
        question,
    )
    logger.info("评测指代消解: '{}' → '{}'", question, resolved)
    return resolved, True


# ==================== 子图执行 ====================

@dataclass
class SubgraphResult:
    """单题子图执行结果（question/resolved_question 双记录，见 spec §4.2）。"""

    question: str
    resolved_question: str
    was_resolved: bool = False
    answer: str = ""
    contexts: List[str] = field(default_factory=list)
    elapsed: float = 0.0
    ok: bool = False
    error: str = ""
    timed_out: bool = False
    empty_context: bool = False


async def run_one(
    question: str,
    workflow: Any,
    history: Optional[List[Dict[str, str]]] = None,
) -> SubgraphResult:
    """执行单题：入口消解（与生产一致）→ RAG 子图（graphrag-query 执行体）。

    子图禁用 checkpointer（__pregel_checkpointer=None，与 lg_builder.py:442-450 同款，
    规避 map-reduce Send 的 checkpoint 序列化问题）；TimeGuard 30s 超时视为失败
    （生产的降级文案在评测中不算成绩，避免指标虚高）。
    """
    resolved_question, was_resolved = await apply_entry_resolution(question, history)

    timeout = TimeoutGuard(timeout_seconds=settings.RAG_TIMEOUT_SECONDS)
    start = time.monotonic()
    try:
        raw = await timeout.wrap(
            workflow.ainvoke(
                {"question": resolved_question, "data": [], "history": []},
                config={"configurable": {"__pregel_checkpointer": None}},
            ),
            fallback=None,  # 超时返回 None，显式判定 timed_out
            conversation_id=f"ragas-eval:{question[:20]}",
        )
        elapsed = time.monotonic() - start
    except Exception as e:  # noqa: BLE001 —— 单题失败不中断整批
        logger.error("评测子图异常: '{}' → {}", question, str(e))
        return SubgraphResult(
            question=question,
            resolved_question=resolved_question,
            was_resolved=was_resolved,
            elapsed=time.monotonic() - start,
            ok=False,
            error=f"{type(e).__name__}: {e}",
        )

    if raw is None:  # TimeoutGuard 超时哨兵
        logger.warning("评测子图超时: '{}' (>{}s)", question, settings.RAG_TIMEOUT_SECONDS)
        return SubgraphResult(
            question=question,
            resolved_question=resolved_question,
            was_resolved=was_resolved,
            elapsed=elapsed,
            timed_out=True,
        )

    # 输出结构（state.py:68-75）：{answer, question, steps, searches, history}
    # 检索上下文出处（node.py:66-80）：searches[*].records.hybrid_docs[*].text（按子任务顺序展开）
    contexts = [
        d.get("text", "")
        for s in (raw.get("searches") or [])
        for d in (s.get("records", {}).get("hybrid_docs", []) if isinstance(s, dict) else getattr(s, "records", {}).get("hybrid_docs", []))
    ]
    contexts = [c for c in contexts if c]
    answer = raw.get("answer", "")

    return SubgraphResult(
        question=question,
        resolved_question=resolved_question,
        was_resolved=was_resolved,
        answer=answer,
        contexts=contexts,
        elapsed=elapsed,
        ok=True,
        empty_context=not contexts,
    )


async def run_all(
    questions: List[str],
    workflow: Any,
    concurrency: int = 4,
    history: Optional[List[Dict[str, str]]] = None,
) -> List[SubgraphResult]:
    """并发执行全部题目（信号量限流，控制 embedding/DeepSeek/子图并发）。"""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(q: str) -> SubgraphResult:
        async with sem:
            return await run_one(q, workflow, history=history)

    results = list(await asyncio.gather(*(_guarded(q) for q in questions)))
    ok = sum(1 for r in results if r.ok)
    logger.info("子图评测完成: {}/{} 题成功", ok, len(questions))
    return results


# ==================== 评测数据集与四指标 ====================

def build_metrics(metric_names: List[str]) -> List[Any]:
    """按名称构建指标实例（ragas 0.4.3 新旧类名并存，统一用新式类）。"""
    from ragas.metrics import (
        Faithfulness,
        LLMContextPrecisionWithReference,
        LLMContextRecall,
        ResponseRelevancy,
    )

    _AVAILABLE = {
        "faithfulness": Faithfulness,
        "answer_relevancy": ResponseRelevancy,
        "context_precision": LLMContextPrecisionWithReference,
        "context_recall": LLMContextRecall,
    }
    bad = [n for n in metric_names if n not in _AVAILABLE]
    if bad:
        raise ValueError(f"未知指标: {bad}（可用: {list(_AVAILABLE)}）")
    return [_AVAILABLE[n]() for n in metric_names]


def build_eval_dataset(
    results: List[SubgraphResult],
    references: List[Dict[str, Any]],
) -> EvaluationDataset:
    """合并子图结果与合成集的 reference 字段，构建 ragas EvaluationDataset。

    字段映射（spec §4.4）：
        user_input          = 原始 question（用户实际问的）
        response            = 子图 answer（基于消解后 query 的生成）
        retrieved_contexts  = 子图检索上下文（hybrid_docs 文本）
        reference           = 合成器生成的标准答案
        reference_contexts  = 合成器生成的 grounding 分块
    失败/超时/空上下文题不进入数据集（由 __main__ 单列 failures）。
    """
    ref_by_question = {r.get("user_input"): r for r in references}
    # 0.4.3 的 from_list 接收 dict 列表（内部 SingleTurnSample(**item)）——
    # 传构造好的 SingleTurnSample 实例会 TypeError: argument after ** must be a mapping（实测）
    samples = [
        {
            "user_input": res.question,
            "response": res.answer,
            "retrieved_contexts": res.contexts,
            "reference": (ref_by_question.get(res.question) or {}).get("reference"),
            "reference_contexts": (ref_by_question.get(res.question) or {}).get(
                "reference_contexts"
            ),
        }
        for res in results
    ]
    return EvaluationDataset.from_list(samples)


async def run_metrics(dataset: EvaluationDataset, metrics: List[Any]) -> Any:
    """执行四指标评测（ragas aevaluate，异步原生；batch_size 批量并行 judge 调用）。

    raise_exceptions=False：失败样本返回 NaN，不中断整轮评测（报告单列标注）。
    """
    from ragas import aevaluate
    from ragas.run_config import RunConfig

    from evaluation.llm_factory import build_judge_embeddings, build_judge_llm

    return await aevaluate(
        dataset,
        metrics=metrics,
        llm=build_judge_llm(),               # ragas 0.4 现代 provider（Instructor 结构化输出）
        embeddings=build_judge_embeddings(), # ragas 现代 provider（不加 Langchain 包装层）
        run_config=RunConfig(
            timeout=settings.RAGAS_JUDGE_TIMEOUT,
            max_workers=4,  # 机器负载控制：默认 16 并发与子图/reranker 叠加易被环境终止（实测 4 次被停）
        ),
        batch_size=settings.RAGAS_BATCH_SIZE,
        raise_exceptions=False,
    )
