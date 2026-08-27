from app.lg_agent.lg_states import AgentState, Router
from app.lg_agent.lg_prompts import (
    ROUTER_SYSTEM_PROMPT,
    GENERAL_QUERY_SYSTEM_PROMPT,
    GET_IMAGE_SYSTEM_PROMPT,
    RISK_INTERCEPT_REPLY,
    TRANSFER_HUMAN_REPLY,
    AFTERSALE_PLACEHOLDER_REPLY,
    COMPLAINT_PLACEHOLDER_REPLY,
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
from app.lg_agent.lg_states import AgentState, InputState, Router
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.planner.node import create_planner_node
from app.lg_agent.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.multi_tool import create_multi_tool_workflow
from typing import Dict, List
from langchain_core.messages import AIMessage
from langchain_core.runnables.base import Runnable
import base64
import os
import aiohttp
import json
import time
from pathlib import Path

from typing import Literal

from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.memory import MemoryManager
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.agent_safety import (
    ScopeGuard, TimeoutGuard,
)


# 构建日志记录器
logger = get_logger(service="lg_builder")

async def analyze_and_route_query(
    state: AgentState, *, config: RunnableConfig
) -> dict[str, Router]:
    """Analyze the user's query and determine the appropriate routing.

    This function uses a language model to classify the user's query and decide how to route it
    within the conversation flow.

    Args:
        state (AgentState): The current state of the agent, including conversation history.
        config (RunnableConfig): Configuration with the model used for query analysis.

    Returns:
        dict[str, Router]: A dictionary containing the 'router' key with the classification result (classification type and logic).
    """
    # ③ 经营范围预检（关键词级，零延迟）
    user_question = state.messages[-1].content if state.messages else ""
    scope_guard = ScopeGuard()
    in_scope, scope_reason = scope_guard.check(user_question)
    if not in_scope:
        logger.warning("经营范围预检拦截: {}", scope_reason)
        return {"router": Router(type="general", risk="none", logic=f"超出经营范围: {scope_reason}")}

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
    except Exception as e:
        logger.error("Router 结构化输出失败，降级为 general/none: {}", str(e))
        response = Router(type="general", risk="none", logic="结构化输出失败降级")
    logger.info("Analyze user query type completed, result: {}", response)
    return {"router": response}

def route_query(
    state: AgentState,
) -> Literal[
    "risk_intercept", "transfer_human",
    "respond_to_general_query", "create_research_plan", "create_image_query",
    "aftersale_placeholder", "complaint_placeholder",
]:
    """根据场景+风险分类确定下一步操作（risk 拦截优先级最高）。

    Args:
        state (AgentState): 当前代理状态，包括路由器的分类。

    Returns:
        下一步操作节点名。
    """
    _type = state.router["type"]
    _risk = state.router["risk"]
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

    if _type == "general":
        logger.info("意图路由: 类型={} → 节点=respond_to_general_query | query: '{}'", _type, query)
        return "respond_to_general_query"
    elif _type == "presale":
        logger.info("意图路由: 类型={} → 节点=create_research_plan(RAG 检索) | query: '{}'", _type, query)
        return "create_research_plan"
    elif _type == "aftersale":
        logger.info("意图路由: 类型={} → 节点=aftersale_placeholder | query: '{}'", _type, query)
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
        state.messages, system_prompt=system_prompt, conversation_id=conversation_id
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

    # 使用大模型生成查询/多跳、并行查询计划
    if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
        model = ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL, temperature=settings.LLM_TEMPERATURE, tags=["research_plan"], extra_body={"thinking": {"type": "disabled"}})
    else:
        model = ChatOllama(model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL, temperature=settings.LLM_TEMPERATURE, tags=["research_plan"], extra_body={"thinking": {"type": "disabled"}})

    # 创建多工具工作流（planner → 向量检索 → summarize → final_answer）
    multi_tool_workflow = create_multi_tool_workflow(
        llm=model,
    )

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
builder.add_node(create_image_query)

# 添加边
builder.add_edge(START, "analyze_and_route_query")
builder.add_conditional_edges("analyze_and_route_query", route_query)


graph = _LazyGraph()

# from IPython.display import Image, display
# display(Image(graph.get_graph().draw_mermaid_png()))