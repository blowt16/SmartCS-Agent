from typing import List, Dict, AsyncGenerator, Callable, Optional
from openai import AsyncOpenAI
from app.core.config import settings
import json
from app.core.logger import get_logger
from app.core.database import AsyncSessionLocal
from app.models.conversation import Conversation, DialogueType
from app.models.message import Message
from app.services.redis_semantic_cache import RedisSemanticCache
import time
import asyncio

logger = get_logger(service="deepseek")

class DeepseekService:
    def __init__(self, model: Optional[str] = None):
        logger.info("Initializing Deepseek Service")
        self.client = AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL
        )
        # model 显式传入（如 RESOLVE_MODEL 消解降档）时优先；否则用 DEEPSEEK_MODEL
        self.model = model or settings.DEEPSEEK_MODEL
        self.cache = RedisSemanticCache.get_instance(prefix=settings.REDIS_CACHE_PREFIX)

    async def _stream_cached_response(self, response: str, delay: float = None) -> AsyncGenerator[str, None]:
        if delay is None:
            delay = settings.STREAM_DELAY
        """模拟流式返回缓存的响应"""
        # 每次返回4个字符
        chunks = [response[i:i + 4] for i in range(0, len(response), 4)]
        for chunk in chunks:
            await asyncio.sleep(delay)  # 50ms延迟
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    async def generate_stream(
        self, 
        messages: List[Dict],
        user_id: Optional[int] = None,
        conversation_id: Optional[int] = None,
        on_complete: Optional[Callable[[int, int, List[Dict], str], None]] = None
    ) -> AsyncGenerator[str, None]:
        """流式生成回复"""
        try:
            # 为每个用户获取独立的缓存实例（池化复用，避免重建连接与清理任务）
            cache = RedisSemanticCache.get_instance(prefix=settings.REDIS_CACHE_PREFIX, user_id=user_id)
            
            start_time = time.time()
            
            # 检查缓存（resolve_llm=self：注入 LLM 供指代消解使用）
            cached_response = await cache.lookup(messages, resolve_llm=self)
            if cached_response:
                response_time = time.time() - start_time
                logger.info("Cache hit! Response time: {:.4f} seconds", response_time)
                
                # 模拟流式返回，因为速率太快了
                async for chunk in self._stream_cached_response(cached_response):
                    yield chunk
                
                if on_complete and user_id is not None and conversation_id is not None:
                    await on_complete(user_id, conversation_id, messages, cached_response)
                return

            # 缓存未命中,调用API
            full_response = []
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True
            )

            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    # 收集原始文本（缓存/落库用完整原文）；SSE 传输时再 JSON 编码
                    content = chunk.choices[0].delta.content
                    full_response.append(content)
                    yield f"data: {json.dumps(content, ensure_ascii=False)}\n\n"
            
            # 完整响应
            complete_response = "".join(full_response)
            
            # 更新缓存
            await cache.update(messages, complete_response, resolve_llm=self)
            
            response_time = time.time() - start_time
            logger.info("Cache miss. Response time: {:.4f} seconds", response_time)
            
            # 如果有回调，执行回调
            if on_complete and user_id is not None and conversation_id is not None:
                await on_complete(user_id, conversation_id, messages, complete_response)
                
        except Exception as e:
            logger.exception("Error in generate_stream: {}", str(e))
            error_msg = json.dumps(f"生成回复时出错: {str(e)}", ensure_ascii=False)
            yield f"data: {error_msg}\n\n"

    async def generate(self, messages: List[Dict], temperature: float = None, max_tokens: int = None) -> str:
        """非流式生成回复

        Args:
            messages: OpenAI 格式消息列表
            temperature: 采样温度（None 时用 API 默认值）
            max_tokens: 最大输出 token 数（None 时用 API 默认值）
        """
        try:
            kwargs = {}
            if temperature is not None:
                kwargs["temperature"] = temperature
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False,
                **kwargs
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error("Generation error: {}", str(e))
            raise 