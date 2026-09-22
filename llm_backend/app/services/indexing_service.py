"""标准 RAG 索引服务:校验 → MD5 → 去重 → 解析 → 清洗 → 分块 → 嵌入 → 单事务入库。

原子性(见 spec §3):全链路零中间态——解析/清洗/分块/嵌入全在内存,
任何 DB 写入只发生在最后一步且只有一个事务;失败无痕,重传即重试。
"""
import hashlib
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

import numpy as np
from langchain_core.documents import Document as LangchainDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logger import get_logger
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.services.doc_parser import parse_text_file, parse_docx, parse_md_string, Segment
from app.services.embedding_provider import embed_in_batches
from app.services.mineru_client import MinerUError, parse_pdf
from app.services.text_cleaner import clean_text

logger = get_logger(service="indexing")

# 进度回调:(阶段标签, 百分比 0-100, 细分说明) -> 协程
ProgressFn = Callable[[str, int, str], Awaitable[None]]

# 阶段权重(和为 100),按典型耗时占比拍的经验值。
# 不同文件类型必然有偏差:.md 解析近乎瞬时 → 会先跳到 50% 再慢慢走嵌入;
# PDF 走 MinerU 云端 → 解析阶段吃掉 45% 的大部分。这是"真实进度"的代价:
# 宁可真实地跳一下再匀速走,也不用定时器编造平滑假进度。
_STAGES = [
    ("validate", "校验文件",    5),
    ("parse",    "解析文档",   45),
    ("clean",    "清洗文本",    5),
    ("split",    "切分片段",    5),
    ("embed",    "生成向量",   35),
    ("store",    "写入知识库",  5),
]
_LABELS = {_key: _label for _key, _label, _w in _STAGES}
_WEIGHTS = {_key: _w for _key, _label, _w in _STAGES}


class IndexingService:
    """RAG 索引服务(校验 → MD5 去重 → 解析 → 清洗 → 分块 → 嵌入 → 原子入库)"""

    def __init__(self):
        # add_start_index: split_documents 给每块打上原文起始字符位置,供章节归属定位
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.CHUNK_SIZE,
            chunk_overlap=settings.CHUNK_OVERLAP,
            separators=["\n\n", "\n", "。", "！", "？", "，", " ", ""],
            add_start_index=True,
        )

    # ==================== 分块归属 ====================

    @staticmethod
    def _locate_chapter(spans: List[tuple[int, int, str, str]], pos: int) -> str:
        """块内首个非空字符所在段 → 章节(单值,块首归属,语义不变)。

        段间以 \\n\\n 连接,连接符为"空字符":块起点落在连接符上时顺延到下一段
        (spans 的 end 不含连接符,故 pos < end 即命中本段或顺延后的下一段)。
        """
        for start, end, chapter, _sku in spans:
            if pos < end:
                return chapter
        return ""

    @staticmethod
    def _collect_skus(spans: List[tuple[int, int, str, str]], start: int, end: int) -> List[str]:
        """块文本区间 [start, end) 覆盖段的全部商品编码,按首次出现序去重。

        与 _locate_chapter 单值归属不同:chunk 跨商品为全文统一切分的必然态,
        sku 必须多值收集(不猜主体,主体判断下放消费侧 LLM,见 SPEC_RAG_SKU_METADATA D3)。
        重叠区(chunk_overlap)使边界块与邻块重复收集同编码——符合预期(两块文本均真实覆盖该段)。
        """
        skus: List[str] = []
        seen: set[str] = set()
        for s, e, _ch, sku in spans:
            if sku and s < end and e > start and sku not in seen:
                seen.add(sku)
                skus.append(sku)
        return skus

    # ==================== 解析 ====================

    async def _parse(self, path: str, ext: str) -> List[Segment]:
        """按类型解析为 Segment 列表;失败抛异常(由 process_file 分类)。"""
        if ext == "pdf":
            md = await parse_pdf(path)          # MinerU 输出 markdown
            return parse_md_string(md)          # 复用 md 标题章节逻辑
        if ext == "docx":
            return parse_docx(path)
        return parse_text_file(path, ext)

    # ==================== 核心流程 ====================

    async def process_file(
        self,
        file_info: Dict[str, Any],
        on_progress: Optional[ProgressFn] = None,
    ) -> Dict[str, Any]:
        """单个文件全链路:失败路径零 DB 写入。

        on_progress: 可选进度回调(阶段标签, 百分比, 细分说明) -> 协程。
        默认 None 时不发任何事件,既有调用方(/api/upload 与客户端上传)
        执行路径逐行不变。发射一律走 _emit,不在别处直接调 on_progress。
        """
        _pct = 0

        async def _emit(key: str, detail: str = "") -> None:
            nonlocal _pct
            if on_progress is None:
                return
            _pct += _WEIGHTS[key]
            # 上限 99:100 只由 commit 的 done 事件给出,保证"最后一个 progress < 100"
            await on_progress(_LABELS[key], min(_pct, 99), detail)

        path = file_info["path"]
        original_name = file_info.get("original_name", os.path.basename(path))
        user_id = str(file_info.get("user_id", 0))
        ext = os.path.splitext(original_name)[1].lstrip(".").lower()

        # 1. 校验(扩展名/大小/空)
        if ext not in settings.allowed_extensions:
            return self._fail("unsupported", f"不支持的文件格式: .{ext}")
        try:
            size = os.path.getsize(path)
        except OSError as e:
            return self._fail("parse_error", f"文件不可读: {e}")
        if size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
            return self._fail("too_large", f"文件大小超过限制(最大 {settings.MAX_FILE_SIZE_MB}MB)")
        if size == 0:
            return self._fail("empty_file", "文件为空")
        await _emit("validate")

        # 2. MD5 指纹
        with open(path, "rb") as f:
            md5_hex = hashlib.md5(f.read()).hexdigest()

        # 3. 查重(快速路径;并发由唯一约束兜底)
        async with AsyncSessionLocal() as s:
            dup = await s.execute(
                select(Document.id).where(
                    Document.user_id == user_id, Document.md5 == md5_hex
                )
            )
            if dup.scalar() is not None:
                return {"status": "duplicate", "md5": md5_hex,
                        "original_filename": original_name, "user_id": user_id}

        # 4. 解析
        await _emit("parse", "MinerU 云端解析中…" if ext == "pdf" else "")
        try:
            segments = await self._parse(path, ext)
        except MinerUError as e:
            return self._fail(e.category, e.detail)
        except Exception as e:
            logger.exception("解析失败: {}", e)
            return self._fail("parse_error", str(e))
        if not segments:
            return self._fail("empty_file", "解析后为空")

        # 5. 清洗 + 6. 分块(全文统一递归切分,2026-08-23):
        #    段间 \n\n 连接成全文 → split_documents 一次切分 → 块归属=块内首个非空
        #    字符所在段的章节(段字符轴定位,见 spec_plan/SPEC_CHUNK_MERGE_STRATEGY.md §3)
        await _emit("clean")
        clean_segments: List[tuple[str, str, str]] = []   # (text, chapter, sku)
        for seg in segments:
            text = clean_text(seg.text) if settings.TEXT_CLEAN_ENABLED else seg.text.strip()
            if text:
                clean_segments.append((text, seg.chapter, seg.sku))
        if not clean_segments:
            return self._fail("empty_file", "清洗后无内容")

        # 段字符轴:(start, end, chapter, sku);每段后 +2 补偿 \n\n 连接符,与 join 严格同步
        spans: List[tuple[int, int, str, str]] = []
        cap = 0
        for text, chapter, sku in clean_segments:
            spans.append((cap, cap + len(text), chapter, sku))
            cap += len(text) + 2
        full_text = "\n\n".join(t for t, _, _ in clean_segments)

        await _emit("split")
        docs = self.text_splitter.split_documents([LangchainDocument(page_content=full_text)])
        chunks: List[str] = []
        chapters: List[str] = []
        chunk_skus: List[List[str] | None] = []
        for d in docs:
            content = d.page_content
            if len(content.strip()) < settings.CHUNK_MIN_SIZE:
                continue
            chunks.append(content)
            start = d.metadata["start_index"]
            # chapter 单值(块首归属)与 sku_codes 多值(区间收集)并存,语义不同
            chapters.append(self._locate_chapter(spans, start))
            skus = self._collect_skus(spans, start, start + len(content))
            chunk_skus.append(skus or None)   # 空列表统一为 NULL(JSONB 存储面一致)
        if not chunks:
            return self._fail("empty_file", "清洗分块后无内容")

        # 多商品块占比统计(SPEC_RAG_SKU_METADATA 归档前置验证项落地)
        multi = sum(1 for s in chunk_skus if s and len(s) > 1)
        logger.info(
            "sku 注入完成: 多商品块 {} / {} ({:.1%}),含商品块 {} / {}",
            multi, len(chunks), multi / len(chunks),
            sum(1 for s in chunk_skus if s), len(chunks),
        )

        # 7. 嵌入(退避重试已内置于 embed_in_batches;全部成功才进事务)
        # 批次进度映射进"生成向量"那 35% 的区间(base 取 _emit("split") 之后的累计值)。
        # on_progress 为 None 时走不带 on_batch 的原调用形态:既保持客户端路径逐行不变,
        # 也让既有测试的 monkeypatch 替身(只接受 texts 一个参数)零改动继续可用。
        if on_progress is None:
            embeddings = await embed_in_batches(chunks)
        else:
            base = _pct

            async def _on_batch(done: int, total: int) -> None:
                await on_progress(
                    _LABELS["embed"],
                    min(base + int(_WEIGHTS["embed"] * done / total), 99),
                    f"嵌入中 {done}/{total} 批",
                )

            embeddings = await embed_in_batches(chunks, on_batch=_on_batch)
            # ⚠️ 批次发射走的是 base+offset,不推进 _pct 游标;这里必须把嵌入阶段的权重补上。
            # 漏了这一步,后面的 _emit("store") 会从 60 加到 65 —— 低于批次发射的 77/95,
            # 进度条会【倒退】(实测踩过)。游标与发射值必须同源。
            _pct = base + _WEIGHTS["embed"]
        if not embeddings or len(embeddings) != len(chunks) or any(
            np.count_nonzero(v) == 0 for v in embeddings
        ):
            return self._fail("embedding_failed", "Embedding 生成失败(重试后仍全零)")

        # 8. 单事务原子写入(documents + chunks)
        try:
            async with AsyncSessionLocal() as s:
                doc = Document(
                    md5=md5_hex, original_filename=original_name, user_id=user_id,
                    file_type=ext, file_size=size, chunk_count=len(chunks),
                )
                s.add(doc)
                await s.flush()
                rows = [
                    DocumentChunk(
                        chunk_id=f"{user_id}_{md5_hex}_{i:04d}",
                        md5=md5_hex, file_type=ext,
                        source=original_name, file_path=path, user_id=user_id,
                        chunk_index=i, content=c, embedding=e, chapter=ch, sku_codes=sk,
                    )
                    for i, (c, e, ch, sk) in enumerate(zip(chunks, embeddings, chapters, chunk_skus))
                ]
                s.add_all(rows)
                await s.commit()
        except IntegrityError:
            # 并发上传同文件:唯一约束冲突 → 回滚 → duplicate
            return {"status": "duplicate", "md5": md5_hex,
                    "original_filename": original_name, "user_id": user_id}

        await _emit("store", f"{len(chunks)} 个片段")
        logger.info("入库完成: {} 个文本块(文件 {})", len(chunks), original_name)
        return {"status": "success", "md5": md5_hex, "chunks": len(chunks),
                "original_filename": original_name, "user_id": user_id}

    @staticmethod
    def _fail(error: str, detail: str) -> Dict[str, Any]:
        return {"status": "failed", "error": error, "detail": detail}

    async def process_directory(self, directory_path: str, user_id: int = 0) -> Dict[str, Any]:
        """批量处理目录(供 ingest 脚本与调试使用)。"""
        results = []
        for root, _, files in os.walk(directory_path):
            for file in sorted(files):
                file_path = os.path.join(root, file)
                results.append(await self.process_file(
                    {"path": file_path, "original_name": file, "user_id": user_id}
                ))
        return {"status": "success", "processed_files": len(results), "results": results}
