"""RAG 检索入口集成测试：向量路停用过滤（A1b）+ 返回结构护栏（A6）。

命中真实 Postgres（与 test_bm25_retriever.py 同先例），语料按 test_user_id 隔离，
conftest.cleanup_test_data 清理。向量路走真实 embedding API。
"""
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.services.indexing_service import IndexingService
from app.services.rag_retriever_service import get_rag_retriever_service

# 生产语料中不存在的独特词，避免与库内 42 块竞争 top_k
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
    async with AsyncSessionLocal() as s:
        await s.execute(
            text("UPDATE documents SET status=:st WHERE user_id=:u"),
            {"st": status, "u": user_id},
        )
        await s.commit()


async def _insert_unique_corpus(test_user_id: str, tmp_path) -> str:
    marker = f"{_MARK_PREFIX}{test_user_id}"
    p = tmp_path / "unique.txt"
    p.write_text(_marked_case(test_user_id), encoding="utf-8")
    r = await IndexingService().process_file(
        {"path": str(p), "original_name": p.name, "user_id": test_user_id}
    )
    assert r["status"] == "success", r
    return marker


async def test_vector_search_excludes_disabled_document(
    test_user_id, tmp_path, cleanup_test_data
):
    """A1b：文档停用后，向量路必须查不到它的块；重新启用后恢复。

    三条断言缺一不可 —— 只写"停用后不命中"会**空过**：`_vector_search` 在
    embedding 全零时 return []、`_safe()` 把异常吞成 []，任一路整体返回空时
    否定断言自动成立。故必须先有正向断言证明"过滤器确实在筛东西"。
    """
    marker = await _insert_unique_corpus(test_user_id, tmp_path)
    svc = get_rag_retriever_service()

    vec = await svc._vector_search(marker, 10)
    assert [d for d in vec if d["user_id"] == test_user_id], (
        "【正向】启用时向量路应命中，否则本用例测不到任何东西"
    )

    await _set_status(test_user_id, "disabled")
    vec = await svc._vector_search(marker, 10)
    assert not [d for d in vec if d["user_id"] == test_user_id], (
        "停用后向量路不应命中该文档的块"
    )

    await _set_status(test_user_id, "enabled")
    vec = await svc._vector_search(marker, 10)
    assert [d for d in vec if d["user_id"] == test_user_id], "重新启用后应恢复命中"


async def test_search_result_structure_unchanged(test_user_id, tmp_path, cleanup_test_data):
    """A6：混合检索返回的 doc 字典字段集不变（下游 summarize / @tool 依赖）。

    特别守 `md5` **不在**返回结构里 —— 断言停用过滤时若按 d["md5"] 写会恒真空过，
    本用例把"没有这个键"固化成契约，让那种写法立刻 KeyError 而不是静默通过。
    """
    marker = await _insert_unique_corpus(test_user_id, tmp_path)
    docs = await get_rag_retriever_service().search(marker)

    assert docs, "混合检索应返回结果（正向断言）"
    base = {"text", "id", "chunk_id", "source", "file_path", "user_id", "chunk_index",
            "sku_codes", "chapter"}
    # 分数字段按"该 doc 经过了哪条路"出现：向量路 score、BM25 路 bm25_score、融合后 rrf_score、
    # 精排后 rerank_score。这里只固化"必有字段"与"不许有 md5"，不固化分数字段的组合。
    optional = {"score", "bm25_score", "rrf_score", "rerank_score"}
    for d in docs:
        missing = base - set(d)
        assert not missing, f"返回结构缺字段 {missing}（下游消费方会 KeyError）: {sorted(d)}"
        assert set(d) <= base | optional, f"返回结构多出未约定字段: {set(d) - base - optional}"
        assert "md5" not in d, "md5 不应出现在返回结构里（断言过滤时不能按它写）"


async def test_vector_search_excludes_orphan_chunk(test_user_id, cleanup_test_data):
    """A2：孤儿块（无对应 documents 行）向量路也必须排除 —— D2 白名单语义。

    ⚠️ 有意行为不是 bug：找不到启用中的文档就不放行。
    """
    marker = f"{_MARK_PREFIX}孤儿{test_user_id}"
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

    vec = await get_rag_retriever_service()._vector_search(marker, 10)
    assert not [d for d in vec if d["user_id"] == test_user_id], (
        "孤儿块应被向量路排除（白名单语义）—— 若此处失败，说明过滤条件被放宽了"
    )
