"""RAGAS 评测 CLI 入口：python -m evaluation [参数]

用法：
    python -m evaluation --testset-size 20 --user 1                        # 全流程
    python -m evaluation --only-synthesize --testset-size 10               # 仅合成并缓存
    python -m evaluation --skip-synthesize --testset-file <缓存>.jsonl     # 复用缓存重跑

流程与真实系统一致：合成题（无历史）→ 入口指代消解（原样通过）
→ graphrag-query RAG 子图 → 四指标评测 → 报告归档。
"""
import argparse
import asyncio
import sys
from pathlib import Path
from typing import List

from evaluation import report as report_mod
from evaluation import testset_builder
from evaluation.llm_factory import build_agent_llm
from evaluation.runner import build_eval_dataset, build_metrics, run_all, run_metrics

# 允许 python -m evaluation（llm_backend 下）与直接脚本两种运行方式
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.core.database  # noqa: E402,F401 —— Windows 必选：Select-事件循环补丁（psycopg async 拒用 Proactor）

DEFAULT_METRICS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]


def parse_args():
    from app.core.config import settings

    p = argparse.ArgumentParser(prog="evaluation", description="RAGAS 四指标评测（graphrag-query RAG 模块）")
    p.add_argument("--testset-size", type=int, default=settings.RAGAS_DEFAULT_TESTSET_SIZE,
                   help="合成题数（默认 %(default)s）")
    p.add_argument("--user", type=str, default="1",
                   help="知识归属 user_id（document_chunks 过滤，默认 %(default)s）")
    p.add_argument("--testset-file", type=str, default=None,
                   help="评测集缓存文件路径（--skip-synthesize 时必填）")
    p.add_argument("--max-docs", type=int, default=settings.RAGAS_MAX_CORPUS_DOCS,
                   help="喂合成器的分块上限（默认 %(default)s）")
    p.add_argument("--metrics", type=str, default=",".join(DEFAULT_METRICS),
                   help="指标子集（逗号分隔，如 faithfulness,context_precision）")
    p.add_argument("--concurrency", type=int, default=4,
                   help="子图侧并发数（默认 %(default)s）")
    p.add_argument("--results-dir", type=str, default="",
                   help=f"报告输出目录（默认 {settings.RAGAS_RESULTS_DIR}，相对 llm_backend/）")
    p.add_argument("--only-synthesize", action="store_true", help="仅合成评测集并缓存，不跑评测")
    p.add_argument("--skip-synthesize", action="store_true", help="复用 --testset-file 缓存，不重新合成")
    return p.parse_args()


async def main(args) -> int:
    import time

    from app.core.config import settings
    from app.core.logger import get_logger

    logger = get_logger(service="evaluation.cli")
    start = time.monotonic()

    if args.skip_synthesize and not args.testset_file:
        logger.error("--skip-synthesize 需要 --testset-file 指定缓存文件")
        return 2

    # ---- 合成评测集 ----
    if args.skip_synthesize:
        testset = testset_builder.load_cached_testset(Path(args.testset_file))
    else:
        documents = await testset_builder.load_corpus_documents(args.max_docs, args.user)
        cache_path = Path("evaluation/results") / f"testset_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        testset = await asyncio.to_thread(
            testset_builder.synthesize_testset, documents, args.testset_size, cache_path
        )
        if args.only_synthesize:
            logger.info("仅合成模式完成：{} ({}题)", cache_path, len(testset.to_list()))
            return 0

    rows = testset.to_list()
    references: List[dict] = []
    questions: List[str] = []
    for row in rows:
        ref = {k: row.get(k) for k in ("user_input", "reference", "reference_contexts")}
        references.append(ref)
        questions.append(row.get("user_input") or row.get("question") or "")
    questions = [q for q in questions if q]
    if not questions:
        logger.error("评测集无有效题目（{} 条）——检查合成/缓存文件内容", len(rows))
        return 2
    logger.info("评测集就绪: {} 题", len(questions))

    # ---- 子图执行（graphrag-query RAG 模块，与生产一致） ----
    from app.lg_agent.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.multi_tool import (
        create_multi_tool_workflow,
    )

    workflow = create_multi_tool_workflow(llm=build_agent_llm())
    results = await run_all(questions, workflow, concurrency=args.concurrency)

    done = [r for r in results if r.ok and r.contexts]
    if not done:
        logger.error("全部题目执行失败/无上下文——检查 DB、embedding API 与子图日志")
        return 2

    # ---- 四指标评测 ----
    metric_names = [m.strip() for m in args.metrics.split(",") if m.strip()]
    dataset = build_eval_dataset(done, references)
    metrics = build_metrics(metric_names)
    raw_result = await run_metrics(dataset, metrics)

    report = report_mod.summarize(
        raw_result, results, metric_names,
        meta={
            "user_id": args.user,
            "testset_size": len(questions),
            "evaluated_samples": len(done),
            "concurrency": args.concurrency,
            "judge_model": settings.RAGAS_JUDGE_MODEL,
            "judge_base_url": settings.RAGAS_JUDGE_BASE_URL,
            "batch_size": settings.RAGAS_BATCH_SIZE,
            "metrics": metric_names,
            "elapsed_seconds": round(time.monotonic() - start, 1),
            "cache_file": args.testset_file or "synthesized-new",
        },
    )
    report_mod.dump_report(report, Path(settings.RAGAS_RESULTS_DIR), results_dir_override=args.results_dir)
    report_mod.print_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
