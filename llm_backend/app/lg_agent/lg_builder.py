from app.lg_agent.lg_states import AgentState, Router
from app.lg_agent.lg_prompts import (
    ROUTER_SYSTEM_PROMPT,
    GET_ADDITIONAL_SYSTEM_PROMPT,
    GENERAL_QUERY_SYSTEM_PROMPT,
    GET_IMAGE_SYSTEM_PROMPT,
    GUARDRAILS_SYSTEM_PROMPT,
    RAGSEARCH_SYSTEM_PROMPT,
    CHECK_HALLUCINATIONS,
    GENERATE_QUERIES_SYSTEM_PROMPT
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
from app.lg_agent.lg_states import AgentState, InputState, Router, GradeHallucinations
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.planner.node import create_planner_node
from app.lg_agent.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.multi_tool import create_multi_tool_workflow
from app.lg_agent.kg_sub_graph.kg_neo4j_conn import get_neo4j_graph
from pydantic import BaseModel
from typing import Dict, List
from langchain_core.messages import AIMessage
from langchain_core.runnables.base import Runnable
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.utils.utils import retrieve_and_parse_schema_from_graph_for_prompts
from langchain_core.prompts import ChatPromptTemplate
import base64
import os
import aiohttp
import asyncio
import json
import time
from pathlib import Path

from typing import Literal
from pydantic import BaseModel, Field

from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.memory import MemoryManager
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.agent_safety import (
    ScopeGuard, TimeoutGuard, BudgetGuard, HallucinationGuard,
)


class AdditionalGuardrailsOutput(BaseModel):
    """
    格式化输出，用于判断用户的问题是否与图谱内容相关
    """
    decision: Literal["end", "continue"] = Field(
        description="Decision on whether the question is related to the graph contents."
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
        logger.warning(f"经营范围预检拦截: {scope_reason}")
        return {"router": Router(type="general-query", logic=f"超出经营范围: {scope_reason}")}

    # 选择模型实例，通过.env文件中的AGENT_SERVICE参数选择
    if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
        model = ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL, temperature=settings.LLM_TEMPERATURE, tags=["router"])
        logger.info(f"Using DeepSeek model: {settings.DEEPSEEK_MODEL}")
    else:
        model = ChatOllama(model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL, temperature=settings.LLM_TEMPERATURE, tags=["router"])
        logger.info(f"Using Ollama model: {settings.OLLAMA_AGENT_MODEL}")

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
    logger.info(f"Managed messages: {len(managed_messages)} (original: {len(state.messages)})")
    
    # 使用结构化输出，输出问题类型
    response = cast(
        Router, await model.with_structured_output(Router).ainvoke(messages)
    )
    logger.info(f"Analyze user query type completed, result: {response}")
    return {"router": response}

def route_query(
    state: AgentState,
) -> Literal["respond_to_general_query", "get_additional_info", "create_research_plan", "create_image_query"]:
    """根据查询分类确定下一步操作。

    Args:
        state (AgentState): 当前代理状态，包括路由器的分类。

    Returns:
        Literal["respond_to_general_query", "get_additional_info", "create_research_plan", "create_image_query"]: 下一步操作。
    """
    _type = state.router["type"]
    
    # 检查配置中是否有图片路径，如果有，优先处理为图片查询
    if hasattr(state, "config") and state.config and state.config.get("configurable", {}).get("image_path"):
        logger.info("检测到图片路径，转为图片查询处理")
        return "create_image_query"

    if _type == "general-query":
        return "respond_to_general_query"
    elif _type == "additional-query":
        return "get_additional_info"
    elif _type == "graphrag-query":
        return "create_research_plan"
    elif _type == "image-query":
        return "create_image_query"
    else:
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
        model = ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL, temperature=settings.LLM_TEMPERATURE, tags=["general_query"])
    else:
        model = ChatOllama(model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL, temperature=settings.LLM_TEMPERATURE, tags=["general_query"])
    
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

async def get_additional_info(
    state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[BaseMessage]]:
    """生成一个响应，要求用户提供更多信息。

    当路由确定需要从用户那里获取更多信息时，将调用此函数。

    Args:
        state (AgentState): 当前代理状态，包括对话历史和路由逻辑。
        config (RunnableConfig): 用于配置响应生成的模型。

    Returns:
        Dict[str, List[BaseMessage]]: 包含'messages'键的字典，其中包含生成的响应。
    """
    logger.info("------continue to get additional info------")
    
    # 使用大模型生成回复
    if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
        model = ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL, temperature=settings.LLM_TEMPERATURE, tags=["additional_info"])
    else:
        model = ChatOllama(model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL, temperature=settings.LLM_TEMPERATURE, tags=["additional_info"])

    # 如果用户的问题是电商相关，但与自己的业务无关，则需要返回"无关问题"

    # 首先连接 Neo4j 图数据库
    neo4j_graph = None
    try:
        neo4j_graph = get_neo4j_graph()
        logger.info("success to get Neo4j graph database connection")
    except Exception as e:
        logger.error(f"failed to get Neo4j graph database connection: {e}")

    # 定义电商经营范围
    scope_description = """
    个人电商经营范围：智能家居产品，包括但不限于：
    - 智能照明（灯泡、灯带、开关）
    - 智能安防（摄像头、门锁、传感器）
    - 智能控制（温控器、遥控器、集线器）
    - 智能音箱（语音助手、音响）
    - 智能厨电（电饭煲、冰箱、洗碗机）
    - 智能清洁（扫地机器人、洗衣机）
    
    不包含：服装、鞋类、体育用品、化妆品、食品等非智能家居产品。
    """

    scope_context = (
        f"参考此范围描述来决策:\n{scope_description}"
        if scope_description is not None
        else ""
    )

    # 动态从 Neo4j 图表中获取图表结构
    graph_context = (
        f"\n参考图表结构来回答:\n{retrieve_and_parse_schema_from_graph_for_prompts(neo4j_graph)}"
        if neo4j_graph is not None
        else ""
    )

    message = scope_context + graph_context + "\nQuestion: {question}"

    # 拼接提示模版
    full_system_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                GUARDRAILS_SYSTEM_PROMPT,
            ),
            (
                "human",
                (message),
            ),
        ]
    )

    # 构建格式化输出的 Chain， 如果匹配，返回 continue，否则返回 end
    guardrails_chain = full_system_prompt | model.with_structured_output(AdditionalGuardrailsOutput)
    guardrails_output = await guardrails_chain.ainvoke(
            {"question": state.messages[-1].content if state.messages else ""}
        )

    # 根据格式化输出的结果，返回不同的响应
    if guardrails_output.decision == "end":
        logger.info("-----Fail to pass guardrails check-----")
        return {"messages": [AIMessage(content="抱歉，我家暂时没有这方面的商品，可以在别家看看哦~")]}
    else:
        logger.info("-----Pass guardrails check-----")
        system_prompt = GET_ADDITIONAL_SYSTEM_PROMPT.format(
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
        logger.warning(f"User Upload Image Not Found: {image_path}")
        return {"messages": [AIMessage(content="抱歉，我无法查看这张图片，请重新上传。")]}
    
    # 获取视觉模型配置
    api_key = settings.VISION_API_KEY
    base_url = settings.VISION_BASE_URL
    vision_model = settings.VISION_MODEL
    
    if not api_key or not base_url or not vision_model:
        logger.error("Vision Model Configuration Not Complete")
        return {"messages": [AIMessage(content="抱歉，我无法查看这张图片，请重新上传。")]}
    
    logger.info(f"Using Vision Model: {vision_model} to process image: {image_path}")
    
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
            
            logger.info(f"Image Compressed, Original Size: {width}x{height}, New Size: {resized_img.width}x{resized_img.height}")
        
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
            "max_tokens": 4000,
            "temperature": 0.7
        }
        
        # 发送API请求
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=60  # 增加超时时间
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    image_description = result["choices"][0]["message"]["content"]
                    logger.info(f"Successfully processed image and generated description")
                    # 使用图片描述和用户问题生成最终回复
                    # 从lg_prompts导入电商客服模板
                    
                    # 构建回复请求
                    if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
                        model = ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL, temperature=settings.LLM_TEMPERATURE, tags=["image_query"])
                    else:
                        model = ChatOllama(model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL, temperature=settings.LLM_TEMPERATURE, tags=["image_query"])
                    # 使用专门的图片查询提示模板
                    system_prompt = GET_IMAGE_SYSTEM_PROMPT.format(
                        image_description=image_description
                    )
                    messages = [{"role": "system", "content": system_prompt}] + state.messages
                    response = await model.ainvoke(messages)
                    return {"messages": [response]}    
        
                else:
                    error_text = await response.text()
                    logger.error(f"Vision API Request Failed: {response.status} - {error_text}")
                    return {"messages": [AIMessage(content=f"抱歉，我无法查看这张图片，请重新上传。")]}





    except Exception as e:
        logger.error(f"Error processing image: {str(e)}")
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
        model = ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL, temperature=settings.LLM_TEMPERATURE, tags=["research_plan"])
    else:
        model = ChatOllama(model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL, temperature=settings.LLM_TEMPERATURE, tags=["research_plan"])

    # P1 新增：根据查询复杂度选择最优策略
    # 从路由阶段获取复杂度信息，使用 .get() 安全访问（兼容旧版本）
    router = state.router
    complexity = router.get("complexity", 0.5) if isinstance(router, dict) else 0.5
    reasoning_required = router.get("reasoning_required", False) if isinstance(router, dict) else False

    # 策略选择逻辑：
    # - 简单查询（complexity < 0.3 且不需要推理）→ 预定义 Cypher 模板（最快）
    # - 中等查询（0.3 <= complexity <= 0.7）→ LLM 自动选择工具
    # - 复杂查询（complexity > 0.7 或需要推理）→ 向量检索（最强）
    if complexity < 0.3 and not reasoning_required:
        tool_preference = "predefined_cypher"
        logger.info(f"简单查询(complexity={complexity:.2f})，优先使用预定义 Cypher 策略")
    elif complexity > 0.7 or reasoning_required:
        tool_preference = "vector_search_query"
        logger.info(f"复杂查询(complexity={complexity:.2f}, reasoning={reasoning_required})，使用向量检索策略")
    else:
        tool_preference = None  # 无偏好，让 LLM 自动选择
        logger.info(f"中等查询(complexity={complexity:.2f})，使用 LLM 自动选择工具策略")

    # 初始化必要参数
    # 1. Neo4j图数据库连接 - 使用配置中的连接信息
    neo4j_graph = None
    try:
        neo4j_graph = get_neo4j_graph()
        logger.info("success to get Neo4j graph database connection")
    except Exception as e:
        logger.error(f"failed to get Neo4j graph database connection: {e}")

    # step 3. 定义工具模式列表
    from app.lg_agent.kg_sub_graph.kg_tools_list import predefined_cypher, vector_search_query
    tool_schemas: List[type[BaseModel]] = [predefined_cypher, vector_search_query]

    # 3. 预定义的Cypher查询 - 为电商场景定义有用的查询
    from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.cypher_dict import predefined_cypher_dict

    # 定义电商经营范围
    scope_description = """
    个人电商经营范围：智能家居产品，包括但不限于：
    - 智能照明（灯泡、灯带、开关）
    - 智能安防（摄像头、门锁、传感器）
    - 智能控制（温控器、遥控器、集线器）
    - 智能音箱（语音助手、音响）
    - 智能厨电（电饭煲、冰箱、洗碗机）
    - 智能清洁（扫地机器人、洗衣机）
    
    不包含：服装、鞋类、体育用品、化妆品、食品等非智能家居产品。
    """

    # 创建多工具工作流，传入 tool_preference 以根据复杂度选择策略
    multi_tool_workflow = create_multi_tool_workflow(
        llm=model,
        graph=neo4j_graph,
        tool_schemas=tool_schemas,
        predefined_cypher_dict=predefined_cypher_dict,
        scope_description=scope_description,
        tool_preference=tool_preference,  # 根据复杂度选择的策略偏好
    )
    
    # ====== 查询预处理管道（改写 → 纠错 → 实体识别 → 扩展 → Multi-Query + HyDE）======
    # ④ 预算控制：每步 LLM 调用前检查预算，超预算跳过非必要步骤
    budget = BudgetGuard()

    # 第一步：上下文感知改写（必要）
    # 多轮对话中用户可能用代词（"那个""它"）或省略主语（"有货吗"），
    # 需要结合历史消息把问题补全为独立、完整的查询，否则后续检索无法匹配
    from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.query_rewriting.node import (
        context_aware_rewrite,
        rewrite_query,
    )
    resolved_question = state.messages[-1].content if state.messages else ""  # 默认用原始问题
    if budget.can_call("context_rewrite", essential=True):
        resolved_question = await context_aware_rewrite(model, state.messages)
        budget.record("context_rewrite", tokens=500, essential=True)

    # 第二步：查询纠错（非必要，可跳过）
    # 修正错别字（如"扫第机器人"→"扫地机器人"），确保实体识别基于正确文本
    from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.query_rewriting.query_correction import (
        correct_query,
        expand_query,
    )
    corrected_question = resolved_question
    if budget.can_call("query_correction", essential=False):
        corrected_question = await correct_query(model, resolved_question)
        budget.record("query_correction", tokens=300, essential=False)

    # 第三步：实体识别与链接（必要）
    # 从用户问题中识别产品名、类别名等实体，链接到 Neo4j 节点
    from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.query_rewriting.entity_recognition import (
        recognize_and_link_entities,
    )
    if budget.can_call("entity_recognition", essential=True) and neo4j_graph is not None:
        linked_entities = await recognize_and_link_entities(model, neo4j_graph, corrected_question)
        budget.record("entity_recognition", tokens=400, essential=True)
        if linked_entities:
            entity_summary = "; ".join(
                f"{e.name}→{e.node_type}(id={e.node_id})" for e in linked_entities if e.linked
            )
            logger.info(f"实体识别链接结果: {entity_summary}")

    # 第四步：查询扩展（非必要，可跳过）
    # 补充同义词（如"灯泡"→"LED灯"），扩大检索覆盖面
    expanded_question = corrected_question
    if budget.can_call("query_expansion", essential=False):
        expanded_question = await expand_query(model, corrected_question)
        budget.record("query_expansion", tokens=300, essential=False)

    # 第五步：查询改写 Multi-Query + HyDE（非必要，可跳过）
    enhanced_question = expanded_question
    if budget.can_call("multi_query", essential=False):
        rewritten = await rewrite_query(model, expanded_question)
        enhanced_question = rewritten.enhanced_query
        budget.record("multi_query", tokens=500, essential=False)

    logger.info(f"预处理预算消耗: {budget.total_calls} 次调用, {budget.total_tokens} tokens")

    # 准备输入状态 — 用增强后的问题替代原始问题
    input_state = {
        "question": enhanced_question,
        "data": [],
        "history": []
    }

    # ⑥ 超时保护：包装工作流调用，30 秒超时返回降级回答
    timeout = TimeoutGuard(timeout_seconds=30)
    response = await timeout.wrap(
        multi_tool_workflow.ainvoke(input_state),
        fallback={"answer": "抱歉，系统处理超时，请稍后再试。"},
        conversation_id=conversation_id or "",
    )
    return {"messages": [AIMessage(content=response["answer"])]}

async def check_hallucinations(
    state: AgentState, *, config: RunnableConfig
) -> dict[str, Any]:
    """Analyze the user's query and checks if the response is supported by the set of facts based on the document retrieved,
    providing a binary score result.

    This function uses a language model to analyze the user's query and gives a binary score result.

    Args:
        state (AgentState): The current state of the agent, including conversation history.
        config (RunnableConfig): Configuration with the model used for query analysis.

    Returns:
        dict[str, Router]: A dictionary containing the 'router' key with the classification result (classification type and logic).
    """
    if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
        model = ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL, temperature=settings.LLM_TEMPERATURE, tags=["hallucinations"])
    else:
        model = ChatOllama(model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL, temperature=settings.LLM_TEMPERATURE, tags=["hallucinations"])
    
    system_prompt = CHECK_HALLUCINATIONS.format(
        documents=state.documents,
        generation=state.messages[-1]
    )

    messages = [
        {"role": "system", "content": system_prompt}
    ] + state.messages

    logger.info("---CHECK HALLUCINATIONS---")
    
    response = cast(GradeHallucinations, await model.with_structured_output(GradeHallucinations).ainvoke(messages))
    
    return {"hallucination": response} 


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
builder.add_node(get_additional_info)
builder.add_node("create_research_plan", create_research_plan)  # 这里是子图
builder.add_node(create_image_query)

# 添加边
builder.add_edge(START, "analyze_and_route_query")
builder.add_conditional_edges("analyze_and_route_query", route_query)


graph = _LazyGraph()

# from IPython.display import Image, display
# display(Image(graph.get_graph().draw_mermaid_png()))