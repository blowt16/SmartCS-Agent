"""管理端知识库接口测试(SPEC_ADMIN_CONSOLE §8.5 / plan Task D5)。

覆盖 6 个端点:列表 / stage(只落盘不写库) / commit(SSE 进度 + 错误分界) /
unstage(幂等) / PATCH(description 可还原为 NULL) / DELETE。

两条硬约束(收尾清理,不遵守就会静默污染演示数据):
1. 改到种子文档的 description 后必须用 `json={"description": None}` 还原为 NULL —— 用 `""`
   会留下空串而非 NULL,前端 `description || '—'` 恰好兜住,肉眼看不出问题。
2. `_staging/` 不在 conftest 的 `cleanup_test_data` 清理范围内(它只按 user_id 删 DB 行),
   每个碰过暂存文件的用例都要在 finally 里 unstage,否则残留会堆积。
"""
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# ⚠️ main 必须在【collection 阶段】导入(与 tests/test_admin_auth.py 同做法):
# 从仓库根跑 pytest 时,根目录另有一个无关的 D:\SmartCS-Agent\main.py,用例执行阶段
# 它排在 sys.path[0],函数内 `from main import app` 会命中错文件(实测报
# "cannot import name 'app' from 'main'"),连 conftest 的 _login 也会栽在同一处。
# 这里先把 llm_backend 提到最前导入一次,正确的 main 进 sys.modules 后,后续 import
# 全命中缓存。
_BACKEND = str(Path(__file__).resolve().parent.parent)
if _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

from main import app  # noqa: E402

from app.api.admin.knowledge import STAGING_DIR  # noqa: E402

SEED_AFTERSALES = "京东自营售后政策.docx"
SEED_PRODUCT = "京东智能家具产品知识文档.docx"

# 走的是"真正吃过解析→嵌入→入库"的链路,故用真 md 内容:
# 每段长度刻意 > CHUNK_SIZE(500) 的一半,保证两段不会被合并 → 分块数 ≈ 段数。
_FILLER = "本节用于验证暂存与索引链路的行为，包含商品参数与售后政策说明。"


def _md_bytes(paragraphs: int = 3, para_len: int = 350) -> bytes:
    """构造含 N 段正文的 md。段落间以空行分隔,单段长度 < CHUNK_SIZE。"""
    lines = ["# 管理端测试知识文档"]
    for i in range(paragraphs):
        body = f"第{i + 1}节 测试正文："
        while len(body) < para_len:
            body += _FILLER
        lines.append(body)
    return ("\n\n".join(lines) + "\n").encode("utf-8")


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client():
    """ASGI 直连(不起 uvicorn),与 tests/test_documents_api.py 同模式。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ==================== 现查 DB 的 helper(不写死数字) ====================


async def _counts() -> tuple[int, int]:
    """全表 documents / document_chunks 行数。"""
    from sqlalchemy import func, select

    from app.core.database import AsyncSessionLocal
    from app.models.document import Document
    from app.models.document_chunk import DocumentChunk

    async with AsyncSessionLocal() as s:
        docs = (await s.execute(select(func.count()).select_from(Document))).scalar()
        chunks = (await s.execute(select(func.count()).select_from(DocumentChunk))).scalar()
    return docs, chunks


async def _counts_by_md5(md5: str) -> tuple[int, int]:
    """指定 md5 的 documents / document_chunks 行数。"""
    from sqlalchemy import func, select

    from app.core.database import AsyncSessionLocal
    from app.models.document import Document
    from app.models.document_chunk import DocumentChunk

    async with AsyncSessionLocal() as s:
        docs = (await s.execute(
            select(func.count()).select_from(Document).where(Document.md5 == md5)
        )).scalar()
        chunks = (await s.execute(
            select(func.count()).select_from(DocumentChunk).where(DocumentChunk.md5 == md5)
        )).scalar()
    return docs, chunks


async def _db_state(md5: str):
    """现查 (description, status) ;行不存在返回 None。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.document import Document

    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(Document.description, Document.status)
            .where(Document.md5 == md5).order_by(Document.id)
        )).first()
    return None if row is None else (row[0], row[1])


def _staging_files() -> set[str]:
    """暂存目录下的文件名集合。"""
    if not STAGING_DIR.exists():
        return set()
    return {p.name for p in STAGING_DIR.iterdir() if p.is_file()}


# ==================== HTTP helper ====================


async def _stage(client, token: str, user_id: str, content: bytes, filename: str) -> str:
    """调 stage,返回 md5。"""
    r = await client.post(
        "/api/admin/knowledge/stage",
        headers=_bearer(token),
        files={"file": (filename, content, "text/markdown")},
        data={"user_id": user_id},
    )
    assert r.status_code == 200, r.text
    return r.json()["md5"]


async def _commit_sse(client, token: str, payload: dict) -> tuple[int, str, list[dict]]:
    """用 SSE 流式读法消费 commit(不能 .json()),返回 (status, content-type, 事件列表)。"""
    events: list[dict] = []
    async with client.stream(
        "POST", "/api/admin/knowledge/commit", headers=_bearer(token), json=payload
    ) as resp:
        status = resp.status_code
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("text/event-stream"):
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:"):].strip()))
    return status, ctype, events


async def _list_items(client, token: str) -> list[dict]:
    r = await client.get(
        "/api/admin/knowledge", headers=_bearer(token), params={"page_size": 100}
    )
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _seed_md5(client, token: str, filename: str) -> str:
    items = await _list_items(client, token)
    item = next((i for i in items if i["original_filename"] == filename), None)
    assert item is not None, f"列表里找不到种子文档 {filename}"
    return item["md5"]


async def _unstage(client, token: str, md5: str) -> None:
    """收尾用:删暂存文件(端点幂等,不存在也返回 200)。"""
    await client.delete(f"/api/admin/knowledge/stage/{md5}", headers=_bearer(token))


async def _delete_doc(client, token: str, md5: str) -> None:
    """收尾用:删 documents + chunks(已在则 404,不抛异常)。"""
    await client.delete(f"/api/admin/knowledge/{md5}", headers=_bearer(token))


# ==================== 1~2 列表 ====================


async def test_list_without_user_id_includes_seed_docs(client, admin_token):
    """D6 核心行为:管理端列表不接收 user_id,也不做归属过滤。"""
    # 不传任何参数(含 user_id)也要能拿到 —— 这是 D6 的验收点
    r = await client.get("/api/admin/knowledge", headers=_bearer(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    # 不写死 == 2:与本文件其它用例的新增/删除解耦,也容忍人工验收新增的文档
    assert body["total"] >= 2
    assert body["items"]

    items = await _list_items(client, admin_token)
    by_name = {i["original_filename"]: i for i in items}
    assert SEED_AFTERSALES in by_name, "列表缺少种子文档:京东自营售后政策.docx"
    assert SEED_PRODUCT in by_name, "列表缺少种子文档:京东智能家具产品知识文档.docx"
    # 两行种子的 chunk_count(§2.1 实测值)
    assert by_name[SEED_AFTERSALES]["chunk_count"] == 4
    assert by_name[SEED_PRODUCT]["chunk_count"] == 38


async def test_list_item_fields_have_no_title(client, admin_token):
    """列表元素字段集:不含 title(表格首列直接用 original_filename,§4.4 决策)。"""
    items = await _list_items(client, admin_token)
    item = next(i for i in items if i["original_filename"] == SEED_AFTERSALES)
    for field in ("original_filename", "description", "status", "chunk_count",
                  "created_at", "owner_id", "md5", "file_type", "file_size", "id"):
        assert field in item, f"列表元素缺字段 {field}"
    # 防止后面有人把 title 加回来
    assert "title" not in item
    assert item["owner_id"] == "6"
    assert item["status"] == "enabled"


# ==================== 3~6 stage / unstage ====================


async def test_stage_writes_disk_without_touching_db(client, admin_token, test_user_id):
    """两阶段设计的核心断言:stage 只落盘,一行 DB 都不写。"""
    content = _md_bytes(paragraphs=2)
    filename = "暂存测试.md"
    before = await _counts()
    r = await client.post(
        "/api/admin/knowledge/stage", headers=_bearer(admin_token),
        files={"file": (filename, content, "text/markdown")},
        data={"user_id": test_user_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    md5 = body["md5"]
    try:
        assert md5 == hashlib.md5(content).hexdigest()
        assert body["original_filename"] == filename
        assert body["file_type"] == "md"
        assert body["file_size"] == len(content)
        assert body["duplicate"] is False
        assert body["existing"] is None

        # 磁盘上出现了暂存文件
        assert (STAGING_DIR / f"{md5}.md").is_file()
        # 库里一行都没多(indexing 完全没跑)
        assert await _counts() == before
        assert await _counts_by_md5(md5) == (0, 0)
    finally:
        await _unstage(client, admin_token, md5)


async def test_unstage_really_reverts(client, admin_token, test_user_id):
    """取消能真撤销:暂存文件消失,且从头到尾没写过库。"""
    baseline = await _counts()
    md5 = await _stage(client, admin_token, test_user_id, _md_bytes(paragraphs=2), "撤销测试.md")
    assert (STAGING_DIR / f"{md5}.md").is_file()

    r = await client.delete(f"/api/admin/knowledge/stage/{md5}", headers=_bearer(admin_token))
    assert r.status_code == 200, r.text
    assert r.json() == {"md5": md5, "deleted": True}
    assert not (STAGING_DIR / f"{md5}.md").exists()
    assert await _counts() == baseline


async def test_unstage_is_idempotent(client, admin_token, test_user_id):
    """幂等:没东西可删也返回 200 + deleted:false,不是 404。"""
    md5 = await _stage(client, admin_token, test_user_id, _md_bytes(paragraphs=2), "幂等测试.md")
    first = await client.delete(f"/api/admin/knowledge/stage/{md5}", headers=_bearer(admin_token))
    assert first.status_code == 200
    assert first.json()["deleted"] is True

    second = await client.delete(f"/api/admin/knowledge/stage/{md5}", headers=_bearer(admin_token))
    assert second.status_code == 200, "第二次 unstage 必须是 200,不是 404"
    assert second.json() == {"md5": md5, "deleted": False}


async def test_stage_rejects_exe_and_empty_file(client, admin_token, test_user_id):
    """stage 校验:被拒的文件不该在 _staging/ 留下垃圾。"""
    files_before = _staging_files()

    r1 = await client.post(
        "/api/admin/knowledge/stage", headers=_bearer(admin_token),
        files={"file": ("bad.exe", b"MZ\x90\x00", "application/octet-stream")},
        data={"user_id": test_user_id},
    )
    assert r1.status_code == 400
    assert "不支持" in r1.json()["detail"]

    r2 = await client.post(
        "/api/admin/knowledge/stage", headers=_bearer(admin_token),
        files={"file": ("empty.md", b"", "text/markdown")},
        data={"user_id": test_user_id},
    )
    assert r2.status_code == 400

    # 对比调用前后目录内容:没有新增任何文件
    assert _staging_files() - files_before == set()
    assert not (STAGING_DIR / f"{hashlib.md5(b'').hexdigest()}.md").exists()


# ==================== 7~8 commit(SSE 进度 + done) ====================


async def test_commit_sse_progress_and_done_event(client, admin_token, test_user_id):
    """commit 走完整链路:SSE 流式返回 + 进度单向递增 + 6 阶段齐全 + done 带完整文档行。

    用 33 段 md(> 25 个片段 → 4 个嵌入批次)才看得出「生成向量」的批次细分。
    """
    content = _md_bytes(paragraphs=33, para_len=350)
    filename = "大文件测试.md"
    desc = "管理端测试:大文件提交"
    before = await _counts()
    md5 = None
    try:
        md5 = await _stage(client, admin_token, test_user_id, content, filename)
        status, ctype, events = await _commit_sse(client, admin_token, {
            "md5": md5, "original_filename": filename, "description": desc,
        })

        # ⑦ commit 是 SSE
        assert status == 200, f"commit 应 200,实际 {status}: {events}"
        assert ctype.startswith("text/event-stream"), ctype
        assert events, "SSE 流为空"

        progress = [e for e in events if e.get("type") == "progress"]
        assert progress, "没有任何 progress 事件"

        # ⑧ 进度单调不回退,且最后一个 < 100(100 只出现在 done 语义里)
        percents = [e["percent"] for e in progress]
        assert percents == sorted(percents), f"进度倒退: {percents}"
        assert percents[-1] < 100, percents

        # 阶段去重后覆盖全部 6 个
        stages = {e["stage"] for e in progress}
        assert stages == {"校验文件", "解析文档", "清洗文本", "切分片段",
                          "生成向量", "写入知识库"}, stages

        # 嵌入批次细分:必须出现「嵌入中 N/M 批」且 M > 1
        batches = [e for e in progress
                   if re.fullmatch(r"嵌入中 \d+/\d+ 批", e.get("detail") or "")]
        assert batches, f"没有嵌入批次进度: {[e['detail'] for e in progress]}"
        totals = {int(re.search(r"/(\d+) 批", e["detail"]).group(1)) for e in batches}
        assert max(totals) >= 3, f"嵌入批次总数应 >= 3(33 个片段),实际 {totals}"
        last_n, last_m = re.search(r"嵌入中 (\d+)/(\d+) 批", batches[-1]["detail"]).groups()
        assert last_n == last_m, f"最后一批应是 N == M,实际 {batches[-1]['detail']}"

        # done 事件
        done = events[-1]
        assert done["type"] == "done", f"末帧应为 done: {events[-1]}"
        doc = done["document"]
        assert doc["duplicate"] is False
        assert doc["chunk_count"] >= 25, doc["chunk_count"]
        assert doc["description"] == desc
        assert doc["created_at"]
        assert doc["original_filename"] == filename
        assert doc["md5"] == md5
        assert doc["owner_id"], "owner_id 取自令牌,不应为空"

        # done 的 document 与列表 items 元素字段集一致(duplicate 是额外的)
        item = next((i for i in await _list_items(client, admin_token) if i["md5"] == md5), None)
        assert item is not None, "列表里查不到刚提交的文档"
        assert set(doc) - {"duplicate"} == set(item)
        assert item["description"] == desc
        assert item["chunk_count"] == doc["chunk_count"]

        # 库里确实有对应行
        assert await _counts_by_md5(md5) == (1, doc["chunk_count"])
        assert await _counts() == (before[0] + 1, before[1] + doc["chunk_count"])

        # commit 成功后删掉暂存文件
        assert not (STAGING_DIR / f"{md5}.md").exists(), "成功后暂存文件应已删除"
    finally:
        if md5:
            await _delete_doc(client, admin_token, md5)
            await _unstage(client, admin_token, md5)


async def test_commit_failure_is_sse_error_event_not_http_4xx(client, admin_token, test_user_id):
    """错误分界(流已开始之后):HTTP 头已发出,失败只能走 error 事件,且保留暂存文件可重试。"""
    content = b"   \n\n  \t  \n   "   # 只含空白:过得了 stage 的空文件校验,倒在 commit 的解析
    filename = "空白测试.md"
    before = await _counts()
    md5 = None
    try:
        md5 = await _stage(client, admin_token, test_user_id, content, filename)
        status, ctype, events = await _commit_sse(client, admin_token, {
            "md5": md5, "original_filename": filename,
        })
        assert status == 200, "流已开始,改不了状态码 —— 必须是 200 而不是 4xx"
        assert ctype.startswith("text/event-stream")
        assert events
        last = events[-1]
        assert last["type"] == "error", f"末帧应为 error 事件: {last}"
        # 实测:空白 md 倒在解析阶段(doc_parser.parse_text_file 抛
        # ValueError「文本文件所有编码均解码失败或为空」),实际 error 码是 parse_error;
        # spec §8.5 预测的 empty_file 未发生 —— 行为本身正确(200 + error 事件 + 保留文件),
        # 两种都接受,不为凑断言去改实现。
        assert last["error"] in ("empty_file", "parse_error"), last
        assert last["detail"]

        # 失败保留暂存文件(便于重试)
        assert (STAGING_DIR / f"{md5}.md").exists(), "失败时暂存文件必须保留"
        # 失败路径零 DB 写入
        assert await _counts() == before
        assert await _counts_by_md5(md5) == (0, 0)
    finally:
        if md5:
            await _unstage(client, admin_token, md5)


async def test_commit_missing_staging_file_is_plain_404(client, admin_token):
    """错误分界(流开始之前):暂存文件不存在 → 普通 404 JSON,不是 SSE。"""
    r = await client.post(
        "/api/admin/knowledge/commit", headers=_bearer(admin_token),
        json={"md5": "0" * 32, "original_filename": "没暂存过.md"},
    )
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json"), r.headers["content-type"]
    assert "暂存文件不存在" in r.json()["detail"]


async def test_commit_invalid_md5_is_422(client, admin_token):
    """md5 格式非法同样在流开始前拦下(pydantic pattern → 422)。"""
    r = await client.post(
        "/api/admin/knowledge/commit", headers=_bearer(admin_token),
        json={"md5": "xyz", "original_filename": "x.md"},
    )
    assert r.status_code == 422


async def test_commit_duplicate_does_not_add_row(client, admin_token, test_user_id):
    """重复文件:第二次 commit 返回 duplicate:true,只更新描述,documents 不新增行。"""
    content = _md_bytes(paragraphs=3)
    filename = "重复测试.md"
    md5 = None
    try:
        md5 = await _stage(client, admin_token, test_user_id, content, filename)
        _, _, events = await _commit_sse(client, admin_token, {
            "md5": md5, "original_filename": filename, "description": "第一次",
        })
        first = events[-1]
        assert first["type"] == "done", first
        assert first["document"]["duplicate"] is False
        after_first = await _counts()
        assert await _counts_by_md5(md5) == (1, first["document"]["chunk_count"])

        # 第一次 commit 已删掉暂存文件,同一文件再暂存一次(同一路径,天然幂等)
        again_md5 = await _stage(client, admin_token, test_user_id, content, filename)
        assert again_md5 == md5
        _, _, events2 = await _commit_sse(client, admin_token, {
            "md5": md5, "original_filename": filename, "description": "第二次",
        })
        second = events2[-1]
        assert second["type"] == "done", second
        assert second["document"]["duplicate"] is True
        assert second["document"]["id"] == first["document"]["id"]
        assert second["document"]["chunk_count"] == first["document"]["chunk_count"]
        assert second["document"]["description"] == "第二次"   # 只更新描述
        # 不新增行
        assert await _counts() == after_first
        assert await _counts_by_md5(md5) == (1, first["document"]["chunk_count"])
        # 重复路径也删暂存文件
        assert not (STAGING_DIR / f"{md5}.md").exists()
    finally:
        if md5:
            await _delete_doc(client, admin_token, md5)
            await _unstage(client, admin_token, md5)


# ==================== PATCH / DELETE ====================


async def test_patch_description_can_be_restored_to_null(client, admin_token):
    """PATCH 能把 description 还原为 NULL —— 「用 exclude_unset 而非 is not None」的直接验收。"""
    md5 = await _seed_md5(client, admin_token, SEED_AFTERSALES)
    try:
        r = await client.patch(
            f"/api/admin/knowledge/{md5}", headers=_bearer(admin_token),
            json={"description": "管理端测试写入的描述"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["description"] == "管理端测试写入的描述"
        assert await _db_state(md5) == ("管理端测试写入的描述", "enabled")

        # 关键断言:传 None 必须写回 NULL(实现若写成 `if payload.description is not None`,
        # 这里会保持上一步的值 → 本用例失败)
        r2 = await client.patch(
            f"/api/admin/knowledge/{md5}", headers=_bearer(admin_token),
            json={"description": None},
        )
        assert r2.status_code == 200
        assert r2.json()["description"] is None
        assert (await _db_state(md5))[0] is None, "description 必须是 NULL,不是空串"

        # 没传的字段不动:只发 status 不该清空 description
        r3 = await client.patch(
            f"/api/admin/knowledge/{md5}", headers=_bearer(admin_token),
            json={"status": "enabled"},
        )
        assert r3.status_code == 200
        assert r3.json()["status"] == "enabled"
        assert (await _db_state(md5))[0] is None
    finally:
        # 还原种子文档原值:NULL(用 "" 会留下空串 —— 前端 description || '—' 恰好兜住,
        # 看不出问题,属静默污染演示数据)
        await client.patch(
            f"/api/admin/knowledge/{md5}", headers=_bearer(admin_token),
            json={"description": None},
        )


async def test_patch_missing_md5_404(client, admin_token):
    r = await client.patch(
        f"/api/admin/knowledge/{'0' * 32}", headers=_bearer(admin_token),
        json={"description": "x"},
    )
    assert r.status_code == 404


async def test_patch_invalid_status_422(client, admin_token):
    md5 = await _seed_md5(client, admin_token, SEED_PRODUCT)
    r = await client.patch(
        f"/api/admin/knowledge/{md5}", headers=_bearer(admin_token),
        json={"status": "bogus"},
    )
    assert r.status_code == 422
    # 非法值被拦下,库里状态没被动过
    assert (await _db_state(md5))[1] == "enabled"


async def test_delete_missing_md5_404(client, admin_token):
    r = await client.delete(f"/api/admin/knowledge/{'0' * 32}", headers=_bearer(admin_token))
    assert r.status_code == 404


# ==================== 全链路 ====================


async def test_stage_commit_patch_delete_full_chain(
    client, admin_token, test_user_id, cleanup_test_data
):
    """stage → commit(带描述) → 列表可见 → PATCH 停用 → DELETE → documents/chunks 都清空。"""
    content = _md_bytes(paragraphs=3)
    filename = "全链路测试.md"
    md5 = None
    try:
        md5 = await _stage(client, admin_token, test_user_id, content, filename)
        status, ctype, events = await _commit_sse(client, admin_token, {
            "md5": md5, "original_filename": filename, "description": "全链路描述",
        })
        assert status == 200 and ctype.startswith("text/event-stream")
        assert events[-1]["type"] == "done", events[-1]
        doc = events[-1]["document"]
        assert doc["description"] == "全链路描述"
        assert doc["chunk_count"] > 0
        assert doc["status"] == "enabled"

        # 列表能查到且描述正确
        item = next((i for i in await _list_items(client, admin_token) if i["md5"] == md5), None)
        assert item is not None
        assert item["description"] == "全链路描述"

        # PATCH 改状态
        p = await client.patch(
            f"/api/admin/knowledge/{md5}", headers=_bearer(admin_token),
            json={"status": "disabled"},
        )
        assert p.status_code == 200
        assert p.json()["status"] == "disabled"
        assert (await _db_state(md5))[1] == "disabled"

        # DELETE:documents 与 document_chunks 一起清掉(不留孤儿块)
        d = await client.delete(f"/api/admin/knowledge/{md5}", headers=_bearer(admin_token))
        assert d.status_code == 200, d.text
        assert d.json()["deleted"] is True
        assert await _counts_by_md5(md5) == (0, 0)
    finally:
        if md5:
            await _delete_doc(client, admin_token, md5)
            await _unstage(client, admin_token, md5)
