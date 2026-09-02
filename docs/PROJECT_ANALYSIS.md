# SmartCS-Agent 项目深度分析报告

> **分析日期**: 2026-08-08（§4 核心模块详解于 2026-09-02 按代码实测状态重写） | **版本**: 1.1 | **许可**: MIT

---

## 目录

1. [项目概述](#1-项目概述)
2. [技术栈全景分析](#2-技术栈全景分析)
3. [系统架构设计](#3-系统架构设计)
4. [核心模块详解](#4-核心模块详解)
5. [系统运行流程](#5-系统运行流程)
6. [设计模式应用](#6-设计模式应用)
7. [数据流转](#7-数据流转)
8. [项目亮点深度分析](#8-项目亮点深度分析)
9. [项目优点](#9-项目优点)
10. [项目缺点与改进建议](#10-项目缺点与改进建议)
11. [部署架构](#11-部署架构)
12. [综合评价](#12-综合评价)

---

## 1. 项目概述

SmartCS-Agent 是一个**基于 FastAPI + LangGraph 的智能电商客服系统**，深度集成了 pgvector 向量检索（标准 RAG 管道）、混合检索、语义缓存、多轮对话管理等功能。项目面向智能家居电商场景，内置 10 款产品知识文档、1,800 条电商 FAQ 和 2,600+ 条真实客服对话数据。

**核心能力**: 场景+风险双维意图识别（单次合并输出，场景驱动分支、风险拦截优先）→ 混合检索（HNSW ∥ pg_jieba BM25 → RRF → Reranker 精排）→ 流式响应

---

## 2. 技术栈全景分析

### 2.1 技术栈总览

```mermaid
graph TB
    subgraph "前端层 Frontend"
        A[Vue.js 静态页面]
    end

    subgraph "API 网关层 Gateway"
        B[FastAPI<br/>异步 REST + SSE 流式]
    end

    subgraph "Agent 编排层 Orchestration"
        C[LangGraph StateGraph<br/>场景+风险双维意图识别 + 子图工作流]
    end

    subgraph "LLM 服务层"
        D1[DeepSeek V3<br/>主对话/推理/路由]
        D2[EmbeddingProvider<br/>qwen text-embedding-v4 API<br/>1024 维（local/ollama 分支保留）]
        D3[Qwen-VL (qwen-vl-max)<br/>图片分析 Vision]
    end

    subgraph "知识检索层"
        E1[pgvector 向量检索<br/>标准 RAG 管道]
        E3[混合检索 BM25 + 向量 + RRF]
    end

    subgraph "存储层"
        F1[(PostgreSQL 16 + pgvector<br/>用户/会话/消息/向量/检查点)]
        F2[(Redis 7<br/>语义缓存/摘要缓存)]
    end

    subgraph "基础设施"
        G[Docker Compose<br/>一键部署 + Healthcheck]
    end

    A --> B --> C --> D1
    C --> D2
    C --> D3
    C --> E1
    C --> E3
    B --> F1
    B --> F2
    G --> F1
    G --> F2
```

### 2.2 详细技术清单

| 层次 | 技术 | 版本 | 职责 |
|------|------|------|------|
| **后端框架** | FastAPI | 0.100+ | REST API，原生 async/await，SSE 流式响应 |
| **Agent 编排** | LangGraph | latest | StateGraph 多路由 Agent，PostgresSaver 会话检查点持久化 |
| **LLM 基座** | DeepSeek API | V3 | 对话生成、意图路由、推理 |
| **本地 LLM** | Ollama | - | 可切换的本地 LLM 替代方案 |
| **文档检索** | pgvector | 0.5+ | 向量表 document_chunks（HNSW 索引），标准 RAG 索引管道 |
| **Embedding** | EmbeddingProvider | qwen text-embedding-v4 | 统一向量接口（DashScope API，1024 维，默认 qwen；local/ollama 分支保留） |
| **Embedding 通道** | DashScope OpenAI-compatible API | text-embedding-v4 | 索引/检索/混合检索/语义缓存统一走 qwen API（aiohttp，无本地模型主链路） |
| **向量缓存** | Redis | 7 Alpine | 语义缓存（余弦相似度 ≥ 0.90 命中） |
| **精排** | sentence-transformers CrossEncoder | bge-reranker-v2-m3 | RRF 融合 top-20 → 精排 top-5（替代 LLM 相关性评分，GPU/fp16 可加速） |
| **全文检索** | PostgreSQL pg_jieba | jiebacfg 精确模式 | 数据库内 BM25（ts_rank_cd + GIN 倒排索引），替代应用内存 jieba + rank_bm25 |
| **关系数据库** | PostgreSQL | 16（pgvector 镜像） | 用户、会话、消息持久化 + 向量检索 + LangGraph 检查点 |
| **LLM SDK** | OpenAI SDK (AsyncOpenAI) | - | 兼容 DeepSeek API |
| **LangChain** | langchain-core/deepseek/ollama | - | LLM 抽象层，结构化输出 |
| **前端** | Vue | 编译静态 dist | 聊天 UI 界面（非主要重点） |
| **部署** | Docker + Docker Compose | - | 2 基础服务（PostgreSQL(pgvector)/Redis）；App 应用本地运行（uvicorn/run.py） |
| **搜索** | SerpAPI | - | Function Calling 联网搜索 |
| **图片处理** | Pillow (PIL) | - | 上传图片压缩/格式转换 |
| **异步 HTTP** | aiohttp | - | 视觉 API 异步调用 |

---

## 3. 系统架构设计

### 3.1 整体架构图

```mermaid
flowchart TB
    subgraph Client["🖥️ 客户端"]
        Browser["浏览器 Chat UI"]
    end

    subgraph FastAPI["⚡ FastAPI 服务层"]
        direction TB
        MW["LoggingMiddleware<br/>CORS Middleware"]
        LG["/api/langgraph/query<br/>Agent 多路由 SSE"]
        UPLOAD["/api/upload<br/>文件上传 + 索引"]
        AUTH["/api/register /api/token<br/>认证"]
        CONV["/api/conversations<br/>会话管理"]
    end

    subgraph Factory["🏭 LLM 工厂层"]
        LLMF["LLMFactory"]
        DS["DeepseekService<br/>+ 语义缓存"]
        OS["OllamaService"]
    end

    subgraph Agent["🤖 LangGraph Agent 层"]
        direction TB
        ROUTER["意图路由器<br/>场景+风险双维合并识别"]
        RISK["风险拦截/转人工节点<br/>静态话术"]
        GEN["闲聊节点<br/>general"]
        IMG["图片分析节点<br/>Vision API"]
        KG["知识库查询子图<br/>Multi-Tool Workflow（售前）"]
        PLACE["售后/投诉安抚占位节点<br/>业务 Agent 接口预留"]
    end

    subgraph KGTools["🔧 知识检索工具链"]
        direction LR
        GRAG["RAGRetrieverService<br/>HNSW ∥ pg_jieba BM25 并行<br/>RRF 融合"]
        RG["Reranker 精排<br/>bge-reranker-v2-m3"]
    end

    subgraph Store["💾 存储层"]
        PostgreSQL[(PostgreSQL+pgvector)]
        Redis[(Redis)]
    end

    Browser --> FastAPI
    FastAPI --> Factory
    Factory --> Agent
    ROUTER --> RISK & GEN & IMG & KG & PLACE
    KG --> KGTools
    KGTools --> Redis
    LLMF --> PostgreSQL
    LLMF --> Redis
```

### 3.2 项目目录结构

```
SmartCS-Agent/
├── llm_backend/                          # 后端主目录
│   ├── main.py                           # FastAPI 入口，所有 API 端点定义
│   ├── run.py                            # 服务启动脚本
│   └── app/
│       ├── core/                         # 核心配置层
│       │   ├── config.py                 # Pydantic Settings 配置（环境变量映射）
│       │   ├── database.py              # PostgreSQL 异步连接（psycopg）
│       │   ├── security.py              # JWT 认证
│       │   ├── hashing.py               # 密码哈希
│       │   ├── logger.py                # 结构化日志
│       │   └── middleware.py            # 请求日志中间件
│       ├── api/
│       │   └── auth.py                  # 认证路由（注册/登录）
│       ├── services/                     # 业务服务层
│       │   ├── llm_factory.py           # LLM 工厂模式
│       │   ├── deepseek_service.py      # DeepSeek + 语义缓存
│       │   ├── ollama_service.py        # Ollama 备选
│       │   ├── redis_semantic_cache.py  # Redis 语义缓存（asyncio + ZSET 索引 + 分级指代消解）
│       │   ├── pronoun_detector.py      # 指代检测器（三层规则引擎，缓存/入口共用门控）
│       │   ├── pronoun_resolver.py      # 指代消解器（LLM 补全，temperature=0，失败降级）
│       │   ├── rag_retriever_service.py # RAG 检索核心服务（HNSW ∥ BM25 并行 → RRF → 精排，唯一检索入口）
│       │   ├── reranker_service.py      # bge-reranker-v2-m3 精排（CrossEncoder，GPU/fp16，失败降级）
│       │   ├── rag_tool.py              # langchain @tool 薄封装（rag_retrieval）
│       │   ├── conversation_service.py  # 会话 CRUD
│       │   └── indexing_service.py      # 标准 RAG 索引构建（解析→分块→pgvector 入库）
│       ├── lg_agent/                     # LangGraph Agent 层
│       │   ├── lg_builder.py            # StateGraph 构建 + 路由 + 5 节点
│       │   ├── lg_states.py             # 状态定义（Router/AgentState）
│       │   ├── lg_prompts.py            # 15+ 提示词模板
│       │   └── kg_sub_graph/            # 知识库检索子图
│       │       ├── kg_tools_list.py     # 工具 Schema 定义
│       │       └── agentic_rag_agents/
│       │           ├── workflows/       # 多工具工作流
│       │           └── components/
│       │               ├── customer_tools/   # 检索节点（vector_search_query，调 RAGRetrieverService）
│       │               ├── hybrid_retrieval/ # bm25_sql_retriever.py（pg_jieba SQL BM25）+ rrf_fusion.py
│       │               ├── memory/         # 三层记忆管理器
│       │               ├── agent_safety/   # 护栏（Scope/Timeout）
│       │               └── planner/        # 任务分解
│       ├── models/                     # SQLAlchemy 模型
│       │   ├── conversation.py
│       │   ├── message.py
│       │   ├── user.py
│       │   └── document_chunk.py        # pgvector 文档块表
│       ├── prompts/                    # 搜索提示词
│       └── tools/                      # 搜索工具定义
├── scripts/                           # 工具脚本
│   ├── init_db.py                     # 数据库初始化（pgvector 扩展 + HNSW 索引）
│   ├── generate_product_knowledge.py  # CSV → 产品知识文档
│   ├── download_datasets.py           # 下载电商 FAQ 数据集
│   └── download_jddc.py              # 下载 JDDC 对话数据集
├── frontend/                          # Vue3 SFC 前端工程（由 chat.html 重构）
├── docker/                            # Docker 构建上下文
│   └── postgres/Dockerfile            # pgvector + pg_jieba 自定义镜像（cppjieba 源码编译）
├── docker-compose.yml                 # 2 基础服务编排（PostgreSQL 自定义镜像/Redis，App 本地运行）
├── Dockerfile                         # Python 3.13-slim 镜像（uv 安装锁定依赖）
├── pyproject.toml                     # Python 依赖清单（uv 管理）
├── uv.lock                            # 依赖锁定文件
├── .env.example                       # 环境变量模板（60+ 项配置）
└── .env.docker                        # Docker 环境变量
```

---

## 4. 核心模块详解（2026-09-02 按代码实测状态重写，总分结构）

> **写作口径**：本章按 2026-09-02 仓库源码逐文件核对 + `logs/` 现网日志抽查的**真实运行状态**撰写（意图澄清、指代消解/语义缓存入口链路、RAG 检索收敛、工具层三态协议均已落地）。讲解采用**总分结构**：先 §4.0 给出全链路总览图与模块清单（总），再按"入口 → 出口"的请求流向逐个模块展开（分）；复杂模块（§4.4 售前导购）先给模块总流程图，再在其下按子图粒度拆解（如 ragTool 检索运行流程）。与本报告 §2/§3/§5 中较早描述不一致之处，以本章为准。

### 4.0 模块总览（总）

SmartCS-Agent 的可执行程序只有一个：`llm_backend/` 下的 FastAPI 应用（`uvicorn main:app :8000`）。一次"用户提问 → AI 回复"的请求，按序穿过以下模块：

```mermaid
flowchart TB
    subgraph Client["客户端"]
        FE["Vue3 前端（frontend/dist 静态托管）"]
    end

    subgraph In["① 入口与 HTTP 网关（§4.1）"]
        API["FastAPI app<br/>main.py：中间件 + 全部端点"]
    end

    subgraph Pre["② 前置处理（§4.2）"]
        PREP["入口指代消解（规则门控→LLM 补全）<br/>+ 语义缓存检索（命中短路不进图）"]
    end

    subgraph Graph["③ LangGraph 主图（§4.3）"]
        ROUTER["analyze_and_route_query<br/>场景+风险双维意图识别<br/>ScopeGuard 预检 + 路由决策"]
    end

    subgraph Biz["④ 业务执行模块（§4.4/§4.5）"]
        PRESALE["售前导购：MultiTool 子图 + ragTool 检索（§4.4）"]
        NODES["业务应答节点：general / 风险拦截 / 转人工<br/>售后与投诉占位 / 澄清 / 图片（§4.5）"]
    end

    subgraph Out["⑤ LLM 服务与流式出口（§4.8）"]
        SSE["SSE 流式输出 + 语义缓存回写<br/>+ 前端会话落库"]
    end

    subgraph Sup["贯穿支撑"]
        MEM["三层记忆管理（§4.6）"]
        GUARD["安全护栏 ScopeGuard/TimeoutGuard（§4.7）"]
    end

    subgraph Store["存储"]
        PG[("PostgreSQL 16 + pgvector/pg_jieba<br/>document_chunks / product_price_stock<br/>conversations / messages / 检查点")]
        REDIS[("Redis 7<br/>语义缓存 vec/resp/meta+ZSET<br/>记忆摘要 memory:summary:*")]
    end

    FE -->|"POST /api/langgraph/query (multipart, SSE 响应)"| API
    API --> PREP
    PREP --> ROUTER
    ROUTER --> PRESALE & NODES
    MEM -.被识别/业务节点调用.-> ROUTER
    MEM -.被业务节点调用.-> NODES
    GUARD -.路由节点内.-> ROUTER
    PRESALE --> Out
    NODES --> Out
    Out --> FE
    PRESALE --> PG
    PRESALE --> REDIS
    API --> PG
    API --> REDIS
```

**模块清单**（与下文章节一一对应；"运行流程"列即该模块的运行流程图位置）：

| 阶段 | 模块 | 核心文件 | 运行流程 |
|------|------|---------|---------|
| ① 入口 | HTTP 网关与应用装配 | `llm_backend/main.py`、`run.py`、`app/core/middleware.py`、`app/core/config.py` | §4.1（应用启动 + 请求分发） |
| ② 前置 | 入口指代消解 + 语义缓存 | `main.py` 入口段、`app/services/pronoun_detector.py`、`pronoun_resolver.py`、`redis_semantic_cache.py` | §4.2（模块总图 + 检测/缓存子图） |
| ③ 主图 | 双维意图识别与路由 | `app/lg_agent/lg_builder.py`、`lg_states.py`、`lg_prompts.py`；检查点 `psycopg_pool` + `AsyncPostgresSaver` | §4.3（主图运行 + 识别节点/路由子图） |
| ④ 业务 | 售前导购（RAG 检索） | `lg_builder.py:create_research_plan`、`kg_sub_graph/.../workflows/multi_agent/multi_tool.py`、`edges.py`、`components/{planner,customer_tools,summarize,final_answer}/`、`app/services/rag_retriever_service.py`、`reranker_service.py` | §4.4（模块总图 → 子图① 工作流 → 子图② 工具层 → 子图③ ragTool @tool → 子图④ 混合检索管线） |
| ④ 业务 | 业务应答节点群 | `lg_builder.py` 各节点、`lg_prompts.py` 话术/模板 | §4.5 |
| ⑤ 出口 | LLM 服务与 SSE 流式出口 | `app/services/llm_factory.py`、`deepseek_service.py`、`ollama_service.py`；`main.py process_stream` | §4.8 |
| 支撑 | 三层记忆管理 | `components/memory/{memory_manager,memory_compressor,memory_cache,token_budget}.py` | §4.6 |
| 支撑 | 安全护栏 | `components/agent_safety/{scope_guard,safety_guards}.py` | §4.7 |
| 支撑 | 会话存储 / 认证 / 知识索引 / 商品数据 / 前端 | `conversation_service.py`、`api/auth.py`、`indexing_service.py` 等 | §4.9 |

### 4.1 入口与 HTTP 网关模块

**模块职责**：进程入口与请求面。`run.py` 负责启动（Windows 事件循环补丁），`main.py` 装配 FastAPI 应用、挂中间件与全部 HTTP 端点，`lifespan` 在启动时初始化 LangGraph 检查点连接池并编译主图（`_LazyGraph` 延迟代理）。

**运行流程图（启动装配 → 请求分发）**：

```mermaid
flowchart TB
    subgraph Startup["启动流程"]
        S1["python run.py<br/>（工作目录切到 llm_backend/）"] --> S2["Windows 补丁：uvicorn 事件循环<br/>Proactor → Selector（psycopg 异步兼容）"]
        S2 --> S3["uvicorn main:app<br/>host=0.0.0.0 port=8000 reload"]
        S3 --> S4["lifespan 启动 → init_checkpointer()"]
        S4 --> S5["AsyncConnectionPool 打开<br/>conninfo=POSTGRES_DSN（min1/max10）"]
        S5 --> S6["AsyncPostgresSaver.setup()<br/>（建检查点表）"]
        S6 --> S7["builder.compile(checkpointer)<br/>结果挂到 _LazyGraph 全局延迟代理"]
        S7 --> S8["挂静态前端 frontend/dist → '/'"]
    end

    subgraph Request["请求分发"]
        R1["HTTP 请求"] --> R2["LoggingMiddleware<br/>request_id（contextvar）+ 计时<br/>响应头 X-Request-ID"]
        R2 --> R3["CORSMiddleware allow_origins=*"]
        R3 --> R4{"端点路由"}
        R4 -->|"POST /api/langgraph/query"| R5["→ §4.2 前置处理（对话主链路）"]
        R4 -->|"POST /api/register /api/token<br/>GET /api/users/me"| R6["→ 认证路由（api/auth.py，§4.9.1）"]
        R4 -->|"POST /api/upload<br/>GET/DELETE /api/documents*"| R7["→ 知识索引入口（§4.9.2）"]
        R4 -->|"/api/conversations* 系列"| R8["→ 会话 CRUD（§4.9.1）"]
        R4 -->|"POST /api/upload/image"| R9["闲置端点（前端直传 langgraph/query 的 image 字段）"]
        R4 -->|"GET /（其余路径）"| R10["前端静态文件（未构建 dist 则 404）"]
    end
```

**关键实现点**：

- **全项目唯一进程**：根目录 `main.py` 仅是 pyproject 占位 stub；Docker Compose 只编排 PostgreSQL/Redis 两个基础服务，应用在宿主机以 `python run.py` 运行（`llm_backend/` 内，README §运行）。
- **启动期依赖**：LangGraph 检查点（PostgresSaver）必须在事件循环内构造，故连接池 + `setup()` + `compile()` 全部推迟到 `lifespan`；请求期经 `_LazyGraph` 代理首次访问即解析真实 graph，未初始化时抛明确错误。
- **中间件**：`LoggingMiddleware`（uuid → contextvar → 回写 `X-Request-ID`，结构化日志 `http_request`）；CORS 全放开。
- **端点鉴权现状**：除 `GET /api/users/me` 外全部端点**不校验 JWT**，`user_id` 由请求体/query 直接信任（缺陷记录见 §10）。
- **遗留死端点**：`POST /chat-rag`（无 `/api` 前缀）引用未定义的 `RAGChatService`，调用即 500——前端不调用，属历史残留。

### 4.2 前置处理模块：入口指代消解 + 语义缓存

**模块职责**：在主图执行前完成两件事——(1) 多轮指代/省略句补全为自包含完整问题；(2) 语义缓存检索，命中则短路返回不进图。模块由 `main.py` 的入口段 + `pronoun_detector` / `pronoun_resolver` / `redis_semantic_cache` 组成，消解器以鸭子类型依赖 LLM 服务（`DeepseekService`/`OllamaService` 均可）。

**模块总运行流程图**：

```mermaid
flowchart TD
    Q["用户消息到达 POST /api/langgraph/query"] --> TH{"aget_state(thread_id)<br/>有会话历史?"}
    TH -->|"有（多轮）"| HIST["取检查点 messages<br/>→ history_messages"]
    TH -->|"无（首条）"| HISTN["history_messages = []"]
    HIST --> DET["detect_pronoun(query)<br/>规则三层判定（毫秒级）"]
    HISTN --> DET
    DET -->|"NEED_RESOLVE 含指代/省略"| NEED{"有历史可参考?"}
    NEED -->|"是"| RES["LLM 消解 resolve_pronouns<br/>history+query 一次补全<br/>temp=0，超时(ms)/空→降级原句"]
    NEED -->|"否（首条即代词句）"| SKIP["跳过缓存检索<br/>（无完整问题可作 key）"]
    RES --> RECHECK["消解后重新 detect_pronoun"]
    RECHECK -->|"仍 NEED_RESOLVE（消解失败降级）"| SKIP
    RECHECK -->|"已完整"| LOOK
    DET -->|"PASS_THROUGH / SKIP_CACHE"| LOOK
    SKIP --> GRAPH["进入主图意图识别（§4.3）"]
    LOOK["cache.lookup(完整消息)<br/>① embedding ② 遍历 ZSET 索引算余弦"] -->|"≥ 阈值命中"| HIT["_stream_cached 模拟流式<br/>逐 4 字符/50ms 推 SSE<br/>X-Conversation-ID 响应头"]
    LOOK -->|"未命中"| GRAPH
    GRAPH -->|"图完整运行后"| WB{"非空回答 且<br/>非 NEED_RESOLVE?"}
    WB -->|"是"| UP["cache.update（lookup/update 同源消解）"]
    HIT --> SSE["SSE 出口（§4.8）"]
    UP --> SSE
```

**子图①：指代检测三层判定**（`pronoun_detector.detect_pronoun`，纯字符串规则、无 LLM）：

```mermaid
flowchart TD
    A["输入用户消息"] --> B{"纯语气词?<br/>（好的/谢谢/知道了…）"}
    B -->|"是"| C["SKIP_CACHE：不查不写缓存<br/>（RESOLVE_SKIP_FILLER 门控）"]
    B -->|"否"| N["“有那些”→“有哪些”归一<br/>（疑问词误写，防误判远指）"]
    N --> P{"命中指代词?<br/>这个/那个/它/该产品/上述…"}
    P -->|"是"| R["NEED_RESOLVE"]
    P -->|"否"| E{"短句（≤15 字）且以省略触发词开头?<br/>有货/多少钱/能退/怎么…"}
    E -->|"是"| R
    E -->|"否"| T["PASS_THROUGH：完整问题透传<br/>（~80% 消息零额外开销）"]
```

**子图②：语义缓存读写与索引维护**（`redis_semantic_cache.py`，key 均基于**消解后**消息 MD5）：

```mermaid
flowchart LR
    subgraph Keys["缓存条目（按用户隔离 prefix= cache:{user_id}）"]
        V["vec:{md5} 向量 JSON"]
        R["resp:{md5} 回复文本"]
        M["meta:{md5} 访问元数据"]
        Z["index：ZSET<br/>member=hash_id, score=last_access"]
    end
    subgraph Lookup["lookup 路径"]
        L1["消解后消息"] --> L2["qwen text-embedding-v4<br/>向量化"]
        L2 --> L3{"索引为空?"}
        L3 -->|"是"| L4["scan_iter 重建一次"]
        L3 -->|"否"| L5["遍历 index 全部 hash<br/>逐条 numpy 余弦比较"]
        L4 --> L5
        L5 --> L6{"最大余弦 ≥ 阈值?<br/>（REDIS_CACHE_THRESHOLD）"}
        L6 -->|"是"| L7["读 resp + 更新 meta/索引 score<br/>返回缓存文本"]
        L6 -->|"否"| L8["未命中"]
    end
    subgraph Update["update 路径（同源）"]
        U1["消解后消息 → md5 → 写 vec/resp/meta<br/>ZADD index（score=now）"]
        U2["TTL = REDIS_CACHE_EXPIRE"]
    end
    subgraph Clean["清理任务（实例池幂等启动）"]
        C1["每 REDIS_CACHE_CLEANUP_INTERVAL 检查<br/>条目 > MAX_SIZE → 按 last_access 升序删最旧"]
    end
```

**要点**：

- 两个独立实例池：语义缓存 `RedisSemanticCache.get_instance(prefix, user_id)` 按用户池化（每用户一个实例 + 一个清理任务）；记忆摘要缓存 `MemoryCache` 按会话（§4.6）。
- 分级门控把 LLM 消解调用控制在约 15% 含指代消息（日志佐证），温度 0 + 2s 超时（`.env` 建议 ≥15s）+ 失败降级原句；语气词既不消解也不写缓存。
- 缓存命中判定为**逐条线性余弦比较**（ZSET 仅提供条目清单与清理排序，非 ANN），命中后 `update_metadata` 刷新 score，支撑 LRU 淘汰。
- 向量化通道由 `EMBEDDING_TYPE` 决定：现网日志为 qwen text-embedding-v4（DashScope，1024 维 + L2 归一化），可切 ollama/local 兜底（`embed_in_batches` 承担索引侧分批重试）。
- 总开关：`SEMANTIC_CACHE_ENABLED=false`（查与写全关）、`RESOLVE_ENABLED=false`（退化无消解行为），用于调试一键回滚。

### 4.3 主图模块：LangGraph 双维意图识别与路由

**模块职责**：系统的调度中枢。`lg_builder.py` 定义 9 节点 StateGraph，单次 LLM 低温结构化输出同时判定**场景 type + 风险 risk** 两个维度（risk 拦截优先级最高），按结果条件路由到业务节点；会话状态由 PostgresSaver 检查点持久化（thread_id 维度）。

**模块运行流程图**（拓扑 + 请求期执行）：

```mermaid
flowchart TB
    subgraph Compile["构建期（模块加载）"]
        B1["StateGraph(AgentState, input=InputState)"]
        B2["add_node × 9：<br/>analyze_and_route_query / respond_to_general_query<br/>risk_intercept / transfer_human<br/>create_research_plan / create_image_query<br/>aftersale_placeholder / complaint_placeholder / clarify_node"]
        B1 --> B2
    end
    subgraph Serve["请求期"]
        E["graph.astream(InputState(messages=消解后query),<br/>stream_mode=messages, thread_config)"]
        E --> A["analyze_and_route_query<br/>（ScopeGuard → MemoryManager → LLM 双维识别）"]
        A --> R{"route_query 条件路由"}
        R -->|"risk=violation"| N1["risk_intercept（静态）"]
        R -->|"risk=high_risk"| N2["transfer_human（静态）"]
        R -->|"image_path 存在"| N3["create_image_query"]
        R -->|"type=clarify"| N4["clarify_node（LLM+模板兜底）"]
        R -->|"type=general"| N5["respond_to_general_query（LLM）"]
        R -->|"type=presale"| N6["create_research_plan → 售前子图（§4.4）"]
        R -->|"type=aftersale"| N7["aftersale_placeholder（静态）"]
        R -->|"type=complaint"| N8["complaint_placeholder（静态）"]
        R -->|"未知 type"| N9["raise ValueError（理论不可达）"]
        N1 --> CKPT["节点返回 → PostgresSaver 自动落检查点<br/>→ 下一轮 aget_state 恢复上下文"]
    end
```

**子图①：意图识别节点运行流程**（`analyze_and_route_query`）：

```mermaid
flowchart TD
    A["进入节点<br/>取 state.messages 最后一条"] --> B["ScopeGuard.check<br/>关键词+正则超范围预检（§4.7）"]
    B -->|"超范围"| BX["直接返回 Router(general, none,<br/>logic=超经营范围原因)<br/>——走 general 节点拒绝话术，零 LLM"]
    B -->|"通过"| C["选模型：AGENT_SERVICE<br/>DeepSeek / Ollama<br/>ROUTER_TEMPERATURE=0 + 禁用 thinking"]
    C --> D["MemoryManager.manage 压缩历史<br/>（含最近完整轮 + 摘要，§4.6）"]
    D --> E["[system ROUTER_SYSTEM_PROMPT] + 管理后消息"]
    E --> F["model.with_structured_output(Router)"]
    F -->|"成功"| G["Router{logic, type, risk}"]
    F -->|"校验失败/异常"| FALL["降级 Router(general, none,<br/>logic=结构化输出失败降级)，不抛异常"]
    G --> OUT["返回 {router: ...}"]
    FALL --> OUT
```

**子图②：路由决策流程**（`route_query`，risk 优先于场景；`image_path` 存在时图片优先于 type）：

```mermaid
flowchart TD
    A["route_query(state)"] --> R1{"risk == violation ?"}
    R1 -->|"是"| K1["risk_intercept"]
    R1 -->|"否"| R2{"risk == high_risk ?"}
    R2 -->|"是"| K2["transfer_human"]
    R2 -->|"否"| R3{"config 含 image_path ?"}
    R3 -->|"是"| K3["create_image_query"]
    R3 -->|"否"| R4{"router.type"}
    R4 -->|"clarify"| K4["clarify_node"]
    R4 -->|"general"| K5["respond_to_general_query"]
    R4 -->|"presale"| K6["create_research_plan"]
    R4 -->|"aftersale"| K7["aftersale_placeholder"]
    R4 -->|"complaint"| K8["complaint_placeholder"]
    R4 -->|"image"| K3
    R4 -->|"其他"| K9["ValueError"]
```

**Router 输出结构与路由表**（`lg_states.py`）：

| 维度 | 取值 | 说明 |
|------|------|------|
| `type` | presale / aftersale / complaint / general / image / clarify | 场景维度（6 类）；售后子场景（退货/物流/订单）不在识别层判定，下沉业务 Agent |
| `risk` | none / violation / high_risk | 风险维度；violation=违规拦截（解除限速/改装电池/越狱等），high_risk=转人工 |
| `logic` | str | 分类理由/次要意图，注入应答节点 prompt |

- 路由质量受控：golden set 46 条（单轮 41 + 多轮 5）二维准确率实测 46/46（`llm_backend/scripts/eval_intent_golden.py`，跑真实 Router 结构化输出）。
- 多轮上下文由检查点承载：`thread_id`（前端 `X-Conversation-ID` 回传）→ `aget_state` 恢复 → 追加本轮 → 节点执行后自动写回；`lg_agent/main.py` 提供同图 CLI 调试入口。
- 子图 `Send` map-reduce 与 PostgresSaver 序列化冲突（`Object of type Send is not JSON serializable`）已在售前节点规避（见 §4.4）。

### 4.4 售前导购模块：MultiTool 子图 + ragTool 检索工具链（总 — 分）

**模块职责**：承接 `type=presale`（商品参数/价格/推荐/使用咨询）。模块由三层组成：主图节点 `create_research_plan`（容器）→ **Multi-Tool 工作流子图**（planner → 并行检索 → summarize → final_answer 的 map-reduce 编排）→ **检索工具层**（向量检索节点直接消费 `RAGRetrieverService` 混合检索管线；另有两枚 langchain `@tool` 薄封装 `rag_retrieval` / `product_stock_lookup` 已按"动态查库 + 静态查知识"互补协议落地并单测，**生产链路当前未 bind_tools**，接入售前/售后 Agent 时即按 §4.4.3/§4.4.4 流程运行）。其中 ragTool 指向的**混合检索管线**运行流程最为复杂，按"总 → 分"拆解如下。

#### 4.4.1 模块总运行流程图（节点容器层）

```mermaid
flowchart TD
    A["路由 type=presale → 进入 create_research_plan"] --> B["模型选择 AGENT_SERVICE<br/>temperature=LLM_TEMPERATURE<br/>tags=['research_plan']（供出口过滤）"]
    B --> C["create_multi_tool_workflow(llm)<br/>每请求编译一次子图（无跨请求复用）"]
    C --> D["question = 末条消息<br/>（入口已前置消解，此处直接取用）"]
    D --> E["input_state = {question, data:[], history:[]}"]
    E --> F["TimeoutGuard(30s).wrap(<br/>multi_tool_workflow.ainvoke(input,<br/>config={'__pregel_checkpointer': None})）"]
    F -->|"正常返回"| G["answer → AIMessage 返回主图<br/>→ SSE 出口（§4.8）"]
    F -->|"asyncio.wait_for 超时"| H["降级话术：<br/>「抱歉，系统处理超时，请稍后再试。」"]
    G --> END2["（子图结果不入检查点：规避 Send 序列化 Bug；<br/>会话记忆由主图检查点承担）"]
    H --> END2
```

#### 4.4.2 子图①：Multi-Tool 工作流运行流程

```mermaid
flowchart TD
    START["START"] --> P["Planner 节点<br/>PLANNER_SYSTEM_PROMPT + llm.with_structured_output(PlannerOutput)"]
    P -->|"输出 tasks: Task[]（分解失败→回退单任务=原问题）"| MAP["map_reduce_planner_to_customer_tools<br/>返回 List[Send]"]
    MAP -->|"Send × N 并行"| VS["customer_tools 节点 ×N<br/>（vector_search_query）<br/>每个子任务一次检索"]
    VS --> SZ["summarize 节点<br/>prompt(电商客服风格) \| llm \| StrOutputParser<br/>以全部 records 为事实生成客服话术"]
    SZ -->|"summary"| FA["final_answer 节点（纯透传，无 LLM）<br/>answer=summary，构造 history_record"]
    FA -->|"answer"| OUT["END（OutputState: answer/question/…）"]

    style VS fill:#fff3cd
```

- **planner**：问题拆分为独立子任务（规则：不依赖/去重/合并相互依赖），并行度即任务数。
- **检索节点**（`customer_tools/node.py`）：每个子任务调一次 `RAGRetrieverService.search(task)`，结果以 `records.result`（拼接文本）与 `records.hybrid_docs` 进 `searches` 状态（`Annotated add` 聚合）。
- **summarize**：同一模型实例（tags=`research_plan`）；要求仅基于检索事实、不道歉、不用"根据系统"机械表达、亲和口吻（亲～/emoji）。
- 无结果时 `summary="No data to summarize."`，final_answer 原样透传（无显式兜底话术，属已知边界）。

#### 4.4.3 子图②：检索工具层现状（生产节点直连 + 两枚 @tool 待接入）

| 检索入口 | 位置 | 现状 |
|---------|------|------|
| `vector_search_query` 节点 | `customer_tools/node.py` | **生产在用**（§4.4.2 子图的检索环节），直连 `RAGRetrieverService.search()`，无三态协议，异常记 errors 列表 |
| `rag_retrieval`（@tool） | `app/tools/rag_tool.py` | 已落地 + 三态协议 + 单测（`tests/test_rag_tool.py`），**生产未 bind**（全仓无 `bind_tools` 调用） |
| `product_stock_lookup`（@tool） | `app/tools/product_stock_tool.py` | 同上（`tests/test_product_stock_tool.py`）；数据源 `product_price_stock` 表（§4.9.3） |

两枚 @tool 的目标用法（`llm.bind_tools([rag_retrieval, product_stock_lookup])` 的 LLM 工具编排、降级链与 SKU 对齐演进）见 `docs/spec_plan/已完成/SPEC_RAG_TOOL_OPTIMIZATION.md` 与 `SPEC_PRODUCT_STOCK_TOOL.md`——工具已就绪，Agent 编排层待接入。

#### 4.4.4 子图③：ragTool（`rag_retrieval` @tool）运行流程

```mermaid
flowchart TD
    A["LLM/调用方 ainvoke rag_retrieval(query)"] --> B{"入参校验<br/>query 空/纯空白?"}
    B -->|"是"| E1["返回 error：invalid_argument<br/>retryable=true（修正参数后可重试）<br/>并提示生成检索问题或转澄清"]
    B -->|"否"| C["_search_with_retry(query.strip())"]
    C --> D{"asyncio.wait_for 超时保护<br/>TOOL_DB_TIMEOUT_SECONDS=10s"}
    D -->|"瞬时错误<br/>(db_timeout/db_connection/api_unavailable)"| R1{"自动重试 ≤ TOOL_RETRY_TIMES<br/>间隔 0.5s"}
    R1 -->|"重试仍失败"| E2["返回 error（分类类型）<br/>retryable=false<br/>提示暂不可用/转人工，禁编造"]
    D -->|"永久错误(db_config/unknown)"| E2
    R1 -->|"重试成功"| OK
    D -->|"成功"| OK["docs = RAGRetrieverService.search(query)<br/>（混合检索管线，见子图④）"]
    OK --> EMPTY{"docs 为空?"}
    EMPTY -->|"是"| E3["返回空结果提示<br/>+ 可执行建议（换措辞/转 stock/澄清）"]
    EMPTY -->|"否"| OUT["拼接：每段【来源:文件名】前缀 + 正文<br/>以换行分隔返回（LLM 直接消费）"]
```

#### 4.4.5 子图④：ragTool 核心——RAGRetrieverService 混合检索管线运行流程

生产检索节点与 `rag_retrieval` @tool 共用同一入口 `RAGRetrieverService.search(query)`，唯一检索入口（门面）。内部结构：

```mermaid
flowchart TB
    Q["search(query)<br/>retrieval_top_n=HYBRID_RETRIEVAL_TOP_N(20)<br/>top_k=RERANKER_TOP_K(5)/兜底 5"] --> P1["① asyncio.gather 两路并行<br/>（每路 _safe 包装：单路失败降级为空，不阻塞融合）"]
    P1 --> V["向量路 _vector_search：<br/>embed([query]) qwen text-embedding-v4<br/>→ 全零向量则跳过该路<br/>→ DocumentChunk.embedding<br/>cosine_distance ORDER BY + LIMIT 20<br/>（触发 HNSW ANN）"]
    P1 --> B["BM25 路 bm25.search：<br/>pg_jieba 双配置分词并集 OR 语义<br/>（精确模式 ∪ 单字兜底）→ content_tsv @@ tsq<br/>走 GIN 倒排 → ts_rank_cd 排序<br/>LIMIT BM25_TOP_K(20)"]
    V --> F["② RRF 融合 rrf_fuse<br/>按 chunk_id 去重<br/>score=Σ1/(60+rank)<br/>取 RRF_TOP_K(20)"]
    B --> F
    F --> E{"RERANKER_ENABLED<br/>且融合结果非空?"}
    E -->|"是"| RE["③ RerankerService.rerank<br/>asyncio.to_thread 线程池（防阻塞事件循环）<br/>CrossEncoder bge-reranker-v2-m3<br/>cuda + fp16 / cpu 自动<br/>max_length 512, batch 8<br/>对 top-20 打相关分 → top-5"]
    E -->|"否"| D1["直接用 fused[:top_k]"]
    RE -->|"模型加载/评分失败返回 None"| D1
    RE -->|"成功"| OUT2["返回 top-5 docs<br/>（id/text/source/chunk_id/…<br/>+rrf_score[/rerank_score]）"]
    D1 --> OUT2
    OUT2 --> CONSUMER["消费方：<br/>vector_search_query 节点（生产）<br/>/ rag_retrieval @tool（待接入）"]
```

**关键参数与降级设计**：

| 环节 | 参数（config 默认） | 降级 |
|------|---------------------|------|
| 向量候选 | `HYBRID_RETRIEVAL_TOP_N=20` | Embedding API 失败 → 该路空 |
| BM25 候选 | `BM25_TOP_K=20` | SQL 异常 → 该路空 |
| 融合 | `RRF_TOP_K=20`、`RRF_FUSION_K=60`，去重键 `chunk_id`（内容确定性，跨环境稳定） | 仅成功路参与融合 |
| 精排 | `RERANKER_TOP_K=5`；模型 `BAAI/bge-reranker-v2-m3`（本地权重 `llm_backend/models/`，logs 佐证 cuda+fp16 实测加载） | 开关关/加载失败 → 融合 top-5 |

### 4.5 业务应答节点模块群

**模块职责**：路由表命中的各终端应答节点（§4.3 路由的落点）。先总后分：LLM 型节点（general/clarify/image）与静态话术节点（risk/转人工/售后/投诉占位）两大类。

| 节点 | 触发 | 走 LLM? | 输出 |
|------|------|--------|------|
| `respond_to_general_query` | type=general（含 ScopeGuard 超范围降级） | ✅ | 闲聊/超范围拒绝话术 |
| `risk_intercept` | risk=violation | ❌ | 静态：违规拒绝 + 合规引导（对应福客 D5） |
| `transfer_human` | risk=high_risk | ❌ | 静态：无法在线处理，转人工复核（对应福客 D3/D4） |
| `aftersale_placeholder` / `complaint_placeholder` | type=aftersale / complaint | ❌ | 静态："服务升级中" / 安抚话术（接口预留，子图就位仅换路由目的地） |
| `clarify_node` | type=clarify | ✅（失败模板兜底） | 电商统一风格针对性澄清（一次一问） |
| `create_image_query` | image_path 存在或 type=image | ✅（Vision API 先行） | 图片内容客服回复 |

**general 节点运行流程**：

```mermaid
flowchart TD
    A["进入 respond_to_general_query"] --> B["模型：AGENT_SERVICE<br/>LLM_TEMPERATURE=0.7<br/>tags=['general_query']"]
    B --> C["system = GENERAL_QUERY_SYSTEM_PROMPT.format(<br/>logic=router.logic)<br/>——超范围时注入 logic 触发拒绝话术"]
    C --> D["MemoryManager.manage 历史压缩（§4.6）"]
    D --> E["LLM 生成闲聊回复"]
    E --> F["{messages: [AIMessage]} → SSE"]
```

**clarify 节点运行流程**（电商"亲～"风格，一次只问一个）：

```mermaid
flowchart TD
    A["进入 clarify_node"] --> B["system = CLARIFY_SYSTEM_PROMPT.format(logic)<br/>结合用户原话 + router logic 困惑点"]
    B --> C["MemoryManager 历史管理"]
    C --> D["LLM 生成澄清问题"]
    D -->|"内容非空"| OK["{messages: [AIMessage]}"]
    D -->|"空/异常/超时"| FB["静态模板兜底<br/>CLARIFY_FALLBACK_REPLY"]
    OK --> SSE["→ SSE"]
    FB --> SSE
```

**image 节点运行流程**（PIL 压缩 → Qwen-VL → 客服 LLM 二次生成）：

```mermaid
flowchart TD
    A["进入 create_image_query"] --> B{"image_path 存在?"}
    B -->|"否"| AP["返回道歉文案"]
    B -->|"是"| C{"VISION_API_KEY/BASE_URL/MODEL 完整?"}
    C -->|"否"| AP
    C -->|"是"| D["PIL 打开 → LANCZOS 等比缩至最长边 ≤1024<br/>→ JPEG quality=85 → base64"]
    D --> E["aiohttp POST {VISION_BASE_URL}/chat/completions<br/>VISION_MODEL（qwen-vl 系），max_tokens=VISION_MAX_TOKENS"]
    E -->|"非 200/异常"| AP
    E -->|"200"| F["image_description → GET_IMAGE_SYSTEM_PROMPT.format()"]
    F --> G["AGENT_SERVICE LLM 结合图片描述 + 历史生成客服回复"]
    AP --> OUT2["{messages:[AIMessage]} → SSE"]
    G --> OUT2
```

**静态话术节点组运行流程**（risk_intercept / transfer_human / aftersale_placeholder / complaint_placeholder，共用模式）：

```mermaid
flowchart TD
    E1["risk=violation"] --> N1["risk_intercept"]
    E2["risk=high_risk"] --> N2["transfer_human"]
    E3["type=aftersale"] --> N3["aftersale_placeholder"]
    E4["type=complaint"] --> N4["complaint_placeholder"]
    N1 --> T1["静态话术常量<br/>RISK_INTERCEPT_REPLY"]
    N2 --> T2["静态话术常量<br/>TRANSFER_HUMAN_REPLY"]
    N3 --> T3["AFTERSALE_PLACEHOLDER_REPLY<br/>（服务升级中提示）"]
    N4 --> T4["COMPLAINT_PLACEHOLDER_REPLY<br/>（安抚话术）"]
    T1 --> M["{messages: [AIMessage(content=话术)]}<br/>不走 LLM、无外部调用 → SSE"]
    T2 --> M
    T3 --> M
    T4 --> M
```

### 4.6 记忆管理模块（三层压缩 + Redis 增量缓存）

**模块职责**：被识别节点与各 LLM 业务节点调用（`MemoryManager(llm, cache=MemoryCache())` 每次调用新建），把完整 `state.messages` 压缩到 token 预算内：最近 N 轮原文 + 老消息两级摘要。

**模块总运行流程图**（`MemoryManager.manage`）：

```mermaid
flowchart TD
    A["manage(messages, system_prompt, conversation_id)"] --> B["按 (Human, AI) 配成轮次<br/>跳过 System，容忍不规则"]
    B --> C{"Redis 有会话摘要?<br/>memory:summary:{conversation_id}"}
    C -->|"命中"| D["还原 high/medium 摘要<br/>记录 compressed_turns 起点"]
    C -->|"未命中"| E["摘要为空，起点 0"]
    D --> F["分层：recent = 最近 5 轮<br/>older = 其余"]
    E --> F
    F --> G{"older 非空 且 有新增轮次?"}
    G -->|"是（增量压缩）"| H1{"older 前段（medium_start 之前）"}
    H1 -->|"有"| HI["compress_high：高层摘要 ≤100 字<br/>+ 关键实体（携带上次摘要续压）"]
    H1 -->|"无"| MI
    G -->|"否（无 older / 无新增）"| SKIP2["跳过压缩<br/>（摘要直接用 Redis 缓存结果）"]
    HI --> MI["compress_medium：<br/>最近 10 轮（6~15）压缩为 ≤200 字<br/>+ 关键实体/用户意图"]
    SKIP2 --> S
    MI --> S["拼摘要文本：[历史摘要]+[关键实体]<br/>[近期摘要]+[相关实体]"]
    S --> T["Token 预算检查<br/>history_summary > 800 → 按句子裁剪<br/>recent > 2000 → 从最老倒序裁到预算内"]
    T --> O["输出：[SystemMessage(摘要)] + 最近完整消息"]
    O --> W{"conversation_id 且 older 非空?"}
    W -->|"是"| SV["save_summary 写 Redis<br/>compressed_turns=len(older) TTL 24h"]
    W -->|"否"| RT["返回消息列表给节点"]
    SV --> RT
```

**分层与预算（代码实测值）**：

| 层 | 范围 | 处理 | 预算 |
|----|------|------|------|
| 第三层（高） | 最早轮次 | LLM 压缩 ≤100 字 + 关键实体 | history_summary 合计 800 |
| 第二层（中） | 窗口外最近 10 轮（约 6~15） | LLM 压缩 ≤200 字 + 实体/意图 | （同上） |
| 第一层（近） | 最近 5 轮完整原文 | 仅按需裁剪（最老优先丢弃） | recent_history 2000 |
| — | system prompt / 检索文档 | 固定占用 | 500 / 4000（预留回答 700，总 8000） |

**要点**：增量压缩以 `compressed_turns` 为起点只压新增轮次（省 LLM 调用）；高层压缩携带上次摘要续压；无 Redis 时内存字段兜底（`_high_summary/_medium_summary`）；TTL `MEMORY_CACHE_TTL=86400`。

### 4.7 安全护栏模块

| 护栏 | 位置 | 机制 | 效果 |
|------|------|------|------|
| **ScopeGuard** | `analyze_and_route_query` 第一步（§4.3 子图①） | 关键词 + 正则零延迟预检 | 超经营范围消息**不走 LLM**，直接降级 general 拒绝话术 |
| **TimeoutGuard** | `create_research_plan` 包售前子图 | `asyncio.wait_for(30s)` | 超时返回降级话术，不阻塞会话 |

**ScopeGuard 判定流程**（与路由层 risk 维度互补：规则先拦、LLM 精确判）：

```mermaid
flowchart TD
    A["用户问题"] --> B{"命中超范围关键词?<br/>服装/食品/医药/汽车房产/金融/违禁…"}
    B -->|"是"| X["拒绝：Router(general,<br/>logic=超出经营范围(关键词))"]
    B -->|"否"| C{"命中超范围正则?<br/>(买|推荐).(衣服…)<br/>(有|卖).(车|房|股票…)"}
    C -->|"是"| Y["拒绝同上"]
    C -->|"否"（不确定）"| Z["放行 → LLM risk 维度精确判断"]
```

**TimeoutGuard 运行流程**：`wrap(coro, fallback)` → `asyncio.wait_for` 计时 → 超时记日志（含 conversation_id）返回 fallback；未超时原样返回。设计依据：`docs/spec_plan/已完成/SPEC_REMOVE_QUERY_PREPROCESSING.md`（查询预处理管道与 BudgetGuard 已随子图简化移除，事实性保障由 Reranker + 生成 LLM 承担）。

### 4.8 LLM 服务与流式出口模块

**模块职责**：(1) `LLMFactory` 按 `CHAT_SERVICE` 提供 `generate/generate_stream` 鸭子类型服务（入口消解、语义缓存、旧 RAG 聊天链路使用）；(2) LangGraph 主图以 langchain `ChatDeepSeek`/`ChatOllama` 实例驱动节点；(3) `main.py process_stream` 把图的 `messages` 流过滤后 SSE 推送，图结束后回写语义缓存。

**LLM 服务选择**：

| 用途 | 实例化方式 | 说明 |
|------|-----------|------|
| 节点/子图推理 | `lg_builder` 内按 `AGENT_SERVICE` new `ChatDeepSeek`/`ChatOllama` | 各节点 tags 区分（router/general_query/clarify/image/research_plan），DeepSeek 侧 `extra_body thinking=disabled` |
| 路由识别 | 同上，`ROUTER_TEMPERATURE=0` | 结构化输出 |
| 入口消解/缓存消解 | `LLMFactory.create_chat_service()` → `DeepseekService`/`OllamaService` `.generate()` | 鸭子类型注入 `resolve_llm` |
| 视觉 | 独立 `VISION_*` 配置的 qwen-vl API（`aiohttp` 直连，§4.5 image） | 不经 LLMFactory |

**SSE 出口与缓存回写运行流程**（`main.py`）：

```mermaid
flowchart TD
    A["graph.astream(stream_mode='messages')"] --> B["逐 (chunk, metadata) 处理"]
    B --> F1{"chunk.content 非空<br/>且 tags 不含 research_plan<br/>且无 tool_calls?"}
    F1 -->|"是"| P["SSE 推送 data: {JSON content}"]
    F1 -->|"tool_calls"| L["仅 debug 日志（子图规划中间件）"]
    F1 -->|"research_plan 标记"| DROP["丢弃（子图 planner/summarize 中间 token）"]
    P --> N{"流结束"}
    N -->|"是"| C1{"决策非 NEED_RESOLVE<br/>且 complete_response 非空?"}
    C1 -->|"是"| WB["cache.update 回写语义缓存<br/>（key=消解后消息，与 lookup 同源）"]
    C1 -->|"否（空回答/含指代）"| RET
    N --> HDR["响应头 X-Conversation-ID = thread_id"]
    WB --> RET["SSE 全量返回（前端 §4.9.4）"]
    HDR --> RET
```

**缓存命中时的出口**（不进图）：`_stream_cached` 每 4 字符/50ms 模拟流式，前端体验与真流式一致。

**对话落库**：LangGraph 路径的消息持久化由**前端**在流结束后调用 `POST /api/conversations/save-messages`（服务端 `ConversationService.save_message`，注意硬编码 `user_id=0`）；DB 的 `conversations`（int id）与 LangGraph `thread_id`（uuid）是两套会话标识，靠前端维护对应关系（§4.9.1/§4.9.4）。

### 4.9 支撑模块群

#### 4.9.1 会话存储与认证模块

- **ConversationService**（`app/services/conversation_service.py`，全静态方法）：`create_conversation`（初始"新会话"+`dialogue_type=NORMAL`）、`save_message`（按会话追加 user/assistant 两条 Message；`messages_count==0` 时用首条用户消息自动生成 ≤20 字标题）、`get_user_conversations`（倒序）、`get_conversation_messages`（归属校验）、`delete_conversation`、`update_conversation_name`。`DialogueType`：NORMAL / DEEP_THINKING / WEB_SEARCH / RAG。运行上属简单的表 CRUD，**持久化的"运行流程"即前端 §4.8 出口图落库一步**。
- **认证**（`api/auth.py` + `core/{security,hashing}.py`）：`POST /api/register`（email/username 双唯一，冲突 400）、`POST /api/token`（bcrypt 校验，JWT HS256，payload sub=email）、`GET /api/users/me`（唯一带 `Depends(get_current_user)` 的端点）。运行流程为常规 JWT 签发/校验，**注意**：除 users/me 外业务端点全部不校验令牌（现状缺陷，见 §10.1/§10.3.5）；OpenAPI `tokenUrl="/token"` 与真实 `/api/token` 不一致。

#### 4.9.2 知识库索引模块（docx → document_chunks）

**模块职责**：把上传文件/`knowledge_data/` 目录解析为分块 + 向量 + BM25 索引入库（`indexing_service.py` + `doc_parser.py` + `text_cleaner.py` + `mineru_client.py`），构成 §4.4.5 检索管线的语料侧。生产商品文档由 `scripts/build_smart_furniture_docx.py` / `build_jd_aftersales_docx.py` 从 TSV/政策源生成（知识分层：价格库存等动态信息不进 docx，见 §4.9.3）。

**运行流程图**：

```mermaid
flowchart TD
    A["POST /api/upload 或 ingest_knowledge.py 目录遍历"] --> B{"扩展名白名单 txt/md/pdf/docx<br/>且 ≤ MAX_FILE_SIZE_MB=30<br/>且非空?"}
    B -->|"否"| E1["HTTP 400（unsupported/too_large/empty_file）<br/>不落任何 DB 记录"]
    B -->|"是"| C["MD5 查重（(user_id, md5) 快路径）"]
    C -->|"已存在"| E2["status=duplicate（幂等跳过）"]
    C -->|"新文件"| D["解析分派："]
    D -->|"txt/md"| P1["parse_text_file（utf-8→gbk→…编码降级）"]
    D -->|"docx"| P2["parse_docx：段落+表格按文档序<br/>章节栈规则 → Segment(text, chapter)"]
    D -->|"pdf"| P3["MinerU 云端 v4：提交→轮询(3s/≤300s)<br/>→ 下载 markdown → parse_md 按 # 标题切章节"]
    P1 --> CL["清洗 clean_text（TEXT_CLEAN_ENABLED）<br/>控制字符/页码/目录行；不删裸数字行"]
    P2 --> CL
    P3 --> CL
    CL --> S["RecursiveCharacterTextSplitter<br/>chunk 500 / overlap 50<br/>中文句读分隔符；<5 字符短块过滤"]
    S --> EM["embed_in_batches（≤10 条/批）<br/>qwen text-embedding-v4<br/>批失败指数退避重试（[1,2,4]s ×3）<br/>全零 = 失败"]
    EM --> DB["单事务写 documents + chunks<br/>chunk_id={user_id}_{md5}_{index:04d}"]
    DB --> RDY["检索侧自动就绪：<br/>content_tsv 生成列（jiebacfg 分词，INSERT 自动维护）<br/>+ 预建 HNSW / GIN / 唯一索引（init_db.py 幂等建，不再 drop_all）"]
```

#### 4.9.3 商品动态数据模块（product_price_stock + product_stock_lookup）

**知识分层**：价格/库存属动态信息，只存表不写 docx。`llm_backend/scripts/import_product_price_stock.py` 从 `scripts/data/jd_smart_furniture.tsv` 按 `product_name` 幂等 upsert（范围价取均值、0=无货）；表结构 `product_name`(唯一) / `category` / `current_price`(Numeric) / `stock_quantity`(Integer) / `updated_at`。

**product_stock_lookup @tool 运行流程**（与 rag_retrieval 同为三态协议，现状=落地+单测，待 Agent bind）：

```mermaid
flowchart TD
    A["ainvoke(product_name, category?, limit=5)"] --> B{"product_name 空?"}
    B -->|"是"| E1["error invalid_argument retryable=true<br/>提示提取商品名/向用户澄清"]
    B -->|"否"| C["参数预处理：去全部空格<br/>+ 转义 %/_（防注入式全表匹配）<br/>limit 钳制 1~20"]
    C --> D["SQLAlchemy 查询：<br/>func.replace(name,' ','') ILIKE '%kw%' ESCAPE<br/>+ category 过滤 + updated_at DESC + limit"]
    D --> G{"wait_for(10s) + 瞬时错误自动重试<br/>(db_timeout/db_connection)"}
    G -->|"重试仍失败"| E2["error（分类）retryable=false<br/>提示暂不可用，禁编造价格"]
    D -->|"永久错误(db_config/unknown)"| E2
    G -->|"成功且 0 行"| E3["empty：未找到 + 建议<br/>（换词/静态信息转 rag_retrieval/澄清）"]
    G -->|"成功且有行"| OK["ok JSON：{status, count, data:<br/>[{product_name, category,<br/>current_price, stock_quantity, updated_at}]}"]
```

**意图语义**：泛查询（"有哪些 XX/卖什么"）`limit=20` 全量清单，单商品详情默认 5——LLM 编排层按查询意图取值（设计约定，随 Agent 接入生效）。

#### 4.9.4 前端模块（出口接收端）

Vue3 SFC 工程（`frontend/`，dev 由 vite 代理 `/api` → `:8000`；生产由后端将 `frontend/dist` 静态挂载于 `/`——dist 未构建则 404）。主聊天单页：登录门 → Sidebar（会话 CRUD）→ ChatArea → DocsPanel。

**SSE 流接收运行流程**（`composables/useChat.js`）：

```mermaid
flowchart TD
    A["sendMessage(content, image)"] --> B["无会话则先建 DB 会话<br/>（conversations 表 int id）"]
    B --> C["POST /api/langgraph/query（multipart）<br/>conversation_id = 上次 X-Conversation-ID<br/>（首轮为空 → 后端生成 uuid thread）"]
    C --> D["读响应头 X-Conversation-ID<br/>→ 存入 langgraphConversationId 复用"]
    D --> E["fetch body reader 解析 SSE data: 行"]
    E --> F{"parsed.interruption?"}
    F -->|"是"| F1["保留 conversation_id，提示等待确认（预留分支）"]
    F -->|"否"| G["字符串/数组/文本片段 → 追加 aiContent<br/>（Vue 响应式替换）"]
    G --> H{"流结束"}
    H -->|"是"| I["POST /api/conversations/save-messages<br/>（DB 会话落库本轮 user+assistant）"]
    F1 --> E
```

**双会话标识**：DB `conversations.id`（列表/历史/落库）与 LangGraph `thread_id`（图状态检查点）分离，由前端桥接。图片随消息以 `image` 字段直传 langgraph/query（`/api/upload/image` 端点闲置）。调用端点矩阵见 §4.1 分发图（auth/conversations/documents/upload 全套已接入）。

---

## 5. 系统运行流程

### 5.1 完整请求生命周期

```mermaid
flowchart TD
    C["客户端请求<br/>POST /api/langgraph/query"]
    C --> API["FastAPI 入口<br/>检查 thread_id"]
    API -->|"无 thread_id"| NEW["新会话<br/>生成 thread_id + InputState"]
    API -->|"已有 thread_id"| CTD["多轮会话<br/>PostgresSaver 加载检查点"]
    API -->|"存在中断"| RSM["中断恢复<br/>Command(resume) 继续人工确认"]
    NEW --> RESOLVE["入口前置指代消解<br/>规则门控 → LLM 补全<br/>（图执行前完成）"]
    CTD --> RESOLVE
    RSM --> RESOLVE

    RESOLVE --> CACHE{"语义缓存检索<br/>key=消解后消息<br/>按 user_id 隔离"}
    CACHE -->|"命中"| HIT["⚡ 短路返回缓存回答<br/>模拟流式 SSE（不进图）"]
    CACHE -->|"未命中"| STREAM["graph.astream<br/>stream_mode=messages"]
    HIT --> SSE

    STREAM --> SG["analyze_and_route_query<br/>ScopeGuard 关键词预检"]
    SG -->|"不通过"| GQ["general 闲聊节点<br/>超经营范围拒绝话术"]
    SG -->|"通过"| RT["LLM 路由器<br/>场景+风险双维识别：type + risk"]

    RT -->|"risk=violation"| RISK["5.4 风险拦截<br/>违规拒绝话术"]
    RT -->|"risk=high_risk"| TRANS["5.4 转人工<br/>无法在线处理话术"]
    RT -->|"type=general"| GEN["5.3 general 闲聊<br/>纯 LLM 闲聊"]
    RT -->|"type=presale"| KG["5.5 presale 售前<br/>向量检索子图"]
    RT -->|"type=aftersale"| PLACE["5.7 售后占位<br/>接口预留"]
    RT -->|"type=complaint"| PLACE2["5.7 投诉安抚占位<br/>接口预留"]
    RT -->|"type=clarify"| CLARIFY["5.8 意图澄清<br/>电商风格询问"]
    RT -->|"type=image"| IMG["5.6 image 图片<br/>Qwen-VL + LLM"]

    GEN --> SSE["SSE 流式返回<br/>逐 chunk 推送 data: {content}"]
    RISK --> SSE
    TRANS --> SSE
    KG --> SSE
    PLACE --> SSE
    PLACE2 --> SSE
    CLARIFY --> SSE
    IMG --> SSE
    GQ --> SSE
    KG -.->|"完整回答生成后"| WRITEBACK["语义缓存回写<br/>update（非空才写）"]
```

**关键说明**:

1. **入口三态**：同一端点根据 thread_id 区分新会话 / 多轮续聊 / 中断恢复（human-in-the-loop）
2. **入口前置指代消解**：多轮代词/省略（"那个有货吗"）在 `graph.astream` 前完成改写（规则门控 + LLM 补全），首条消息无历史直接透传；图内意图识别与检索拿到的均为完整问题
3. **入口语义缓存检索**：消解后、进图前按 user_id 查缓存（key=消解后消息），命中短路返回不进图；未命中走图，完整回答生成后回写（非空才写，语气词/失败不写）；含指代且无历史可消解时跳过缓存检索
4. **状态持久化**：PostgresSaver 检查点在每个节点执行后自动写入 PostgreSQL，服务重启不丢失
5. **流式输出**：`stream_mode="messages"` 让每个 AIMessage chunk 实时推送给前端
6. 对话记录落库（conversations/messages 表）由前端经 `/api/conversations/save-messages` 接口保存，langgraph 路径的会话状态由检查点承载

### 5.2 意图路由决策流程

```mermaid
flowchart TD
    Q["用户输入"]
    RESOLVE["入口指代消解<br/>规则门控 → LLM 补全<br/>（图执行前完成）"]
    CACHE{"语义缓存检索<br/>key=消解后消息"}
    SG["ScopeGuard<br/>关键词预检"]
    Q --> RESOLVE
    RESOLVE --> CACHE
    CACHE -->|"命中"| SHORT["⚡ 短路返回缓存回答<br/>不进图"]
    CACHE -->|"未命中"| SG
    SG -->|"不通过"| GEN["general 闲聊节点<br/>超经营范围拒绝话术"]
    SG -->|"通过"| ROUTER["LLM 路由器<br/>场景+风险双维合并识别<br/>type + risk<br/>（ROUTER_TEMPERATURE=0）"]

    ROUTER -->|"risk=violation"| RISK["风险拦截<br/>明确拒绝 + 合规引导"]
    ROUTER -->|"risk=high_risk"| TRANS["转人工<br/>无法在线直接处理"]
    ROUTER -->|"type=general"| GEN2["general 闲聊<br/>纯 LLM 电商客服风格回复"]
    ROUTER -->|"type=presale"| KG["presale 售前<br/>知识库检索子图"]
    ROUTER -->|"type=aftersale"| PLACE["售后占位<br/>（售后 Agent 接口预留）"]
    ROUTER -->|"type=complaint"| PLACE2["投诉安抚占位<br/>（安抚 Agent 接口预留）"]
    ROUTER -->|"type=clarify"| CLARIFY["意图澄清<br/>电商统一风格询问"]
    ROUTER -->|"type=image"| IMG["image 图片<br/>Qwen-VL + LLM"]

    KG --> GRAG["向量检索 vector_search_query<br/>HNSW ∥ pg_jieba BM25 → RRF → 精排"]
```

### 5.3 general 闲聊意图运行流程

节点 `respond_to_general_query`：纯 LLM 对话，不调用任何外部检索；历史消息经 MemoryManager 压缩（Redis 摘要缓存）后注入提示词。ScopeGuard 超经营范围拦截后也走本节点（注入"超出经营范围"logic 触发拒绝话术）。

```mermaid
flowchart TD
    A["进入节点<br/>respond_to_general_query"] --> B["模型选择<br/>AGENT_SERVICE: DeepSeek / Ollama"]
    B --> C["系统提示词<br/>GENERAL_QUERY_SYSTEM_PROMPT<br/>注入路由 logic（分类理由）"]
    C --> D["历史管理<br/>MemoryManager 三层压缩<br/>最近 5 轮原文 + 旧消息摘要<br/>（摘要 Redis 缓存）"]
    D --> E["LLM 生成闲聊回复"]
    E --> F["返回 {messages: [AIMessage]}"]
    F --> G["SSE 流式返回前端"]
```

### 5.4 风险拦截 / 转人工运行流程

节点 `risk_intercept`（risk=violation）与 `transfer_human`（risk=high_risk）：**静态话术节点，不走 LLM**——违规咨询明确拒绝 + 合规引导（对应福客 D5），高风险操作/投诉升级说明无法在线直接处理（对应福客 D3/D4 转人工复核）。原 additional-query 追问节点已删除（追问逻辑下沉到后续业务 Agent）。

```mermaid
flowchart TD
    A["进入节点<br/>risk_intercept / transfer_human"] --> B["静态话术常量<br/>RISK_INTERCEPT_REPLY / TRANSFER_HUMAN_REPLY"]
    B --> C["返回 {messages: [AIMessage(content=话术)]}"]
    C --> D["SSE 流式返回前端"]
```

### 5.5 presale 售前导购运行流程（原 graphrag-query）

节点 `create_research_plan`：售前场景（商品参数/价格/推荐/使用咨询）复用现有 RAG 子图，直接进入 Multi-Tool 子图（TimeoutGuard 30 秒超时保护）。入口环节（main.py `/api/langgraph/query`，图执行前）：指代消解（多轮代词/主语补全）→ 语义缓存检索——命中直接短路返回（不进图），未命中才进入本节点（query 已是完整问题），完整回答生成后回写缓存。

```mermaid
flowchart TD
    A["进入节点<br/>create_research_plan<br/>（query 已由入口消解）"] --> B["构建 Multi-Tool 子图<br/>Planner → Send 并行 → 向量检索<br/>→ Summarize → FinalAnswer"]
    A --> R["TimeoutGuard 30s 超时保护<br/>ainvoke 子图"]
    R --> S2["Planner 任务分解<br/>Send 并发派发子任务"]
    S2 --> S3["混合检索（每子任务）<br/>HNSW ∥ pg_jieba BM25 并行<br/>RRF 融合 top-20"]
    S3 --> S4["Reranker 精排<br/>bge-reranker-v2-m3 top-5"]
    S4 --> S5["Summarize 结果汇总<br/>客服风格生成"]
    S5 --> S7["FinalAnswer 组装输出<br/>写入会话历史"]
    R -->|"超时"| S1B["降级回答<br/>「抱歉，系统处理超时，请稍后再试」"]
    S1B --> OUT
    S7 --> OUT
    OUT --> END["SSE 流式返回前端"]
```

### 5.6 image-query 图片分析意图运行流程

节点 `create_image_query`：PIL 压缩 → base64 → Qwen-VL 视觉分析 → 结合图片描述由 LLM 生成客服回复。

```mermaid
flowchart TD
    A["进入节点<br/>create_image_query"] --> B{"image_path 存在?"}
    B -->|"否"| B1["返回道歉<br/>「我无法查看这张图片，请重新上传」"]
    B -->|"是"| C{"VISION_API_KEY / BASE_URL<br/>/ MODEL 配置完整?"}
    C -->|"否"| C1["返回道歉<br/>视觉模型配置不完整"]
    C -->|"是"| D["PIL 图片压缩<br/>最长边 1024px，JPEG 85%"]
    D --> E["base64 编码"]
    E --> F["Qwen-VL API 分析<br/>POST /chat/completions<br/>返回图片描述"]
    F -->|"非 200"| F1["返回道歉<br/>视觉接口调用失败"]
    F -->|"200"| G["GET_IMAGE_SYSTEM_PROMPT<br/>注入图片描述"]
    G --> H["LLM 生成客服风格回复<br/>（DeepSeek / Ollama）"]
    B1 --> I["返回 {messages: [AIMessage]}"]
    C1 --> I
    F1 --> I
    H --> I
    I --> J["SSE 流式返回前端"]
```

### 5.7 售后 / 投诉安抚占位节点

节点 `aftersale_placeholder` / `complaint_placeholder`：**业务 Agent 接口占位**——返回"服务升级中"提示（静态话术，不走 LLM）。接口与 multi_tool 子图同构（`question+history → answer`），后续售后 Agent（工作流骨架 + LLM 决策点 + RAG tool，方式 C）/ 投诉安抚 Agent 子图就位后，仅替换路由目的地，识别模块与接口形状不动。售后子场景（退货退款/物流/订单查询）由售后 Agent 工作流骨架第一步结合订单/历史上下文判断，识别层不承担。

```mermaid
flowchart TD
    A["进入节点<br/>aftersale_placeholder / complaint_placeholder"] --> B["静态话术<br/>AFTERSALE_PLACEHOLDER_REPLY<br/>/ COMPLAINT_PLACEHOLDER_REPLY"]
    B --> C["返回 {messages: [AIMessage(content=话术)]}"]
    C --> D["SSE 流式返回前端"]
```

### 5.8 意图澄清运行流程

节点 `clarify_node`（type=clarify，意图不明）：以电商统一风格（"亲～"）结合用户原话与 router logic 针对性询问真实意图；生成失败/超时降级静态模板。澄清后用户回答 → 下一轮正常重新识别（含澄清问答的消息流自然流转）。触发边界：语义无法归类才澄清（无主题词/无上文指代/碎片语气词）；明确但缺细节（议价/损坏）不澄清；risk 优先于 clarify。设计依据 `docs/spec_plan/已完成/SPEC_INTENT_CLARIFY.md`。

```mermaid
flowchart TD
    A["进入节点<br/>clarify_node"] --> B["CLARIFY_SYSTEM_PROMPT<br/>注入 logic 困惑点 + 用户原话"]
    B --> C["MemoryManager 历史管理"]
    C --> D["LLM 生成针对性澄清<br/>（亲～风格，一次一问）"]
    D -->|"成功"| E["返回 {messages: [AIMessage]}"]
    D -->|"失败/为空"| F["静态模板兜底<br/>CLARIFY_FALLBACK_REPLY"]
    E --> G["SSE 流式返回前端"]
    F --> G
```

---

## 6. 设计模式应用

| 设计模式 | 应用位置 | 具体实现 |
|---------|---------|---------|
| **工厂模式** | `LLMFactory` | `create_chat_service()` 根据配置返回不同实例 |
| **策略模式** | `config.py` 服务选择 | `CHAT_SERVICE` / `REASON_SERVICE` / `AGENT_SERVICE` 分别选择 DeepSeek/Ollama |
| **状态图模式** | `lg_builder.py` | LangGraph `StateGraph` + 条件边实现多路由 Agent 编排 |
| **观察者/回调模式** | `deepseek_service.py` | `on_complete` 回调触发消息持久化，解耦 LLM 和存储 |
| **建造者模式** | `lg_builder.py` | `builder.add_node().add_edge().compile()` 构建状态图 |
| **单例模式** | `checkpointer_pool`（AsyncConnectionPool） | LangGraph PostgresSaver 全局持久化存储 |
| **模板方法模式** | 提示词模板 | 预定义提示词 + 动态参数注入 |
| **装饰器模式** | `LoggingMiddleware` | FastAPI 中间件统一日志 |
| **门面模式** | `RAGRetrieverService` | 唯一检索入口：HNSW ∥ BM25 并行 → RRF → 精排，graph 节点与 @tool 共用 |

---

## 7. 数据流转

### 7.1 用户消息存储

```
用户消息 + AI回复 → ConversationService.save_message()
  → PostgreSQL: conversations 表 (会话元信息)
  → PostgreSQL: messages 表 (用户消息 + 助手回复)
  → 首条消息自动生成会话标题 (前 20 字)
```

### 7.2 语义缓存存储

```
用户问题 → 分级指代消解 → EmbeddingProvider → Redis:
  {prefix}:vec:{md5}  → JSON 向量（md5 基于消解后消息）
  {prefix}:resp:{md5} → 回复文本
  {prefix}:meta:{md5} → 访问元数据
  {prefix}:index      → ZSET 有序索引（member=hash_id, score=last_access）
                        替代 keys() 全库扫描；cleanup 按 score 升序淘汰
```

### 7.3 标准 RAG 索引管道

```
原始文档 (PDF/DOCX/TXT)
  → 文档解析 (PyPDF2 / python-docx / TXT)
  → 文本清洗
  → RecursiveCharacterTextSplitter 分块 (500/50)
  → Embedding (EmbeddingProvider, qwen text-embedding-v4 API 1024 维, 分批 ≤10 条/请求)
  → pgvector 入库 (document_chunks 表, HNSW 索引)
  → BM25 侧自动就绪：content_tsv 生成列（jiebacfg 精确模式分词）+ GIN 倒排索引
    （CREATE EXTENSION pg_jieba；生成列随 INSERT 自动维护，无重建窗口）
```

---

## 8. 项目亮点深度分析

### 8.1 🌟 场景+风险双维合并意图识别路由

**创新点**: 一次 LLM 低温结构化输出同时判定**场景意图 + 风险意图**两个维度，risk 拦截优先级最高（违规/高风险消息不进入任何业务处理路径），场景驱动路由并预留业务 Agent 接口。

```python
class Router(TypedDict):
    """Classify user query: scenario + risk."""
    logic: str                      # 分类理由（多意图时简述次要意图）
    type: Literal[
        "presale",                  # 售前：商品咨询/参数/价格活动/推荐导购
        "aftersale",                # 售后：退货退款/物流异常/订单查询
        "complaint",                # 投诉安抚：情绪不满/投诉（情绪主导）
        "general",                  # 闲聊
        "image",                    # 图片
        "clarify",                  # 意图不明：语义无法归类 → 澄清节点
    ]
    risk: Literal[
        "none", "violation", "high_risk",
    ]                               # violation=违规咨询拦截；high_risk=高风险操作转人工
```

**技术价值**:

- 原 4 类技术路由合并进场景体系（graphrag-query→presale，additional-query 删除——追问下沉到业务 Agent）
- 多意图取主导意图（句首发/最强烈者；risk/complaint 永远优先），次要意图记入 logic
- 售后子场景（退货/物流/订单）不由识别层判断——判断需订单/历史上下文，下沉到售后 Agent 工作流骨架第一步（简化设计 2026-08-27）
- 售后/投诉安抚占位节点接口与 RAG 子图同构，业务 Agent（售前/售后/安抚）就位后仅改路由目的地
- 结构化输出校验失败降级 general/none 不抛异常；golden set 评测（46 条，含意图澄清/意图模糊/多意图/超范围/图片风险/多轮上下文）二维准确率 100%（脚本 `llm_backend/scripts/eval_intent_golden.py`）
- 设计依据与演进方向（任务分配器/多 Agent 协同/主 Agent 汇总/并行规则）见 `docs/spec_plan/已完成/SPEC_INTENT_RECOGNITION_OPTIMIZATION.md` §8.3

### 8.2 🌟 混合检索 + RRF 融合 + Reranker 精排

**创新点**: 不是简单的 BM25+向量双路检索，而是一个**4 步闭环**（两路检索全部下沉数据库，应用层只发 SQL 收排名）：

1. **pg_jieba BM25 SQL 精确匹配**（ts_rank_cd + GIN 倒排索引，jiebacfg 精确模式与查询同源，DB 内增量维护）
2. **pgvector HNSW 向量语义匹配**（qwen text-embedding-v4 API 编码，ORDER BY 距离触发 ANN 索引）
3. **RRF 倒数排名融合**（不依赖绝对分数，数学上更鲁棒；两路 `asyncio.gather` 并行，耗时 = max 非 sum）
4. **bge-reranker-v2-m3 精排**（CrossEncoder 拼接 (query, doc) 打分，top-20 → top-5；替代原 LLM 相关性评分，GPU/fp16 加速，失败自动降级为融合 top-K）

```python
# RRF 融合公式
score(doc) = Σ 1/(k + rank_i)  # k=60, rank_i 是文档在第 i 路检索中的排名
```

**技术价值**: 融合了关键词精确匹配和语义理解的优势，精排用交叉编码器做最终把关，防止不相关结果污染 LLM 上下文；检索计算全部下沉 PostgreSQL（应用内存零语料驻留），语料规模增长不再线性放大应用侧成本。

### 8.3 🌟 三层记忆管理 + Redis 增量缓存

**创新点**: 不仅压缩历史对话，还实现了**增量式摘要更新**和**Redis 缓存跨会话复用**：

```
第一层: 最近 5 轮 (完整原文)
第二层: 轮次 6-15 (压缩为 ~200 字摘要)
第三层: 轮次 16+ (压缩为 ~100 字摘要 + 关键实体)
```

**技术价值**:

- 增量压缩：新轮次只压缩未处理的，不从零开始
- Redis 缓存：跨请求复用摘要，避免重复 LLM 调用
- Token 预算控制：动态裁剪确保不超限
- 关键实体追踪：保留产品名称等关键信息不丢失

### 8.4 🌟 语义缓存的工程化实现

**创新点**: 不是简单的键值缓存，而是基于 **Embedding 向量余弦相似度**的语义级缓存：

```
"扫地机器人多少钱" ≈ "这个扫地机什么价格"
→ 向量余弦相似度 0.95 ≥ 0.90 → 缓存命中
```

**工程细节**:

- 按用户隔离缓存 (`prefix:user_id:...`)
- **分级指代消解**：三层规则引擎（显性指代 / 省略主语 / 纯语气词）先过滤，80% 完整问题零开销透传、15% 含指代消息才调 LLM 补全（temperature=0 保证确定性、2s 超时失败降级透传）、纯语气词不查不写避免污染
- **lookup/update 同源消解**：缓存 key 基于消解后消息 MD5，保证"那个有货吗"与"扫地机器人X1有货吗"命中同一缓存
- **graphrag 入口前置检索**：`/api/langgraph/query` 消解后、进图前查缓存，命中短路跳过整个图流程；未命中走图、完整回答后回写（两条链路共享按 user_id 的缓存池）
- **ZSET 有序索引** + `redis.asyncio` 异步客户端 + 实例池化（消除 keys 全库扫描与事件循环阻塞）
- LRU 自动清理（ZSET score 排序）+ 访问次数统计
- 模拟流式返回保持前端体验一致

### 8.5 🌟 Docker Compose 一键部署 + Healthcheck

**创新点**: 3 个服务 (PostgreSQL/Redis/App) 通过 `depends_on` + `condition: service_healthy` 确保启动顺序：

```yaml
app:
  depends_on:
    postgres:
      condition: service_healthy   # pg_isready
    redis:
      condition: service_healthy   # redis-cli ping
```

**技术价值**: 避免了常见的 "服务启动但数据库未就绪" 的竞态问题，开箱即用。

---

## 9. 项目优点

### 9.1 架构设计

| 优点 | 说明 |
|------|------|
| **模块化清晰** | 配置/服务/Agent/组件分层明确，职责单一 |
| **高可扩展性** | 工厂模式 + 策略模式，新增 LLM 只需添加服务类 |
| **Agent 编排成熟** | LangGraph StateGraph 提供可视化的状态流转 |
| **会话持久化** | PostgresSaver + thread_id 实现多轮对话状态管理（重启不丢失） |
| **SSE 流式响应** | 所有 LLM 接口统一使用 Server-Sent Events，用户体验好 |
| **Human-in-the-Loop** | 支持中断/恢复机制，实现人工确认流程 |

### 9.2 技术深度

| 优点 | 说明 |
|------|------|
| **检索质量闭环** | 混合检索 → Reranker 精排，DB 内检索 + 交叉编码器把关 |
| **多层护栏** | 范围预检 + 超时保护，层层保障 |
| **内存管理成熟** | 三层摘要 + Token 预算 + Redis 缓存，处理长对话 |
| **成本控制意识** | 语义缓存降本 + 入口指代消解门控（仅约 15% 含指代消息调 LLM） |

### 9.3 工程实践

| 优点 | 说明 |
|------|------|
| **配置管理规范** | Pydantic Settings 类型安全，60+ 项配置集中管理 |
| **日志系统完善** | 结构化日志，按服务分级，请求追踪 |
| **Docker 部署完善** | Multi-service + healthcheck + 数据持久化 |
| **文档规范** | README 清晰，API 端点完整列举 |
| **知识库丰富** | 内置产品文档 + FAQ + 真实客服对话数据 |

---

## 10. 项目缺点与改进建议

### 10.1 严重问题 🔴

#### 10.1.1 硬编码的凭据信息

`docker-compose.yml` 中包含明文数据库密码：

```yaml
POSTGRES_PASSWORD: smartcs_agent_pwd
```

**影响**: 如果仓库公开，凭据泄露风险。

**建议**: 使用 Docker Secrets 或 `.env` 文件管理敏感信息，在 docker-compose 中通过 `${VAR}` 引用。

### 10.2 中等问题 🟡

#### 10.2.1 单元测试覆盖不足（已部分缓解）

已建立 pytest 测试体系（`app/test/`：`test_entry_cache.py` / `test_fastapi.py` / `test_pronoun_resolve.py`，当前 9 项全通过），意图识别路由另有 golden set 评测脚本 `scripts/eval_intent_golden.py`（46 条二维准确率）。但向量检索、语义缓存、混合检索等核心模块仍无 pytest 覆盖。

**建议**:

- 为向量检索、语义缓存、混合检索添加 pytest 测试
- 集成 LangGraph 的测试工具进行 Agent 行为验证

#### 10.2.2 前端过于简单

前端已重构为 Vue3 SFC 工程（frontend），chat.html 已并入。原 chat.html 单文件实现缺少：

- 用户登录 UI
- 会话列表展示
- 图片上传交互
- 加载状态/错误处理
- 移动端适配

**建议**: 使用现代前端框架（React/Vue SPA）重构或集成到 FastAPI Jinja2 模板。

#### 10.2.3 init_db 每次启动清空业务表

```python
# scripts/init_db.py 每次启动先 drop_all 再 create_all
await conn.run_sync(Base.metadata.drop_all)
await conn.run_sync(Base.metadata.create_all)
```

**影响**: 每次启动（含容器重启）清空用户/会话/消息/document_chunks 业务表数据（LangGraph 检查点表不受影响）。

**建议**: 改为 create-only（仅建表 + 扩展 + 索引），表结构演进交给 Alembic 管理。

#### 10.2.4 缺少 API 限流

所有 API 端点都没有速率限制。

**影响**: 恶意或异常高频调用可能导致 LLM API 费用失控。

**建议**: 集成 `slowapi` 或 Redis 令牌桶实现 IP/用户级别限流。

#### 10.2.5 历史上的 GraphRAG 源码内嵌问题（已迁移解决）

历史上 `llm_backend/app/graphrag/` 直接包含了 Microsoft GraphRAG 的完整源码（约 80+ 文件），存在以下问题：

- 增加仓库体积
- 难以跟随上游更新
- 版本管理混乱
- 索引构建耗时 5-30 分钟/次，LLM 成本高
- Local/Global/DRIFT/Basic 检索能力对电商客服场景过剩（实体关系类查询由知识库文档检索承担）

**解决**: 已迁移为标准 RAG 管道（pgvector 向量检索），删除 `llm_backend/app/graphrag/` 源码目录、`scripts/build_graphrag_index.py` 与 graphrag 依赖，索引构建降至秒级，事实查询效果持平。

#### 10.2.6 缺少输入校验

`main.py` 中的请求模型缺少字段级别校验：

```python
class ChatMessage(BaseModel):
    messages: List[Dict[str, str]]  # 没有长度限制
    user_id: int                     # 没有范围校验
    conversation_id: int             # 没有存在性校验
```

**建议**: 添加 `Field(min_length=1, max_length=50)` 等约束。

### 10.3 改进建议 🟢

#### 10.3.1 引入异步任务队列

文件上传和索引构建 (`IndexingService`) 是 CPU 密集型操作，当前直接阻塞请求线程。

**建议**: 引入 Celery + Redis 或 ARQ 异步任务队列处理。

#### 10.3.2 添加监控和可观测性

**建议**:

- 集成 OpenTelemetry 追踪 LLM 调用链
- 添加 Prometheus metrics (请求延迟、缓存命中率、LLM Token 消耗)
- 设置 LLM API 调用告警

#### 10.3.3 实现 A/B 测试框架

当前策略选择（简单/中等/复杂）依赖硬编码阈值。

**建议**: 实现配置化的 A/B 测试，对比不同策略的效果，数据驱动优化。

#### 10.3.4 添加降级策略

当 DeepSeek API 不可用时，当前直接抛出 500 错误。

**建议**: 实现 fallback 链：DeepSeek → Ollama → 预定义回复。

#### 10.3.5 数据隐私增强

用户上传的图片/文件直接存储在本地文件系统。

**建议**:

- 敏感图片自动脱敏
- 定期清理过期文件
- 实现访问控制（当前 `/api/conversations/{id}/messages` 的 `user_id` 通过 query 参数传递，不安全）

---

## 11. 部署架构

### 11.1 Docker Compose 拓扑

```mermaid
graph TB
    subgraph "Docker Network: smartcs-agent_default（仅基础服务）"
        PG["smartcs-agent-postgres<br/>自定义镜像 pgvector + pg_jieba :5432<br/>（docker/postgres/Dockerfile 编译 cppjieba）<br/>healthcheck: pg_isready"]
        REDIS["smartcs-agent-redis<br/>Redis 7-alpine :6379<br/>healthcheck: redis-cli ping"]
    end

    subgraph "本地运行（非 Docker）"
        APP["应用：uvicorn main:app :8000<br/>Python 3.13（run.py 启动）"]
    end

    subgraph "Volumes"
        V1["pg_data"]
        V2["redis_data"]
    end

    Browser -->|":8000"| APP
    APP -->|"DB_HOST=localhost"| PG
    APP -->|"REDIS_HOST=localhost"| REDIS
    PG --- V1
    REDIS --- V2
```

### 11.2 启动流程

```mermaid
sequenceDiagram
    participant DC as Docker Compose
    participant M as PostgreSQL
    participant R as Redis
    participant A as App (本地)

    DC->>M: 启动 PostgreSQL(pgvector) 容器
    M->>M: pg_isready (每10s)
    M-->>DC: healthy ✓

    DC->>R: 启动 Redis 容器
    R->>R: redis-cli ping (每10s)
    R-->>DC: healthy ✓

    A->>A: cd llm_backend && python -m scripts.init_db (建表 + pgvector/pg_jieba 扩展 + HNSW/GIN 索引 + content_tsv 生成列)
    A->>A: python run.py（Windows 事件循环补丁 + uvicorn main:app --reload :8000）
    A-->>DC: 服务就绪 ✓
```

---

## 12. 综合评价

### 12.1 技术成熟度矩阵

| 维度 | 评分 | 说明 |
|------|------|------|
| 架构设计 | ⭐⭐⭐⭐⭐ | 分层清晰，模块解耦，设计模式运用恰当 |
| 技术深度 | ⭐⭐⭐⭐⭐ | 混合检索+评分、三层记忆管理、语义缓存均为业界前沿实践 |
| 代码质量 | ⭐⭐⭐⭐ | 整体结构良好，部分模块缺失、硬编码问题需修复 |
| 测试覆盖 | ⭐ | 完全缺失，需从零建立测试体系 |
| 文档完善 | ⭐⭐⭐⭐ | README 详尽，API 文档完整，缺少开发者指南 |
| 部署运维 | ⭐⭐⭐⭐ | Docker 一键部署完善，缺少监控和告警 |
| 安全性 | ⭐⭐ | 凭据硬编码、缺少限流和输入校验 |
| 可扩展性 | ⭐⭐⭐⭐⭐ | 工厂模式+策略模式，新增功能成本低 |

### 12.2 适用场景

| 场景 | 适合度 | 说明 |
|------|--------|------|
| 电商客服原型 | ⭐⭐⭐⭐⭐ | 开箱即用，多轮对话+知识库完善 |
| 学习 LangGraph | ⭐⭐⭐⭐⭐ | 完整的 StateGraph + 子图 + 多工具编排案例 |
| 学习标准 RAG | ⭐⭐⭐⭐⭐ | pgvector 向量管道 + 混合检索的实际应用 |
| 学习 RAG 优化 | ⭐⭐⭐⭐ | 语义缓存、Reranker 精排、混合检索等最佳实践 |
| 生产部署 | ⭐⭐⭐ | 需补充测试、限流、监控后才能上生产 |

### 12.3 总结

SmartCS-Agent 是一个**技术深度优秀、工程完整性良好但生产就绪度不足**的 AI 客服系统。它在以下方面展现了较强的技术实力：

1. **Agent 编排**: LangGraph StateGraph 的运用成熟，子图嵌套、条件路由、会话持久化、中断恢复等技术点处理得当
2. **检索增强**: 混合检索（HNSW ∥ pg_jieba BM25）+ RRF 融合 + Reranker 精排形成完整的检索质量保障链路
3. **工程降本**: 语义缓存的实现精细（按用户隔离、LRU清理、流式模拟），三层记忆管理的 Token 预算控制
4. **系统思维**: 入口指代消解 + 语义缓存前置（图执行前完成上下文处理与缓存短路），子图保持精简，冗余 LLM 调用持续收敛

主要短板在于**测试缺失**和**生产级运维特性不足**（日志监控、限流降级、凭据管理），建议在这些方面投入改进后再用于生产环境。

---

> **分析工具**: Claude Code
> **分析范围**: 全项目 Python 服务，配置化管理，3 Docker 服务
> **核心模块覆盖率**: 100%

