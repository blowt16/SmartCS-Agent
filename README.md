# SmartCS-Agent - 智能电商客服系统

<div align="center">

![Python](https://img.shields.io/badge/Python-3.8+-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-005571?logo=fastapi)
![LangGraph](https://img.shields.io/badge/LangGraph-Multi--Agent-green)
![DeepSeek](https://img.shields.io/badge/DeepSeek-V3-orange)
![Neo4j](https://img.shields.io/badge/Neo4j-Knowledge%20Graph-brightgreen?logo=neo4j)
![License](https://img.shields.io/badge/License-MIT-yellow)

*基于 FastAPI + LangGraph + Neo4j 的智能电商客服系统，集成 GraphRAG 知识检索与多轮对话管理*

</div>

---

## 项目亮点

| 特性 | 说明 |
|------|------|
| **5 路意图路由** | LangGraph StateGraph 实现：闲聊 / 追问 / 知识库查询 / 图片分析 / 文件处理，自动识别用户意图并路由 |
| **Text2Cypher 闭环** | 自然语言 → Cypher 查询 → 正则 + LLM 双重校验 → 自动修正 → 执行，实现图数据库的自然语言查询 |
| **混合检索 + 相关性评分** | BM25 + 向量检索 RRF 融合，LLM 逐条评分过滤不相关结果，不足时自动切换策略重检索 |
| **4 种 GraphRAG 检索策略** | Local / Global / DRIFT / Basic，覆盖事实查询、宏观分析、多跳推理、快速检索等场景 |
| **语义缓存** | 基于 Embedding 向量余弦相似度（阈值 0.90），相同语义问题直接返回缓存，降低 LLM 调用成本 |
| **幻觉检测** | LLM 校验生成回答是否基于事实数据，为回答质量提供最后保障 |
| **Docker Compose 一键部署** | MySQL + Redis + Neo4j + App，healthcheck 保障启动顺序，开箱即用 |
| **丰富的知识库** | 内置产品知识文档 + 1,800 条电商 FAQ + 2,600+ 条真实客服对话（JDDC 数据集） |

## 系统架构

```
用户请求 → FastAPI API 层
  │
  ├─ /api/chat ──────────→ LLMFactory → DeepSeek/Ollama → SSE 流式响应
  │                                              ↑
  │                                     Redis 语义缓存检查
  │
  ├─ /api/search ────────→ Function Calling → SerpAPI 联网搜索
  │
  ├─ /api/langgraph/query ──→ LangGraph Agent（5 路意图路由）
  │    │
  │    ├─ general-query → 纯 LLM 闲聊
  │    ├─ additional-query → 护栏检查 + 追问
  │    ├─ graphrag-query → Multi-Tool Workflow
  │    │    ├─ Text2Cypher（NL → Cypher 查询）
  │    │    ├─ PredefinedCypher（预定义模板 + 向量匹配）
  │    │    └─ GraphRAG（4 种检索策略）
  │    │         └─ 混合检索(BM25+向量) → 相关性评分(LLM) → 策略切换重检索
  │    ├─ image-query → GPT-4o 图片分析
  │    └─ file-query → 文件处理
  │
  └─ /api/upload ────────→ GraphRAG 索引构建
```

## 技术栈

| 层次 | 技术 | 作用 |
|------|------|------|
| 后端框架 | FastAPI | REST API，原生异步，SSE 流式响应 |
| 智能体 | LangGraph | StateGraph 多路由 Agent 编排，支持会话持久化 |
| LLM | DeepSeek API | 对话、推理、Agent（工厂模式可切换 Ollama） |
| 知识图谱 | Neo4j | 电商数据（商品、订单、客户），Text2Cypher 查询 |
| 文档检索 | Microsoft GraphRAG | 文档索引 + 4 种检索策略 + 混合检索 |
| Embedding | SiliconFlow (BAAI/bge-m3) | 语义向量生成，免费 API |
| 向量缓存 | Redis | 语义缓存（余弦相似度 >= 0.90 命中） |
| 数据库 | MySQL | 用户、会话、消息持久化 |
| 前端 | Vue（静态 dist） | 聊天界面 |

## 快速开始

### 环境要求

- Python 3.8+
- Docker & Docker Compose（推荐）

### Docker Compose 一键部署（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/blowt16/SmartCS-Agent.git
cd SmartCS-Agent

# 2. 编辑 .env.docker 填入 API Key
#    必填：DEEPSEEK_API_KEY
#    可选：SERPAPI_KEY、VISION_API_KEY

# 3. 一键启动
docker compose up -d

# 4. 查看日志
docker compose logs -f app
```

服务将在 `http://localhost:8000` 启动。

### 手动部署

```bash
# 1. 克隆项目
git clone https://github.com/blowt16/SmartCS-Agent.git
cd SmartCS-Agent

# 2. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate   # Windows
source .venv/bin/activate  # Linux/Mac
pip install -r requirements.txt

# 3. 配置环境变量
cp llm_backend/.env llm_backend/.env
# 编辑 llm_backend/.env 填入 API Key 和数据库连接信息

# 4. 启动 MySQL、Redis、Neo4j（可用 Docker 单独启动）
docker compose up -d mysql redis neo4j

# 5. 初始化数据库
cd scripts
python init_db.py

# 6. 启动服务
cd llm_backend
python run.py
```

### 构建 GraphRAG 知识库索引

```bash
# 确保 llm_backend/.env 中以下变量已配置：
#   GRAPHRAG_API_KEY, Embedding_API_KEY
PYTHONIOENCODING=utf-8 python scripts/build_graphrag_index.py
```

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/chat` | POST | 普通聊天（SSE 流式） |
| `/api/reason` | POST | 深度推理（SSE 流式） |
| `/api/search` | POST | 联网搜索（Function Calling） |
| `/api/langgraph/query` | POST | Agent 多路由查询（SSE 流式） |
| `/api/upload` | POST | 上传文件 -> GraphRAG 索引构建 |
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
│   │   │   ├── search_service.py     # SerpAPI 联网搜索
│   │   │   ├── redis_semantic_cache.py # Redis 语义缓存
│   │   │   └── indexing_service.py   # GraphRAG 索引构建
│   │   ├── lg_agent/                 # LangGraph 智能体
│   │   │   ├── lg_builder.py         # StateGraph 构建与路由
│   │   │   ├── lg_states.py          # 状态定义（Router/AgentState）
│   │   │   └── kg_sub_graph/         # 知识图谱子图
│   │   │       └── agentic_rag_agents/
│   │   │           └── components/
│   │   │               ├── customer_tools/  # GraphRAG 查询 + 混合检索
│   │   │               ├── cypher_tools/    # Text2Cypher 闭环
│   │   │               ├── relevance_grader.py  # LLM 相关性评分
│   │   │               └── hybrid_retrieval/    # BM25+向量 RRF 融合
│   │   ├── graphrag/                 # Microsoft GraphRAG 集成
│   │   ├── models/                   # SQLAlchemy 数据模型
│   │   ├── api/                      # 认证路由
│   │   └── prompts/                  # 提示词模板
│   └── static/dist/                  # Vue 编译后的前端
├── scripts/                          # 工具脚本
│   ├── generate_product_knowledge.py # 从 CSV 生成产品知识文档
│   ├── download_datasets.py          # 下载开源电商 FAQ 数据集
│   ├── download_jddc.py              # 下载 JDDC 客服对话数据集
│   └── build_graphrag_index.py       # 手动构建 GraphRAG 索引
├── docker-compose.yml                # Docker Compose 配置
├── Dockerfile                        # Docker 镜像构建
├── .env.docker                       # Docker 环境变量
├── .env.example                      # 环境变量模板
└── STUDY_NOTES.md                    # 项目学习文档（面试准备）
```

## 设计模式

| 模式 | 应用位置 | 说明 |
|------|---------|------|
| **工厂模式** | `LLMFactory` | 通过 `.env` 配置切换 DeepSeek/Ollama，不改代码 |
| **策略模式** | `config.py` | CHAT/REASON/AGENT 可分别选不同 LLM 服务 |
| **回调模式** | `deepseek_service.py` | `on_complete` 回调触发消息持久化，解耦 LLM 和存储 |
| **责任链** | Text2Cypher | 生成 -> 校验 -> 修正 -> 执行流水线 |
| **状态图** | LangGraph | `StateGraph` + 条件边实现多路由 Agent |

## 数据集说明

本项目内置了三类知识库数据（位于 `llm_backend/app/graphrag/data/input/`）：

| 数据类型 | 文件数 | 来源 | 说明 |
|---------|--------|------|------|
| 产品知识文档 | 10 | 从项目 CSV 聚合生成 | 涵盖 8 个品类 10 款智能家居产品 |
| 电商 FAQ | 81 | [Chinese-EcomQA](https://huggingface.co/datasets/OpenStellarTeam/Chinese-EcomQA) | 1,800 条品牌/商品/推荐问答 |
| 客服对话 | 85 | [JDDC](https://github.com/SimonJYang/JDDC-Baseline) | 2,600+ 条真实电商客服对话 |

## 许可证

MIT License

## 致谢

本项目基于原项目 [wang219416/GraphRAG-](https://github.com/wang219416/GraphRAG-) 二次开发，在此向原作者 [wang219416](https://github.com/wang219416) 的开源贡献表示感谢。

- [Microsoft GraphRAG](https://github.com/microsoft/graphrag) - 文档索引与检索
- [LangGraph](https://github.com/langchain-ai/langgraph) - Agent 编排框架
- [JDDC](https://jddc.jd.com/) - 京东客服对话数据集
- [Chinese-EcomQA](https://huggingface.co/datasets/OpenStellarTeam/Chinese-EcomQA) - 中文电商问答数据集
- [SiliconFlow](https://siliconflow.cn/) - 免费 Embedding API
