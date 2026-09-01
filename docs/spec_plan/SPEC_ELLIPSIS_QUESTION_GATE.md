# 多轮省略句疑问句门控消解（主流调研方法落地）实施规格

> **用途**: 解决项目问题 #7（多轮省略句"需要充电吗"漏补全）。基于主流方案调研（LangChain/LlamaIndex 多轮 query 重写模式）裁剪落地：**疑问句门控**（语言级静态语法词表，零维护）∧ 多轮 → LLM 补全；prompt 强化（自包含透传出口 + 输出约束 + 指令规则）；消解模型可配置降档控成本
> **技术栈**: Python + FastAPI + DeepSeek/Ollama（复用现有消解链路，零新增依赖）
> **状态**: **待实施**（2026-09-01 决策，与 rag_tool HyDE / 指代消解层 1 门控同分支交付）
> **关联文档**: [[SPEC_SEMANTIC_CACHE_RESOLVE.md]] [[SPEC_PRONOUN_RESOLUTION_OPTIMIZATION.md]] [[2026-09-01-rag-tool-hyde-design.md]] [[PROJECT_ANALYSIS.md]] [[项目问题.md #7]]

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

### 1.1 问题（项目问题 #7）

多轮对话省略句（无代词、无主语）不进指代消解：

- "这个按摩椅怎么样" → "需要充电吗"：第二轮无代词无主语，层 1（代词表）不命中；层 2（`ELLIPSIS_TRIGGERS` 22 词）不含"需要/要/带"等 → PASS_THROUGH → 检索/缓存拿到残缺 query
- 换问法无穷（"要充电吗""带充电口吗""需不需要充电""支持米家App连接吗"），补词是打地鼠——**触发词穷举结构上不完备**

### 1.2 目标

| 目标 | 量化指标 |
|---|---|
| 多轮省略句漏检率 → 0 | 多轮 ∧ 疑问句的消息全部触发消解（不依赖任何业务词表） |
| 词表零维护 | 门控词表为语言级封闭语法类（疑问代词/助词/结构），不随商品上新变化 |
| 成本可控 | 门控挡掉非问句消息（陈述/祈使，消解收益≈0）；消解模型可配置降档 |
| 行为兼容 | `RESOLVE_ENABLED=false` 总开关语义不变；消解失败降级原消息不变 |

---

## 2. 调研结论

2026-09-01 网络检索主流多轮对话 query 处理方案：

| 来源 | 做法 |
|---|---|
| [LangChain `create_history_aware_retriever`](https://github.com/langchain-ai/langchain/blob/bc5a0ef6cab57821a69470fe205b9a5dbf1dabc8/libs/langchain/langchain/chains/history_aware_retriever.py) | `RunnableBranch`：无历史 → 直通检索；**有历史 → 一律 LLM 重写**，prompt 内给出口"已自包含则原样返回" |
| [LlamaIndex `CondenseQuestion`](https://developers.llamaindex.ai/python/framework/module_guides/deploying/chat_engines/usage_pattern/) | 有历史即重写为独立查询，按设计无条件 |
| [IBM Granite RAG](https://huggingface.co/ibm-granite/granitelib-rag-r1.0) | 微调专用 Query Rewrite 模型，同样无条件走重写 |
| [Nodrat 实践](https://github-wiki-see.page/m/selmanays/nodrat/wiki/conversational-query-rewriting) | 不重写清单：助手相关/元问题、指令类跟进、显式主语例外（自锚定）；重写应 <500ms/轮 |
| [DEV RAG Series](https://dev.to/wonderlab/rag-series-18-conversational-rag-the-pronoun-problem-in-multi-turn-dialogue-2g7g) | "改写前检索"是基础架构而非优化项；自包含检测由重写 LLM 完成（prompt 出口） |

**关键结论**：

1. **自包含检测不是规则闸门，是重写 prompt 里的一句话**——"如果问题已完整独立，直接原样返回"，LLM 就是语义闸门
2. 主流不做规则门控（无历史直通是唯一门控），因为多轮消息普遍依赖上下文，重写调用便宜且收益可靠
3. 实践要点：输出仅问题文本（无解释）、截断守卫（max_tokens/首行）、重写结果仅用于检索（不展示给用户）、指令类跟进不盲改
4. 本项目**不直接采用**"有历史即重写"：成本敏感（多轮消息 +1 次 LLM 调用 × 全部消息），需规则门控收缩调用面——但门控必须**免费且召回≈1**（见 §3.1）

---

## 3. 方案设计

### 3.1 门控：疑问句判定（替换层 2 触发词判据）

**判据**：`has_history` ∧ 疑问句判定命中 → NEED_RESOLVE

疑问句判定 = 语言级静态语法词表（**零维护**，与 ELLIPSIS_TRIGGERS 业务词表的本质区别）：

| 类别 | 内容（基础集，实施时定稿） | 示例 |
|---|---|---|
| 疑问代词 | 多少、多久、多长、多大、多宽、什么、怎么、怎样、怎么样、为什么、哪里、哪个、哪种、哪些、几 | "多少钱" ✓ "颜色有几种" ✓ |
| 疑问助词 | 吗、呢 | "需要充电吗" ✓ "价格呢" ✓ |
| 能愿疑问结构 | 能不能、可不可以、会不会、要不要、需不需要、是不是、有没有、能不能 | "需不需要充电" ✓ |
| A不A 常用 | 好不好、贵不贵、大不大、行不行、耐不耐、值不值 | "质量好不好" ✓ |
| 句尾问号 | 以 ？/? 结尾 | 任意带问号语句 |

命中任一 → 疑问句 → 消解。

**召回论证**（为什么漏检≈0）：真省略句（需要补全主语）几乎全部是疑问句——"需要充电吗""带充电口吗""支持米家App连接吗""有没有白色的""多少钱""能放卧室吗"。门控遗漏的只有**非问句**（"看下价格""发个链接"），这类消息不是检索 query，补全收益≈0，可接受（且 agent 场景由调用方 LLM 补全）。

**误触发代价**：陈述句带"吗/呢"罕见；"吧"类祈使（"给我吧"）**不入词表**（避免歧义）。误触发 = 一次 LLM 调用原样返回，符合"宁可多检测"既有哲学。

### 3.2 补全：prompt 强化（调研实践落地）

`pronoun_resolver.py` 的 `RESOLVE_SYSTEM_PROMPT` 调整：

1. **规则 3 升级为显式主路径**（对齐主流出口）：
   "如果当前问题已完整独立（包含明确主语，不依赖上下文），**直接原样返回，不要添加或修改任何信息**"
2. **新增指令类规则**（Nodrat 实践）："如果当前问题是命令式指令（如'查一下价格''搜一下X'），仅补全为完整的查询意图，不要改写为实体搜索"
3. 保留：规则 4（不添加历史未出现信息）、规则 5（仅输出问题文本，无解释）——输出约束与截断（`RESOLVE_MAX_TOKENS=200`）已有，确认保留

### 3.3 成本：消解模型可配置降档

- 现状：消解走 `CHAT_SERVICE` 模型（DeepSeek ~2s/次）
- 新增配置 `RESOLVE_MODEL`（可选，默认空 = 沿用现状；配置后消解走指定模型）——消解任务是"补全短句"，对模型能力要求低，可指向本地 Ollama 或更快的模型档位，目标重写延迟 <500ms/轮（主流建议）
- 默认不降档，上线实测延迟/质量后再调（先实测再定方案）

### 3.4 兼容性

- 依赖 [[SPEC_PRONOUN_RESOLUTION_OPTIMIZATION.md]] 引入的 `has_history` 参数（该 plan 先行实施：层 1 历史门控 + `_resolve_message` 传参）
- 层 1（代词，含历史门控）、层 3（语气词）**不动**；本 spec 仅替换层 2 判据
- 消解结果继续用于缓存 key + 图内 query（现状链路不变）

---

## 4. 数据流与代码形态

### 4.1 `pronoun_detector.py`（层 2 判据替换）

```python
# 删除：ELLIPSIS_TRIGGERS（22 词）及 ELLIPSIS_MAX_LEN 触发词注释
# 新增：QUESTION_MARKERS（疑问代词/助词/能愿结构/A不A 静态元组）
# 保留：PRONOUNS / FILLER_PHRASES / ELLIPSIS_MAX_LEN=15 不再参与层 2（或删除，见下）

def _is_question(text: str) -> bool:
    """疑问句判定：句尾问号 ∨ 命中疑问词表（语言级静态，零维护）"""
    return text.endswith(("？", "?")) or any(w in text for w in QUESTION_MARKERS)

def detect_pronoun(text: str, skip_filler: bool = True, has_history: bool = False) -> DetectionDecision:
    # 层3 纯语气词（不变，优先级最高）
    if skip_filler and _is_filler(text):
        return DetectionDecision.SKIP_CACHE

    # 层1 显性指代词（has_history 门控，来自 SPEC_PRONOUN_RESOLUTION_OPTIMIZATION）
    if has_history:
        for pronoun in PRONOUNS:
            if pronoun in text:
                return DetectionDecision.NEED_RESOLVE

    # 层2 疑问句门控（替换触发词判据）：多轮 ∧ 疑问句 → 消解
    if has_history and _is_question(text):
        return DetectionDecision.NEED_RESOLVE

    return DetectionDecision.PASS_THROUGH
```

- `ELLIPSIS_MAX_LEN`：层 2 不再用长度判据——"需要充电吗"(5) 与"支持米家App连接吗"(10) 都在句法上统一由疑问句判定覆盖，长度无判别价值；常量随触发词一并删除
- 默认 `has_history=False` → 无历史一律 PASS_THROUGH（含层 1/层 2）——单轮行为统一为"直通"（对齐 LangChain 无历史直通）

### 4.2 `pronoun_resolver.py`（prompt 强化）

`RESOLVE_SYSTEM_PROMPT` 按 §3.2 调整规则 3/新增指令规则；其余（历史格式化/降级/截断）不变。

### 4.3 `config.py`

```python
RESOLVE_MODEL: str = ""   # 消解专用模型（可选）；空 = 沿用 CHAT_SERVICE 模型；配置后消解走指定模型（控延迟/成本）
```

---

## 5. 判定示例

| query | 多轮 | 疑问句 | 层 1 | 结果 | 说明 |
|---|---|---|---|---|---|
| 需要充电吗 | ✓ | ✓（吗） | — | NEED_RESOLVE | 修复目标场景 |
| 带充电口吗 | ✓ | ✓（吗） | — | NEED_RESOLVE | 换问法 ✓ |
| 需不需要充电 | ✓ | ✓（需不需要） | — | NEED_RESOLVE | 换问法 ✓ |
| 支持米家App连接吗 | ✓ | ✓（吗） | — | NEED_RESOLVE | 换问法 ✓ |
| 可以充电吗 | ✓ | ✓（吗） | — | NEED_RESOLVE | 原触发词也覆盖（判据超集） |
| 多少钱 | ✓ | ✓（多少） | — | NEED_RESOLVE | 原触发词覆盖（判据超集） |
| 颜色有几种 | ✓ | ✓（几） | — | NEED_RESOLVE | 原触发词**未覆盖**（新增） |
| 这个按摩椅怎么样 | ✓ | ✓（怎么样） | ✓ 代词 | NEED_RESOLVE | 层 1 命中（不变） |
| 芝华仕按摩椅多少钱 | ✓ | ✓ | — | NEED_RESOLVE | 自包含疑问句 → LLM 规则 3 原样返回（多一次廉价调用，宁可多检测） |
| 换一个 | ✓ | ✗ | — | PASS_THROUGH | 祈使句，非检索 query，省调用 |
| 发个链接 | ✓ | ✗ | — | PASS_THROUGH | 同上 |
| 看下价格 | ✓ | ✗ | — | PASS_THROUGH | **已知盲区**（非问句，记录） |
| 需要充电吗 | ✗（首条） | ✓ | — | PASS_THROUGH | 无历史直通（对齐主流） |
| 好的 | — | — | — | SKIP_CACHE | 层 3 优先（不变） |

---

## 6. 成本与影响面

| 维度 | 分析 |
|---|---|
| 消解面 | 触发词时代 ~15% → 多轮疑问句消息（预估 30~50%）；仍明显窄于"有历史全量消解"（多轮全消息 ~60-70%） |
| 单次成本 | 模型可配置降档（§3.3），目标 <500ms/轮；DeepSeek 现状 ~2s 可接受兜底 |
| 缓存收益 | 多轮缓存 key 变为完整问题 → 命中率上升，部分自偿 |
| 顺带修复 | 首条消息触发词空转（"多少钱"）随 `has_history` 门控一并消除（层 2 也要求 has_history）——SPEC_PRONOUN_RESOLUTION_OPTIMIZATION 的已知残余 #1 在本 plan 消除 |
| 行为变化 | 单轮（无历史）一律透传，与 LangChain 对齐；多轮省略句从"漏检"变"补全" |

---

## 7. 测试方案

### 7.1 `app/test/test_pronoun_resolve.py`（层 2 用例重写）

- **命中用例**（换问法全覆盖，每类 ≥5 条）：`("需要充电吗", True, NEED_RESOLVE)` / `("带充电口吗", ...)` / `("需不需要充电", ...)` / `("支持米家App连接吗", ...)` / `("颜色有几种", ...)` / `("质量好不好", ...)` / `("价格呢", ...)`
- **透传用例**：`("换一个", True, PASS_THROUGH)` / `("发个链接", True, PASS_THROUGH)` / `("看下价格", True, PASS_THROUGH)`（盲区留档）/ 无历史全透传：`("多少钱", False, PASS_THROUGH)`（新行为，替代原"首条消息触发词空转"）
- 层 1/层 3 用例不变（含 has_history 列）

### 7.2 `app/test/test_entry_cache.py`

- S1（"那个有货吗" 无历史）：层 1 跳过 → 层 2 要求 has_history → **PASS_THROUGH** → 缓存 lookup 执行（mock 未命中）→ graph 照常——**断言需调整**（原"跳过 lookup"改为"执行未命中"；与原 SPEC_PRONOUN_RESOLUTION_OPTIMIZATION 的 S1 预期一致——该 plan 下"那个有货吗"曾靠层 2 触发词兜住，本 plan 层 2 加 has_history 后兜不住，属预期行为变化）

### 7.3 resolver prompt 强化用例（FakeLLM 模式）

- 自包含疑问句 → 断言 LLM 输出与输入一致（规则 3 主路径）
- 省略疑问句 → 断言补全包含历史实体
- 指令类（"查一下价格"）→ 断言补全为查询意图而非实体搜索

### 7.4 多轮省略句评测集（可选，对齐调研"评测 Contextual Recall"）

构造 10+ 条真实形态多轮对话（第一轮带实体、第二轮省略句），断言：门控全命中（漏检率=0）∧ 补全后 query 含历史实体。

---

## 8. 验证方案（按序执行）

1. `python app/test/test_pronoun_resolve.py` → 全绿
2. `python app/test/test_entry_cache.py` → 全绿（S1 断言按 §7.2 调整）
3. 全量回归：`uv run pytest -q`
4. 手动端到端：两轮「这个按摩椅怎么样」→「需要充电吗」→ 日志确认消解为"这款按摩椅需要充电吗"→ 缓存/检索拿到完整 query
5. 观测：消解三态日志（unchanged/changed/error）评估 no-op 率与补全质量，数据驱动决定是否调词表/模型

---

## 9. 决策记录

| 决策点 | 决议 |
|---|---|
| 采用"有历史全量重写"（主流原样）？ | **否**——每多轮消息 +1 次 LLM 调用，本项目成本敏感（曾删 HyDE 管道）；需门控收缩调用面 |
| 门控选型 | **疑问句判定**——免费判别器中唯一"召回≈1 + 零维护"的：真省略句几乎全疑问；词表为语言级封闭语法类（不随商品变化）；误触发（陈述带吗/呢）罕见且代价仅一次廉价调用 |
| 替换对象 | 层 2 触发词判据（22 词业务表）——打地鼠结构，替换后为超集（原触发词句全为疑问句） |
| 长度判据 | 随触发词删除（`ELLIPSIS_MAX_LEN`）——疑问句判定天然覆盖短长省略句，长度无判别价值 |
| 自包含判断归属 | LLM 规则 3（prompt 出口，主流做法）——门控只管"可能是省略句"，语义完整性由 LLM 判定 |
| 消解模型 | 新增 `RESOLVE_MODEL` 配置（默认空=现状）；实测后再决定是否降档 |
| 与层 1 门控 plan 关系 | 依赖先行实施（has_history 参数）；本 plan 落地后消除其已知残余 #1（首条消息触发词空转） |
| 盲区 | "看下价格"类非问句不消解——非检索 query，agent 场景由调用方 LLM 补全，接受 |

---

## 10. 风险与避坑清单

1. **词表边界**：疑问助词只收"吗/呢"，**不收"吧"**（"给我吧"是祈使，误触发浪费调用）；A不A 用词表收录常用形（好不好/贵不贵…），不搞正则（"不好"子串会误命中"好不好"）
2. **S1 断言调整**："那个有货吗"无历史 → 层 2 需 has_history → 透传 → 缓存 lookup 执行（mock 未命中）——与原 plan 的 S1 预期（靠层 2 兜住）不同，属预期行为变化，勿按旧断言写测试
3. **prompt 强化回归**：规则 3 表述变化可能影响存量消解质量——FakeLLM 用例 + 手动端到端回归
4. **`RESOLVE_MODEL` 空值语义**：空 = 沿用现状（行为不变）；配置后需验证服务签名兼容（`generate(messages, temperature=, max_tokens=)` 鸭子类型，DeepseekService/OllamaService 均可）
5. **与 SPEC_PRONOUN_RESOLUTION_OPTIMIZATION 的实施顺序**：层 1 历史门控先行（本 spec 的 `has_history` 依赖它）；同分支、可分两次提交
6. **观测先行**：no-op 率（unchanged 占比）是"门控过宽"的直接度量——若显著偏高（如 >40%），优先检查疑问词表是否误收陈述句常用词，再考虑收窄
