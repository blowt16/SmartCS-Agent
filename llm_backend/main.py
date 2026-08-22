from fastapi import FastAPI, HTTPException, UploadFile, File, Query, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
from app.services.llm_factory import LLMFactory
from fastapi.staticfiles import StaticFiles
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from app.core.logger import get_logger, log_structured
from app.core.middleware import LoggingMiddleware
from app.core.config import settings
from app.api import api_router
from app.core.database import AsyncSessionLocal
from app.models.conversation import Conversation, DialogueType
from app.models.message import Message
from sqlalchemy import select
from app.services.conversation_service import ConversationService
import uuid
import os
from app.services.indexing_service import IndexingService
import sys
from app.lg_agent.lg_states import AgentState, InputState
from app.lg_agent.utils import new_uuid
from app.lg_agent.lg_builder import graph, init_checkpointer, close_checkpointer
from app.services.pronoun_detector import detect_pronoun, DetectionDecision
from app.services.pronoun_resolver import resolve_pronouns
from app.services.redis_semantic_cache import RedisSemanticCache
from langchain_core.messages import HumanMessage, AIMessage
import json
import asyncio


# 配置上传目录 - RAG 功能的
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# logger 变量就被初始化为一个日志记录器实例。
# 之后，便可以在当前文件中直接使用 logger.info()、logger.error() 等方法来记录日志，而不需要进行其他操作。
logger = get_logger(service="main")

# 入口指代消解用的 LLM 服务（懒加载复用，避免每请求重建客户端）
_resolve_llm_service = None


def _get_resolve_llm():
    """获取指代消解的 LLM 服务（DeepseekService/OllamaService，具备 generate 鸭子类型）"""
    global _resolve_llm_service
    if _resolve_llm_service is None:
        _resolve_llm_service = LLMFactory.create_chat_service()
    return _resolve_llm_service


async def _stream_cached(response: str, delay: float = None):
    """模拟流式返回缓存的响应（与 DeepseekService 行为一致，保持前端体验）"""
    if delay is None:
        delay = settings.STREAM_DELAY
    # 每次返回4个字符
    chunks = [response[i:i + 4] for i in range(0, len(response), 4)]
    for chunk in chunks:
        await asyncio.sleep(delay)
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

# 启动时初始化 LangGraph Postgres 检查点（连接池 + 检查点表 + 编译 graph）
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_checkpointer()
    yield
    await close_checkpointer()


# 创建 FastAPI 应用实例
app = FastAPI(title="SmartCS-Agent REST API", lifespan=lifespan)

# 添加日志中间件， 使用 LoggingMiddleware 来统一处理日志记录，从而替代 FastAPI 的原生打印日志。
app.add_middleware(LoggingMiddleware)

# CORS设置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 在生产环境中要设置具体的域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. 用户注册、登录路由通过 api_router 路由挂载到 /api 前缀
app.include_router(api_router, prefix="/api")

class RAGChatRequest(BaseModel):
    messages: List[Dict[str, str]]
    index_id: str
    user_id: int

class CreateConversationRequest(BaseModel):
    user_id: int

class UpdateConversationNameRequest(BaseModel):
    name: str

class LangGraphRequest(BaseModel):
    query: str
    user_id: int
    conversation_id: Optional[str] = None
    image: Optional[UploadFile] = None


@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    user_id: str = Form(...)
):
    """上传文件并准备 RAG 处理"""
    try:
        log_structured("file_upload", {
            "user_id": user_id,
            "filename": file.filename,
            "content_type": file.content_type,
        })

        # 1. 创建基于UUID的一级目录
        user_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"user_{user_id}"))
        first_level_dir = UPLOAD_DIR / user_uuid
        
        # 2. 创建基于时间戳的二级目录
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        second_level_dir = first_level_dir / timestamp
        second_level_dir.mkdir(parents=True, exist_ok=True)
        
        # 3. 生成带时间戳的文件名
        original_name, ext = os.path.splitext(file.filename)
        new_filename = f"{original_name}_{timestamp}{ext}"
        file_path = second_level_dir / new_filename
        
        # 保存文件
        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)
            
        # 获取文件信息
        file_info = {
            "filename": new_filename,
            "original_name": file.filename,
            "size": len(content),
            "type": file.content_type,
            "path": str(file_path).replace('\\', '/'),
            "user_id": user_id,
            "user_uuid": user_uuid,
            "upload_time": timestamp,
            "directory": str(second_level_dir)
        }

        # 4. 处理文件索引
        indexing_service = IndexingService()
        index_result = await indexing_service.process_file(file_info)

        # 契约:处理类错误 200+status;校验类错误 400(见 spec §3 API 响应契约)
        if index_result.get("status") == "failed" and index_result.get("error") in (
            "unsupported", "too_large", "empty_file",
        ):
            raise HTTPException(status_code=400, detail=index_result.get("detail", index_result.get("error")))

        # 合并结果
        result = {**file_info, "index_result": index_result}

        return result

    except HTTPException:
        raise  # 内层已明确的 HTTP 契约错误(如校验类 400),不套 500
    except Exception as e:
        logger.exception("Upload failed for user {}: {}", user_id, str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/documents")
async def list_documents(user_id: str = Query(...)):
    """文件级文档列表(按 user_id 过滤,时间倒序)。"""
    from sqlalchemy import select, desc
    from app.models.document import Document

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Document).where(Document.user_id == user_id).order_by(desc(Document.created_at))
        )).scalars().all()
    return {
        "user_id": user_id,
        "total": len(rows),
        "documents": [
            {
                "md5": d.md5, "original_filename": d.original_filename,
                "file_type": d.file_type, "file_size": d.file_size,
                "page_count": d.page_count, "chunk_count": d.chunk_count,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in rows
        ],
    }


@app.delete("/api/documents/{md5}")
async def delete_document(md5: str, user_id: str = Query(...)):
    """按文件删除:chunks 与记录同事务删除;md5 不存在返回 404。"""
    from sqlalchemy import delete
    from app.models.document import Document
    from app.models.document_chunk import DocumentChunk

    async with AsyncSessionLocal() as session:
        doc = (await session.execute(
            select(Document).where(Document.user_id == user_id, Document.md5 == md5)
        )).scalar_one_or_none()
        if doc is None:
            raise HTTPException(status_code=404, detail=f"文档不存在: {md5}")
        await session.execute(delete(DocumentChunk).where(
            DocumentChunk.user_id == user_id, DocumentChunk.md5 == md5
        ))
        await session.delete(doc)
        await session.commit()
    return {"md5": md5, "deleted": True}


@app.post("/chat-rag")
async def rag_chat_endpoint(request: RAGChatRequest):
    """基于文档的问答接口"""
    try:
        logger.info("Processing RAG chat request for user {}", request.user_id)
        rag_chat_service = RAGChatService()
        
        return StreamingResponse(
            rag_chat_service.generate_stream(
                request.messages,
                request.index_id
            ),
            media_type="text/event-stream"
        )
    except Exception as e:
        logger.exception("RAG chat error for user {}: {}", request.user_id, str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/conversations")
async def create_conversation(request: CreateConversationRequest):
    """创建新会话"""
    try:
        conversation_id = await ConversationService.create_conversation(request.user_id)
        return {"conversation_id": conversation_id}
    except Exception as e:
        logger.exception("Error creating conversation: {}", str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/conversations/user/{user_id}")
async def get_user_conversations(user_id: int):
    """获取用户的所有会话"""
    try:
        conversations = await ConversationService.get_user_conversations(user_id)
        return conversations
    except Exception as e:
        logger.exception("Error getting conversations: {}", str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: int, user_id: int):
    """获取会话的所有消息"""
    try:
        messages = await ConversationService.get_conversation_messages(conversation_id, user_id)
        return messages
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Error getting messages: {}", str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int):
    """删除会话及其所有消息"""
    try:
        conversation_service = ConversationService()
        await conversation_service.delete_conversation(conversation_id)
        return {"message": "会话已删除"}
    except Exception as e:
        logger.exception("删除会话失败: {}", str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/conversations/{conversation_id}/name")
async def update_conversation_name(
    conversation_id: int,
    request: UpdateConversationNameRequest
):
    """修改会话名称"""
    try:
        conversation_service = ConversationService()
        await conversation_service.update_conversation_name(conversation_id, request.name)
        return {"message": "会话名称已更新"}
    except Exception as e:
        logger.exception("更新会话名称失败: {}", str(e))
        raise HTTPException(status_code=500, detail=str(e))

class SaveMessagesRequest(BaseModel):
    conversation_id: int
    user_message: str
    assistant_message: str

@app.post("/api/conversations/save-messages")
async def save_messages(request: SaveMessagesRequest):
    """保存一轮对话消息（用户消息 + AI回复）"""
    try:
        await ConversationService.save_message(
            user_id=0,
            conversation_id=request.conversation_id,
            messages=[{"role": "user", "content": request.user_message}],
            response=request.assistant_message
        )
        return {"message": "消息已保存"}
    except Exception as e:
        logger.exception("保存消息失败: {}", str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/langgraph/query")
async def langgraph_query(
    query: str = Form(...),
    user_id: int = Form(...),
    conversation_id: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None)
):
    """使用LangGraph处理用户查询，支持图片上传"""
    try:
        log_structured("langgraph_query", {
            "user_id": user_id,
            "conversation_id": conversation_id or "new",
            "query": query[:200],
            "has_image": image is not None,
        })

        # 处理图片上传
        image_path = None
        if image:
            # 创建图片存储目录
            image_dir = Path("uploads/images")
            image_dir.mkdir(parents=True, exist_ok=True)
            
            # 生成带时间戳的文件名
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            original_name, ext = os.path.splitext(image.filename)
            new_filename = f"{original_name}_{timestamp}{ext}"
            image_path = image_dir / new_filename
            
            # 保存图片
            content = await image.read()
            with open(image_path, "wb") as f:
                f.write(content)
            
            logger.info("Saved image {} for user {}", new_filename, user_id)
        
        # 使用conversation_id作为thread_id，如果没有提供则创建新的
        thread_id = conversation_id if conversation_id else new_uuid()
        thread_config = {
            "configurable": {
                "thread_id": thread_id, 
                "user_id": user_id,
                "image_path": str(image_path) if image_path else None
            }
        }
        
        # 获取当前线程状态（用于判断是延续线程还是新线程）
        state_history = None
        try:
            if thread_id:
                state_history = await graph.aget_state(thread_config)
                if state_history:
                    logger.info("Found existing conversation state for thread_id: {}", thread_id)
        except Exception as e:
            logger.warning("Error retrieving state: {}. Starting with fresh state.", e)

        # ===== 入口前置指代消解（多轮代词/省略统一在此补全）=====
        # 含指代消息（"那个有货吗"）在进入意图模块前改写为完整问题，
        # 因此图内不再重复消解；首条消息（无历史）无上下文依赖，直接透传
        resolved_query = query
        history_messages = []
        if state_history:
            for m in state_history.values.get("messages", []):
                if isinstance(m, (HumanMessage, AIMessage)):
                    history_messages.append({
                        "role": "assistant" if isinstance(m, AIMessage) else "user",
                        "content": m.content,
                    })
        decision = detect_pronoun(query, skip_filler=settings.RESOLVE_SKIP_FILLER)
        if history_messages and decision == DetectionDecision.NEED_RESOLVE:
            resolved_query = await resolve_pronouns(
                _get_resolve_llm(),
                history_messages + [{"role": "user", "content": query}],
                query,
            )
            logger.info("入口指代消解: '{}' → '{}'", query, resolved_query)
            # 消解失败降级时仍为 NEED_RESOLVE（无完整问题可作缓存 key）
            decision = detect_pronoun(resolved_query)

        # ===== 语义缓存检索（消解后、进图前；命中短路，跳过整个图流程）=====
        # 缓存内容由 graphrag/chat 链路完整回答后写入（ScopeGuard 把关的范围内回答），
        # 且 key 基于消解后消息——入口查缓存天然只命中经营范围内的完整问题
        cache = RedisSemanticCache.get_instance(prefix=settings.REDIS_CACHE_PREFIX, user_id=user_id)
        cached_response = None
        if decision == DetectionDecision.NEED_RESOLVE:
            logger.info("含指代且无历史可消解，跳过缓存检索")
        else:
            cached_response = await cache.lookup(
                history_messages + [{"role": "user", "content": resolved_query}],
                resolve_llm=_get_resolve_llm(),
            )
        if cached_response:
            logger.info("语义缓存命中，短路返回: '{}'", resolved_query)
            response = StreamingResponse(
                _stream_cached(cached_response),
                media_type="text/event-stream"
            )
            response.headers["X-Conversation-ID"] = thread_id
            return response

        # 新会话或正常多轮对话，始终用 InputState 输入
        # LangGraph 通过 thread_id 自动维护上下文状态
        logger.info("Processing with InputState" + (" (continuing thread {})" if state_history else " (new thread)"), thread_id)
        input_state = InputState(messages=resolved_query)

        async def process_stream():
            # 收集完整回答，图结束后回写语义缓存
            complete_response = []
            async for c, metadata in graph.astream(
                input=input_state,
                stream_mode="messages",
                config=thread_config
            ):
                if c.content and "research_plan" not in metadata.get("tags", []) and not c.additional_kwargs.get("tool_calls"):
                    complete_response.append(c.content)
                    content_json = json.dumps(c.content, ensure_ascii=False)
                    yield f"data: {content_json}\n\n"
                elif c.additional_kwargs.get("tool_calls"):
                    tool_data = c.additional_kwargs.get("tool_calls")[0]["function"].get("arguments")
                    logger.debug("Tool call: {}", tool_data)
            # 图完整结束后回写（非空才写，避免空响应/失败响应污染缓存）
            if decision != DetectionDecision.NEED_RESOLVE and complete_response:
                updated = await cache.update(
                    history_messages + [{"role": "user", "content": resolved_query}],
                    "".join(complete_response),
                    resolve_llm=_get_resolve_llm(),
                )
                if updated:
                    logger.info("语义缓存已回写: '{}'", resolved_query)

        response = StreamingResponse(
            process_stream(),
            media_type="text/event-stream"
        )

        # 添加会话ID到响应头，方便前端获取
        response.headers["X-Conversation-ID"] = thread_id

        return response
        
    except Exception as e:
        logger.exception("LangGraph query error: {}", str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/upload/image")
async def upload_image(
    image: UploadFile = File(...),
    user_id: int = Form(...),
    conversation_id: Optional[str] = Form(None)
):
    """上传图片并返回图片存储路径"""
    try:
        # 创建图片存储目录
        image_dir = Path("uploads/images")
        if conversation_id:
            image_dir = image_dir / conversation_id
        image_dir.mkdir(parents=True, exist_ok=True)
        
        # 生成带时间戳的文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        original_name, ext = os.path.splitext(image.filename)
        new_filename = f"{original_name}_{timestamp}{ext}"
        image_path = image_dir / new_filename
        
        # 保存图片
        content = await image.read()
        with open(image_path, "wb") as f:
            f.write(content)
        
        # 获取图片信息
        image_info = {
            "filename": new_filename,
            "original_name": image.filename,
            "size": len(content),
            "type": image.content_type,
            "path": str(image_path).replace('\\', '/'),
            "user_id": user_id,
            "conversation_id": conversation_id,
            "upload_time": timestamp
        }
        
        logger.info("Image uploaded: {}", image_info)
        
        return image_info
        
    except Exception as e:
        logger.exception("Image upload failed for user {}: {}", user_id, str(e))
        raise HTTPException(status_code=500, detail=str(e))

# 最后挂载静态文件（前端 Vue3 工程构建产物位于项目根目录 frontend/dist，主入口 http://127.0.0.1:8000）
STATIC_DIR = Path(__file__).parent.parent / "frontend" / "dist"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
