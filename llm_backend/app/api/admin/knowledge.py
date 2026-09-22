"""管理端知识库:全平台文档列表 + 两阶段上传(stage 只落盘 / commit 走完整索引链路并推 SSE 进度)。

检索侧不按 user_id 过滤(向量路与 BM25 路均无该谓词),故管理端上传的文档客户端立刻可检索
——这正是知识库管理模块成立的前提,也决定了本模块的列表接口不做 user_id 过滤。
"""
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal, get_db
from app.core.logger import get_logger
from app.core.security import require_admin
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.user import User
from app.schemas.admin import KnowledgeCommit, KnowledgeUpdate
from app.services.indexing_service import IndexingService

logger = get_logger(service="admin_knowledge")

router = APIRouter()

# 解析:knowledge.py → api/admin → api → app → llm_backend,再拼 uploads/_staging
# 与 main.py 的 UPLOAD_DIR(= CWD 下的 "uploads")等价 —— run.py 会 chdir 到 llm_backend
STAGING_DIR = Path(__file__).resolve().parent.parent.parent.parent / "uploads" / "_staging"

# md5 是 32 位十六进制;服务端用正则校验后才拼路径,绝不接受客户端回传任意路径(目录穿越面)
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")


def _ext_of(filename: str) -> str:
    """取扩展名(含点,小写);无扩展名返回空串。"""
    return os.path.splitext(filename)[1].lower()


def _doc_row(d: Document) -> dict:
    """文档行 → 列表元素结构(列表 / commit done / PATCH 响应三处共用)。"""
    return {
        "id": d.id,
        "md5": d.md5,
        "original_filename": d.original_filename,
        "description": d.description,
        "file_type": d.file_type,
        "file_size": d.file_size,
        "chunk_count": d.chunk_count,
        "status": d.status,
        "owner_id": d.user_id,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _cleanup_stale_staging() -> int:
    """机会式清理过期暂存文件,返回删除个数。

    在 stage 端点开头调用:残留只在 stage 时产生,所以"有上传就有清理"在触发时机上完备,
    泄漏量的上界 = 最后一次使用该功能之前的未提交文件数,不随时间无限增长。
    删除失败静默跳过 —— 文件可能正被并发的 unstage / commit 处理,那种情况下"没删成"不是错误。
    """
    if not STAGING_DIR.exists():
        return 0
    cutoff = time.time() - settings.KNOWLEDGE_STAGE_TTL_HOURS * 3600
    removed = 0
    for p in STAGING_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("清理过期暂存文件 {} 个(TTL {}h)", removed, settings.KNOWLEDGE_STAGE_TTL_HOURS)
    return removed


# ==================== 列表 ====================


@router.get("")
async def list_knowledge(
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    keyword: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """全平台文档列表(不接收 user_id,不做归属过滤)。按 created_at DESC。"""
    conds = []
    kw = keyword.strip()
    if kw:
        # 刻意不匹配 md5:搜「1」会命中几乎所有十六进制 md5 → 用户以为搜索坏了。
        # 关键词是纯数字时改匹配 id 等值,与列表「文档编号」列语义一致。
        sub = [Document.original_filename.ilike(f"%{kw}%")]
        if kw.isdigit():
            sub.append(Document.id == int(kw))
        conds.append(or_(*sub))

    total = (await db.execute(
        select(func.count()).select_from(Document).where(*conds)
    )).scalar()
    rows = (await db.execute(
        select(Document).where(*conds)
        .order_by(Document.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_doc_row(d) for d in rows],
    }


# ==================== 两阶段上传:第 1 步(暂存,不索引) ====================


@router.post("/stage")
async def stage_knowledge(
    file: UploadFile = File(...),
    user_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """只把文件落到磁盘并取出基本信息,不解析内容、不清洗、不分块、不嵌入、不写任何 DB 行。

    这是「取消能真撤销」的全部依据:撤销时数据库里从来就没有过痕迹。
    """
    _cleanup_stale_staging()

    filename = file.filename or ""
    ext = _ext_of(filename).lstrip(".")
    if ext not in settings.allowed_extensions:
        raise HTTPException(400, f"不支持的文件格式: .{ext}")

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(400, "文件为空")
    if len(content) > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
        raise HTTPException(400, f"文件大小超过限制(最大 {settings.MAX_FILE_SIZE_MB}MB)")

    md5_hex = hashlib.md5(content).hexdigest()

    # 校验全部通过后才落盘 —— 否则被拒的文件会在 _staging/ 留下垃圾
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staged_path = STAGING_DIR / f"{md5_hex}{_ext_of(filename)}"
    staged_path.write_bytes(content)

    dup = (await db.execute(
        select(Document).where(Document.user_id == user_id, Document.md5 == md5_hex)
        .order_by(Document.id)
    )).scalars().first()

    return {
        "md5": md5_hex,
        "original_filename": filename,
        "file_type": ext,
        "file_size": len(content),
        "duplicate": dup is not None,
        "existing": None if dup is None else {
            "id": dup.id,
            "created_at": dup.created_at.isoformat() if dup.created_at else None,
            "chunk_count": dup.chunk_count,
            "description": dup.description,
        },
    }


# ==================== 两阶段上传:第 2 步(提交索引,SSE 流式) ====================


@router.post("/commit")
async def commit_knowledge(
    payload: KnowledgeCommit,
    current_user: User = Depends(require_admin),
):
    """真正解析、清洗、分块、嵌入、落库,并边跑边推进度事件。

    SSE 分帧格式与既有 /api/langgraph/query 一致(data: {json}\\n\\n)。
    HTTP 状态码的分界:流开始之前(暂存文件不存在 / md5 非法 / 请求体不合法)仍是普通
    4xx JSON;流开始之后(解析失败/嵌入失败/写库异常)HTTP 头已发出,只能走 error 事件。
    """
    staged_path = STAGING_DIR / f"{payload.md5}{_ext_of(payload.original_filename)}"
    if not staged_path.exists():
        raise HTTPException(404, f"暂存文件不存在或已被清理: {payload.md5}")
    # 刷新 mtime,防并发机会式清理误删(暂存超 24h 后又恰好有另一次 stage 在跑)
    os.utime(staged_path)

    # 归属身份取自令牌,不取自请求体(KnowledgeCommit schema 里也没有该字段);
    # 组级依赖已跑过一次 require_admin,FastAPI 对同依赖同请求复用缓存,这里不重复查库。
    user_id = str(current_user.id)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def on_progress(stage: str, percent: int, detail: str = "") -> None:
            await queue.put({"type": "progress", "stage": stage,
                             "percent": percent, "detail": detail})

        async def run() -> None:
            """跑索引 + 后处理,结果塞队列。异常一律转 error 事件(流里改不了状态码)。"""
            try:
                result = await IndexingService().process_file(
                    {"path": str(staged_path),
                     "original_name": payload.original_filename,
                     "user_id": user_id},
                    on_progress=on_progress,
                )
                event = await _finalize(result, payload, user_id, staged_path)
            except Exception as e:  # noqa: BLE001 —— 流已开始,任何异常都只能转 error 事件
                logger.exception("commit 失败: {}", e)
                event = {"type": "error", "error": "internal", "detail": str(e)}
            await queue.put(event)
            await queue.put(None)                       # 结束哨兵

        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            # 客户端断开(刷新浏览器)→ Starlette 取消生成器 → 顺手取消索引任务。
            # 安全性:process_file 是"最后一步单事务写入",取消发生在写入前 = 零写入,不留半成品。
            if not task.done():
                task.cancel()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _finalize(result: dict, payload: KnowledgeCommit, user_id: str, staged_path: Path) -> dict:
    """commit 的表外后处理:写描述 / 查回完整行 / 清理暂存文件。

    失败时【保留】暂存文件(便于重试);成功与重复则删除。
    """
    status = result.get("status")
    if status == "failed":
        return {"type": "error", "error": result.get("error", "internal"),
                "detail": result.get("detail", "")}

    # 不用 Depends(get_db):本协程在路由函数返回之后才执行,走显式会话不依赖依赖退栈时机
    async with AsyncSessionLocal() as s:
        doc = (await s.execute(
            select(Document)
            .where(Document.user_id == user_id, Document.md5 == payload.md5)
            .order_by(Document.id)
        )).scalars().first()
        if doc is None:
            return {"type": "error", "error": "internal",
                    "detail": "索引已执行但查不到对应文档记录"}
        if payload.description is not None:
            doc.description = payload.description
        await s.commit()
        await s.refresh(doc)
        row = _doc_row(doc)

    try:
        os.remove(staged_path)
    except OSError:
        logger.warning("暂存文件删除失败(不影响本次提交): {}", staged_path)

    return {"type": "done", "document": {**row, "duplicate": status == "duplicate"}}


# ==================== 撤销暂存 ====================


@router.delete("/stage/{md5}")
async def unstage_knowledge(md5: str):
    """撤销暂存:删掉落盘文件,不碰任何 DB。幂等("本来就不在"与"刚删掉"是同一个结果)。"""
    if not _MD5_RE.match(md5):
        raise HTTPException(400, f"md5 格式非法: {md5}")
    deleted = False
    if STAGING_DIR.exists():
        # 请求里没有 original_filename,拿不到扩展名 → glob 定位;不接受客户端回传路径
        for p in STAGING_DIR.glob(f"{md5}.*"):
            try:
                p.unlink()
                deleted = True
            except OSError:
                continue
    return {"md5": md5, "deleted": deleted}


# ==================== 编辑描述/状态 与 删除 ====================


@router.patch("/{md5}")
async def update_knowledge(
    md5: str,
    payload: KnowledgeUpdate,
    db: AsyncSession = Depends(get_db),
):
    """写描述/状态。只有这两个可写字段 —— 文件名/类型/大小/片段数/创建时间都是客观事实。"""
    # 同 md5 可能挂多个 user_id:只更新 id 最小的那一行,不能因 scalar_one_or_none 抛 500
    rows = (await db.execute(
        select(Document).where(Document.md5 == md5).order_by(Document.id)
    )).scalars().all()
    if not rows:
        raise HTTPException(404, f"文档不存在: {md5}")

    doc = rows[0]
    # 必须用 exclude_unset 区分"没传"和"传了 null":写成 `if payload.x is not None`
    # 会让 description 永远回不到 NULL(存量行正是 NULL 状态,改一次就再也恢复不了)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(doc, field, value)
    await db.flush()
    await db.refresh(doc)
    return _doc_row(doc)


@router.delete("/{md5}")
async def delete_knowledge(md5: str, db: AsyncSession = Depends(get_db)):
    """按 md5 全量删:删该 md5 的所有 documents 行 + 对应 chunks(同事务)。"""
    rows = (await db.execute(select(Document).where(Document.md5 == md5))).scalars().all()
    if not rows:
        raise HTTPException(404, f"文档不存在: {md5}")
    # chunks 不删会留下孤儿块,检索侧仍会召回已删文档
    await db.execute(delete(DocumentChunk).where(DocumentChunk.md5 == md5))
    for r in rows:
        await db.delete(r)
    return {"md5": md5, "deleted": True, "documents": len(rows)}
