"""pytest 公共 fixtures:确保 app 可导入,提供测试数据清理。"""
import asyncio
import sys
import uuid
from pathlib import Path

import pytest

if sys.platform == "win32":
    # psycopg 异步驱动不支持 Windows 默认的 ProactorEventLoop。
    # 必须在 pytest-asyncio 创建测试事件循环前切换策略(database.py 的
    # 设置在测试循环创建后才生效,迟于此)。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

LLM_BACKEND = Path(__file__).resolve().parent.parent
if str(LLM_BACKEND) not in sys.path:
    sys.path.insert(0, str(LLM_BACKEND))


@pytest.fixture
def test_user_id() -> str:
    """每个测试独立的 user_id,避免与生产数据/测试间互扰。"""
    return f"test_{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def cleanup_test_data(test_user_id):
    """测试结束删除该 user 的 documents/chunks(含失败注入的残留)。"""
    yield
    from sqlalchemy import delete
    from app.core.database import AsyncSessionLocal
    from app.models.document import Document
    from app.models.document_chunk import DocumentChunk

    async with AsyncSessionLocal() as s:
        await s.execute(delete(DocumentChunk).where(DocumentChunk.user_id == test_user_id))
        await s.execute(delete(Document).where(Document.user_id == test_user_id))
        await s.commit()
