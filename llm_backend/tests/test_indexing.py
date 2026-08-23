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

# 12 个短节(每节约 35 字,章节粒度≈段落粒度)+ 2 个产品节,总量 ~550 字:
# 本 case 明确回归"分块(2026-08-23 全文直接切)":旧逐段切会碎成 ~14 块,全文切应收敛为 2~3 块
_MERGE_CASE = (
    "# 一、智能沙发系列\n"
    + "".join(
        f"## 产品{i} 参数\n型号 PK-{i}\n描述 参数说明第{i}行\n补充 补充信息{i}\n"
        for i in range(1, 13)
    )
    + "## SF-2000\n品牌 芝华仕\n功能 电动可躺\n价格 5999\n"
    + "## SF-1000\n品牌 某品牌\n价格 3999\n"
)

async def test_success_with_metadata(svc, test_user_id, tmp_path, cleanup_test_data):
    path = _file(tmp_path, "p", _MERGE_CASE, "md")
    result = await svc.process_file({"path": path, "original_name": "p.md", "user_id": test_user_id})
    assert result["status"] == "success"
    assert result["chunks"] <= 3

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(DocumentChunk).where(DocumentChunk.user_id == test_user_id))).scalars().all()
        doc = (await s.execute(select(Document).where(Document.user_id == test_user_id))).scalar_one()
    assert doc.md5 and doc.file_type == "md" and doc.file_size > 0
    assert doc.chunk_count == len(rows)
    # chunk 元数据
    assert all(r.chunk_id.startswith(f"{test_user_id}_") for r in rows)
    assert all(r.chunk_id.endswith(f"_{i:04d}") for i, r in enumerate(rows))
    assert all(r.md5 == doc.md5 for r in rows)
    assert all(r.file_type == "md" for r in rows)
    # 分块(全文直接切,2026-08-23):块数收敛(≤3)且无碎片块;所有块归属=块首字符所在章节
    assert result["chunks"] <= 3
    assert all(len(r.content.strip()) >= 100 for r in rows)
    assert all(r.chapter.startswith("一、智能沙发系列") for r in rows)


# ---- 归属定位纯函数边界(全文直接切,2026-08-23) ----

def test_locate_chapter_span_edge():
    """_locate_chapter:块起点落在段间空位(\\n\\n)时顺延到下一段;段内命中本段。"""
    svc = IndexingService()
    spans = [(0, 5, "章A"), (7, 20, "章B")]        # 段A 0-4(含连接符 5-6)
    assert svc._locate_chapter(spans, 2) == "章A"   # 段 A 内部
    assert svc._locate_chapter(spans, 5) == "章B"   # 段间空位(连接符) → 顺延
    assert svc._locate_chapter(spans, 6) == "章B"
    assert svc._locate_chapter(spans, 15) == "章B"  # 段 B 内部
    assert svc._locate_chapter(spans, 99) == ""     # 越界(理论上不可达) → 空


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
