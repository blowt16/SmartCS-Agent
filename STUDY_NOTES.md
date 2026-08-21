# SmartCS-Agent 项目架构文档

> 智能电商客服系统，基于 FastAPI + LangGraph 构建

---

## 建议阅读路径

本文档按**数据流**组织，推荐以下渐进式阅读顺序：

1. **入门** → [1. Overview](#1-overview) → [2. Quick Start](#2-quick-start) → [3. Architecture Overview](#3-architecture-overview)
2. **Agent 核心** → [4. LangGraph Agent 设计](#4-langgraph-agent-设计) → [5. 知识图谱查询](#5-知识图谱查询) → [6. 联网搜索与图片分析](#6-联网搜索与图片分析)
3. **知识系统** → [7. Neo4j 知识图谱](#7-neo4j-知识图谱) → [8. 文档检索管道（标准 RAG）](#8-文档检索管道标准-rag) → [8.5 检索增强管道](#85-检索增强管道retrieval-augmentation-pipeline)
4. **平台功能** → [9. LLM 服务层](#9-llm-服务层) → [10. 语义缓存](#10-语义缓存) → [11. 安全认证](#11-安全认证) → [12. 流式响应与前后端交互](#12-流式响应与前后端交互) → [13. 数据模型与持久化](#13-数据模型与持久化)
5. **面试准备** → [14. 面试高频问答](#14-面试高频问答) → [15. 踩坑记录](#15-踩坑记录) → [16. 设计模式总结](#16-设计模式总结)

每个章节独立成篇，也可直接跳转到感兴趣的部分。

---

# Part 1: 项目概览

## 1. Overview

SmartCS-Agent 是一个**智能电商客服系统**。用户可以通过文字、图片等方式咨询商品信息，系统自动识别用户意图，路由到不同的处理模块。普通闲聊直接用 LLM 回答；商品查询通过 pgvector 向量检索 + 混合检索实现；需要联网的问题调用搜索 API。

### 技术栈总览

| 层次 | 技术 | 作用 |
|------|------|------|
| 后端框架 | FastAPI | REST API 服务，原生异步支持 |
| 智能体 | LangGraph | 多路由 Agent 编排（StateGraph） |
| LLM | DeepSeek / Ollama | 大语言模型对话、推理（工厂模式切换） |
| 文档检索 | 标准 RAG（pgvector） | 文档解析→分块→Embedding→pgvector 表（HNSW），混合检索（BM25+向量 RRF） |
| 向量缓存 | Redis | 语义缓存（基于 Embedding 向量相似度） |
| 数据库 | PostgreSQL（pgvector） | 用户、会话、消息持久化 + 向量检索 + LangGraph 检查点 |
| 前端 | Vue3 SFC（frontend） | 聊天界面 |

### 与同类项目的定位差异

| 维度 | SmartCS-Agent（本项目） | SuperMew（参考项目） |
|---|---|---|
| 定位 | 智能电商客服系统 | 文档 Q&A + RAG 参考架构 |
| 核心场景 | 商品咨询、图片分析、联网搜索 | 上传文档 → 基于文档问答 |
| Agent 复杂度 | 5路意图路由 | 2路路由 |
| 知识图谱 | 无（已移除，知识库统一走 pgvector） | 无 |
| 搜索策略 | 标准 RAG：BM25 + 向量 RRF 混合检索 | 自建混合搜索（dense + sparse RRF） |
| 独有亮点 | 幻觉检测、语义缓存、图片分析、**混合检索+相关性评分** | 三级分块、查询改写、Auto-Merge、Rerank |

---

## 2. Quick Start

### 环境要求

- Python 3.8+
- Redis 6.0+
- PostgreSQL 16+（pgvector 镜像，Docker 部署）

### 安装部署

```bash
# 1. 克隆项目
git clone <repository-url>
cd deepseek_agent

# 2. 创建虚拟环境
# 3. 安装依赖（uv 自动创建 .venv 并锁定版本）
uv sync
.venv\Scripts\activate   # Windows

# 4. 配置环境变量
cp llm_backend/.env llm_backend/.env
# 编辑 .env 填入 API 密钥和数据库连接信息

# 5. 初始化数据库（建表 + pgvector 扩展 + HNSW 索引）
cd llm_backend
python -m scripts.init_db

# 6. 启动服务
cd llm_backend
python run.py
```

服务将在 `http://localhost:8000` 启动。

### Docker Compose 一键部署

如果不想手动安装 PostgreSQL、Redis，可以用 Docker Compose 一键启动：

```bash
# 1. 编辑 .env.docker 填入真实 API Key
# 2. 一键启动所有服务
docker compose up -d

# 3. 查看日志
docker compose logs -f app

# 4. 停止所有服务
docker compose down
```

**架构说明**：`docker-compose.yml` 定义了 4 个服务：

| 服务 | 镜像 | 端口 | 作用 |
|------|------|------|------|
| postgres | pgvector/pgvector:pg16 | 5432 | 用户/会话/消息持久化 + 向量检索 + LangGraph 检查点 |
| redis | redis:7-alpine | 6379 | 语义缓存 |
| app | 自构建 | 8000 | FastAPI 应用 |

关键设计：
- **healthcheck**：每个数据库服务都有健康检查，app 用 `depends_on: condition: service_healthy` 确保数据库就绪后才启动
- **.env.docker**：数据库连接配置在 `docker-compose.yml` 的 `environment` 中（使用容器内网络主机名），LLM API Key 等从 `.env.docker` 读取
- **向量库数据**：pgvector 表 `document_chunks`（与业务库共用 PostgreSQL 实例，HNSW 索引，`VECTOR_TABLE_NAME` 配置）
- **Ollama 访问**：容器内用 `host.docker.internal` 访问宿主机的 Ollama 服务

### 配置文件说明（`.env` 关键项）

```bash
# LLM 服务选择
CHAT_SERVICE=deepseek          # deepseek 或 ollama
REASON_SERVICE=deepseek
AGENT_SERVICE=deepseek

# API 密钥
DEEPSEEK_API_KEY=sk-xxxxx
SERPAPI_KEY=xxxxx

# PostgreSQL
DB_HOST=localhost
DB_PORT=5432
DB_NAME=smartcs_agent

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
```

> 参见：[11. 安全认证](#11-安全认证) 了解密码安全存储策略。

---

## 3. Architecture Overview

### 三层架构

```
┌─────────────────────────────────────────────────────┐
│                   API 层（main.py）                   │
│   /api/langgraph/query  /api/upload                  │
│   /api/langgraph/query  /api/conversations           │
│   /api/register  /api/token                          │
├─────────────────────────────────────────────────────┤
│                   服务层（app/services/）              │
│   LLMFactory → DeepSeekService / OllamaService       │
│   ConversationService → PostgreSQL                  │
│   IndexingService → pgvector                         │
│   RedisSemanticCache → Redis + Ollama Embedding      │
├─────────────────────────────────────────────────────┤
│                   数据层                              │
│   PostgreSQL（用户/会话/消息/向量/检查点）                  │
│   Redis（语义缓存）                              │
└─────────────────────────────────────────────────────┘
```

### 完整数据流

```
用户请求 → FastAPI main.py
  │
  ├─ /api/langgraph/query ──→ LangGraph Agent
  │    │
  │    ├─ analyze_and_route_query（意图分析）
  │    │    ├─ general-query → 闲聊（纯 LLM）
  │    │    ├─ additional-query → 追问 + 护栏检查
  │    │    ├─ graphrag-query → Multi-Tool Workflow
  │    │    │    └─ 向量检索（pgvector）＋混合检索（BM25+向量 RRF）
  │    │    ├─ image-query → GPT-4o 图片分析
  │    │    └─ file-query → 文件处理
  │    │
  │    └─ check_hallucinations（幻觉检测）
  │
  └─ /api/upload ──→ IndexingService → 解析→分块→Embedding→pgvector 入库
```

### 目录结构与模块映射

| 模块路径 | 功能描述 | 主要文件 |
|----------|----------|----------|
| `app/services/` | 业务逻辑服务层 | `llm_factory.py`, `deepseek_service.py`, `ollama_service.py`, `redis_semantic_cache.py`, `conversation_service.py` |
| `app/lg_agent/` | LangGraph 智能体系统 | `lg_builder.py`, `lg_states.py`, `lg_prompts.py`, `kg_sub_graph/` |
| `llm_backend/knowledge_data/` | 数据准备脚本输出目录 | 产品知识 / 电商FAQ / 客服对话（TXT） |
| `app/core/` | 核心配置与工具 | `config.py`, `database.py`, `logger.py`, `security.py` |
| `app/models/` | SQLAlchemy 数据模型 | `user.py`, `conversation.py`, `message.py` |
| `app/api/` | 认证路由 | `auth.py` |

---

# Part 2: Agent 系统

> Agent 系统是 SmartCS-Agent 最核心的子系统，负责分析用户意图并路由到对应的处理模块。

## 4. LangGraph Agent 设计

### 解决什么问题

用户的消息类型多种多样——闲聊、商品查询、图片分析、追问细节等。需要一个统一的入口来自动分析意图，并将请求路由到最合适的处理模块。

### 整体流程图

```
                    ┌─────────────────────┐
                    │       START         │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ analyze_and_route   │  ← LLM 分析用户问题类型
                    │     _query          │
                    └──────────┬──────────┘
                               │
        ┌──────────┬───────────┼───────────┬──────────┐
        │          │           │           │          │
        ▼          ▼           ▼           ▼          ▼
  ┌──────────┐ ┌────────┐ ┌─────────┐ ┌────────┐ ┌──────┐
  │ general  │ │additional│ │graphrag │ │ image  │ │ file │
  │ _query   │ │_query    │ │_query   │ │ _query │ │_query│
  │ (闲聊)   │ │(追问细节)│ │(知识库) │ │(图片)  │ │(TODO)│
  └──────────┘ └────────┘ └─────────┘ └────────┘ └──────┘
```

### 状态定义（`lg_states.py`）

三个关键数据类：

| 类名 | 作用 |
|------|------|
| `Router` | 路由分类结果，包含 `type`（5种类型）和 `logic`（分类理由） |
| `InputState` | 输入状态，只有 `messages`（对话消息列表） |
| `AgentState` | 完整状态，继承 InputState，增加 router/steps/question/answer/hallucination |

5种路由类型：
- `general-query` — 闲聊，不查数据库
- `additional-query` — 问题不明确，需要追问
- `graphrag-query` — 查知识库（pgvector 文档检索）
- `image-query` — 图片分析
- `file-query` — 文件处理（未实现）

```python
class Router(TypedDict):
    logic: str
    type: Literal["general-query", "additional-query", "graphrag-query", "image-query", "file-query"]

@dataclass(kw_only=True)
class InputState:
    messages: Annotated[list[AnyMessage], add_messages]

@dataclass(kw_only=True)
class AgentState(InputState):
    router: Router
    steps: list[str]
    question: str
    answer: str
    hallucination: GradeHallucinations
```

### 6个节点详解（`lg_builder.py`）

#### 节点1：analyze_and_route_query（意图分析）

- 用 LLM 分析用户消息，输出结构化 Router 对象
- 关键代码：`model.with_structured_output(Router).ainvoke(messages)`
- **知识点**：`with_structured_output(Router)` 让 LLM 的输出严格符合 Router 的格式（type + logic），底层原理是 LangChain 把 Router 的 JSON Schema 告诉 LLM，LLM 按格式输出

#### 节点2：respond_to_general_query（普通闲聊）

- 纯 LLM 对话，不调用任何外部服务
- 使用电商客服风格的提示词（`GENERAL_QUERY_SYSTEM_PROMPT`）

#### 节点3：get_additional_info（追问 + 护栏）

**双重机制**：
1. **护栏检查（Guardrails）**：让 LLM 判断问题是否在经营范围（智能家居）内
   - 超范围 → "我家暂时没有这方面的商品"
   - 范围内 → LLM 生成追问
2. **追问**：如果在经营范围内但信息不足，LLM 生成追问

#### 节点4：create_research_plan（知识库检索）

> 参见：[5. 知识图谱查询](#5-知识图谱查询) 了解该节点的完整内部架构

#### 节点5：create_image_query（图片分析）

1. 用 PIL 库压缩图片（最大 1024px，JPEG 85% 质量）
2. 转 base64 编码
3. 调用视觉模型（GPT-4o）分析图片内容
4. 把图片描述传给 LLM，生成电商客服风格的回复

#### 节点6：check_hallucinations（幻觉检测）

- 用 LLM 检查生成的回答是否基于事实数据
- `binary_score` = "1" 表示基于事实，"0" 表示有幻觉

### 图构建代码

```python
builder = StateGraph(AgentState, input=InputState)
builder.add_node(analyze_and_route_query)
builder.add_node(respond_to_general_query)
builder.add_node(get_additional_info)
builder.add_node("create_research_plan", create_research_plan)
builder.add_node(create_image_query)
builder.add_node(create_file_query)

builder.add_edge(START, "analyze_and_route_query")
builder.add_conditional_edges("analyze_and_route_query", route_query)

graph = builder.compile(checkpointer=checkpointer)
```

`checkpointer` 为 `AsyncPostgresSaver`（PostgreSQL），让 LangGraph 支持对话持久化——用户可以关闭页面再回来继续聊，服务重启状态也不丢失。

### 关键设计模式

| 模式 | 体现 |
|------|------|
| 状态图（StateGraph） | LangGraph 核心，通过状态在节点间传递数据 |
| 条件边（conditional_edges） | 路由分类后走不同分支 |
| 结构化输出 | `with_structured_output(Router)` 让 LLM 输出指定格式 |
| 检查点（checkpointer） | `AsyncPostgresSaver`（PostgreSQL）支持会话持久化和中断恢复 |
| 子图（SubGraph） | create_research_plan 内部调用完整的多工具工作流 |
| 工厂模式 | LLMFactory 根据 config 创建 DeepSeek/Ollama 实例 |

### 数据流转示例

用户问 "智能灯泡多少钱？"：

```
1. 输入 → InputState(messages="智能灯泡多少钱？")

2. analyze_and_route_query
   → LLM 分析：type=graphrag-query, logic="询问商品价格"

3. route_query（条件边）
   → 根据 type 路由到 create_research_plan

4. create_research_plan
   → 创建 multi_tool_workflow（Guardrails → Planner → 向量检索 → Summarize → FinalAnswer）
   → pgvector 检索"智能灯泡"相关文档块 → 混合检索 + 相关性评分
   → 返回结果

5. 返回 {messages: [AIMessage(content="智能灯泡价格是xx元...")]}
```

---

## 5. 知识库查询（Multi-Tool Workflow）

> 这是 Agent 中最复杂的节点，内部嵌套了一个完整的 Multi-Tool Workflow。
> ⚠️ 本章原描述「知识图谱查询（Text2Cypher / PredefinedCypher）」，相关功能已于 2026-08-16 随 Neo4j 退役（见本章末尾「历史演进」），当前实现为纯向量检索编排。

### 解决什么问题

用户问的商品/售后/使用说明等查询需要从知识库（pgvector 文档块）中检索数据，需要一个统一的编排层完成范围检查、任务分解、检索与汇总。

### Multi-Tool Workflow 内部架构

```
Guardrails（范围检查）→ Planner（任务分解）→ 向量检索（customer_tools 节点）
  └── VectorStoreQuery 余弦 Top-K → 混合检索（BM25 + 向量 RRF）→ 相关性评分
→ Summarize（汇总）→ Final Answer（最终回答）
```

### 向量检索（customer_tools 节点）

1. **VectorStoreQuery**：本地 SentenceTransformer 编码 query
2. **pgvector 检索**：`cosine_distance` 在 document_chunks 表做 Top-K 余弦检索
3. **混合检索补充**：拉取全量语料构建 HybridRetriever（BM25 + 向量 RRF 融合），补充关键词精确命中
4. **相关性评分**：LLM 逐条过滤不相关文档
5. 检索结果汇总进 Summarize，生成客服风格回答

### 护栏机制（Guardrails）

- 让 LLM 判断用户问题是否在经营范围内（智能家居）
- 超范围问题直接拒绝，避免 LLM 产生无关回答

### 幻觉检测

用 LLM 检查生成的回答是否基于事实数据。`binary_score` = "1" 表示基于事实，"0" 表示有幻觉。这为回答质量提供了最后一道保障。

> 参见：[8. 文档检索管道（标准 RAG）](#8-文档检索管道标准-rag) 了解文档检索的实现。

### 历史演进：Text2Cypher 与 PredefinedCypher（2026-08-16 已移除）

旧版在 Planner 后接入 Tool Selection 节点，按查询复杂度三选一：

- **Text2Cypher**：LLM 把自然语言翻译为 Neo4j Cypher，生成 → 正则校验 → LLM 校验 → 修正（最多 3 次）→ 执行
- **PredefinedCypher**：28 条预定义模板 + 向量匹配选模板，直接填参执行
- **向量检索**：pgvector 文档检索

迁移原因：Neo4j 与 PostgreSQL 双数据库 + 图查询/向量检索双引擎并存，运维与查询路径复杂度高；业务问答集中在文档知识库场景，混合检索已能覆盖；统一到 PostgreSQL + pgvector 后，知识库查询收敛为单一检索路径，运维面大幅收敛。

---

## 6. 联网搜索与图片分析

### 联网搜索（Function Calling）

**解决什么问题**：用户的问题可能需要实时信息（如新闻、天气），这些信息不在本地知识库中。

**Function Calling 工作流**：

```
1. 把用户问题 + 工具定义 发送给 LLM
2. LLM 判断：
   ├─ finish_reason="tool_calls" → LLM 决定调用搜索工具
   │   → 执行 SerpAPI 搜索 → 把结果拼接成新提示 → LLM 总结回答
   └─ finish_reason="stop" → LLM 认为不需要搜索，直接回答
```

**`tools` 参数**：告诉 LLM 有哪些工具可用、每个工具的参数格式。LLM 不会直接执行工具，而是返回要调用的工具名和参数，由代码来执行。

### 图片分析（GPT-4o 视觉模型）

**流程**：
1. 用户上传图片 → 前端用 FormData 发送
2. 后端用 PIL 库压缩图片（最大 1024px，JPEG 85% 质量）
3. 转 base64 编码
4. 调用 GPT-4o 视觉模型分析图片内容
5. 把图片描述传给 LLM，生成电商客服风格的回复

### 追问与信息补充机制

当用户的问题不明确时，Agent 会进入 `additional-query` 路由：
- 先做**护栏检查**，判断问题是否在经营范围内
- 如果在经营范围内但信息不足，LLM 自动生成追问
- 通过 LangGraph 的**中断恢复机制**（`Command(resume=...)`）等待用户补充信息后继续对话

---

# Part 3: 知识系统

> 知识系统是 SmartCS-Agent 的数据基础，核心为标准 RAG 文档检索管道（pgvector）。

## 7. Neo4j 知识图谱

> ⚠️ 本章原描述 Neo4j 图数据库设计（商品/订单实体建模与 Text2Cypher 查询），相关功能与依赖已于 2026-08-16 整体移除：docker-compose 的 neo4j 服务、`langchain-neo4j` 依赖、predefined_cypher/text2cypher 组件、实体识别链接管道、Cypher 安全校验器等。移除原因：业务问答集中在文档知识库场景，图查询与向量检索双引擎并存带来运维与查询路径复杂度；统一到 PostgreSQL + pgvector 后，知识库查询收敛为单一向量检索路径。以下保留历史模型速览，供面试复盘参考。

### 历史模型速览

曾用于商品/分类/关系查询的 Cypher 模式（已随功能移除）：

```cypher
-- 查商品价格
MATCH (p:Product {name:'智能灯泡'}) RETURN p.price

-- 查关联关系
MATCH (p:Product)-[:BELONGS_TO]->(c:Category) RETURN p.name, c.name
```

---

## 8. 文档检索管道（标准 RAG）

### 解决什么问题（为什么从 GraphRAG 迁移到标准 RAG）

项目早期曾用 Microsoft GraphRAG 做文档检索，实践中发现它与本项目的场景并不匹配，最终迁移到标准 RAG 管道：

| 问题 | 当时 GraphRAG 的代价 | 迁移后的收益 |
|------|---------------------|-------------|
| 索引成本高 | 建索引需 LLM 抽取实体关系，一次 5-30 分钟，API 成本高 | 索引秒级完成，全程无 LLM 调用 |
| 维护成本高 | 80+ 内嵌源码，版本升级困难 | 管道只有解析→清洗→分块→入库，代码量小 |
| 场景不匹配 | 电商客服以事实查询为主，用不到 Global/DRIFT 多跳推理 | 事实查询效果持平，复杂度大幅降低 |
| 数据冗余 | GraphRAG 抽取实体关系与知识库文档检索冗余 | pgvector 统一知识库检索，单一数据源 |

> 复盘结论：**选型先看场景**。GraphRAG 适合需要跨文档归纳、多跳推理的宏观分析；电商客服的事实查询场景，标准 RAG 足矣。

### 管道全景

```
┌─────────────────────────────────────────────────────────────┐
│                 标准 RAG 索引管道（IndexingService）            │
│                                                             │
│  上传文档（PDF / DOCX / TXT）                                  │
│    → 文档解析（PyPDF2 / python-docx / UTF-8 文本）              │
│    → _clean_text 清洗（统一换行、去噪声）                        │
│    → RecursiveCharacterTextSplitter 分块                      │
│       chunk_size=500, overlap=50（分隔符含中文标点）             │
│    → Embedding（统一 EmbeddingProvider，bge-m3 1024 维）       │
│    → pgvector 入库（document_chunks 表，HNSW 索引）             │
└─────────────────────────────────────────────────────────────┘
```

### 标准 RAG 在项目中的使用方式

**主路径：嵌入 LangGraph Agent（生产使用）**

```
用户问题 → LangGraph Agent → 意图识别为 "graphrag-query"
  → create_research_plan → Multi-Tool Workflow → Tool Selection
  → LLM 选择工具 "vector_search_query"（原 microsoft_graphrag_query）
  → customer_tools/node.py 的 VectorStoreQuery 类（替代原 GraphRAGAPI）
  → pgvector 向量检索 + HybridRetriever（BM25 + 向量 RRF 融合）
  → RelevanceGrader（LLM 相关性评分过滤）→ 生成回答
```

关键代码在 `lg_agent/kg_sub_graph/agentic_rag_agents/components/`：
- `customer_tools/node.py`：`VectorStoreQuery` 类封装 pgvector 查询（SQLAlchemy AsyncSession），`search()` 方法执行余弦 Top-K 检索
- `hybrid_retrieval/`：HybridRetriever（BM25 + 向量 RRF 融合）
- `relevance_grader.py`：LLM 相关性评分，过滤不相关结果
- `kg_tools_list.py` 与 `tool_selection/node.py`：工具名已由 `microsoft_graphrag_query` 改为 `vector_search_query`

**索引路径：上传文件自动入库（`app/services/`）**

| 文件 | 作用 |
|------|------|
| `app/services/indexing_service.py` | IndexingService：解析→清洗→分块→Embedding→pgvector 入库 |
| `app/services/embedding_provider.py` | 统一 Embedding 提供器（local / ollama / qwen 后端可切换） |

### 索引构建流程

**入口**：`indexing_service.py` 的 `process_file()` 方法

**完整流程**：
```
1. 用户上传文件（POST /api/upload）
2. 保存到 uploads/{user_uuid}/{timestamp}/ 目录
3. 按文件类型解析：
   - .pdf → PyPDF2 逐页提取文本
   - .docx → python-docx 读取段落
   - 其他 → 按 UTF-8 文本读取
4. _clean_text() 清洗：统一换行符、压缩连续空行、去首尾空白
5. RecursiveCharacterTextSplitter 分块（chunk_size=500, overlap=50）
6. bge-m3 向量化（1024 维）
7. 写入 pgvector 表 document_chunks（带 source/file_path/user_id 元数据列）
```

| 步骤 | 做什么 | 是否调用 LLM |
|------|--------|------------|
| 1 | 文档解析（PDF / DOCX / TXT → 纯文本） | 否 |
| 2 | _clean_text 文本清洗 | 否 |
| 3 | 递归分块（分隔符含中文标点） | 否 |
| 4 | Embedding 向量化（bge-m3，1024 维） | 否 |
| 5 | pgvector 入库（秒级完成） | 否 |

**关键代码**（indexing_service.py）：
```python
text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=50,
    separators=["\n\n", "\n", "。", "！", "？", "，", " ", ""],  # 含中文标点
)

# pgvector 文档块模型（与业务库共用 PostgreSQL）
class DocumentChunk(Base):
    __tablename__ = "document_chunks"     # settings.VECTOR_TABLE_NAME
    id = Column(Integer, primary_key=True)
    content = Column(Text, nullable=False)             # 分块文本
    embedding = Column(Vector(1024), nullable=False)   # bge-m3 向量化
    # source / file_path / user_id / chunk_index 元数据列
```

### 数据准备脚本

内置知识库数据由脚本生成，输出到 `llm_backend/knowledge_data/`，上传入库后即可检索：

| 脚本 | 数据内容 | 输出目录 |
|------|---------|---------|
| `scripts/generate_product_knowledge.py` | 产品知识文档 | `knowledge_data/product_knowledge/` |
| `scripts/download_datasets.py` | 电商 FAQ 数据集 | `knowledge_data/ecommerce_faq/` |
| `scripts/download_jddc.py` | 京东客服对话语料 | `knowledge_data/` |

### pgvector 表结构

标准 RAG 只用一张表（`document_chunks`）存所有分块，每条记录字段：

| 字段 | 内容 | 用途 |
|------|------|------|
| `id` | 自增主键 | 分块唯一标识 |
| `content` | 分块后的文本片段 | 检索返回的原始证据文本 |
| `source` / `file_path` / `user_id` / `chunk_index` | 元数据列 | 按用户隔离、溯源原文 |
| `embedding` | bge-m3 生成的 1024 维 `vector` 列 | 向量相似度检索（HNSW 索引） |

对比 GraphRAG 时代需要维护 6 张 parquet 表（entities/relationships/text_units/communities/community_reports/covariates），迁移后只需一张表，数据加载和更新逻辑大幅简化。

### 查询时的数据加载流程

以 Agent 调用向量检索为例（`customer_tools/node.py`）：

```python
class VectorStoreQuery:
    def __init__(self):
        self.encoder = SentenceTransformer(settings.EMBEDDING_MODEL)

    async def search(self, query: str, top_k: int = 10):
        query_vec = self.encoder.encode([query], normalize_embeddings=True).tolist()[0]
        distance = DocumentChunk.embedding.cosine_distance(query_vec)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(DocumentChunk, distance.label("distance"))
                .order_by(distance).limit(top_k)
            )
        ...
```

**更新后查询自动可见**：pgvector 表持久化在 PostgreSQL 中，新文档写入后查询即可命中，无需重新加载索引（对比 GraphRAG 时代需要把 parquet 读入内存、索引更新后重新实例化刷新缓存）。

### 增量更新机制

- pgvector 按行管理分块，新上传的文件只会插入新行，不影响已有数据
- 更新方式：
  - **Web API**：`POST /api/upload`（自动触发解析入库）
  - **批量脚本**：`IndexingService.process_directory()` 处理整个目录（如 `knowledge_data/` 下的全部文档）

### 配置系统详解（.env / config.py 核心配置项）

```bash
# ---- 向量库配置 ----
VECTOR_TABLE_NAME=document_chunks        # pgvector 表名（与业务库共用 PostgreSQL）

# ---- Embedding 配置 ----
EMBEDDING_TYPE=ollama                    # local / ollama / qwen 可切换
EMBEDDING_MODEL=bge-m3                   # 默认模型
EMBEDDING_DIMENSION=1024                 # 向量维度

# ---- 分块配置 ----
CHUNK_SIZE=500
CHUNK_OVERLAP=50

# ---- 检索配置 ----
VECTOR_SEARCH_TOP_K=10                   # 向量检索返回数
HYBRID_RETRIEVAL_TOP_K=5                 # 混合检索最终返回数
HYBRID_RETRIEVAL_TOP_N=20                # 混合检索候选数
RELEVANCE_GRADING_ENABLED=true           # 相关性评分开关
```

---

## 8.5 检索增强管道（Retrieval Augmentation Pipeline）

> 混合检索 + 相关性评分，确保喂给 LLM 的检索结果既全面又精准。

### 解决什么问题

向量检索的结果不一定都和用户问题相关。如果直接把不相关的结果喂给 LLM，会产生幻觉或偏题的回答。检索增强管道在"检索"和"生成"之间插入两道关卡：

```
用户问题
  → 向量检索（pgvector）
  → 混合检索（BM25 + 向量检索 + RRF 融合）
  → 相关性评分（LLM 逐条评分，过滤不相关结果）
  → 相关结果不足时如实返回，不强行补足
  → 输出高质量检索结果给下游 LLM
```

### 混合检索（Hybrid Retrieval）

**代码位置**：`components/hybrid_retrieval/hybrid_retriever.py`

单一检索方式有盲区：
- **纯向量检索**：精确型号匹配差（"X1-Pro" 不一定能匹配到 "X1-Pro"），罕见专有名词匹配差
- **纯 BM25**：不理解语义（"灯泡" 不等于 "LED灯"）

混合检索把两路结果用 **RRF（Reciprocal Rank Fusion）** 融合：

```
查询 "扫地机器人X1故障排查"
  ├─ BM25 检索 top-20（关键词匹配）
  │    命中: "X1 Pro 故障代码E03", "X1 滤网更换步骤"
  │
  ├─ 向量检索 top-20（语义匹配）
  │    命中: "扫地机器人常见故障排除指南", "智能清洁设备维修手册"
  │
  └─ RRF 融合 → top-5 最终结果
       公式: RRF_score(d) = Σ 1/(k + rank_i)   (k=60)
```

**RRF 融合原理**：不用分数，只用排名。第 1 名得 1/61 分，第 2 名得 1/62 分...两路分数相加就是最终分数。这样不同检索方式的分数量纲差异就不影响了。

### 相关性评分（Relevance Grading）

**代码位置**：`components/relevance_grader.py`

**为什么需要：** 即使混合检索的结果也不一定都相关。比如搜索"退款流程"，可能检索到"快递退回流程"——字面相似但用户不需要。

**工作流程：**

```
检索结果（N 条）
  → LLM 逐条评分（relevant / irrelevant）
  → 过滤掉 irrelevant
  → 相关结果如实返回，不足时不重试（宁缺毋滥）
```

**关键设计决策：**
- **LLM 做评分**：比规则匹配更智能，能理解"提到相同实体但内容无关"的情况
- **temperature=0**：评分不需要创造性，确定性输出更稳定
- **单次评分不重试**：评分不足时如实返回，不再切换策略重检索（早期「切换 GraphRAG 策略重检索」的逻辑已随 GraphRAG 一起移除）
- **可配置开关**：`RELEVANCE_GRADING_ENABLED` 可关闭，降级为直接返回全部结果

### 集成位置

在 `customer_tools/node.py` 的 `vector_search_query()` 节点函数中，检索后、返回前插入评分：

```python
# 1. 向量检索 + 混合检索（BM25 + 向量 RRF 融合）
vector_results = vector_store.search(query, top_k=settings.VECTOR_SEARCH_TOP_K)
hybrid_results = retriever.search(query, top_k=settings.HYBRID_RETRIEVAL_TOP_K, ...)

# 2. 相关性评分：过滤不相关的检索结果
if hybrid_results and settings.RELEVANCE_GRADING_ENABLED:
    hybrid_results = await grade_relevance(
        llm=grader_llm,
        query=query,
        documents=hybrid_results,
        content_key="text",
    )
```

---

# Part 4: 平台功能

> 平台功能层为 Agent 系统和知识系统提供底层支撑：LLM 调用、缓存、认证、流式传输、数据持久化。

## 9. LLM 服务层

### 解决什么问题

系统需要在多种 LLM 之间灵活切换（在线 API vs 本地模型），同时保持业务代码不变。

### 工厂模式（`llm_factory.py`）

```python
class LLMFactory:
    @staticmethod
    def create_chat_service():
        if settings.CHAT_SERVICE == ServiceType.DEEPSEEK:
            return DeepseekService()  # 在线 API
        else:
            return OllamaService()    # 本地部署
```

**为什么用工厂模式？** 切换模型只需改一行配置（`.env` 文件中的 `CHAT_SERVICE=deepseek` 或 `CHAT_SERVICE=ollama`），不需要修改任何业务代码。这就是"开闭原则"——对扩展开放，对修改关闭。

### DeepSeek 服务（`deepseek_service.py`）

封装与 DeepSeek API 的交互，支持流式输出和语义缓存。

**核心流程**：
```
用户消息 → Redis 语义缓存检查
  ├─ 命中(相似度≥0.90) → 模拟流式返回缓存结果
  └─ 未命中 → 调用 DeepSeek API 流式生成 → 存入缓存 → 回调保存消息
```

**知识点**：
- `AsyncOpenAI`：DeepSeek 兼容 OpenAI 的 API 格式，所以用 OpenAI SDK 即可调用
- `stream=True`：开启流式模式，LLM 每生成一段文字就返回，而不是等全部生成完
- `on_complete` 回调：使用回调函数模式，在生成完成后触发消息持久化，解耦了 LLM 调用和数据存储

> 参见：[10. 语义缓存](#10-语义缓存) 了解缓存检查的详细机制。

### Ollama 本地模型服务

Ollama 是本地部署的 LLM 服务，用于离线场景或降低 API 成本。通过工厂模式的 `ServiceType.OLLAMA` 切换。

---

## 10. 语义缓存

### 解决什么问题

重复调用 LLM 既浪费钱又增加响应延迟。传统缓存用精确匹配（"灯泡多少钱" ≠ "灯泡价格"），语义缓存能识别语义相同的问题。

### 传统缓存 vs 语义缓存

| 维度 | 传统缓存 | 语义缓存 |
|------|---------|----------|
| 匹配方式 | 精确字符串匹配 | Embedding 向量余弦相似度 |
| "灯泡多少钱" = "灯泡价格"？ | 否 | 是 |
| 实现复杂度 | 低 | 中（需要 Embedding 模型） |

### 核心算法

1. 把用户消息通过 Ollama 的 Embedding 模型转成向量（一串数字）
2. 遍历 Redis 中所有缓存的向量，计算余弦相似度
3. 如果最高相似度 >= 阈值(0.90)，认为问题相同，返回缓存答案
4. 否则调用 LLM，把新问题和答案存入 Redis

### 余弦相似度

```
cos(A,B) = A·B / (||A|| × ||B||)
```

结果在 [-1, 1] 之间：
- 1 表示完全相同
- 0 表示无关
- -1 表示完全相反

### 缓存命中时的处理

命中缓存后，不是一次性返回结果，而是**模拟流式返回**——把缓存结果分段 yield 出去。这样做是因为如果瞬间返回完整答案，用户会感觉"异常快"，体验不自然。

---

## 11. 安全认证

### 解决什么问题

用户注册/登录需要安全的密码存储和会话管理。

### 双重哈希策略

```
前端: 用户输入密码 → SHA256 哈希（保护明文不在网络传输中暴露）
后端: SHA256 结果 → bcrypt 哈希（防止数据库泄露后被彩虹表攻击）
存储: bcrypt 哈希值存入 PostgreSQL
```

**为什么前端也要做一次 SHA256？** HTTPS 已经加密了传输内容，但 SHA256 是额外一层保护。即使 SSL 被中间人攻击，攻击者拿到的也是 SHA256 哈希值，不是原始密码。

### JWT 认证流程

```
登录 → 验证密码 → 生成 JWT Token（包含 email 和过期时间）
访问受保护 API → 请求头带 Bearer Token → 解码验证 → 获取用户信息
```

**JWT（JSON Web Token）** 是一种无状态认证方式。服务器不需要存储 session，Token 本身包含了用户信息和签名，可以自验证。

---

## 12. 流式响应与前后端交互

### 解决什么问题

LLM 生成回答需要时间，如果等全部生成完再返回，用户会看到长时间空白。SSE 流式传输让用户看到文字一个字一个字"蹦出来"。

### 前端基础概念

#### HTML / CSS / JS 三件套

- **HTML** = 网页的骨架，定义页面有哪些元素（按钮、输入框等）
- **CSS** = 网页的皮肤，定义元素长什么样（颜色、大小、位置）
- **JavaScript (JS)** = 网页的大脑，定义交互行为（点击后发生什么）

#### Vue.js 数据驱动

Vue 是一个 JS 框架，让你不用手动操作 HTML 元素，而是通过"数据驱动"自动更新页面：

```javascript
// 传统方式（手动操作 DOM）
document.getElementById("msg").innerText = "你好"

// Vue 方式（数据驱动）
// HTML: <div>{{ message }}</div>
data: { message: "你好" }  // 只需改数据，页面自动更新
```

#### 编译后的前端

项目中 `frontend/dist/` 存放的是编译后的前端文件：
```
dist/
├── index.html                    # 入口 HTML
└── assets/
    ├── index-B0CElU7P.js         # 所有 Vue 源码编译压缩后的 JS
    └── index-35IQ2nDy.css        # 所有样式编译压缩后的 CSS
```

### SSE（Server-Sent Events）原理

```
普通请求:  用户发送 → ............等待............ → 完整回复一次性显示
SSE 流式:  用户发送 → "你" → "你好" → "你好，" → "你好，我" → ... → 完整回复
```

后端格式：`data: 内容\n\n`

```python
# FastAPI 后端
return StreamingResponse(
    chat_service.generate_stream(messages=...),
    media_type="text/event-stream"  # SSE 标准 MIME 类型
)
```

### 前端流式读取（ReadableStream API）

```javascript
static async handleChatStream(query, conversationId, mode, onMessage) {
  // 1. 选择 API 端点
  let endpoint;
  switch (mode) {
    case "agent":   endpoint = "/api/langgraph/query"; break;
  }

  // 2. 发送 POST 请求
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages: [...], user_id: ..., conversation_id: ... })
  });

  // 3. 获取 ReadableStream（流式读取器）
  const reader = response.body.getReader();
  const decoder = new TextDecoder();

  // 4. 循环读取数据块
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const text = decoder.decode(value);  // 二进制 → 字符串
    const lines = text.split("\n").filter(line => line.startsWith("data: "));

    for (const line of lines) {
      const content = JSON.parse(line.slice(6));  // 去掉 "data: " 前缀
      if (content.includes("<think")) {
        onMessage({ type: "think", content: content });
      } else {
        onMessage({ type: "response", content: content });
      }
    }
  }
}
```

**关键知识点**：
- `response.body.getReader()`：浏览器 Streams API，逐块读取服务器数据
- `TextDecoder`：把二进制（Uint8Array）转成字符串
- `reader.read()`：每次读取一块，返回 `{ done, value }`
- `data: 内容\n\n`：SSE 标准格式

### LangGraph Agent 的 SSE 处理

```javascript
async function sendAgentQuery(query, conversationId, imageFile) {
  const formData = new FormData();
  formData.append("query", query);
  formData.append("user_id", localStorage.getItem("user_id"));
  if (conversationId) formData.append("conversation_id", conversationId);
  if (imageFile) formData.append("image", imageFile);  // 附加图片

  const response = await fetch("/api/langgraph/query", {
    method: "POST",
    body: formData  // 不能设 Content-Type，浏览器自动设 multipart/form-data
  });

  const newConversationId = response.headers.get("X-Conversation-ID");
  // ... 同样的流式读取逻辑
}
```

**知识点**：
- **FormData**：浏览器内置的表单数据对象，用于上传文件
- **不能手动设 Content-Type**：浏览器会自动设 `multipart/form-data; boundary=...`，手动设会破坏 boundary
- **X-Conversation-ID**：后端在响应头中返回会话 ID

### 前后端交互全景图

```
┌─────────────────────────────────────────────────────────────────┐
│                      用户浏览器（前端）                            │
│                                                                 │
│  ┌──────────┐    ┌───────────────┐    ┌───────────────────┐    │
│  │ 登录/注册 │───→│ POST /api/token│───→│ 获得 JWT Token    │    │
│  │  页面     │    │ POST /api/register  │ 存入 localStorage │    │
│  └──────────┘    └───────────────┘    └───────────────────┘    │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                     主聊天页面                             │   │
│  │                                                          │   │
│  │  ① 创建会话 ──→ POST /api/conversations                  │   │
│  │  ② 获取历史 ──→ GET  /api/conversations/user/{id}        │   │
│  │  ③ 获取消息 ──→ GET  /api/conversations/{id}/messages    │   │
│  │                                                          │   │
│  │  发送消息时，根据选择模式调用不同 API：                      │   │
│  │  └─ Agent   ─→ POST /api/langgraph/query(SSE 流式)       │   │
│  │                                                          │   │
│  │  文件上传 ──→ POST /api/upload (FormData)                 │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTP 请求/响应
┌──────────────────────────────▼──────────────────────────────────┐
│                   FastAPI 后端服务器（localhost:8000）             │
│                                                                 │
│  /api/langgraph/query → LangGraph Agent → SSE 流式响应          │
│  /api/upload → IndexingService → pgvector 向量库入库           │
│  /api/conversations → PostgreSQL → JSON 响应                    │
│  /api/register, /api/token → bcrypt + JWT → JSON 响应           │
└─────────────────────────────────────────────────────────────────┘
```

### CORS 跨域中间件

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     # 允许所有来源（开发环境）
    allow_credentials=True,  # 允许 Cookie 和 Authorization 头
    allow_methods=["*"],     # 允许所有 HTTP 方法
    allow_headers=["*"],     # 允许所有请求头
)
```

**为什么需要 CORS？** 浏览器有"同源策略"安全限制：前端在 `localhost:5173` 运行，后端在 `localhost:8000`，端口不同 = 不同源。CORS 中间件就是后端告诉浏览器"我允许跨域"。

### 静态文件挂载

```python
STATIC_DIR = Path(__file__).parent.parent / "frontend" / "dist"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
```

把 Vue 编译后的 `dist/` 挂载到根路径 `/`。**必须放在最后**，因为 `app.mount("/")` 会匹配所有路径，放在前面会拦截 API 路由。

### Pydantic 请求模型

```python
class ChatMessage(BaseModel):
    messages: List[Dict[str, str]]
    user_id: int
    conversation_id: int
```

Pydantic 做了三件事：
1. **自动解析**：前端发来的 JSON 自动转成 Python 对象
2. **自动校验**：如果漏传了 `user_id`，自动返回 422 错误
3. **自动文档**：Swagger UI（`/docs`）自动显示每个字段的类型

### API 端点与前端调用对应关系

| 前端操作 | 前端调用 | 后端端点 | 后端处理 |
|---------|---------|---------|---------|
| 登录 | `axios.post("/api/token")` | `POST /api/token` | 验证密码→返回 JWT |
| 注册 | `axios.post("/api/register")` | `POST /api/register` | bcrypt 哈希→存 PostgreSQL |
| 新建会话 | `fetch("/api/conversations")` | `POST /api/conversations` | PostgreSQL 插入记录 |
| 获取历史会话 | `fetch("/api/conversations/user/{id}")` | `GET /api/conversations/user/{id}` | PostgreSQL 查询 |
| 获取历史消息 | `fetch("/api/conversations/{id}/messages")` | `GET /api/conversations/{id}/messages` | PostgreSQL 查询 |
| Agent 对话 | `fetch("/api/langgraph/query")` | `POST /api/langgraph/query` | LangGraph→SSE |
| 上传文件 | `fetch("/api/upload")` | `POST /api/upload` | FormData→pgvector 入库 |

### 一次完整对话的前后端流程

以"用户发送一条聊天消息"为例：

```
第一步：用户在输入框打字，点击发送按钮
  │
  ▼ 前端 JS
1. 从输入框获取文字内容
2. 在页面上立即显示用户消息（不等后端响应）
3. 构造请求体：{ messages: [{role:"user", content:"你好"}], user_id: 1, conversation_id: 5 }
4. 发送 POST 请求到 /api/langgraph/query
  │
  ▼ 网络传输
5. HTTP 请求通过 CORS 检查，到达 FastAPI 服务器
  │
  ▼ 后端 FastAPI (main.py)
6. FastAPI 用 Pydantic 校验请求参数（ChatMessage 模型）
7. LLMFactory.create_chat_service() 创建 DeepSeek 服务实例
8. 调用 DeepSeek API，开始流式生成
9. 每生成一段文字就 yield "data: {json}\n\n" 发给前端
  │
  ▼ 网络传输（SSE 流）
10. 浏览器通过 response.body.getReader() 逐块接收数据
  │
  ▼ 前端 JS
11. TextDecoder 把二进制转成文字
12. 解析 "data: " 前缀，提取 JSON 内容
13. 通过 Vue 的响应式数据更新页面，文字一个字一个字出现
14. 流结束后，触发 on_complete 回调→后端保存消息到 PostgreSQL
```

> 参见：[9. LLM 服务层](#9-llm-服务层) 了解 LLM 调用的详细实现。
> 参见：[4. LangGraph Agent 设计](#4-langgraph-agent-设计) 了解 Agent 的工作原理。

---

## 13. 数据模型与持久化

### 解决什么问题

用户的账户信息、对话历史、消息记录需要持久化存储，支持会话恢复和历史查询。

### SQLAlchemy ORM 模型

| 模型 | 对应表 | 主要字段 |
|------|--------|---------|
| `User` | users | id, username, email, hashed_password |
| `Conversation` | conversations | id, user_id, title, created_at |
| `Message` | messages | id, conversation_id, role, content, created_at |

### 异步数据库引擎（`database.py`）

```python
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=5,       # 常驻连接数：5 个连接随时待命
    max_overflow=10    # 溢出连接数：高峰期最多再创建 10 个
)
# 总计：5 + 10 = 最多 15 个并发数据库连接
```

**连接池**：每次创建数据库连接都很耗时（TCP 握手+认证），连接池提前创建好多个连接，需要时直接取用，用完归还。

### 配置系统（`config.py`）

```python
class Settings(BaseSettings):    # 继承 pydantic_settings 的 BaseSettings
    DEEPSEEK_API_KEY: str        # 自动从 .env 读取同名的环境变量

    class Config:
        env_file = str(ENV_FILE) # 指定 .env 文件路径
```

**优先级**：环境变量 > .env 文件 > 默认值。部署时可以不修改代码，直接通过环境变量覆盖配置。

**`@property` 方法**：`DATABASE_URL`、`REDIS_URL` 是计算属性，用 `{user}:{password}@{host}:{port}/{db}` 格式拼接出数据库连接字符串。

### `get_db()` 依赖注入

FastAPI 的 `Depends(get_db)` 会在请求时自动创建数据库会话，请求结束时自动提交或回滚。这种模式叫**依赖注入（Dependency Injection）**，把数据库会话的创建和销毁从业务代码中解耦出来。

---

# Part 5: 附录

## 14. 面试高频问答

### Q1: 请介绍一下你的项目

> SmartCS-Agent 是一个智能电商客服系统。用户可以通过文字、图片等方式咨询商品信息，系统会自动识别用户意图，路由到不同的处理模块。普通闲聊直接用 LLM 回答；商品查询通过 pgvector 向量检索 + 混合检索实现；需要联网的问题调用搜索 API。我还实现了语义缓存来减少重复的 LLM 调用。

### Q2: 你的 LangGraph Agent 是怎么设计的？

> 我设计了一个基于 StateGraph 的多路由 Agent。用户消息先经过意图分析节点，LLM 把问题分类为 5 种类型（闲聊/追问/知识库查询/图片/文件），然后通过条件边路由到对应节点。最复杂的知识库查询节点内部嵌套了一个 Multi-Tool Workflow（护栏检查 → 任务分解 → 向量检索 → 结果汇总 → 最终回答），检索统一走 pgvector：余弦 Top-K 召回 + BM25/向量 RRF 混合检索 + LLM 相关性评分。

### Q3: 语义缓存是怎么实现的？

> 传统缓存用精确匹配，"灯泡多少钱"和"灯泡价格"是两个不同的 key。我的语义缓存使用 Ollama 的 bge-m3 模型把用户消息转成向量，然后和 Redis 中所有缓存的向量计算余弦相似度。如果最高相似度超过 0.90 的阈值，就认为问题语义相同，直接返回缓存答案。这样避免了重复调用 LLM，既降低了成本又减少了响应延迟。

### Q4: 你的知识库检索是怎么演进的？

> 项目早期是双引擎：Text2Cypher（LLM 把自然语言翻译成 Neo4j Cypher，走生成-校验-修正-执行闭环）加 pgvector 向量检索，由工具选择节点按复杂度三选一。后来复盘发现电商客服问答集中在文档知识库场景，双数据库 + 双查询引擎的运维与查询路径复杂度远超收益，就把 Neo4j 整体退役，检索统一收敛到 pgvector：余弦 Top-K + BM25/向量 RRF 混合 + LLM 相关性评分。这个演进的核心经验是：技术选型要匹配场景，能力过剩的组件（图查询引擎）要及时裁掉。

### Q5: 你用了哪些设计模式？

> 1. **工厂模式**：LLMFactory 根据配置创建不同的 LLM 服务实例
> 2. **策略模式**：通过 .env 配置切换 DeepSeek 和 Ollama
> 3. **回调模式**：on_complete 参数实现消息持久化
> 4. **责任链模式**：查询预处理管道的 改写→纠错→扩展→HyDE 流水线
> 5. **观察者模式**：LangGraph 的状态变化通知机制

### Q6: 为什么选择这些技术？

> - **FastAPI**：原生异步支持，适合 LLM 流式输出场景
> - **LangGraph**：比纯 LangChain 更适合复杂的多步骤工作流编排，支持状态持久化和中断恢复
> - **PostgreSQL + pgvector**：业务数据与向量检索共用一个实例，运维面最小
> - **标准 RAG（pgvector）**：文档管道简单可靠，混合检索（BM25+向量 RRF）兼顾关键词与语义，事实查询效果与 GraphRAG 持平但成本低一个量级

### Q7: 为什么从 GraphRAG 迁移到标准 RAG？

> GraphRAG 建索引需要 LLM 抽取实体和关系，一次要 5-30 分钟、API 成本高；80+ 内嵌源码维护困难；而且电商客服以事实查询为主，用不到 Global/DRIFT 多跳推理；知识库问答靠文档检索已能覆盖，实体关系抽取是冗余。迁移到标准 RAG（pgvector）后索引秒级完成，事实查询效果持平。核心经验：**选型要看场景**——GraphRAG 适合跨文档归纳、多跳推理的宏观分析，事实查询用标准 RAG 足矣。

### Q8: 你的混合检索（BM25 + 向量 RRF）是怎么设计的？

> 纯向量检索对精确型号、罕见专有名词匹配差，纯 BM25 不理解语义。我的混合检索把两路结果用 RRF 融合：BM25 和向量各取 top-20，RRF 只用排名不用分数（第 n 名得 1/(60+n) 分），两路分数相加取 top-5。这样不同检索方式的分数量纲差异就不会影响融合结果。融合后还会用 LLM 做相关性评分，过滤掉字面相似但内容无关的结果。

---

## 15. 踩坑记录

### 1. psycopg 在 Windows 上 ProactorEventLoop 报错

**现象：** 运行 `init_db.py` 或启动 uvicorn 时报 `Psycopg cannot use the 'ProactorEventLoop' to run in async mode`

**原因：** Windows 上 Python 默认事件循环是 ProactorEventLoop（uvicorn 的 `--loop auto` 也会显式选择它），psycopg 异步模式明确不支持该循环

**修复（两层）：**

1. `app/core/database.py` 顶部全局设置 SelectorEventLoop 策略（覆盖 `asyncio.run` 脚本场景）：

```python
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
```

2. `run.py` 启动前给 uvicorn 的 loop 工厂打补丁（uvicorn 会在创建事件循环时延迟导入该工厂）：

```python
import uvicorn.loops.asyncio as _uv_asyncio_loop

def _selector_loop_factory(use_subprocess: bool = False):
    return asyncio.SelectorEventLoop

_uv_asyncio_loop.asyncio_loop_factory = _selector_loop_factory
```

Docker/Linux 环境无此问题（默认 SelectorEventLoop）。

### 1.1（历史）aiomysql 在 Windows 上 Event Loop 关闭报错

**现象：** 运行 `init_db.py` 时报 `RuntimeError: Event loop is closed`

**原因：** `asyncio.run()` 关闭事件循环后，aiomysql 连接池的 `__del__` 方法尝试清理连接，但事件循环已不存在

**修复：** 在 `init_db.py` 的 `finally` 块中加 `await engine.dispose()` 主动释放连接（该模式沿用至今）

```python
async def init_db():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()  # 关键修复
```

### 2. LangGraph 流式输出中特殊字符处理

**问题：** 回复中包含换行符等特殊字符时前端解析失败

**修复：** 使用 `json.dumps(content, ensure_ascii=False)` 确保特殊字符被正确转义

### 3. Redis 缓存 key 编码问题

**问题：** Windows 下 Redis 存储和读取时编码不一致

**修复：** 统一使用 `.encode('utf-8')` 和 `.decode('utf-8')` 处理所有 key 和 value

---

## 16. 设计模式总结

### 1. 工厂模式（Factory Pattern）

**位置**：`llm_factory.py` 的 `LLMFactory` 类

**应用**：根据 `.env` 配置决定创建 `DeepseekService` 还是 `OllamaService`。切换模型只需改配置，不改代码。

**原理**：定义一个创建对象的接口，让子类决定实例化哪个类。

### 2. 策略模式（Strategy Pattern）

**位置**：`config.py` 的 `ServiceType` 枚举

**应用**：`CHAT_SERVICE`、`REASON_SERVICE`、`AGENT_SERVICE` 可以分别配置不同的 LLM 服务。

**原理**：定义一系列算法，把它们封装起来，使它们可以互相替换。

### 3. 回调模式（Callback Pattern）

**位置**：`deepseek_service.py` 的 `on_complete` 参数

**应用**：LLM 生成完成后，通过回调函数触发消息持久化，解耦了 LLM 调用和数据存储。

**原理**：把函数作为参数传递，在特定事件发生时调用。

### 4. 责任链模式（Chain of Responsibility）

**位置**：（历史）原 Text2Cypher 流水线（已于 2026-08-16 随 Neo4j 移除）

**应用**：生成→校验→修正→执行，每一步处理完传递给下一步，校验失败则退回修正。

**原理**：把处理步骤串成链条，请求沿着链条传递，直到被处理。

### 5. 观察者模式（Observer Pattern）

**位置**：LangGraph 的状态变化通知机制

**应用**：StateGraph 中状态变化时，相关节点自动被触发执行。

**原理**：定义对象间一对多的依赖关系，当一个对象状态改变时，所有依赖它的对象都会被通知。
