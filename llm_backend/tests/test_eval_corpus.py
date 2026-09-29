"""评测语料读取测试（SPEC_DOCUMENT_STATUS_FILTER A8 / D11）。

语料默认改为"读全库"（排除 test_% 测试账号），`--user` 降级为可选收窄 —— 原因：
    ① 原默认值 "1" 早就是坏的（生产数据已迁到 owner 6）；
    ② "为评测把同一内容再挂一份到第二个 user_id"实测会让检索 top-K 出现完全重复的
       条目（RRF 按 chunk_id 去重，而 chunk_id 含 user_id），白占名额、压低
       context_precision —— 正是本模块"语料复用生产分块"立身之本的例外。

纯 DB 读，不调 LLM（RAGAS judge key 只影响合成/打分，与语料读取无关）。
"""
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from evaluation import testset_builder


async def test_corpus_defaults_to_whole_library(test_user_id, cleanup_test_data):
    """不传 user_id = 读全库，且不含 test_% 账号的块。

    ⚠️ 必须自己插一块 test_ 前缀的假数据，否则"不含 test_%"恒真 —— 库里本来就
    没有（生产 42 块全部属于 owner 6）。这是本用例最容易写错的地方。
    """
    marker = f"评测语料探针{test_user_id}"
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO document_chunks "
                "(source,file_path,user_id,chunk_index,chunk_id,md5,file_type,content,embedding) "
                "SELECT 'probe.txt','/tmp/probe',:u,0,:cid,:md5,'txt',:content, embedding "
                "FROM document_chunks LIMIT 1"
            ),
            {"u": test_user_id, "cid": f"{test_user_id}_probe_0000",
             "md5": "0" * 32, "content": marker},
        )
        await s.commit()

    docs = await testset_builder.load_corpus_documents(100)
    assert docs, "全库语料不应为空"
    got = {str(d.metadata["user_id"]) for d in docs}
    assert not any(u.startswith("test_") for u in got), (
        f"默认读全库时必须排除 test_% 测试账号，实际混入: {sorted(got)}"
    )
    assert not any(marker in d.page_content for d in docs), "测试夹具内容不得进入评测语料"


async def test_corpus_user_filter_still_works(test_user_id, cleanup_test_data):
    """显式传 user_id 时收窄语义仍可用（旧调用方式的兼容面）。"""
    marker = f"评测收窄探针{test_user_id}"
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO document_chunks "
                "(source,file_path,user_id,chunk_index,chunk_id,md5,file_type,content,embedding) "
                "SELECT 'narrow.txt','/tmp/narrow',:u,0,:cid,:md5,'txt',:content, embedding "
                "FROM document_chunks LIMIT 1"
            ),
            {"u": test_user_id, "cid": f"{test_user_id}_narrow_0000",
             "md5": "0" * 32, "content": marker},
        )
        await s.commit()

    docs = await testset_builder.load_corpus_documents(100, test_user_id)
    assert len(docs) == 1, f"按 user_id 收窄应只取到本账号的 1 块，实际 {len(docs)}"
    assert marker in docs[0].page_content


async def test_corpus_empty_raises_without_stale_guidance(cleanup_test_data):
    """空语料报错，且文案不再指引"先 ingest 到某个 user_id"（那条路已废弃）。"""
    try:
        await testset_builder.load_corpus_documents(100, "不存在的owner")
        raise AssertionError("空语料应抛 RuntimeError")
    except RuntimeError as e:
        assert "ingest" not in str(e), f"不应再出现已废弃的 ingest 指引: {e}"
        assert "无可用知识分块" in str(e)
