# rag_tool 内 HyDE 查询改写（经典 HyDE + 规则门控）— 设计文档

日期：2026-09-01
分支：feat/tool-plan-impl
关联文档：[[SPEC_RAG_TOOL_OPTIMIZATION.md]] [[SPEC_REMOVE_QUERY_PREPROCESSING.md]] [[PROJECT_ANALYSIS.md §4.9]] [[SPEC_ENTRY_LLM_RESOLUTION.md]]（入口多轮消息 LLM 统一消解，关联工作项）

## 背景与目标

### 背景

1. `rag_retrieval` tool 当前把入参 query **原样**送入检索链路（HNSW ∥ BM25 → RRF → Reranker），无任何改写环节。口语化/模糊问法（"能充电吗" vs 文档"USB+Type-C充电口"）与文档词面存在鸿沟，向量路召回质量受影响
2. rag_tool 将服务于**售前业务 agent**（`llm.bind_tools([rag_retrieval, product_stock_lookup])`），query 已过入口指代消解，是完整问题
3. **与 2026-08-21 删除的查询预处理管道对比**（见 SPEC_REMOVE_QUERY_PREPROCESSING.md，当时删除了 BudgetGuard 门控下的纠错/扩展/Multi-Query+HyDE 三步）：

| 维度 | 已删除管道（2026-08-21） | 本次 HyDE |
|---|---|---|
| 位置 | LangGraph graphrag-query 子图入口 | rag_tool 内（tool 被调用时才执行） |
| 触发 | 每轮 graphrag 查询固定 3~4 次 LLM 调用 | 规则命中才 1 次 LLM 调用 |
| 成本 | 每轮固定开销 | 被 agent 调用门控 + 规则门控双重收敛 |

### 目标

| 目标 | 量化指标 |
|---|---|
| 口语化/模糊 query 向量路召回提升 | 假设文档改写后，改写必要性测试集样本的向量路 top-N 命中率 ≥ 改写前 |
| 成本收敛 | 规则门控下 LLM 调用仅发生在规则命中或空结果兜底路径 |
| 零风险 | fail-open：HyDE 任何失败不影响 tool 三态返回；HYDE_ENABLED=false 完全退化为现状 |

### 设计原则

1. **经典 HyDE 形态**：LLM 生成假设文档 → 仅向量路用假设文档的 embedding 检索；BM25 与 Reranker 始终用原 query（精排语义不失真）
2. **仅 rag_tool 生效**：RAGRetrieverService 只加可选参数，LangGraph 售前路径行为逐字节不变
3. **静态语言级词表**：门控规则只用口语/疑问/规格词表（不随商品上新变化，维护成本≈0），**不引入品牌/品类商品词典**
4. **tool 保持薄封装**：HyDE 核心逻辑（规则 + prompt + LLM 调用）放 `app/services/hyde_service.py`，rag_tool 只做编排与降级

## 1. 决策记录

| 决策点 | 决议 |
|---|---|
| HyDE 形态 | 经典 HyDE（假设文档只走向量路） |
| 实施位置 | 仅 rag_tool 路径；service 层 `search()` 加可选参数 `embed_query` |
| 门控方式 | 规则判定 R1 ∨ R2 + 空结果兜底 R3 |
| 规则词表 | 静态语言级（口语疑问词/模糊疑问词/规格术语），无商品词典 |
| 模块归属 | `app/services/hyde_service.py` + 懒加载单例（对齐 RAGRetrieverService 模式） |
| 降级策略 | fail-open：HyDE 生成异常/超时 → None → 用原 query 检索，不阻断 tool |
| LLM 选择 | 复用 `AGENT_SERVICE`（对齐 lg_builder L68-75：ChatDeepSeek/ChatOllama + thinking disabled） |
| 超时 | 复用 `TOOL_DB_TIMEOUT_SECONDS`（10s），不新增超时配置 |
| 已知盲区 | 书面化混合查询（如"米家智能晾衣机有价格保护吗"）规则不命中、不改写——该场景终局解法为实体识别（CLAUDE.md §3 已知局限），HyDE 收益有限，接受漏改 |

## 2. 规则设计（MVP）

三个静态词表（模块级常量，语言级，不随商品上新变化）：

| 词表 | 内容（示例，实施时定稿） | 判定角色 |
|---|---|---|
| 口语疑问词表 | 咋、咋样、能不能、可不可以、会不会、好不好、耐不耐、怎么弄、咋办、啥、为啥、值不值、贵不贵 + 正则模式 `能/可以…吗`（长度 ≤8 拦截） | R1 |
| 模糊疑问词表 | 多大、多长、多宽、多久、多少、哪个、哪种、什么、怎么样 | R2 |
| 规格术语表 | 尺寸、座深、坐宽、承重、功率、电压、材质、面料、重量、长度、宽度、高度、功能、保修、质保、退换、运费、价格、接口、续航、噪音、升降、遥控、语音、App、按摩、烘干、晾晒、杀菌、照明、电机 | R2 |

### 规则

- **R1 口语疑问结构**：命中口语疑问词表（子串匹配）∨ 命中 `能/可以 + … + 吗` 正则模式 → 改写
- **R2 模糊疑问**：命中模糊疑问词表 ∧ **未命中**规格术语表 → 改写
- **R3 空结果兜底**：R1/R2 均未命中 → 用原 query 检索 → **结果为空** → HyDE 改写重检索一次

判定示例：

| query | R1 | R2 | 结果 | 理由 |
|---|---|---|---|---|
| 能充电吗 | ✓（能…吗） | — | 改写 | 口语 vs 文档"USB+Type-C充电口" |
| 能洗多少件衣服 | ✗（无"吗"结尾） | ✓ | 改写 | 模糊疑问（R2 捕获） |
| 座深多少 | ✗ | ✗（术语"座深"） | 不改写 | 术语已对齐 |
| 保修多久 | ✗ | ✗（术语"保修"） | 不改写 | 术语已对齐，BM25 直接命中 |
| 沙发参数 | ✗ | ✗ | 不改写 | 精确 |
| 米家智能晾衣机有价格保护吗 | ✗ | ✗ | 不改写 | 书面化混合查询——**已知盲区，接受** |

### 盲区（坦诚说明）

书面化混合查询（含术语、无口语词、非具体疑问词结构）不命中规则。该场景的检索失败根因是**商品块 vs 政策块的路由竞争**，终局解法是实体识别（已定演进方向），HyDE 假设文档对其收益有限（商品块仍占优）。MVP 接受此漏改，样例留档于测试集。

## 3. hyde_service 设计（新文件 `app/services/hyde_service.py`）

```python
class HydeService:
    """HyDE 查询改写服务（规则门控 + 假设文档生成，fail-open）"""

    def should_rewrite(self, query: str) -> bool:
        """R1 ∨ R2 规则判定（纯字符串匹配，不抛异常、不调 LLM）"""

    async def generate_hypothetical_document(self, query: str) -> str | None:
        """LLM 生成假设文档；异常/超时 → None（fail-open，调用方降级原 query）"""

def get_hyde_service() -> HydeService:
    """模块级懒加载单例（沿用 lock + double-check 模式）"""
```

- **LLM 客户端**：按 `AGENT_SERVICE` 构建（对齐 lg_builder L68-75）：
  - DeepSeek：`ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL, temperature=settings.HYDE_TEMPERATURE, max_tokens=settings.HYDE_MAX_TOKENS, extra_body={"thinking": {"type": "disabled"}})`
  - Ollama：`ChatOllama(model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL, ...)`
  - 调用 `model.ainvoke()`（异步，不阻塞事件循环）
- **超时**：`asyncio.wait_for(..., timeout=settings.TOOL_DB_TIMEOUT_SECONDS)`，超时 → None
- **Prompt**（语义保真靠 prompt 约束 + 低温承担，无机械后置校验）：
  - system："你是京东智能家居客服系统的知识库文档编写助手。根据用户问题，以商品知识文档条目的口吻撰写一段假设文档片段（50~100 字）。要求：1) 内容是对该问题可能答案的合理推演，覆盖问题涉及的主题（功能/参数/政策）；2) 使用商品文档常见术语；3) 仅输出文档片段本身，不要解释、不要前缀；4) 仅围绕用户问题中出现的实体与主题展开，禁止虚构用户未提及的商品名/品牌/具体数值；5) 必须包含用户问题中的关键词（商品名/功能词/主题词）；6) 不得改变用户意图。"
  - user：`用户问题：{query}`
- **生成前代词残留检测**：query 含"它/这款/那款/该"等指代词 → 直接返回 None（入口消解漏网时防 LLM 瞎猜指代对象偏离语义，fail-open 语义统一）
- **生成后校验**（仅两条，不含语义回显检查）：① 输出非空 ② 长度 ≤100 字；校验失败 → None → 调用方降级原 query
- **word 表定稿**：三个词表在实施时以"命中样例 ≥5 条/词表"为验收定稿（见 §8 测试）

## 4. rag_tool 改造

```python
@tool
async def rag_retrieval(query: str) -> str:
    # 入参校验（不变）

    hyde = get_hyde_service()
    should_rewrite = settings.HYDE_ENABLED and hyde.should_rewrite(query)
    hypothetical = None
    if should_rewrite:
        hypothetical = await hyde.generate_hypothetical_document(query)  # fail-open → None

    try:
        docs = await _search_with_retry(query, embed_query=hypothetical or query)
    except Exception as e:
        # 三态错误返回（不变）

    # R3 空结果兜底：规则未命中 + 空结果 → HyDE 重检索一次
    if not docs and settings.HYDE_ENABLED and not should_rewrite:
        hypothetical = await hyde.generate_hypothetical_document(query)
        if hypothetical:
            docs = await _search_with_retry(query, embed_query=hypothetical)

    # 空/成功返回（不变，空结果建议可注明"已尝试改写仍无结果"）
```

`_search_with_retry` 签名同步扩展：`_search_with_retry(query, embed_query: str | None = None)`。

## 5. RAGRetrieverService 适配（最小改动）

```python
async def search(self, query: str, top_k: Optional[int] = None,
                 embed_query: Optional[str] = None) -> List[Dict[str, Any]]:
    # 向量路：embedding 用 embed_query or query
    # BM25 / Reranker：始终用 query（原样）

async def _vector_search(self, query: str, top_k: int,
                         embed_query: Optional[str] = None) -> List[Dict[str, Any]]:
    query_vec = (await get_embedding_provider().embed([embed_query or query]))[0]
```

- 不传 `embed_query` → 行为与现状**完全一致**（LangGraph 路径零影响，满足"仅 rag_tool"范围约束）

## 6. 配置（config.py，循 `TOOL_*` 惯例）

```python
# HyDE 查询改写（rag_tool 内，经典 HyDE：假设文档只走向量路，BM25/精排用原 query）
HYDE_ENABLED: bool = True          # 总开关；false → 完全退化为现状（一键回滚）
HYDE_TEMPERATURE: float = 0.3     # 假设文档生成温度（稳定优先）
HYDE_MAX_TOKENS: int = 120        # 假设文档最大输出 token（≈80-110 字，含截断余量；目标 50~100 字）
```

复用项（不新增配置）：LLM/模型 = `AGENT_SERVICE`/`DEEPSEEK_MODEL`/`OLLAMA_AGENT_MODEL`；超时 = `TOOL_DB_TIMEOUT_SECONDS`。

## 7. 降级链（fail-open）

| 环节 | 失败 | 行为 |
|---|---|---|
| 规则层 | 纯字符串匹配，不抛异常 | — |
| HyDE 生成（规则命中路径） | LLM 异常/超时 | `logger.warning` + 用原 query 检索，不阻断 tool |
| HyDE 生成（兜底路径） | 同上 | 返回现有"未检索到"建议 |
| 检索本身 | 不变 | 现有三态协议 |

## 8. 测试方案

### 8.1 `tests/test_hyde.py`（新文件，纯规则 + mock LLM）

- **命中样例**：每规则 ≥5 条，断言 `should_rewrite` 全触发（覆盖 R1 子串、R1 正则模式、R2）
- **不误触发**：「保修多久」「座深多少」「沙发参数」等精确查询，断言不触发
- **盲区留档**：「米家智能晾衣机有价格保护吗」断言不触发（预期行为，注释说明理由）
- **生成器**：mock LLM 客户端 → 断言返回假设文档；mock 异常/超时/空输出/超长输出（>100 字）→ 断言返回 None（fail-open）；含代词 query（"它能充电吗"）→ 断言不调 LLM、直接 None

### 8.2 `tests/test_rag_tool.py` 扩展（mock `get_hyde_service`）

- 规则命中路径：断言 `search` 收到 `embed_query=假设文档`
- R3 兜底路径：断言空结果后二次检索且 `embed_query=假设文档`
- 现有三态测试兼容性分析：

| 现有测试 | query | 规则 | HyDE 触发？ | 影响 |
|---|---|---|---|---|
| test_success_* | 沙发参数/灯/门锁 | 不命中 | 否 | 无影响 |
| test_empty_result_advice | 不存在的知识 | 不命中 → 空结果 → **R3 触发** | 是 | **需 mock** `get_hyde_service` 返回 generate→None，断言不变 |
| test_error_fallback_json | 沙发 | 不命中 | 否 | 无影响（检索抛错在 HyDE 之后） |
| test_empty_query_invalid_argument | 空 | — | 否 | 校验前置短路 |

### 8.3 规则层不 mock 的约定

`should_rewrite` 是纯函数，直接测真实词表；`generate_hypothetical_document` 与 tool 编排层 mock LLM/检索——沿用现有 mock 风格（`patch("app.tools.rag_tool.get_rag_retriever_service")`）。

## 9. 验证方案（按序执行）

1. `uv run pytest tests/test_hyde.py -v` → 全绿（规则命中/不误触发/盲区留档）
2. `uv run pytest tests/test_rag_tool.py -v` → 全绿（含扩展用例，现有用例仅 mock 调整）
3. 全量回归：`uv run pytest -q`
4. 手动端到端（可选）：启动服务，发口语化 query（"能充电吗"），日志确认 HyDE 生成与 embed_query 生效；`HYDE_ENABLED=false` 验证退化

## 10. 文档同步

- 本 spec 入库 `docs/superpowers/specs/`
- `PROJECT_ANALYSIS.md` §4.9 补 HyDE 小节：形态/门控/成本与已删除管道的差异（位置、触发、成本三维度对比表）+ 盲区说明
- SPEC_RAG_TOOL_OPTIMIZATION.md 的演进方向表追加一行（HyDE 已实施，指向本 spec）

## 11. 风险与避坑清单

1. **现有测试破坏**：`test_empty_result_advice` 空结果路径会触发 R3 兜底，必须 mock `get_hyde_service`，否则真实调 LLM（联网测试不可控）
2. **"能"子串误命中**：正则模式 `能…吗` 必须限长（≤8 字），防"智能门锁能…"类误判（"智能"含"能"）；实现用正则而非裸子串
3. **R2 规格术语表膨胀**：术语表只收语言级静态词，不收品牌/品类/商品名（维护成本原则）；词表定稿以测试集命中样例为验收
4. **语义缓存不受影响**：入口缓存 key=消解后消息，缓存命中短路不进图不调 tool；HyDE 发生在 tool 内，不涉及缓存键（无需改动）
5. **LangGraph 路径零影响**：`search()` 新参数默认 None；实施后跑一次 graph compile + 售前查询冒烟确认
6. **llm_factory 不参与**：HyDE 用 langchain Chat 客户端（对齐 lg_builder），**不**走 `LLMFactory.create_chat_service()`（那是 CHAT_SERVICE 聊天链路，语义不同）
7. **无回显校验的污染面**：LLM 跑题时假设文档会带偏向量路检索——但经典 HyDE 结构有天然防护：BM25 与 Reranker 始终用原 query，污染面仅限向量路；且低温和 prompt 约束已尽量压住跑题概率
