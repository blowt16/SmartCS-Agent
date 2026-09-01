# 入口多轮消息 LLM 统一消解（指代消除 + 语义补全）实施规格

> **用途**: query 进入系统（入口）时，对多轮对话消息**统一走 LLM 消解**——一次调用同时完成指代消除（代词替换）与语义补全（省略主语补全），替代现状"正则检测（代词表/触发词）→ 命中才 LLM 消解"的两段式流程。纯语气词跳过缓存，无历史直通
> **技术栈**: Python + FastAPI + DeepSeek/Ollama（复用现有消解链路，零新增依赖）
> **状态**: **待实施**（2026-09-01 决策；**替代并删除** SPEC_PRONOUN_RESOLUTION_OPTIMIZATION.md 与 SPEC_ELLIPSIS_QUESTION_GATE.md——均未实施，内容并入本 spec）
> **关联文档**: [[SPEC_SEMANTIC_CACHE_RESOLVE.md]] [[2026-09-01-rag-tool-hyde-design.md]] [[PROJECT_ANALYSIS.md]] [[项目问题.md #7]]

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [调研结论](#2-调研结论)
3. [方案设计](#3-方案设计)
4. [数据流与代码形态](#4-数据流与代码形态)
5. [判定示例](#5-判定示例)
6. [成本与影响面](#6-成本与影响面)
7. [测试方案](#7-测试方案)
8. [验证方案](#8-验证方案)
9. [决策记录](#9-决策记录)
10. [风险与避坑清单](#10-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 现状与问题

现状入口消解是"正则判定 + LLM"两段式（`pronoun_detector.detect_pronoun` 三层 → 命中才 `resolve_pronouns`）：

```
层1 显性指代词（PRONOUNS 固定表）  ← 正则判定指代
层2 省略主语（ELLIPSIS_TRIGGERS 22 词）← 正则判定省略
层3 纯语气词（FILLER_PHRASES）
```

问题：

1. **指代正则不完备**：代词表（它/这款/那个/该…）是封闭枚举，漏任何写法人称/指示表达即漏检
2. **省略正则不完备**（项目问题 #7）：触发词表打地鼠——"需要充电吗""带充电口吗""需不需要充电"等换问法全漏
3. **两层正则本质是同一件事的廉价代理**：判断"消息是否依赖对话上下文"是语义任务，正则只能枚举表象

### 1.2 目标

| 目标 | 说明 |
|---|---|
| 多轮消息统一 LLM 消解 | 指代消除 + 语义补全一次调用完成（含自包含透传），不依赖任何正则判定 |
| 正则判定全部删除 | PRONOUNS / ELLIPSIS_TRIGGERS / INTERROGATIVE_CONFUSION / ELLIPSIS_MAX_LEN 不再存在 |
| 保留两个成本闸门 | 纯语气词跳过缓存（SKIP_CACHE）；无历史直通（PASS_THROUGH）——与主流一致 |
| 成本可控 | 消解模型可配置降档（`RESOLVE_MODEL`）+ 三态日志观测 |

---

## 2. 调研结论

2026-09-01 网络检索主流多轮对话 query 处理方案：

| 来源 | 做法 |
|---|---|
| [LangChain `create_history_aware_retriever`](https://github.com/langchain-ai/langchain/blob/bc5a0ef6cab57821a69470fe205b9a5dbf1dabc8/libs/langchain/langchain/chains/history_aware_retriever.py) | `RunnableBranch`：无历史 → 直通检索；**有历史 → 一律 LLM 重写**，prompt 内给出口"已自包含则原样返回" |
| [LlamaIndex `CondenseQuestion`](https://developers.llamaindex.ai/python/framework/module_guides/deploying/chat_engines/usage_pattern/) | 有历史即重写为独立查询，按设计无条件 |
| [IBM Granite RAG](https://huggingface.co/ibm-granite/granitelib-rag-r1.0) | 微调专用 Query Rewrite 模型，同样无条件走重写 |
| [Nodrat 实践](https://github-wiki-see.page/m/selmanays/nodrat/wiki/conversational-query-rewriting) | 不重写清单：助手相关/元问题、指令类跟进、显式主语例外（自锚定）；重写应 <500ms/轮 |
| [DEV RAG Series](https://dev.to/wonderlab/rag-series-18-conversational-rag-the-pronoun-problem-in-multi-turn-dialogue-2g7g) | "改写前检索"是基础架构而非优化项；指代/省略/自包含检测全部由重写 LLM 完成（prompt 出口） |

**关键结论**：

1. 主流的指代消除与省略补全**都是重写 LLM 一次调用完成**——正则判定不是标配，是退化的替代品
2. 唯一被主流保留的门控：**无历史直通**（LangChain `RunnableBranch` 源码明确）
3. 实践要点：输出仅问题文本（无解释）、截断守卫（max_tokens/首行）、指令类跟进不盲改、重写结果仅用于检索/缓存（不展示给用户）
4. 本项目采用该方案：多轮 → LLM 一次消解（指代 + 省略 + 自包含出口），删除两段式正则

---

## 3. 方案设计

### 3.1 入口流程（`_resolve_message` 重写）

```
query 进入系统（redis_semantic_cache._resolve_message）
  ├─ RESOLVE_ENABLED=false → 原样透传（总开关，不变）
  ├─ 纯语气词（FILLER_PHRASES 整句匹配）→ None（跳过缓存，不查不写）
  ├─ 无历史（首条消息）→ 原样透传（直通，对齐主流）
  ├─ 多轮 → resolve_pronouns(LLM)  ← 统一消解：指代 + 省略 + 自包含出口
  └─ 失败/超时/空 → 降级原消息（现有异常路径，不变）
```

- `detect_pronoun` 三层结构**整体删除**——正则判定不再是消解的前置
- 保留的两个决策：语气词（缓存优化，零成本，有真实判别力）与 has_history（直通门控，主流同款）

### 3.2 prompt 设计（`RESOLVE_SYSTEM_PROMPT` 重写全文）

**system**（完整文本）：

> 你是一个多轮对话的指代消解与语义补全专家。
> 你的任务是根据对话历史，把用户当前问题中依赖上下文的成分（指代词、省略的主语/宾语、不完整信息）补全为完整、独立的问题。
>
> 规则：
> 1. 如果当前问题包含代词（他/她/它/那个/这个/那件/这件/该产品等），用历史中的实体替换
> 2. 如果当前问题是省略句（如"有货吗""多少钱""能退吗""需要充电吗"），从历史中补全主语
> 3. 如果当前问题已完整独立（包含明确主语、不依赖上下文），**直接原样返回，不要添加或修改任何信息**
> 4. 如果当前问题是命令式指令（如"查一下价格"），补全为完整的查询意图，不要改写成实体搜索
> 5. 不要添加历史中没出现过的信息，只做补全，不做扩展
> 6. 只输出消解后的完整问题文本，不要任何解释

**user 消息格式**（沿用 `_format_history` 拼接，不变）：

> 对话历史:
> {history_text}
>
> 当前问题: {raw_query}
>
> 请输出消解后的完整问题：

**参数与约束**：

| 项 | 值 | 说明 |
|---|---|---|
| `RESOLVE_LLM_TEMPERATURE` | 0.0 | 必须为 0——同输入同输出，缓存 key 一致性前提（查写同 key 才能命中） |
| `RESOLVE_MAX_TOKENS` | 200 | 输出截断守卫（对齐主流"输出仅问题文本"） |
| `RESOLVE_TIMEOUT_MS` | .env 配置 | 超时降级原消息（现有异常路径） |

**与现状 prompt 的差异**（强化点）：规则 1/2 措辞细化为"依赖上下文的成分（指代词、省略的主语/宾语、不完整信息）"；规则 3 升级为显式主路径（"直接原样返回，不要添加或修改任何信息"——主流自包含出口）；**新增规则 4**（命令式指令处理，Nodrat 实践）；规则 2 省略句示例补充"需要充电吗"（修复目标场景）。

**输入输出示例（测试断言契约，配合 §7.3 FakeLLM 用例）**：

| 历史尾轮 | 当前问题 | 期望输出 |
|---|---|---|
| 这个按摩椅怎么样 | 需要充电吗 | 这款按摩椅需要充电吗 |
| 芝华仕按摩椅多少钱 | 它支持快充吗 | 芝华仕按摩椅支持快充吗 |
| 芝华仕按摩椅多少钱 | 芝华仕按摩椅多少钱 | 芝华仕按摩椅多少钱（原样返回） |

### 3.3 成本：消解模型可配置降档

- 新增配置 `RESOLVE_MODEL: str = ""`（空 = 沿用 `CHAT_SERVICE` 模型；配置后消解走指定模型——本地 Ollama 或更快档位，目标 <500ms/轮）——**统一入 `.env`**（对齐 `RESOLVE_*`/`TOOL_*` 先例：`config.py` 声明默认值兜底，实际生效值由 `.env` 配置；`.env` 已 gitignore 不入库，内联注释即文档）
- 默认不降档（`.env` 不填即空 = 沿用现状），上线实测延迟/质量后再调（先实测再定方案）
- `.env` 追加行：`RESOLVE_MODEL=                    # 消解专用模型（可选）；空 = 沿用 CHAT_SERVICE 模型`

### 3.4 保留不变的

- `resolve_pronouns` 历史格式化（`_format_history`）、失败降级、`RESOLVE_MAX_TURNS`/`RESOLVE_TIMEOUT_MS` 等配置
- 消解结果消费链：缓存 key（`_get_hash_id(resolved)`）+ 图内 query（现状链路不变）

---

## 4. 数据流与代码形态

### 4.1 `pronoun_detector.py` — 删除

- 文件整体删除：`PRONOUNS` / `ELLIPSIS_TRIGGERS` / `ELLIPSIS_MAX_LEN` / `INTERROGATIVE_CONFUSION` / `FILLER_PHRASES` / `DetectionDecision` / `detect_pronoun`
- `FILLER_PHRASES` + `_is_filler` 移入 `redis_semantic_cache.py`（模块级私有，整句匹配语义不变）

### 4.2 `redis_semantic_cache.py` — `_resolve_message` 重写

```python
async def _resolve_message(self, messages, raw, resolve_llm) -> Optional[str]:
    """入口统一消解：语气词跳过 / 无历史直通 / 多轮 LLM 消解。

    lookup 和 update 必须走同一套逻辑（SPEC 原则三），
    保证消解后的消息在写入和查询时生成相同向量，从而能命中。
    """
    if not settings.RESOLVE_ENABLED:
        return raw

    if settings.RESOLVE_SKIP_FILLER and _is_filler(raw):
        logger.info("纯语气词，跳过缓存: '{}'", raw)
        return None

    # 无历史直通（对齐 LangChain：无 chat_history 不重写）
    history_msgs = [m for m in messages[:-1] if m.get("role") in ("user", "assistant")]
    if not history_msgs:
        return raw

    if resolve_llm is None:
        logger.warning("多轮消息但未注入 LLM，降级为原始消息: '{}'", raw)
        return raw

    return await resolve_pronouns(resolve_llm, messages, raw)
```

### 4.3 `config.py`

```python
RESOLVE_MODEL: str = ""   # 消解专用模型（可选）；空 = 沿用 CHAT_SERVICE 模型；实际生效值由 .env 配置（env 统一入口，见 §3.3）
```

### 4.4 测试文件迁移

- `app/test/test_pronoun_resolve.py` → 重写为 `_resolve_message` 级别测试（见 §7）
- `app/test/test_entry_cache.py` → 同步调整（见 §7.2）

---

## 5. 判定示例

| 消息 | 场景 | 走哪条路径 | 结果 |
|---|---|---|---|
| 需要充电吗 | 多轮（上文按摩椅） | LLM 消解 | 补全："这款按摩椅需要充电吗" |
| 它支持快充吗 | 多轮 | LLM 消解 | 指代替换为历史商品 |
| 那个按摩椅多少钱 | 多轮 | LLM 消解 | 指代替换（"那个按摩椅"→具体型号） |
| 多少钱 | 多轮 | LLM 消解 | 补全主语 |
| 芝华仕按摩椅多少钱 | 多轮 | LLM 消解 | 规则 3 原样返回（自包含）——多一次廉价调用，零语义损失 |
| 换一个 | 多轮 | LLM 消解 | 指令类规则 → 补全为查询意图（或原样，LLM 判断） |
| 好的 / 谢谢 | 多轮 | 语气词 | 跳过缓存（不查不写） |
| 任何 query | 首条消息 | 直通 | 无历史不消解（零成本） |

---

## 6. 成本与影响面

| 维度 | 分析 |
|---|---|
| 消解面 | 触发词时代 ~15% → **多轮非语气词消息全覆盖**（预估 50~70% 消息）——这是主流的固有取舍（用户 2026-09-01 确认采用） |
| 单次成本 | `RESOLVE_MODEL` 可降档（§3.3），目标 <500ms/轮；DeepSeek 现状 ~2s 可接受兜底 |
| 缓存收益 | 多轮缓存 key 从"触发词命中才消解/否则原文"变为**全部完整问题** → 命中率上升，部分自偿 |
| 顺带修复 | ① 首条消息代词/触发词空转（原残余）② 多轮省略句漏检（项目问题 #7）③ 代词表枚举遗漏——三个已知问题一并消除 |
| 行为变化 | 单轮一律直通（对齐主流）；多轮正则判定消失（判定质量完全交给 LLM + 温度 0） |
| 存量缓存 | 多轮消息 key 从原文/旧消解变为新消解 → 旧 key 失效，TTL 自然淘汰，无需迁移 |

---

## 7. 测试方案

### 7.1 `_resolve_message` 级别测试（重写 test_pronoun_resolve.py）

mock `resolve_pronouns`（FakeLLM 模式），覆盖路径：

- **多轮 → 调 LLM**：`("需要充电吗", [历史], NEED_RESOLVE 路径)`——断言 `resolve_pronouns` 被调用且拿到完整 messages
- **无历史直通**：`("多少钱", [], 原样)`——断言不调 LLM
- **语气词跳过**：`("好的", [历史], None)`——断言不调 LLM、返回 None
- **降级**：resolve_pronouns 异常/超时/空 → 返回原消息
- **RESOLVE_ENABLED=false** → 原样透传

### 7.2 `test_entry_cache.py`

- S1（"那个有货吗" 无历史）→ 直通 → 缓存 lookup 执行（mock 未命中）→ graph 照常——断言"lookup 执行但未命中"（原"跳过 lookup"调整）
- 其余流程断言不变（补消解路径 mock）

### 7.3 FakeLLM 消解质量用例（prompt 强化回归）

- 指代消解：历史含"芝华仕按摩椅"，当前"它能调角度吗" → 补全含"芝华仕按摩椅"
- 省略补全：历史同上，当前"需要充电吗" → 补全含实体
- 自包含出口：当前"芝华仕按摩椅多少钱" → 原样返回
- 指令类："查一下价格" → 查询意图（或原样，按 LLM 输出断言契约）
- 不添加：历史无 X，补全结果不含 X

### 7.4 多轮评测集（可选，对齐调研"评测 Contextual Recall"）

构造 10+ 条真实形态多轮对话（指代 + 省略混合），断言消解后 query 含历史实体且语义不变；配合三态日志（unchanged/changed/error）观测 no-op 率与补全质量。

---

## 8. 验证方案（按序执行）

1. 重写后的 `_resolve_message` 测试 → 全绿
2. `test_entry_cache.py` → 全绿（S1 按 §7.2 调整）
3. 全量回归：`uv run pytest -q`
4. 手动端到端：两轮「这个按摩椅怎么样」→「需要充电吗」→ 日志确认消解为"这款按摩椅需要充电吗"；首条消息确认无消解调用
5. 观测：三态日志评估 no-op 率与延迟，数据驱动决定 `RESOLVE_MODEL` 是否降档

---

## 9. 决策记录

| 决策点 | 决议 |
|---|---|
| 消解架构 | **入口统一 LLM 消解**（多轮 → 一次 LLM 调用，指代 + 省略 + 自包含出口）——替代"正则判定 + LLM"两段式；正则层（代词表/触发词/混淆归一）整体删除 |
| 依据 | 主流调研（LangChain/LlamaIndex/IBM Granite 均为"有历史 → LLM 重写"）；正则判定是退化的替代品，无法覆盖换问法/新指代写法 |
| 保留门控 | ① 纯语气词跳过缓存（缓存优化，有真实判别力）② 无历史直通（LangChain RunnableBranch 同款）——两个免费且召回无损的闸门 |
| 疑问句门控（上版中间态） | **不采用**——LLM 统一消解后正则门控失去意义；若实测 no-op 率高/成本不可接受，作为后续成本优化手段保留（疑问词表静态语法类，随时可加） |
| 成本 | 接受"多轮非语气词消息 ×1 LLM 调用"（主流固有取舍）；缓解：`RESOLVE_MODEL` 降档 + 三态日志观测 |
| 被替代 spec | SPEC_PRONOUN_RESOLUTION_OPTIMIZATION（层 1 门控）与 SPEC_ELLIPSIS_QUESTION_GATE（疑问句门控）**删除**——均未实施，内容（调研结论/测试调整/残余分析）并入本 spec |

---

## 10. 风险与避坑清单

1. **成本上升需实测**：消解面扩大 3~5 倍（~15% → 50~70% 消息）——先上线观测三态日志与延迟，`RESOLVE_MODEL` 降档在数据支撑下再做；`RESOLVE_ENABLED=false` 可一键回滚
2. **prompt 强化回归**：规则 3 表述升级后须用 FakeLLM 用例锁定"自包含原样返回"行为；存量消解质量变化靠端到端回归
3. **S1 断言调整**："那个有货吗"无历史由"跳过 lookup"改为"lookup 执行未命中"（直通语义），勿按旧断言写测试
4. **`RESOLVE_MODEL` 空值语义**：空 = 沿用现状（行为不变）；配置后需验证 `generate(messages, temperature=, max_tokens=)` 鸭子类型兼容（DeepseekService/OllamaService 均可）
5. **文件删除同步**：`pronoun_detector.py` 删除后，全项目检索 `detect_pronoun`/`PRONOUNS`/`ELLIPSIS_TRIGGERS` 引用清零（CLAUDE.md 全局检索规则）——`redis_semantic_cache.py` 与 `app/test/` 是已知引用方
6. **FILLER 语义保留**：`_is_filler` 移入缓存模块时保持整句匹配语义（忽略尾部标点），勿改变 SKIP_CACHE 行为
