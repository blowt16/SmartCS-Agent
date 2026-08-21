import asyncio
from unittest.mock import patch

from app.services.embedding_provider import embed_in_batches


def test_first_batch_failure_then_success():
    """瞬断自愈:首次全零(失败)→ 退避重试 → 成功(验收 #25)。"""
    attempts = {"n": 0}

    class FakeProvider:
        async def embed(self, texts):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return [[0.0] * 1024 for _ in texts]  # 全零 = 失败语义
            return [[0.1] * 1024 for _ in texts]

    with patch("app.services.embedding_provider.get_embedding_provider",
               return_value=FakeProvider()), \
         patch("app.services.embedding_provider.settings") as m:
        m.EMBEDDING_MAX_RETRIES = 3
        m.EMBEDDING_DIMENSION = 1024
        result = asyncio.run(embed_in_batches(["a"]))
        assert attempts["n"] == 2
        assert len(result) == 1 and result[0][0] != 0.0


def test_all_retries_fail_returns_zeros():
    """重试耗尽仍失败 → 返回全零(保留旧契约,由调用方检测)。"""
    class AlwaysZero:
        async def embed(self, texts):
            return [[0.0] * 1024 for _ in texts]

    with patch("app.services.embedding_provider.get_embedding_provider",
               return_value=AlwaysZero()), \
         patch("app.services.embedding_provider.settings") as m:
        m.EMBEDDING_MAX_RETRIES = 3
        m.EMBEDDING_DIMENSION = 1024
        result = asyncio.run(embed_in_batches(["a"]))
        assert all(v == 0.0 for v in result[0])
