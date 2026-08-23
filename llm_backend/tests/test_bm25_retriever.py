"""BM25 检索语义集成测试：多词查询在部分词命中时也能召回（AND→OR 回归）。

命中真实 Postgres（与 test_indexing.py 同先例），语料按 test_user_id 隔离，
conftest.cleanup_test_data 清理。依赖 pg_jieba（docker postgres 镜像自带）。
"""
import pytest

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
