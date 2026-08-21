"""索引服务全链路测试:命中真实 Postgres,数据按 test user_id 隔离(conftest 清理)。"""
import asyncio

import pytest
from sqlalchemy import select, delete

from app.core.database import AsyncSessionLocal
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.services.indexing_service import IndexingService


@pytest.fixture
async def svc():
    return IndexingService()


async def _counts(user_id: str) -> tuple[int, int]:
    async with AsyncSessionLocal() as s:
        docs = (await s.execute(select(Document.id).where(Document.user_id == user_id))).all()
        chunks = (await s.execute(select(DocumentChunk.id).where(DocumentChunk.user_id == user_id))).all()
    return len(docs), len(chunks)


def _file(tmp_path, name, content: str, ext="txt") -> str:
    p = tmp_path / f"{name}.{ext}"
    p.write_text(content, encoding="utf-8")
    return str(p)


# ---- 成功链路 + 元数据 ----

async def test_success_with_metadata(svc, test_user_id, tmp_path, cleanup_test_data):
    path = _file(tmp_path, "p", "# 一、智能沙发系列\n## SF-2000\n价格 5999\n## SF-1000\n价格 3999", "md")
    result = await svc.process_file({"path": path, "original_name": "p.md", "user_id": test_user_id})
    assert result["status"] == "success"
    assert result["chunks"] >= 2

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(DocumentChunk).where(DocumentChunk.user_id == test_user_id))).scalars().all()
        doc = (await s.execute(select(Document).where(Document.user_id == test_user_id))).scalar_one()
    assert doc.md5 and doc.file_type == "md" and doc.file_size > 0
    assert doc.chunk_count == len(rows)
    # chunk 元数据
    assert all(r.chunk_id.startswith(f"{test_user_id}_") for r in rows)
    assert all(r.chunk_id.endswith("_0000") or r.chunk_id.endswith("_0001") for r in rows[:2])
    assert all(r.md5 == doc.md5 for r in rows)
    assert all(r.file_type == "md" for r in rows)
    # 章节归属:含 SF-2000 的 chunk 归属章节
    sf2000 = next(r for r in rows if "SF-2000" in r.content and "SF-1000" not in r.content)
    assert "SF-2000" in sf2000.chapter


# ---- 去重 ----

async def test_duplicate_upload(svc, test_user_id, tmp_path, cleanup_test_data):
    path = _file(tmp_path, "dup", "重复内容 abc")
    r1 = await svc.process_file({"path": path, "original_name": "dup.txt", "user_id": test_user_id})
    assert r1["status"] == "success"
    r2 = await svc.process_file({"path": path, "original_name": "dup.txt", "user_id": test_user_id})
    assert r2["status"] == "duplicate"
    docs, chunks = await _counts(test_user_id)
    assert docs == 1 and chunks == r1["chunks"]


# ---- 校验 ----

async def test_unsupported_ext(svc, test_user_id, tmp_path, cleanup_test_data):
    path = _file(tmp_path, "x", "MZ...", "exe")
    result = await svc.process_file({"path": path, "original_name": "x.exe", "user_id": test_user_id})
    assert result["status"] == "failed" and result["error"] == "unsupported"


async def test_empty_file(svc, test_user_id, tmp_path, cleanup_test_data):
    path = _file(tmp_path, "e", "")
    result = await svc.process_file({"path": path, "original_name": "e.txt", "user_id": test_user_id})
    assert result["status"] == "failed" and result["error"] == "empty_file"
    docs, chunks = await _counts(test_user_id)
    assert (docs, chunks) == (0, 0)


# ---- 原子性(故障注入) ----

async def test_embedding_failure_leaves_no_trace(svc, test_user_id, tmp_path, cleanup_test_data):
    path = _file(tmp_path, "a", "# 章\n内容" * 200, "md")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.services.indexing_service.embed_in_batches",
                   _fake_embed_zeros)
        result = await svc.process_file({"path": path, "original_name": "a.md", "user_id": test_user_id})
    assert result["status"] == "failed" and result["error"] == "embedding_failed"
    docs, chunks = await _counts(test_user_id)
    assert (docs, chunks) == (0, 0)  # 失败无痕(验收 #15)
    # 重传可重试
    r = await svc.process_file({"path": path, "original_name": "a.md", "user_id": test_user_id})
    assert r["status"] == "success"


async def _fake_embed_zeros(texts):
    import numpy as np
    return [np.zeros(1024, dtype=np.float32) for _ in texts]


# ---- 并发防重 ----

async def test_concurrent_upload_same_file(svc, test_user_id, tmp_path, cleanup_test_data):
    path = _file(tmp_path, "c", "# 并发\n内容" * 50, "md")
    r = await asyncio.gather(
        svc.process_file({"path": path, "original_name": "c.md", "user_id": test_user_id}),
        svc.process_file({"path": path, "original_name": "c.md", "user_id": test_user_id}),
    )
    statuses = sorted(x["status"] for x in r)
    assert statuses == ["duplicate", "success"]
    docs, chunks = await _counts(test_user_id)
    assert docs == 1  # 唯一约束兜底,不双写(验收 #18)
