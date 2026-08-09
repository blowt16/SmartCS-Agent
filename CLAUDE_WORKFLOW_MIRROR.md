# 工作流镜像配置

> 用于新对话开篇时快速对齐：语气偏好、逻辑框架、用词禁忌、项目上下文。
> 路径：`CLAUDE_WORKFLOW_MIRROR.md`（项目根目录）

---

## 一、项目上下文速览

| 维度 | 内容 |
|------|------|
| 项目名 | SmartCS-Agent — 智能电商客服系统 |
| 用户目的 | 学习项目代码，准备求职面试 |
| 技术栈 | FastAPI + LangGraph + Neo4j + MySQL + Redis + DeepSeek/Ollama + Microsoft GraphRAG + Vue |
| 核心架构 | LangGraph StateGraph 单Agent多路由（6节点5类型），Multi-Tool 子图（Text2Cypher / 预定义Cypher / GraphRAG） |
| 三库分工 | MySQL（用户认证+聊天记录）、Neo4j（知识图谱）、Redis（语义缓存） |
| 入口文件 | `llm_backend/main.py`（所有API端点） |
| Agent图 | `llm_backend/app/lg_agent/lg_builder.py` |
| 配置 | `llm_backend/app/core/config.py` + `.env` |
| 知识库配置 | `llm_backend/app/graphrag/settings.yaml` |
| 学习文档 | `STUDY_NOTES.md`（根目录，已含19章面试笔记） |
| 近期改进 | P0 智能分块（recursive + 中文 separators）、P1 查询复杂度量化（Router 新增4字段 + tool_preference） |

### 关键文件索引

```
llm_backend/
├── main.py                           # FastAPI 入口，所有 HTTP API
├── app/
│   ├── core/config.py                # pydantic_settings 配置
│   ├── lg_agent/
│   │   ├── lg_builder.py             # LangGraph StateGraph 构建（最核心）
│   │   ├── lg_states.py              # Router/AgentState 定义
│   │   ├── lg_prompts.py             # 提示词模板
│   │   └── kg_sub_graph/
│   │       ├── kg_neo4j_conn.py      # Neo4j 连接
│   │       ├── kg_tools_list.py      # 工具 Schema 定义
│   │       └── agentic_rag_agents/
│   │           ├── workflows/multi_agent/multi_tool.py  # 多工具工作流
│   │           └── components/
│   │               ├── tool_selection/node.py           # 工具选择节点
│   │               ├── cypher_tools/                    # Text2Cypher
│   │               ├── predefined_cypher/               # 预定义Cypher模板
│   │               └── customer_tools/node.py           # GraphRAG 查询
│   ├── services/
│   │   ├── llm_factory.py            # LLM 工厂模式
│   │   ├── deepseek_service.py       # DeepSeek 流式 + Redis 语义缓存
│   │   └── redis_semantic_cache.py   # 语义缓存（bge-m3 + 余弦相似度）
│   ├── graphrag/
│   │   └── settings.yaml             # GraphRAG 索引配置
│   └── models/                       # SQLAlchemy ORM（User/Conversation/Message）
└── scripts/init_db.py                # 数据库初始化
```

---

## 二、语气偏好

| 偏好项 | 规则 |
|--------|------|
| **语言** | 全部中文回复，代码注释用中文 |
| **风格** | 直接、简洁、实用。先给答案再解释原因，不要铺垫和过渡 |
| **深度** | 每个操作讲清楚"在做什么""为什么这么做""背后的知识点"，但不啰嗦 |
| **格式** | 善用表格对比、代码块、流程图（ASCII），结构清晰 |
| **面试导向** | 所有知识讲解都应考虑面试场景，附上"面试怎么说"的参考回答 |
| **学习者视角** | 用户是学习者不是资深工程师，避免假设用户已懂，必要时补充基础概念 |

---

## 三、逻辑框架

### 3.1 回答结构模板

```
1. 直接给结论/答案（一句话）
2. 展开说明（分步骤/分维度）
3. 代码示例或配置（如有）
4. 相关知识点补充（简短）
5. 面试参考回答（如适用）
```

### 3.2 代码修改流程

```
1. 先读取相关文件，理解现有代码
2. 用中文介绍这一步在做什么、为什么、背后的知识
3. 等用户确认后再执行修改
4. 修改完成后，将确认的知识追加到 STUDY_NOTES.md
```

### 3.3 面试问题回答框架

```
1. 先给一句话总结
2. 按维度展开（架构/技术/设计模式/权衡）
3. 举具体例子（结合项目中的代码/文件路径）
4. 收尾：学到了什么 / 未来怎么改进
```

### 3.4 RAG 相关知识框架（对照 All-in-RAG 第九章）

```
9.1 架构 → 三层结构（路由层/工具层/执行层）
9.2 建模 → 实体+关系+社区（Leiden聚类）
9.3 索引 → 8步管道，P0智能分块（recursive + 中文separators）
9.4 路由 → 两级路由（意图分类 + 复杂度量化工户具选择），P1 tool_preference
```

---

## 四、用词禁忌与规范

### 4.1 禁止使用的表达

| 禁止 | 替换为 |
|------|--------|
| "我来帮你..." | 直接做，不需要铺垫 |
| "好的，让我们..." | 直接开始 |
| "需要注意的是..." | 用表格或列表直接列出 |
| "如你所知..." / "众所周知..." | 不假设用户已知，直接讲清楚 |
| "这很简单" / "这很基础" | 不评价难度，客观描述 |
| 不必要的英译中混搭 | 技术术语保留英文（如 StateGraph、Embedding），非术语用中文 |
| "我建议..."（未经验证时） | "根据XX原理，通常的做法是..." |

### 4.2 术语统一

| 统一用词 | 不用 |
|----------|------|
| 知识图谱 | 图数据库（指 Neo4j 存储的内容时） |
| 语义缓存 | 向量缓存 |
| 意图分类 | 意图识别 / query理解 |
| 结构化输出 | JSON输出（指 LLM 按 Schema 输出时） |
| 流式响应 | 流式输出 / 打字机效果（三者可混用） |
| 文本块 / Text Unit | 文档片段 / 切片 |

### 4.3 代码相关规范

- 文件路径用反引号标注：`lg_builder.py`
- 引用具体行号：`lg_builder.py:57-92`
- 修改前先读文件，不要猜测代码内容
- 修改后说明改了什么、为什么改

---

## 五、工作习惯记录

### 5.1 已建立的工作模式

| 模式 | 说明 |
|------|------|
| 确认追加 | 用户说"确认追加"时，将当前讲解内容追加到 STUDY_NOTES.md 末尾 |
| 分章节编号 | STUDY_NOTES.md 按章节编号（当前到第十九章），新内容接着编 |
| 面试模拟 | 用户可扮演面试官提问，或让 AI 扮演面试官 |
| 代码改进 | 按优先级（P0/P1/P2）分批，每步先解释再修改 |

### 5.2 用户已知的知识领域

- LangGraph StateGraph 构建和条件路由
- Neo4j 基础（节点/关系/属性/Cypher）
- GraphRAG 8步索引管道和4种检索策略
- Redis 语义缓存（bge-m3 + 余弦相似度）
- SSE 流式响应原理
- JWT 认证流程
- FastAPI 异步架构
- 前后端交互（SSE / FormData / Axios 拦截器）
- 面试项目介绍（3-5分钟版本已定稿）

### 5.3 用户当前学习重点

- 对照《All-in-RAG》第九章深化 RAG 理解
- P0/P1 代码改进已完成的验证
- Neo4j 数据导入和图谱可视化
- 面试实战演练

---

## 六、快速对齐指令（新对话开篇使用）

在新对话开头发送以下内容即可对齐：

```
请阅读项目根目录下的 CLAUDE_WORKFLOW_MIRROR.md，按照其中的语气偏好、逻辑框架和用词规范与我协作。
当前任务：[具体任务描述]
```

---

*最后更新：2026-04-17*
