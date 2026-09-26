from app.lg_agent.lg_states import AgentState, Router
from app.lg_agent.lg_prompts import (
    ROUTER_SYSTEM_PROMPT,
    GENERAL_QUERY_SYSTEM_PROMPT,
    GET_IMAGE_SYSTEM_PROMPT,
    RISK_INTERCEPT_REPLY,
    TRANSFER_HUMAN_REPLY,
    AFTERSALE_PLACEHOLDER_REPLY,
    COMPLAINT_PLACEHOLDER_REPLY,
    CLARIFY_SYSTEM_PROMPT,
    CLARIFY_FALLBACK_REPLY,
)
from langchain_core.runnables import RunnableConfig
from langchain_deepseek import ChatDeepSeek
from langchain_ollama import ChatOllama
from app.core.config import settings, ServiceType
from app.core.logger import get_logger
from typing import cast, Literal, TypedDict, List, Dict, Any
from langchain_core.messages import BaseMessage
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from app.lg_agent.lg_states import AgentState, InputState, Router
from app.lg_agent.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.multi_tool import create_multi_tool_workflow
from typing import Dict, List
from langchain_core.messages import AIMessage
from langchain_core.runnables.base import Runnable
import base64
import math
import os
import aiohttp
import json
import threading
import time
from pathlib import Path

from typing import Literal

from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.memory import MemoryManager
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.agent_safety import (
    ScopeGuard, TimeoutGuard,
)
from app.lg_agent.intent_rules import classify_by_rules


# 构建日志记录器
logger = get_logger(service="lg_builder")

# 售后二级场景合法取值（与 Router.sub_type 的 Literal 保持一致）
_AFTERSALE_SUB_TYPES = frozenset({
    "logistics_query", "return_refund", "exchange", "reship", "order_query", "other",
})


def _normalize_sub_type(router_type: str, sub_type: object) -> str:
    """校正 sub_type 与 type 的跨字段一致性。

    schema 的 Literal 只能约束取值集合，约束不了跨字段搭配——模型可能给出
    type=presale + sub_type=return_refund 这类组合，需在此收敛：
        type≠aftersale → 必须 none；type=aftersale → 不可为 none（兜底 other）。
    """
    if router_type != "aftersale":
        return "none"
    return sub_type if sub_type in _AFTERSALE_SUB_TYPES else "other"


def _normalize_confidence(raw: object) -> float:
    """校正 confidence 为 [0,1] 浮点，非法一律 0.0。

    confidence 只记录、不参与路由（理由见 Router.confidence 注释）。但必须挡掉
    NaN——NaN 参与任何阈值比较恒为 False，将来启用阈值时会静默绕过判定。
    字符串不静默转 float：避免"看起来有值"的假数据污染分布统计。
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    value = float(raw)
    if math.isnan(value) or not 0.0 <= value <= 1.0:
        return 0.0
    return value


async def analyze_and_route_query(
    state: AgentState, *, config: RunnableConfig
) -> dict[str, Router]:
    """Analyze the user's query and determine the appropriate routing.

    两级判定：① 意图规则层（零延迟关键词，命中即短路，不调模型）；
    ② LLM 识别层（结构化输出，规则层未命中时降级至此）。

    Args:
        state (AgentState): The current state of the agent, including conversation history.
        config (RunnableConfig): Configuration with the model used for query analysis.

    Returns:
        dict[str, Router]: 含 'router' 键，值为分类结果（type / sub_type / risk / logic / source）。
    """
    # ③ 经营范围预检（关键词级，零延迟）
    user_question = state.messages[-1].content if state.messages else ""
    scope_guard = ScopeGuard()
    in_scope, scope_reason = scope_guard.check(user_question)
    if not in_scope:
        logger.warning("经营范围预检拦截: {}", scope_reason)
        return {"router": Router(type="general", sub_type="none", risk="none",
                                 logic=f"超出经营范围: {scope_reason}", source="rule",
                                 confidence=1.0)}   # 关键词预检，确定性命中

    # ④ 意图规则层（零延迟）：明确意图直接短路，未命中降级 LLM
    # 设计见 SPEC_INTENT_RULE_LAYER.md —— 规则层不判 risk（风险信号词命中即让行），
    # 故短路结果 risk 恒为 none，不影响违规/高风险拦截。
    rule_hit = classify_by_rules(user_question)
    if rule_hit:
        r_type, r_sub_type, r_reason = rule_hit
        logger.info("意图规则层命中: type={} sub_type={} | {} | query: '{}'",
                    r_type, r_sub_type, r_reason, user_question)
        return {"router": Router(type=r_type, sub_type=r_sub_type, risk="none",
                                 logic=f"规则层判定：{r_reason}", source="rule",
                                 confidence=1.0)}   # 关键词匹配，确定性命中

    # 选择模型实例，通过.env文件中的AGENT_SERVICE参数选择
    # 意图识别/路由为分类决策任务，低温（ROUTER_TEMPERATURE=0）保证同输入同输出
    if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
        model = ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL, temperature=settings.ROUTER_TEMPERATURE, tags=["router"], extra_body={"thinking": {"type": "disabled"}})
        logger.info("Using DeepSeek model: {}", settings.DEEPSEEK_MODEL)
    else:
        model = ChatOllama(model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL, temperature=settings.ROUTER_TEMPERATURE, tags=["router"], extra_body={"thinking": {"type": "disabled"}})
        logger.info("Using Ollama model: {}", settings.OLLAMA_AGENT_MODEL)

    # 拼接提示模版 + 用户的实时问题（包含历史上下文对话）
    # 使用 MemoryManager 管理对话历史，自动压缩老消息为摘要，Redis 缓存增量摘要
    from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.memory import MemoryCache
    conversation_id = config.get("configurable", {}).get("thread_id", None)
    memory_manager = MemoryManager(llm=model, cache=MemoryCache())
    managed_messages = await memory_manager.manage(
        state.messages, conversation_id=conversation_id
    )
    messages = [
        {"role": "system", "content": ROUTER_SYSTEM_PROMPT}
    ] + managed_messages
    logger.info("-----Analyze user query type-----")
    logger.info("Managed messages: {} (original: {})", len(managed_messages), len(state.messages))
    
    # 使用结构化输出，输出场景+风险+技术路由三维结果
    # 校验失败（模型输出非法枚举值）时降级为 general/none，保证路由不抛异常
    try:
        response = cast(
            Router, await model.with_structured_output(Router).ainvoke(messages)
        )
        response["source"] = "llm"
        response["sub_type"] = _normalize_sub_type(
            response.get("type"), response.get("sub_type")
        )
        response["confidence"] = _normalize_confidence(response.get("confidence"))
    except Exception as e:
        logger.error("Router 结构化输出失败，降级为 general/none: {}", str(e))
        response = Router(type="general", sub_type="none", risk="none",
                          logic="结构化输出失败降级", source="llm", confidence=0.0)
    # confidence 只记录不路由（实测判别力弱，见 SPEC §11.5）——此处仅落日志供后续统计
    logger.info("Analyze user query type completed, result: {}", response)
    logger.info("意图置信度: source={} confidence={} | query: '{}'",
                response.get("source"), response.get("confidence"), user_question)
    return {"router": response}

def route_query(
    state: AgentState,
) -> Literal[
    "risk_intercept", "transfer_human",
    "respond_to_general_query", "create_research_plan", "create_image_query",
    "aftersale_placeholder", "complaint_placeholder", "clarify_node",
]:
    """根据场景+风险分类确定下一步操作（risk 拦截优先级最高）。

    Args:
        state (AgentState): 当前代理状态，包括路由器的分类。

    Returns:
        下一步操作节点名。
    """
    _type = state.router["type"]
    _risk = state.router["risk"]
    # 用 .get 而非下标：改造前落盘的 checkpoint 里 router 无 sub_type 字段，
    # 续聊旧会话时下标访问会 KeyError（本次不改路由，sub_type 仅用于日志）
    _sub = state.router.get("sub_type", "none")
    query = state.messages[-1].content if state.messages else ""

    # risk 拦截最优先：违规/高风险消息不进入任何业务处理路径
    if _risk == "violation":
        logger.info("意图路由: risk=violation → 节点=risk_intercept | query: '{}'", query)
        return "risk_intercept"
    elif _risk == "high_risk":
        logger.info("意图路由: risk=high_risk → 节点=transfer_human | query: '{}'", query)
        return "transfer_human"

    # 检查配置中是否有图片路径，如果有，优先处理为图片查询
    if hasattr(state, "config") and state.config and state.config.get("configurable", {}).get("image_path"):
        logger.info("检测到图片路径，转为图片查询处理")
        return "create_image_query"

    if _type == "clarify":
        logger.info("意图路由: 类型={} → 节点=clarify_node(意图澄清) | query: '{}'", _type, query)
        return "clarify_node"
    elif _type == "general":
        logger.info("意图路由: 类型={} → 节点=respond_to_general_query | query: '{}'", _type, query)
        return "respond_to_general_query"
    elif _type == "presale":
        logger.info("意图路由: 类型={} → 节点=create_research_plan(RAG 检索) | query: '{}'", _type, query)
        return "create_research_plan"
    elif _type == "aftersale":
        logger.info("意图路由: 类型={} 二级场景={} → 节点=aftersale_placeholder | query: '{}'",
                    _type, _sub, query)
        return "aftersale_placeholder"
    elif _type == "complaint":
        logger.info("意图路由: 类型={} → 节点=complaint_placeholder | query: '{}'", _type, query)
        return "complaint_placeholder"
    elif _type == "image":
        logger.info("意图路由: 类型={} → 节点=create_image_query | query: '{}'", _type, query)
        return "create_image_query"
    else:
        logger.error("意图路由: 未知类型 {}（预期五类之一） | query: '{}'", _type, query)
        raise ValueError(f"Unknown router type {_type}")
    
async def respond_to_general_query(
    state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[BaseMessage]]:
    """生成对一般查询的响应，完全基于大模型，不会触发任何外部服务的调用，包括自定义工具、知识库查询等。

    当路由器将查询分类为一般问题时，将调用此节点。

    Args:
        state (AgentState): 当前代理状态，包括对话历史和路由逻辑。
        config (RunnableConfig): 用于配置响应生成的模型。

    Returns:
        Dict[str, List[BaseMessage]]: 包含'messages'键的字典，其中包含生成的响应。
    """
    logger.info("-----generate general-query response-----")
    
    # 使用大模型生成回复
    if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
        model = ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL, temperature=settings.LLM_TEMPERATURE, tags=["general_query"], extra_body={"thinking": {"type": "disabled"}})
    else:
        model = ChatOllama(model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL, temperature=settings.LLM_TEMPERATURE, tags=["general_query"], extra_body={"thinking": {"type": "disabled"}})
    
    system_prompt = GENERAL_QUERY_SYSTEM_PROMPT.format(
        logic=state.router["logic"]
    )

    # 使用 MemoryManager 管理对话历史，Redis 缓存增量摘要
    from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.memory import MemoryCache
    conversation_id = config.get("configurable", {}).get("thread_id", None)
    memory_manager = MemoryManager(llm=model, cache=MemoryCache())
    managed_messages = await memory_manager.manage(
        state.messages, conversation_id=conversation_id
    )
    messages = [{"role": "system", "content": system_prompt}] + managed_messages
    response = await model.ainvoke(messages)
    return {"messages": [response]}

async def risk_intercept(
    state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[BaseMessage]]:
    """风险拦截：违规咨询明确拒绝 + 合规引导（静态话术，不走 LLM）。

    对应福客 D5：违规咨询 AI 使用明确拒绝和平台内合规引导话术。
    """
    logger.info("-----risk_intercept: violation 违规咨询拦截-----")
    return {"messages": [AIMessage(content=RISK_INTERCEPT_REPLY)]}


async def transfer_human(
    state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[BaseMessage]]:
    """转人工：高风险操作/投诉升级，说明无法在线直接处理（静态话术）。

    对应福客 D3/D4：敏感问题只述事实不推测原因，转人工复核。
    """
    logger.info("-----transfer_human: high_risk 高风险操作-----")
    return {"messages": [AIMessage(content=TRANSFER_HUMAN_REPLY)]}


async def aftersale_placeholder(
    state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[BaseMessage]]:
    """售后占位节点：返回"服务升级中"提示（静态话术）。

    接口与 multi_tool 子图同构（question+history → answer），
    后续售后 agent 子图就位后仅替换路由目的地。
    """
    logger.info("-----aftersale_placeholder: 售后功能建设中-----")
    return {"messages": [AIMessage(content=AFTERSALE_PLACEHOLDER_REPLY)]}


async def complaint_placeholder(
    state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[BaseMessage]]:
    """投诉安抚占位节点：返回安抚占位话术（静态话术）。

    后续投诉安抚 agent 子图就位后仅替换路由目的地。
    """
    logger.info("-----complaint_placeholder: 投诉安抚功能建设中-----")
    return {"messages": [AIMessage(content=COMPLAINT_PLACEHOLDER_REPLY)]}

async def clarify_node(
    state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[BaseMessage]]:
    """意图澄清节点：意图不明时以电商统一风格针对性询问用户真实意图。

    LLM 生成澄清问题（结合用户原话 + router logic），失败/异常降级静态模板。
    """
    logger.info("-----clarify_node: 意图不明，询问用户真实意图-----")
    try:
        if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
            model = ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL, temperature=settings.LLM_TEMPERATURE, tags=["clarify"], extra_body={"thinking": {"type": "disabled"}})
        else:
            model = ChatOllama(model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL, temperature=settings.LLM_TEMPERATURE, tags=["clarify"], extra_body={"thinking": {"type": "disabled"}})

        system_prompt = CLARIFY_SYSTEM_PROMPT.format(
            logic=state.router["logic"]
        )
        question = state.messages[-1].content if state.messages else ""

        # 使用 MemoryManager 管理对话历史，澄清需结合上文判断问什么
        from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.memory import MemoryCache
        conversation_id = config.get("configurable", {}).get("thread_id", None)
        memory_manager = MemoryManager(llm=model, cache=MemoryCache())
        managed_messages = await memory_manager.manage(
            state.messages, conversation_id=conversation_id
        )
        messages = [{"role": "system", "content": system_prompt}] + managed_messages
        response = await model.ainvoke(messages)
        if response.content and str(response.content).strip():
            return {"messages": [response]}
        logger.warning("澄清节点 LLM 输出为空，降级静态模板")
    except Exception as e:
        logger.error("澄清节点生成失败，降级静态模板: {}", str(e))
    return {"messages": [AIMessage(content=CLARIFY_FALLBACK_REPLY)]}

async def create_image_query(
    state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[BaseMessage]]:
    """处理图片查询并生成描述回复
    
    Args:
        state (AgentState): 当前代理状态，包括对话历史
        config (RunnableConfig): 配置参数，包含线程ID等配置信息
        
    Returns:
        Dict[str, List[BaseMessage]]: 包含'messages'键的字典，其中包含生成的响应
    """
    logger.info("-----Found User Upload Image-----")    
    image_path = config.get("configurable", {}).get("image_path", None)

    if not image_path or not Path(image_path).exists():
        logger.warning("User Upload Image Not Found: {}", image_path)
        return {"messages": [AIMessage(content="抱歉，我无法查看这张图片，请重新上传。")]}
    
    # 获取视觉模型配置
    api_key = settings.VISION_API_KEY
    base_url = settings.VISION_BASE_URL
    vision_model = settings.VISION_MODEL
    
    if not api_key or not base_url or not vision_model:
        logger.error("Vision Model Configuration Not Complete")
        return {"messages": [AIMessage(content="抱歉，我无法查看这张图片，请重新上传。")]}
    
    logger.info("Using Vision Model: {} to process image: {}", vision_model, image_path)
    
    try:
        # 导入图片处理库
        from PIL import Image
        import io
        
        # 读取并压缩图片
        with Image.open(image_path) as img:
            # 设置最大尺寸
            max_size = 1024
            # 计算缩放比例
            width, height = img.size
            ratio = min(max_size / width, max_size / height)
            
            # 如果图片尺寸已经小于最大尺寸，不需要缩放
            if width <= max_size and height <= max_size:
                resized_img = img
            else:
                new_width = int(width * ratio)
                new_height = int(height * ratio)
                resized_img = img.resize((new_width, new_height), Image.LANCZOS)
            
            # 转换为JPEG格式，并调整质量
            img_byte_arr = io.BytesIO()
            if resized_img.mode != 'RGB':
                resized_img = resized_img.convert('RGB')
            resized_img.save(img_byte_arr, format='JPEG', quality=85)
            img_byte_arr.seek(0)
            
            # 转换为base64
            image_data = base64.b64encode(img_byte_arr.read()).decode('utf-8')
            
            logger.info("Image Compressed, Original Size: {}x{}, New Size: {}x{}", width, height, resized_img.width, resized_img.height)
        
        # 构建API请求
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        payload = {
            "model": vision_model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个专业的图像分析助手。请详细分析图片中的内容，特别关注产品细节、品牌、型号等信息。"
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_data}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": settings.VISION_MAX_TOKENS,
            "temperature": 0.7
        }

        # 发送API请求
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=settings.VISION_TIMEOUT
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    image_description = result["choices"][0]["message"]["content"]
                    logger.info("Successfully processed image and generated description")
                    # 使用图片描述和用户问题生成最终回复
                    # 从lg_prompts导入电商客服模板
                    
                    # 构建回复请求
                    if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
                        model = ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL, temperature=settings.LLM_TEMPERATURE, tags=["image_query"], extra_body={"thinking": {"type": "disabled"}})
                    else:
                        model = ChatOllama(model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL, temperature=settings.LLM_TEMPERATURE, tags=["image_query"], extra_body={"thinking": {"type": "disabled"}})
                    # 使用专门的图片查询提示模板
                    system_prompt = GET_IMAGE_SYSTEM_PROMPT.format(
                        image_description=image_description
                    )
                    messages = [{"role": "system", "content": system_prompt}] + state.messages
                    response = await model.ainvoke(messages)
                    return {"messages": [response]}    
        
                else:
                    error_text = await response.text()
                    logger.error("Vision API Request Failed: {} - {}", response.status, error_text)
                    return {"messages": [AIMessage(content=f"抱歉，我无法查看这张图片，请重新上传。")]}





    except Exception as e:
        logger.error("Error processing image: {}", str(e))
        return {"messages": [AIMessage(content=f"抱歉，我无法查看这张图片，请重新上传。")]}

# ==================== 售前 MultiTool 子图单例 ====================
# 原 create_research_plan 每次请求新建 llm + compile 子图（实测固定开销 ~40ms/次：
# llm 实例化 ~45ms、图构建 ~7ms），且每请求新建 llm 客户端导致 httpx 连接池无法跨请求复用。
# 子图节点闭包仅捕获 llm/prompt（无请求态），CompiledStateGraph 可并发 ainvoke
# （无 checkpointer，调用期状态独立），故提升为进程级懒加载单例；
# settings 进程启动即固化（模块级实例），单例缓存无需失效逻辑。
_research_graph = None
_research_graph_lock = threading.Lock()


def _build_research_model(temperature: float):
    """子图模型构造（DEEPSEEK / OLLAMA 分支，thinking 关闭）。

    子图内按节点职责拆两个实例：summarize 沿用 LLM_TEMPERATURE，planner 用
    PLANNER_TEMPERATURE（拆解为确定性任务，收敛为 0，见 SPEC_PLANNER_ENTITY_SPLIT_AND_RETRIEVAL §4.4）。
    **新实例不得打 tags**——闸门按 metadata["langgraph_node"] 节点名判定，
    共享实例上的 tags 曾是售前不流式的根因（2026-09-18 流式整改）。
    """
    if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
        return ChatDeepSeek(
            api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL,
            temperature=temperature, extra_body={"thinking": {"type": "disabled"}},
        )
    return ChatOllama(
        model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL,
        temperature=temperature, extra_body={"thinking": {"type": "disabled"}},
    )


def get_research_graph() -> CompiledStateGraph:
    """懒加载售前 MultiTool 子图单例（双检锁，模式同 get_rag_retriever_service）。

    模型按 settings.AGENT_SERVICE 选择，构造参数与原 create_research_plan 内完全一致
    （thinking 关闭）。**不再打 tags**——原 `research_plan`
    标签粒度覆盖 planner+summarize 两个节点（共享实例），曾被 SSE 出口当内部推理误挡，
    致售前整段返不流式，2026-09-18 随流式整改移除（见 app/lg_agent/stream_filter.py）。
    首建竞态由 _research_graph_lock 收口；compile 为同步操作且仅 ~7ms，不阻塞事件循环。
    """
    global _research_graph
    if _research_graph is None:
        with _research_graph_lock:
            if _research_graph is None:
                _research_graph = create_multi_tool_workflow(
                    llm=_build_research_model(settings.LLM_TEMPERATURE),          # summarize 沿用 0.7
                    planner_llm=_build_research_model(settings.PLANNER_TEMPERATURE),  # planner 收敛为 0
                )
    return _research_graph


async def create_research_plan(
    state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[str] | str]:
    """通过查询本地知识库回答客户问题，执行任务分解，创建分布查询计划。

    Args:
        state (AgentState): 当前代理状态，包括对话历史。
        config (RunnableConfig): 用于配置计划生成的模型。

    Returns:
        Dict[str, List[str] | str]: 包含'steps'键的字典，其中包含研究步骤列表。
    """
    logger.info("------execute local knowledge base query------")

    # 从 config 获取会话 ID
    conversation_id = config.get("configurable", {}).get("thread_id", None)

    # 售前 MultiTool 子图为进程级单例（懒加载复用，见 get_research_graph），
    # 消除每请求 llm 实例化 + 图构建的 ~40ms 固定开销，并复用 llm 连接池
    multi_tool_workflow = get_research_graph()

    # 指代消解已前置到系统入口（main.py /api/langgraph/query），
    # 进入本节点的 query 已是消解后的完整问题，直接取当前问题
    resolved_question = state.messages[-1].content if state.messages else ""

    # 准备输入状态 — 直接使用消解后的问题
    input_state = {
        "question": resolved_question,
        "data": [],
        "history": []
    }

    # ⑥ 超时保护：包装工作流调用，30 秒超时返回降级回答
    # 子图禁用 checkpointer（__pregel_checkpointer=None）：
    #   langgraph 0.3.25 + PostgresSaver 下子图 Send(map-reduce) 的 checkpoint 序列化
    #   会抛 "Object of type Send is not JSON serializable"（已最小复现）；
    #   子图纯 RAG 检索无中断/恢复需求，会话记忆由主图 checkpoint 承担。
    timeout = TimeoutGuard(timeout_seconds=settings.RAG_TIMEOUT_SECONDS)
    response = await timeout.wrap(
        multi_tool_workflow.ainvoke(
            input_state,
            config={"configurable": {"__pregel_checkpointer": None}},
        ),
        fallback={"answer": "抱歉，系统处理超时，请稍后再试。"},
        conversation_id=conversation_id or "",
    )
    return {"messages": [AIMessage(content=response["answer"])]}

# 定义持久化存储：会话检查点存 PostgreSQL（PostgresSaver）
# LangGraph官方地址：https://langchain-ai.github.io/langgraph/how-tos/persistence/
# AsyncPostgresSaver 必须在事件循环内构造，连接池与 graph 编译推迟到
# FastAPI 启动时完成（见 main.py 的 lifespan → init_checkpointer）
checkpointer_pool = AsyncConnectionPool(
    conninfo=settings.POSTGRES_DSN,
    open=False,
    min_size=1,
    max_size=10,
    kwargs={"autocommit": True},
)


class _LazyGraph:
    """graph 延迟代理：lifespan 初始化检查点后编译，请求期再解析真实对象"""

    _graph = None

    def __getattr__(self, name):
        if self._graph is None:
            raise RuntimeError("LangGraph 尚未初始化（Postgres 检查点未就绪）")
        return getattr(self._graph, name)


async def init_checkpointer():
    """打开检查点连接池、创建检查点表并编译 graph（幂等，供 FastAPI lifespan 调用）"""
    if checkpointer_pool.closed:
        await checkpointer_pool.open()
    checkpointer = AsyncPostgresSaver(checkpointer_pool)
    await checkpointer.setup()
    _LazyGraph._graph = builder.compile(checkpointer=checkpointer)
    logger.info("LangGraph PostgresSaver 检查点初始化完成")


async def close_checkpointer():
    """关闭检查点连接池（供 FastAPI lifespan 调用）"""
    if not checkpointer_pool.closed:
        await checkpointer_pool.close()


# 定义状态图
builder = StateGraph(AgentState, input=InputState)
# 添加节点
builder.add_node(analyze_and_route_query)
builder.add_node(respond_to_general_query)
builder.add_node(risk_intercept)
builder.add_node(transfer_human)
builder.add_node("create_research_plan", create_research_plan)  # 这里是子图（售前导购复用）
builder.add_node(aftersale_placeholder)
builder.add_node(complaint_placeholder)
builder.add_node(clarify_node)
builder.add_node(create_image_query)

# 添加边
builder.add_edge(START, "analyze_and_route_query")
builder.add_conditional_edges("analyze_and_route_query", route_query)


graph = _LazyGraph()

# from IPython.display import Image, display
# display(Image(graph.get_graph().draw_mermaid_png()))