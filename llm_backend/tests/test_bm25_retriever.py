"""BM25 检索语义集成测试：多词查询在部分词命中时也能召回（AND→OR 回归）。

命中真实 Postgres（与 test_indexing.py 同先例），语料按 test_user_id 隔离，
conftest.cleanup_test_data 清理。依赖 pg_jieba（docker postgres 镜像自带）。
"""
import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.hybrid_retrieval.bm25_sql_retriever import (
    BM25SQLRetriever,
)
from app.services.indexing_service import IndexingService


async def _insert_corpus(test_user_id, tmp_path):
    """注入迷你语料：完整门锁参数 / 门锁简讯(缺参数字段) / 无关沙发。"""
    svc = IndexingService()
    files = {
        "lock_full.txt": "小米全自动智能门锁Pro 续航 180 天 静音设计。",
        "lock_brief.txt": "小米智能门锁 卧室门使用。",
        "sofa.txt": "芝华仕电动沙发 头等舱 豪华体验。",
    }
    for name, content in files.items():
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        r = await svc.process_file(
            {"path": str(p), "original_name": name, "user_id": test_user_id}
        )
        assert r["status"] == "success"


async def test_bm25_recalls_docs_with_partial_terms(
    test_user_id, tmp_path, cleanup_test_data
):
    """8 词查询仅部分词命中时必须召回含'门锁'的 chunk（旧 AND 语义返回 0）。

    搜索无 user_id 过滤（全局语料存在高分 chunk，如文档总览），
    断言必须按 test_user_id 过滤后再比较 BM25 分数。
    """
    await _insert_corpus(test_user_id, tmp_path)
    results = await BM25SQLRetriever().search(
        "小米全自动智能门锁Pro的详细参数配置", top_k=10
    )
    mine = [r for r in results if r["user_id"] == test_user_id]
    assert mine, f"OR 语义应召回含'门锁'的 chunk，实际: {[r['text'][:30] for r in results]}"
    # 全词命中(5 词)的完整参数 chunk 分数应高于部分命中(3 词)的简讯 chunk
    mine_sorted = sorted(mine, key=lambda r: r["bm25_score"], reverse=True)
    assert "续航" in mine_sorted[0]["text"], (
        f"全词命中的完整参数 chunk 应排前，实际: {[r['text'][:30] + '|' + str(r['bm25_score']) for r in mine_sorted]}"
    )


async def test_bm25_blank_query_returns_empty(test_user_id, tmp_path, cleanup_test_data):
    """全空白/纯标点查询：返回空列表而非 SQL 错误。"""
    await _insert_corpus(test_user_id, tmp_path)
    retriever = BM25SQLRetriever()
    assert await retriever.search("？？？", top_k=5) == []
    assert await retriever.search("   ", top_k=5) == []


async def test_bm25_single_word_still_works(test_user_id, tmp_path, cleanup_test_data):
    """单 token 查询行为不回退：'沙发' 必须命中 sofa chunk。"""
    await _insert_corpus(test_user_id, tmp_path)
    results = await BM25SQLRetriever().search("沙发", top_k=5)
    assert results and "沙发" in results[0]["text"]


async def test_bm25_recalls_split_brand_word(test_user_id, tmp_path, cleanup_test_data):
    """文档侧被拆散的品牌词必须召回（"宁可多召回"核心用例）。

    '品牌：芝华仕' 入库后生成列将 芝华仕 拆为 芝/华/仕 单字 lexeme（实测），
    而查询侧 '芝华仕' 为整词 —— 单路 jiebacfg(仅整词 OR)在此假阴性返回空，
    双配置并集(单字兜底)才命中。本用例在 jiebacfg 单路实现上 FAIL。
    注意搜索无 user_id 过滤，断言必须按 test_user_id 过滤（全局存在整词
    '芝华仕' chunk，否则被生产语料"伪通过"）。
    """
    svc = IndexingService()
    p = tmp_path / "brand.txt"
    p.write_text("商品信息\n品牌：芝华仕 CHEERS\n品类：电动智能沙发", encoding="utf-8")
    r = await svc.process_file(
        {"path": str(p), "original_name": "brand.txt", "user_id": test_user_id}
    )
    assert r["status"] == "success"

    results = await BM25SQLRetriever().search("芝华仕", top_k=50)
    mine = [r for r in results if r["user_id"] == test_user_id]
    assert mine, "拆分形态的品牌词必须被召回"
    assert "芝华仕" in mine[0]["text"]


# ==================== 停用文档过滤（SPEC_DOCUMENT_STATUS_FILTER A1a/A2）====================


# 生产语料中不存在的独特词，避免与库内 42 块竞争 top_k（既有 #8 正是竞争导致）
_MARK_PREFIX = "紫电牌折叠躺椅"


def _marked_case(test_user_id: str) -> str:
    """带唯一标记的迷你语料。

    ⚠️ 标记必须含 test_user_id：内容决定 md5，而 D5 之后 md5 全局唯一 ——
    固定内容会让同一测试跨次运行互撞（上一次的残留行会让本次判为 duplicate）。
    """
    return (
        f"商品信息\n品牌：紫电\n品类：折叠躺椅\n\n"
        f"{_MARK_PREFIX}{test_user_id} 支持三档调节、承重150kg。\n"
        f"规格参数\n{_MARK_PREFIX}{test_user_id} 展开尺寸 180x60cm。\n"
    )


async def _set_status(user_id: str, status: str) -> None:
    """直接改 documents.status（管理端 PATCH 的另一条等价路径）。"""
    async with AsyncSessionLocal() as s:
        await s.execute(
            text("UPDATE documents SET status=:st WHERE user_id=:u"),
            {"st": status, "u": user_id},
        )
        await s.commit()


async def _insert_unique_corpus(test_user_id: str, tmp_path) -> str:
    """注入带唯一标记的语料，返回该标记（用作查询词）。"""
    marker = f"{_MARK_PREFIX}{test_user_id}"
    p = tmp_path / "unique.txt"
    p.write_text(_marked_case(test_user_id), encoding="utf-8")
    r = await IndexingService().process_file(
        {"path": str(p), "original_name": p.name, "user_id": test_user_id}
    )
    assert r["status"] == "success", r
    return marker


async def test_bm25_excludes_disabled_document(test_user_id, tmp_path, cleanup_test_data):
    """A1a：文档停用后，BM25 路必须查不到它的块；重新启用后恢复。

    三条断言缺一不可 —— 只写"停用后不命中"会**空过**：
    查询词打偏或该路降级返回空时，否定断言自动成立，什么都没测到。
    """
    marker = await _insert_unique_corpus(test_user_id, tmp_path)

    hits = await BM25SQLRetriever().search(marker, top_k=10)
    assert [r for r in hits if r["user_id"] == test_user_id], (
        "【正向】启用时应命中，否则本用例测不到任何东西"
    )

    await _set_status(test_user_id, "disabled")
    hits = await BM25SQLRetriever().search(marker, top_k=10)
    assert not [r for r in hits if r["user_id"] == test_user_id], (
        "停用后 BM25 不应命中该文档的块"
    )

    await _set_status(test_user_id, "enabled")
    hits = await BM25SQLRetriever().search(marker, top_k=10)
    assert [r for r in hits if r["user_id"] == test_user_id], "重新启用后应恢复命中"


async def test_bm25_excludes_orphan_chunk(test_user_id, cleanup_test_data):
    """A2：孤儿块（无对应 documents 行）必须被排除 —— D2 白名单语义。

    ⚠️ 这是【有意行为】不是 bug：找不到启用中的文档就不放行。修这个用例的
    失败时请先确认需求，不要直接把过滤条件放宽成"黑名单"。
    """
    marker = f"{_MARK_PREFIX}孤儿{test_user_id}"
    # 插一个孤儿块：user_id 与 md5 都不在 documents 里（embedding 复制现成的）
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO document_chunks "
                "(source,file_path,user_id,chunk_index,chunk_id,md5,file_type,content,embedding) "
                "SELECT 'orphan.txt','/tmp/orphan.txt',:u,0,:cid,:md5,'txt',:content, embedding "
                "FROM document_chunks LIMIT 1"
            ),
            {"u": test_user_id, "cid": f"{test_user_id}_orphan_0000",
             "md5": "0" * 32, "content": marker},
        )
        await s.commit()

    hits = await BM25SQLRetriever().search(marker, top_k=10)
    assert not [r for r in hits if r["user_id"] == test_user_id], (
        "孤儿块应被排除（白名单语义）—— 若此处失败，说明过滤条件被放宽了"
    )
