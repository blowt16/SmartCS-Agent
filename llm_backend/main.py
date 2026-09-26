from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
from app.services.llm_factory import LLMFactory
from fastapi.staticfiles import StaticFiles
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from app.core.logger import get_logger, log_structured, log_round_start, log_round_end
from app.core.middleware import LoggingMiddleware
from app.core.config import settings
from app.api import api_router
from app.models.conversation import Conversation, DialogueType
from app.models.message import Message
from sqlalchemy import select
from app.services.conversation_service import ConversationService
import os
import sys
from app.lg_agent.lg_states import AgentState, InputState
from app.lg_agent.utils import new_uuid
from app.lg_agent.lg_builder import graph, init_checkpointer, close_checkpointer
from app.lg_agent.stream_filter import StreamChunkFilter
from app.services.pronoun_detector import _is_filler  # 语气词闸门临时借用（SPEC_ENTRY_LLM_RESOLUTION 落地后 FILLER 迁入 redis_semantic_cache，届时改 import）
from app.services.pronoun_resolver import resolve_pronouns_ex
from app.services.redis_semantic_cache import RedisSemanticCache
from langchain_core.messages import HumanMessage, AIMessage
import json
import asyncio
import time


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


async def _stream_cached(response: str, delay: float = None, t0: float = None):
    """模拟流式返回缓存的响应（与 DeepseekService 行为一致，保持前端体验）

    t0 由调用方传入本轮计时起点时，流结束后补一条轮次耗时日志（缓存路径无图中链路日志，
    不补的话这一轮在日志里没有结束边界）。
    """
    if delay is None:
        delay = settings.STREAM_DELAY
    # 每次返回4个字符
    chunks = [response[i:i + 4] for i in range(0, len(response), 4)]
    try:
        for chunk in chunks:
            await asyncio.sleep(delay)
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    finally:
        if t0 is not None:
            log_round_end(t0, "缓存命中（短路）")

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
        # 轮次计时起点：取在耗时动作（消解/缓存/图执行）之前，故耗时含全链路
        round_t0 = log_round_start(query, user=user_id, conv=conversation_id or "new")

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

        # 提取线程内历史消息（langchain 消息 → dict 列表，供消解与缓存使用）
        history_messages = []
        if state_history:
            for m in state_history.values.get("messages", []):
                if isinstance(m, (HumanMessage, AIMessage)):
                    history_messages.append({
                        "role": "assistant" if isinstance(m, AIMessage) else "user",
                        "content": m.content,
                    })

        # ===== 入口统一消解（多轮 → LLM 一次完成指代消除 + 语义补全）=====
        # SPEC_ENTRY_LLM_RESOLUTION §3.1（main 入口部分，2026-09-02 落地）：
        # 正则判定（代词表/省略触发词）不再作为消解前置——多轮消息统一交 LLM，
        # prompt 内自包含出口保证完整问题原样返回；保留两个免费闸门：
        # 纯语气词（不调 LLM）与首条消息（无历史可补，直通）。
        # 注：缓存入口 redis_semantic_cache._resolve_message 仍为旧两段式正则，
        # 遗留问题与后续方案见 docs/项目问题.md #11
        resolved_query = query
        ref_candidates: List[str] = []
        if (
            settings.RESOLVE_ENABLED
            and not (settings.RESOLVE_SKIP_FILLER and _is_filler(query))
            and history_messages
        ):
            # _ex 版本额外返回"多候选指代"信息：用户用了指代但上文有多款同等候选时，
            # 不擅自选定，交由图内澄清节点反问用户（SPEC_MULTI_CANDIDATE_REFERENCE）
            _resolve = await resolve_pronouns_ex(
                _get_resolve_llm(),
                history_messages + [{"role": "user", "content": query}],
                query,
            )
            resolved_query = _resolve.query
            ref_candidates = _resolve.candidates

        # ===== 语义缓存检索（消解后、进图前；命中短路，跳过整个图流程）=====
        # 缓存内容由 graphrag/chat 链路完整回答后写入（ScopeGuard 把关的范围内回答），
        # 且 key 基于消解后消息——入口查缓存天然只命中经营范围内的完整问题
        cache = RedisSemanticCache.get_instance(prefix=settings.REDIS_CACHE_PREFIX, user_id=user_id)
        cached_response = await cache.lookup(
            history_messages + [{"role": "user", "content": resolved_query}],
            resolve_llm=_get_resolve_llm(),
        )
        if cached_response:
            logger.info("语义缓存命中，短路返回: '{}'", resolved_query)
            # 缓存路径同样把耗时打在流末尾（前端仍是等分片放完才看到完整回答）
            response = StreamingResponse(
                _stream_cached(cached_response, t0=round_t0),
                media_type="text/event-stream"
            )
            response.headers["X-Conversation-ID"] = thread_id
            return response

        # 新会话或正常多轮对话，始终用 InputState 输入
        # LangGraph 通过 thread_id 自动维护上下文状态
        logger.info("Processing with InputState" + (" (continuing thread {})" if state_history else " (new thread)"), thread_id)
        input_state = InputState(messages=resolved_query, ref_candidates=ref_candidates)

        async def process_stream():
            # 收集完整回答，图结束后回写语义缓存
            complete_response = []
            graph_done = False          # 图是否跑完（区分"正常结束"与"客户端提前断开"）
            # 出口闸门：内部推理（router/planner）不外泄 + 售前容器节点不重复，
            # 见 app/lg_agent/stream_filter.py（旧 tag 黑名单误挡售前 summarize）
            chunk_filter = StreamChunkFilter()
            try:
                async for c, metadata in graph.astream(
                    input=input_state,
                    stream_mode="messages",
                    config=thread_config
                ):
                    text = chunk_filter.select(c, metadata)
                    if text is None:
                        if c.additional_kwargs.get("tool_calls"):
                            tool_data = c.additional_kwargs.get("tool_calls")[0]["function"].get("arguments")
                            logger.debug("Tool call: {}", tool_data)
                        continue
                    complete_response.append(text)
                    yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"
                graph_done = True
            finally:
                # 轮次结束标记：必须放在生成器内（StreamingResponse 在流开始时即返回，
                # 路由外层计不到真实耗时），且用 finally 保证客户端中途断开也能落日志
                log_round_end(
                    round_t0,
                    f"完成（{len(complete_response)} 分片 / {sum(len(t) for t in complete_response)} 字）"
                    if graph_done else
                    f"中断（客户端断开，已流出 {len(complete_response)} 分片）",
                )
            # 图完整结束后回写（非空才写，避免空响应/失败响应污染缓存；
            # 纯语气词由缓存内部 _resolve_message 判定跳过，不在此门控）
            if complete_response:
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
