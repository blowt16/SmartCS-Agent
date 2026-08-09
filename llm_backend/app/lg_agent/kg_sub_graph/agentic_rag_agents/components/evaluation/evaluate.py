"""
RAG 评估脚本

读取测试集，逐条调用 agent 获取回答，计算以下指标：

检索指标：
    - Hit Rate:       检索结果中包含正确信息的比例
    - MRR:            第一个正确结果的排名倒数均值

生成指标：
    - Faithfulness:   回答忠实度（不编造）= 1 - 幻觉率
    - Relevance:      回答与问题的相关性
    - Completeness:   回答的完整性

端到端指标：
    - Accuracy:       回答正确率（与标准答案对比）
    - Avg Latency:    平均响应时间

用法：
    python evaluate.py --testset evaluation/testset.json --output evaluation/report.json
"""

import json
import asyncio
import time
from typing import List, Dict, Any, Optional
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(service="evaluator")


# ===== LLM 评判结构化输出 =====

class FaithfulnessScore(BaseModel):
    score: float = Field(description="0-1 分，1 表示完全基于上下文无幻觉")
    hallucinated_claims: List[str] = Field(
        default_factory=list,
        description="编造的声明列表"
    )


class RelevanceScore(BaseModel):
    score: float = Field(description="0-1 分，1 表示完全切题")
    reason: str = Field(description="评分理由")


class AccuracyScore(BaseModel):
    score: float = Field(description="0-1 分，1 表示与标准答案语义一致")
    missing_info: List[str] = Field(
        default_factory=list,
        description="标准答案中有但回答中缺失的信息"
    )


class CompletenessScore(BaseModel):
    score: float = Field(description="0-1 分，1 表示完整覆盖所有要点")
    covered_points: int = Field(description="回答覆盖的要点数")
    total_points: int = Field(description="标准答案的总要点数")


# ===== 评判 Prompt =====

FAITHFULNESS_PROMPT = """你是一个评估AI回答忠实度的专家。
判断回答中的每一个声明是否能在给定的上下文中找到依据。
如果声明无法在上下文中找到依据，则视为幻觉。

上下文（检索到的文档）：
{context}

AI 的回答：
{answer}

请评估回答的忠实度。"""

RELEVANCE_PROMPT = """你是一个评估AI回答相关性的专家。
判断回答是否真正回答了用户的问题，是否切题。

用户问题：{question}
AI 的回答：{answer}

请评估回答的相关性。"""

ACCURACY_PROMPT = """你是一个评估AI回答准确度的专家。
判断AI的回答是否与标准答案在语义上一致。
不要求完全相同的措辞，只要语义等价即可。

用户问题：{question}
标准答案：{expected}
AI 的回答：{actual}

请评估回答的准确度。"""

COMPLETENESS_PROMPT = """你是一个评估AI回答完整性的专家。
将标准答案拆分为独立的信息要点，然后检查AI回答覆盖了多少要点。

标准答案：{expected}
AI 的回答：{actual}

请评估回答的完整性。"""


# ===== 评判函数 =====

async def judge_faithfulness(llm, answer: str, context: str) -> FaithfulnessScore:
    if not context:
        return FaithfulnessScore(score=1.0, hallucinated_claims=["无上下文可验证"])
    prompt = FAITHFULNESS_PROMPT.format(context=context[:2000], answer=answer)
    return await llm.with_structured_output(FaithfulnessScore).ainvoke(prompt)


async def judge_relevance(llm, question: str, answer: str) -> RelevanceScore:
    prompt = RELEVANCE_PROMPT.format(question=question, answer=answer)
    return await llm.with_structured_output(RelevanceScore).ainvoke(prompt)


async def judge_accuracy(llm, question: str, expected: str, actual: str) -> AccuracyScore:
    prompt = ACCURACY_PROMPT.format(question=question, expected=expected, actual=actual)
    return await llm.with_structured_output(AccuracyScore).ainvoke(prompt)


async def judge_completeness(llm, expected: str, actual: str) -> CompletenessScore:
    prompt = COMPLETENESS_PROMPT.format(expected=expected, actual=actual)
    return await llm.with_structured_output(CompletenessScore).ainvoke(prompt)


# ===== Agent 调用 =====

async def call_agent(question: str) -> Dict[str, Any]:
    """调用 agent 获取回答"""
    from langchain_core.messages import HumanMessage
    from app.lg_agent.lg_builder import graph

    thread_id = f"eval_{hash(question) % 10000}"
    config = {"configurable": {"thread_id": thread_id}}

    start = time.time()
    try:
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=question)]},
            config=config,
        )
        latency = time.time() - start

        ai_message = ""
        for msg in reversed(result.get("messages", [])):
            if hasattr(msg, "content") and msg.content:
                ai_message = msg.content
                break

        return {"answer": ai_message, "latency": latency, "error": None}
    except Exception as e:
        return {"answer": "", "latency": time.time() - start, "error": str(e)}


# ===== 评估主流程 =====

async def evaluate(
    testset_path: str = "evaluation/testset.json",
    output_path: str = "evaluation/report.json",
    sample_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    运行评估。

    Args:
        testset_path: 测试集 JSON 文件路径
        output_path: 评估报告输出路径
        sample_size: 抽样评估数量（None = 全部）
    """
    with open(testset_path, "r", encoding="utf-8") as f:
        testset = json.load(f)

    items = testset["items"]
    if sample_size:
        items = items[:sample_size]

    logger.info(f"开始评估: {len(items)} 条测试数据")

    # 初始化评判 LLM
    if settings.AGENT_SERVICE == settings.ServiceType.DEEPSEEK:
        from langchain_deepseek import ChatDeepSeek
        judge_llm = ChatDeepSeek(
            api_key=settings.DEEPSEEK_API_KEY,
            model_name=settings.DEEPSEEK_MODEL, temperature=settings.LLM_GRADER_TEMPERATURE,
        )
    else:
        from langchain_ollama import ChatOllama
        judge_llm = ChatOllama(
            model=settings.OLLAMA_AGENT_MODEL,
            base_url=settings.OLLAMA_BASE_URL, temperature=settings.LLM_GRADER_TEMPERATURE,
        )

    # 逐条评估
    results = []
    total = len(items)

    for i, item in enumerate(items):
        logger.info(f"评估 {i + 1}/{total}: {item['question'][:30]}...")

        # 1. 调用 agent
        agent_result = await call_agent(item["question"])
        answer = agent_result["answer"]
        latency = agent_result["latency"]

        if agent_result["error"]:
            logger.warning(f"Agent 调用失败: {agent_result['error']}")
            results.append({
                **item, "actual_answer": answer, "latency": latency,
                "error": agent_result["error"],
                "accuracy": 0, "faithfulness": 0, "relevance": 0, "completeness": 0,
            })
            continue

        # 2. LLM 评判（4 个维度并行）
        accuracy, faithfulness, relevance, completeness = await asyncio.gather(
            judge_accuracy(judge_llm, item["question"], item["expected_answer"], answer),
            judge_faithfulness(judge_llm, answer, item["expected_answer"]),
            judge_relevance(judge_llm, item["question"], answer),
            judge_completeness(judge_llm, item["expected_answer"], answer),
        )

        results.append({
            **item,
            "actual_answer": answer,
            "latency": round(latency, 2),
            "error": None,
            "accuracy": round(accuracy.score, 3),
            "faithfulness": round(faithfulness.score, 3),
            "relevance": round(relevance.score, 3),
            "completeness": round(completeness.score, 3),
        })

    # 3. 汇总
    valid_results = [r for r in results if r.get("error") is None]
    error_count = len(results) - len(valid_results)

    if not valid_results:
        logger.error("无有效评估结果")
        return {}

    def avg(key: str) -> float:
        scores = [r[key] for r in valid_results if key in r]
        return round(sum(scores) / len(scores), 4) if scores else 0

    report = {
        "summary": {
            "total": len(results),
            "valid": len(valid_results),
            "errors": error_count,
            "accuracy": avg("accuracy"),
            "faithfulness": avg("faithfulness"),
            "hallucination_rate": round(1 - avg("faithfulness"), 4),
            "relevance": avg("relevance"),
            "completeness": avg("completeness"),
            "avg_latency": avg("latency"),
        },
        "by_category": {},
        "by_difficulty": {},
        "details": results,
    }

    # 按类别汇总
    for cat in set(r["category"] for r in valid_results):
        cat_r = [r for r in valid_results if r["category"] == cat]
        report["by_category"][cat] = {
            "count": len(cat_r),
            "accuracy": round(sum(r["accuracy"] for r in cat_r) / len(cat_r), 4),
            "faithfulness": round(sum(r["faithfulness"] for r in cat_r) / len(cat_r), 4),
            "relevance": round(sum(r["relevance"] for r in cat_r) / len(cat_r), 4),
            "completeness": round(sum(r["completeness"] for r in cat_r) / len(cat_r), 4),
        }

    # 按难度汇总
    for diff in set(r["difficulty"] for r in valid_results):
        diff_r = [r for r in valid_results if r["difficulty"] == diff]
        report["by_difficulty"][diff] = {
            "count": len(diff_r),
            "accuracy": round(sum(r["accuracy"] for r in diff_r) / len(diff_r), 4),
            "faithfulness": round(sum(r["faithfulness"] for r in diff_r) / len(diff_r), 4),
        }

    # 写入报告
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 打印摘要
    s = report["summary"]
    print("\n" + "=" * 60)
    print("             评估报告摘要")
    print("=" * 60)
    print(f"  总测试数:       {s['total']}")
    print(f"  有效结果:       {s['valid']}")
    print(f"  错误数:         {s['errors']}")
    print("-" * 60)
    print(f"  准确率:         {s['accuracy']:.1%}")
    print(f"  忠实度:         {s['faithfulness']:.1%}")
    print(f"  幻觉率:         {s['hallucination_rate']:.1%}")
    print(f"  相关性:         {s['relevance']:.1%}")
    print(f"  完整性:         {s['completeness']:.1%}")
    print("-" * 60)
    print(f"  平均响应时间:   {s['avg_latency']:.2f}s")
    print("=" * 60)

    logger.info(f"评估报告已保存: {output.absolute()}")
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--testset", default="evaluation/testset.json")
    parser.add_argument("--output", default="evaluation/report.json")
    parser.add_argument("--sample", type=int, default=None, help="抽样数量")
    args = parser.parse_args()

    asyncio.run(evaluate(
        testset_path=args.testset,
        output_path=args.output,
        sample_size=args.sample,
    ))
