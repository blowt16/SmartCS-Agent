"""合成评测集：从生产语料（document_chunks）→ ragas TestsetGenerator。

语料直接读 DB 生产分块（已清洗 + 500/50 分块），与检索链路严格一致：
    1) 合成器产出的 reference_contexts 在生产检索中"找得到"，context_recall 不失真
    2) 零新增解析依赖（评测本来就要连 DB 跑检索）

生成的评测集 to_jsonl 落盘缓存，--skip-synthesize 复用（同题重复合成纯浪费 token）。
"""
from pathlib import Path
from typing import List

from langchain_core.documents import Document
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logger import get_logger
from app.models.document_chunk import DocumentChunk
from evaluation.llm_factory import build_judge_embeddings, build_judge_llm

logger = get_logger(service="evaluation.testset_builder")


async def load_corpus_documents(max_docs: int, user_id: str | None = None) -> List[Document]:
    """从 document_chunks 读取生产分块，包装为 ragas 合成器可用的 Document。

    Args:
        max_docs: 最多读取的分块数（控制合成器 docstore 建立成本，默认 RAGAS_MAX_CORPUS_DOCS）
        user_id: 可选的归属收窄。**不传 = 读全库**（排除 test_% 测试账号）。
                 旧语义是"必填的语料来源"，已废弃 —— 见 SPEC_DOCUMENT_STATUS_FILTER D11：
                 ① 原默认值 "1" 早就是坏的（数据已迁到 owner 6）；
                 ② "为评测把同一内容再挂一份到第二个 user_id"实测会让检索 top-K
                    出现完全重复的条目（RRF 按 chunk_id 去重，而 chunk_id 含 user_id），
                    白占名额、压低 context_precision —— 恰恰违背本模块"语料复用生产
                    分块，保证 reference_contexts 在生产检索中找得到"的立身之本。

    Raises:
        RuntimeError: 无任何可用分块。
    """
    async with AsyncSessionLocal() as session:
        stmt = select(DocumentChunk)
        if user_id:
            stmt = stmt.where(DocumentChunk.user_id == user_id)
        else:
            # 默认读全库:知识库是全平台的,文档身份为 md5(SPEC_DOCUMENT_STATUS_FILTER D4)。
            # 排除 test_ 前缀账号 —— 它们是 pytest 夹具(conftest 的 test_user_id 约定),
            # 测试被强杀时会留下残留块;混进合成语料会生成出自测试夹具的评测题。
            stmt = stmt.where(~DocumentChunk.user_id.like("test_%"))
        stmt = stmt.order_by(DocumentChunk.id).limit(max_docs)
        chunks = (await session.execute(stmt)).scalars().all()

    if not chunks:
        raise RuntimeError(
            "语料库为空：document_chunks 无可用知识分块"
            "（请先经管理端上传知识文档;或显式传 --user <owner_id> 收窄语料）"
        )
    logger.info("评测语料加载完成: {} 块（user_id={}）", len(chunks), user_id or "全库")
    return [
        Document(
            page_content=chunk.content,
            metadata={
                "chunk_id": chunk.chunk_id,
                "source": chunk.source,
                "user_id": chunk.user_id,
            },
        )
        for chunk in chunks
    ]


def synthesize_testset(documents: List[Document], size: int, output_path: Path) -> object:
    """生成评测集（同步函数，调用方以 asyncio.to_thread 执行），落盘 jsonl 缓存。

    Args:
        documents: load_corpus_documents 的产物（production chunks）
        size: 合成题数
        output_path: 缓存文件（testset_*.jsonl）

    Returns:
        ragas.testset.Testset
    """
    from ragas.run_config import RunConfig
    from ragas.testset import TestsetGenerator
    from ragas.testset.synthesizers import SingleHopSpecificQuerySynthesizer

    from ragas.run_config import RunConfig
    from ragas.testset import TestsetGenerator
    from ragas.testset.synthesizers import SingleHopSpecificQuerySynthesizer

    judge_llm = build_judge_llm()
    # llm/embedding 均用 ragas 0.4 现代 provider（build_judge_llm/embeddings 已返回原生）；
    # 不可用 from_langchain：其内部强制包 Langchain Wrapper（已弃用），且 langchain
    # embedding 发百炼 400 / LLM 包装在无 loop 线程炸 'Event loop is closed'（均实测）
    generator = TestsetGenerator(
        llm=judge_llm,
        embedding_model=build_judge_embeddings(),
    )

    # 单跳具体查询合成器：中文业务知识库以单跳问答为主（多跳覆盖复杂，MVP 不合成）
    # run_config：百炼 qwen-max 系 RPM 限流较严（实测默认并发 16 直接 429），
    # 降并发 4 + 指数重试（tenacity，max_retries/max_wait）兜底限流抖动
    testset = generator.generate_with_langchain_docs(
        documents,
        testset_size=size,
        # 0.4.x 分布格式为 (synthesizer, 权重) 元组序列（纯类实例列表会 TypeError）
        query_distribution=[(SingleHopSpecificQuerySynthesizer(llm=judge_llm), 1.0)],
        run_config=RunConfig(
            timeout=settings.RAGAS_JUDGE_TIMEOUT,
            max_workers=4,
            max_retries=6,
            max_wait=30,
        ),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Testset.to_jsonl 用平台默认编码（Windows=GBK）写文件，跨环境必乱；
    # 显式 UTF-8 手写 jsonl（与 load_cached_testset 读写对称）
    import json

    with open(output_path, "w", encoding="utf-8") as f:
        for row in testset.to_list():
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.info("评测集已生成: {} 题 → {}", size, output_path)
    return testset


def load_cached_testset(path: Path) -> object:
    """加载缓存的评测集（--skip-synthesize 复用，UTF-8 jsonl）。"""
    import json

    from ragas.testset import Testset

    if not path.exists():
        raise RuntimeError(
            f"缓存评测集不存在: {path}（请先执行 --only-synthesize 生成）"
        )
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    testset = Testset.from_list(rows)
    logger.info("评测集从缓存加载: {}（{}）", path, len(rows))
    return testset
