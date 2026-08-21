"""RRF 融合按 chunk_id 去重(双路命中同一 chunk 计 1 次)。"""
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.hybrid_retrieval.rrf_fusion import rrf_fuse


def test_rrf_dedups_by_chunk_id():
    vec = [
        {"chunk_id": "u1_md5_0000", "text": "A"},
        {"chunk_id": "u1_md5_0002", "text": "C"},
    ]
    bm25 = [
        {"chunk_id": "u1_md5_0000", "text": "A"},   # 与向量路重复
        {"chunk_id": "u1_md5_0001", "text": "B"},
    ]
    fused = rrf_fuse([vec, bm25], id_key="chunk_id", top_k=10)
    ids = [d["chunk_id"] for d in fused]
    assert len(ids) == len(set(ids)) == 3       # 去重后 3 条,非 4 条
    a = next(d for d in fused if d["chunk_id"] == "u1_md5_0000")
    assert a["rrf_score"] == 1 / (60 + 1) + 1 / (60 + 1)   # 双路 rank=1 各计一次


def test_rrf_without_chunk_id_uses_id_fallback():
    """未带 chunk_id 的 doc 走 id 兜底(兼容旧 doc),不去重也不崩溃。"""
    vec = [{"id": 1, "text": "A"}]
    bm25 = [{"id": 2, "text": "B"}]
    fused = rrf_fuse([vec, bm25], id_key="chunk_id", top_k=10)
    assert len(fused) == 2
