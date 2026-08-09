from typing import Any, Callable, Coroutine, Dict, List
import asyncio
import os
from pathlib import Path
from pydantic import BaseModel, Field

# 导入GraphRAG相关模块
import app.graphrag.graphrag.api as api
from app.graphrag.graphrag.config.load_config import load_config
from app.graphrag.graphrag.callbacks.noop_query_callbacks import NoopQueryCallbacks
from app.graphrag.graphrag.utils.storage import load_table_from_storage
from app.graphrag.graphrag.storage.file_pipeline_storage import FilePipelineStorage

# 导入LLM模块
from langchain_deepseek import ChatDeepSeek
from langchain_ollama import ChatOllama

# 导入混合检索模块
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.hybrid_retrieval import HybridRetriever

# 导入相关性评分模块
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.relevance_grader import grade_and_ensure_min_results

# 导入配置
from app.core.config import settings, ServiceType
from app.core.logger import get_logger

logger = get_logger(service="customer_tools")

# 定义GraphRAG查询的输入状态类型
class GraphRAGQueryInputState(BaseModel):
    task: str
    query: str
    steps: List[str]

# 定义GraphRAG查询的输出状态类型
class GraphRAGQueryOutputState(BaseModel):
    task: str
    query: str
    errors: List[str]
    records: Dict[str, Any]
    steps: List[str]

# 定义GraphRAG API包装器
class GraphRAGAPI:
    def __init__(self, project_dir: str = None, 
                 data_dir_name: str = None,
                 query_type: str = None,
                 response_type: str = None,
                 community_level: int = None,
                 dynamic_community_selection: bool = None):
        # 从环境变量获取配置，如果提供了参数则使用参数值
        self.project_dir = project_dir or settings.GRAPHRAG_PROJECT_DIR
        self.data_dir_name = data_dir_name or settings.GRAPHRAG_DATA_DIR
        self.query_type = query_type or settings.GRAPHRAG_QUERY_TYPE
        self.response_type = response_type or settings.GRAPHRAG_RESPONSE_TYPE
        self.community_level = community_level or settings.GRAPHRAG_COMMUNITY_LEVEL
        self.dynamic_community_selection = dynamic_community_selection if dynamic_community_selection is not None else settings.GRAPHRAG_DYNAMIC_COMMUNITY
        self.config = None
        self.storage = None
        self.entities = None
        self.text_units = None
        self.communities = None
        self.community_reports = None
        self.relationships = None
        self.covariates = None
        self.initialized = False
    
    async def initialize(self):
        """初始化GraphRAG API，加载必要的数据"""
        if self.initialized:
            return
            
        # 构建完整项目路径
        project_directory = os.path.join(self.project_dir, self.data_dir_name)
        
        # 加载配置
        self.config = load_config(Path(project_directory), None, None)
        
        # 创建存储路径
        output_dir = Path(self.config.output.base_dir)
        if not output_dir.is_absolute():
            output_dir = Path(project_directory) / output_dir
        
        # 创建FilePipelineStorage对象
        self.storage = FilePipelineStorage(root_dir=str(output_dir))
        
        # 加载必要的数据文件
        try:
            self.entities = await load_table_from_storage("entities", self.storage)
            self.text_units = await load_table_from_storage("text_units", self.storage)
            self.communities = await load_table_from_storage("communities", self.storage)
            self.community_reports = await load_table_from_storage("community_reports", self.storage)
            self.relationships = await load_table_from_storage("relationships", self.storage)
            
            # 尝试加载协变量数据（可能不存在）
            try:
                self.covariates = await load_table_from_storage("covariates", self.storage)
            except Exception:
                self.covariates = None
            
            self.initialized = True
        except Exception as e:
            raise Exception(f"加载GraphRAG数据文件时出错: {str(e)}")
    
    async def query_graphrag(self, query: str) -> Dict[str, Any]:
        """执行GraphRAG查询"""
        await self.initialize()
        
        # 创建回调对象
        callbacks = []
        context_data = {}
        
        def on_context(context):
            nonlocal context_data
            context_data = context
        
        local_callbacks = NoopQueryCallbacks()
        local_callbacks.on_context = on_context
        callbacks.append(local_callbacks)
        
        try:
            # 根据查询类型执行不同的查询
            if self.query_type.lower() == "local":
                response, context = await api.local_search(
                    config=self.config,
                    entities=self.entities,
                    communities=self.communities,
                    community_reports=self.community_reports,
                    text_units=self.text_units,
                    relationships=self.relationships,
                    covariates=self.covariates,
                    community_level=self.community_level,
                    response_type=self.response_type,
                    query=query,
                    callbacks=callbacks
                )
            
            elif self.query_type.lower() == "global":
                response, context = await api.global_search(
                    config=self.config,
                    entities=self.entities,
                    communities=self.communities,
                    community_reports=self.community_reports,
                    community_level=self.community_level,
                    dynamic_community_selection=self.dynamic_community_selection,
                    response_type=self.response_type,
                    query=query,
                    callbacks=callbacks
                )
            
            elif self.query_type.lower() == "drift":
                response, context = await api.drift_search(
                    config=self.config,
                    entities=self.entities,
                    communities=self.communities,
                    community_reports=self.community_reports,
                    text_units=self.text_units,
                    relationships=self.relationships,
                    community_level=self.community_level,
                    response_type=self.response_type,
                    query=query,
                    callbacks=callbacks
                )
            
            elif self.query_type.lower() == "basic":
                response, context = await api.basic_search(
                    config=self.config,
                    text_units=self.text_units,
                    query=query,
                    callbacks=callbacks
                )
            
            else:
                raise ValueError(f"不支持的查询类型: {self.query_type}")
            
            # 构建结果字典
            result = {
                "response": response,
                "context": context_data
            }
            
            return result
            
        except Exception as e:
            raise Exception(f"执行GraphRAG查询时出错: {str(e)}")

def create_graphrag_query_node(
) -> Callable[
    [GraphRAGQueryInputState],
    Coroutine[Any, Any, Dict[str, List[GraphRAGQueryOutputState] | List[str]]],
]:
    """
    创建GraphRAG查询节点，用于LangGraph工作流。

    返回
    -------
    Callable[[GraphRAGQueryInputState], Dict[str, List[GraphRAGQueryOutputState] | List[str]]]
        名为`graphrag_query`的LangGraph节点。
    """

    async def graphrag_query(
        state: Dict[str, Any],
    ) -> Dict[str, List[GraphRAGQueryOutputState] | List[str]]:
        """
        执行GraphRAG查询 + 混合检索（BM25+向量 RRF融合），合并结果返回。
        """
        errors = list()
        search_result = {}
        hybrid_results = []

        # 获取查询文本
        query = state.get("task", "")
        if not query:
            errors.append("未提供查询文本")
        else:
            # 并行执行 GraphRAG 查询和混合检索
            graphrag_api = GraphRAGAPI()

            async def run_graphrag():
                return await graphrag_api.query_graphrag(query)

            async def run_hybrid():
                try:
                    await graphrag_api.initialize()
                    if graphrag_api.text_units is not None and len(graphrag_api.text_units) > 0:
                        retriever = HybridRetriever(
                            documents=graphrag_api.text_units.to_dict("records"),
                            text_key="text",
                        )
                        return retriever.search(query, top_k=5, retrieval_top_n=20)
                except Exception as e:
                    logger.warning(f"混合检索失败，跳过: {e}")
                return []

            # 并行执行，GraphRAG 和混合检索互不阻塞
            search_result, hybrid_results = await asyncio.gather(
                run_graphrag(), run_hybrid()
            )

            if hybrid_results:
                logger.info(f"混合检索补充了 {len(hybrid_results)} 条文档")

            # 相关性评分：过滤不相关的检索结果，不足时自动重检索
            if hybrid_results and settings.RELEVANCE_GRADING_ENABLED:
                if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
                    grader_llm = ChatDeepSeek(
                        api_key=settings.DEEPSEEK_API_KEY,
                        model_name=settings.DEEPSEEK_MODEL,
                        temperature=0,
                    )
                else:
                    grader_llm = ChatOllama(
                        model=settings.OLLAMA_AGENT_MODEL,
                        base_url=settings.OLLAMA_BASE_URL,
                        temperature=0,
                    )

                hybrid_results = await grade_and_ensure_min_results(
                    llm=grader_llm,
                    query=query,
                    documents=hybrid_results,
                    graphrag_api=graphrag_api,
                    content_key="text",
                )

            return {
                "cyphers": [
                    GraphRAGQueryOutputState(
                        **{
                            "task": state.get("task", ""),
                            "query": query,
                            "statement": "",
                            "parameters": "",
                            "errors": errors,
                            "records": {
                                "result": search_result.get("response", ""),
                                "hybrid_docs": hybrid_results,
                            },
                            "steps": ["execute_graphrag_query"],
                        }
                    )
                ],
                "steps": ["execute_graphrag_query"],
            }
  
    return graphrag_query

