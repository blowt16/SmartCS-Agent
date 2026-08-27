# SmartCS-Agent - 智能电商客服系统

<div align="center">

![Python](https://img.shields.io/badge/Python-3.13-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-005571?logo=fastapi)
![LangGraph](https://img.shields.io/badge/LangGraph-Multi--Agent-green)
![DeepSeek](https://img.shields.io/badge/DeepSeek-V3-orange)
![License](https://img.shields.io/badge/License-MIT-yellow)

*基于 FastAPI + LangGraph 的智能电商客服系统，集成向量知识检索（pgvector）与多轮对话管理*

</div>

---

## 项目亮点

| 特性 | 说明 |
|------|------|
| **场景+风险双维意图识别** | LangGraph StateGraph 单次合并识别：场景（售前/售后/投诉安抚/闲聊/图片）驱动分支 + 风险意图（违规拦截/高风险转人工）独立判断、拦截优先；售后子场景（退货/物流/订单）由后续售后 Agent 内部判断，识别层只管场景路由 |
| **向量知识库检索** | pgvector（HNSW）Top-K 检索 + BM25 混合检索 + LLM 相关性评分，文档上传秒级建索引 |
| **混合检索 + 相关性评分** | BM25 + 向量检索 RRF 融合，LLM 逐条评分过滤不相关结果，不足时自动切换策略重检索 |
| **文档向量检索管道** | 解析 → 清洗 → 语义分块 → Embedding → pgvector 入库（HNSW 索引），秒级索引，配合混合检索增强召回 |
| **语义缓存** | 基于 Embedding 向量余弦相似度（阈值 0.90），相同语义问题直接返回缓存，降低 LLM 调用成本 |
| **幻觉检测** | LLM 校验生成回答是否基于事实数据，为回答质量提供最后保障 |
| **Docker Compose 一键部署** | PostgreSQL(pgvector) + Redis + App，healthcheck 保障启动顺序，开箱即用 |
| **丰富的知识库** | 内置产品知识文档 + 1,800 条电商 FAQ + 2,600+ 条真实客服对话（JDDC 数据集） |

## 系统架构

```
用户请求 → FastAPI API 层
  │
  ├─ /api/langgraph/query ──→ LangGraph Agent（场景+风险双维意图识别）
  │    │                        risk 拦截优先级最高
  │    ├─ risk=violation → 风险拦截（明确拒绝 + 合规引导）
  │    ├─ risk=high_risk → 转人工（说明无法在线直接处理）
  │    ├─ 售前 presale → RAG 子图（Multi-Tool Workflow）
  │    │    └─ 向量检索（pgvector）→ 混合检索(BM25+向量) → 相关性评分(LLM)
  │    ├─ 售后 aftersale → 售后占位节点（售后 Agent 接口预留，后续接入）
  │    ├─ 投诉安抚 complaint → 投诉安抚占位节点（安抚 Agent 接口预留，后续接入）
  │    ├─ 闲聊 general → 纯 LLM 闲聊
  │    └─ 图片 image → 视觉模型（Qwen-VL）图片分析
  │
  ├─ /api/upload ────────→ 文档解析 → 向量入库（pgvector）
  │
  └─ 前端（Vue3 SFC，frontend → dist）─→ SSE 流式响应
       └─ Redis 语义缓存检查（命中时模拟流式短路返回）
```

## 技术栈

| 层次 | 技术 | 作用 |
|------|------|------|
| 后端框架 | FastAPI | REST API，原生异步，SSE 流式响应 |
| 智能体 | LangGraph | StateGraph 多路由 Agent 编排，会话检查点持久化（PostgresSaver） |
| LLM | DeepSeek API | 对话、推理、Agent（工厂模式可切换 Ollama） |
| 文档检索 | pgvector | 语义分块 + 向量入库（HNSW）+ 混合检索（BM25 + 向量 RRF） |
| Embedding | SiliconFlow (BAAI/bge-m3) | 语义向量生成，免费 API |
| 向量缓存 | Redis | 语义缓存（余弦相似度 >= 0.90 命中） |
| 数据库 | PostgreSQL（pgvector） | 用户、会话、消息持久化 + 向量检索 + LangGraph 检查点 |
| 前端 | Vue3 SFC（frontend → dist） | 聊天界面（登录/注册、SSE 流式、知识库上传） |

## 快速开始

### 环境要求

- Python 3.13+
- uv（Python 包管理器，用于本地安装依赖）
- Docker & Docker Compose（仅用于启动 PostgreSQL / Redis 基础服务）

### 本地运行（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/blowt16/SmartCS-Agent.git
cd SmartCS-Agent

# 2. 安装依赖（uv 自动创建 .venv 并按 uv.lock 锁定版本）
uv sync

# 3. 配置环境变量
#    编辑项目根目录 .env，填入 API Key 和数据库连接信息
#    DB_HOST / REDIS_HOST 保持 localhost（对应下方 Docker 启动的基础服务）

# 4. 启动 PostgreSQL、Redis（Docker 仅承载基础服务，应用在本地运行调试）
docker compose up -d

# 5. 初始化数据库（建表 + 启用 pgvector 扩展 + HNSW 索引）
cd llm_backend
python -m scripts.init_db

# 6. 启动服务（uvicorn，开发模式热重载）
cd llm_backend
python run.py
```

### 准备知识库数据

```bash
# 生成三类知识库数据（产品知识 / 电商 FAQ / 客服对话），
# 输出到 llm_backend/knowledge_data/ 目录
python scripts/generate_product_knowledge.py
python scripts/download_datasets.py
python scripts/download_jddc.py
```

文档通过 `/api/upload` 上传后自动完成解析 → 分块 → Embedding → pgvector 入库（`document_chunks` 表），无需手动建索引。

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/langgraph/query` | POST | Agent 多路由查询（SSE 流式 + 语义缓存） |
| `/api/upload` | POST | 上传文件 -> 解析分块 -> 向量入库（pgvector） |
| `/api/conversations` | POST | 创建会话 |
| `/api/conversations/{id}/messages` | GET | 获取历史消息 |
| `/api/register` | POST | 用户注册 |
| `/api/token` | POST | 登录获取 JWT |

## 项目结构

```
├── llm_backend/                      # 后端服务
│   ├── main.py                       # FastAPI 入口，所有 API 端点
│   ├── app/
│   │   ├── core/                     # 配置、数据库、安全、日志
│   │   ├── services/                 # 业务服务层
│   │   │   ├── llm_factory.py        # LLM 工厂模式（DeepSeek/Ollama）
│   │   │   ├── deepseek_service.py   # DeepSeek API + 语义缓存
│   │   │   ├── redis_semantic_cache.py # Redis 语义缓存
│   │   │   └── indexing_service.py   # 文档解析 → 分块 → 向量入库
│   │   ├── lg_agent/                 # LangGraph 智能体
│   │   │   ├── lg_builder.py         # StateGraph 构建与路由
│   │   │   ├── lg_states.py          # 状态定义（Router/AgentState）
│   │   │   └── kg_sub_graph/         # 知识图谱子图
│   │   │       └── agentic_rag_agents/
│   │   │           └── components/
│   │   │               ├── customer_tools/  # 向量检索 + 混合检索
│   │   │               ├── relevance_grader.py  # LLM 相关性评分
│   │   │               └── hybrid_retrieval/    # BM25+向量 RRF 融合
│   │   │   ├── models/                   # SQLAlchemy 数据模型（含 document_chunks 向量表）
│   │   ├── api/                      # 认证路由
│   │   └── prompts/                  # 提示词模板
│   └── frontend/                     # Vue3 SFC 前端工程（npm run build → dist/，主入口 http://127.0.0.1:8000）
├── scripts/                          # 工具脚本
│   ├── generate_product_knowledge.py # 从 CSV 生成产品知识文档
│   ├── download_datasets.py          # 下载开源电商 FAQ 数据集
│   └── download_jddc.py              # 下载 JDDC 客服对话数据集
├── docker-compose.yml                # Docker Compose 配置（仅 postgres/redis）
└── STUDY_NOTES.md                    # 项目学习文档（面试准备）
```

## 设计模式

| 模式 | 应用位置 | 说明 |
|------|---------|------|
| **工厂模式** | `LLMFactory` | 通过 `.env` 配置切换 DeepSeek/Ollama，不改代码 |
| **策略模式** | `config.py` | CHAT/REASON/AGENT 可分别选不同 LLM 服务 |
| **回调模式** | `deepseek_service.py` | `on_complete` 回调触发消息持久化，解耦 LLM 和存储 |
| **状态图** | LangGraph | `StateGraph` + 条件边实现多路由 Agent |

## 数据集说明

本项目内置三类知识库数据（由 `scripts/` 脚本生成，位于 `llm_backend/knowledge_data/`）：

| 数据类型 | 文件数 | 来源 | 说明 |
|---------|--------|------|------|
| 产品知识文档 | 10 | 从项目 CSV 聚合生成 | 涵盖 8 个品类 10 款智能家居产品 |
| 电商 FAQ | 81 | [Chinese-EcomQA](https://huggingface.co/datasets/OpenStellarTeam/Chinese-EcomQA) | 1,800 条品牌/商品/推荐问答 |
| 客服对话 | 85 | [JDDC](https://github.com/SimonJYang/JDDC-Baseline) | 2,600+ 条真实电商客服对话 |

## 许可证

MIT License

## 致谢

本项目基于原项目 [wang219416/GraphRAG-](https://github.com/wang219416/GraphRAG-) 二次开发，在此向原作者 [wang219416](https://github.com/wang219416) 的开源贡献表示感谢。

- [LangGraph](https://github.com/langchain-ai/langgraph) - Agent 编排框架
- [JDDC](https://jddc.jd.com/) - 京东客服对话数据集
- [Chinese-EcomQA](https://huggingface.co/datasets/OpenStellarTeam/Chinese-EcomQA) - 中文电商问答数据集
- [SiliconFlow](https://siliconflow.cn/) - 免费 Embedding API
