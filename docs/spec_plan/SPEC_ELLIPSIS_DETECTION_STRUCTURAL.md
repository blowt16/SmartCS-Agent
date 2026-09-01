# 省略主语检测结构判据化（替换触发词穷举）实施规格

> **用途**: 把 `pronoun_detector.py` 第二层"省略主语"检测从 `ELLIPSIS_TRIGGERS` 触发词穷举改为**结构判据**（多轮上下文 ∧ 短查询 ∧ 非语气词 → 需要消解），物理删除触发词表；修复多轮无代词省略句（如"需要充电吗"）漏检问题，且对任意换问法鲁棒（不依赖词表）
> **技术栈**: Python + FastAPI + DeepSeek/Ollama（复用现有消解链路，零新增依赖）
> **状态**: **待实施**（2026-09-01 决策，与 rag_tool HyDE 同分支交付）
> **关联文档**: [[SPEC_SEMANTIC_CACHE_RESOLVE.md]] [[2026-09-01-rag-tool-hyde-design.md]] [[PROJECT_ANALYSIS.md]]

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状链路与问题](#2-现状链路与问题)
3. [改动设计](#3-改动设计)
4. [成本与影响面](#4-成本与影响面)
5. [测试方案](#5-测试方案)
6. [验证方案](#6-验证方案)
7. [决策记录](#7-决策记录)
8. [风险与避坑清单](#8-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. **多轮对话场景复现**：user 第一轮"这个按摩椅怎么样"，第二轮"需要充电吗"——第二轮 query 无代词、无主语，语义依赖第一轮
2. 当前入口消解第一层（显性指代词：它/这款/那个…）对"需要充电吗"不命中；第二层（省略主语触发词表）**不含"需要"** → PASS_THROUGH → 残缺 query 直达检索，结果泛化错误
3. **"可以充电吗"被覆盖的说明**：`ELLIPSIS_TRIGGERS` 含"可以"，此类问法已处理；但"需要充电吗""要充电吗""带充电口吗""充电方便吗""需不需要充电"等**均漏检**——触发词穷举本质是打地鼠，词表追不完用户问法

### 1.2 目标

| 目标 | 量化指标 |
|---|---|
| 多轮省略主语漏检率 → 0 | 多轮 ∧ ≤10 字 ∧ 非语气词的 query 全部触发消解（不依赖任何词表） |
| 词表零维护 | 删除 `ELLIPSIS_TRIGGERS`（22 个词）及配套注释 |
| 顺带修复首条消息空转 | 无历史时不再触发消解（现状生产代码对首条消息触发词也调 LLM，空转返回原样） |
| 行为兼容 | `RESOLVE_ENABLED=false` 总开关语义不变；消解失败降级原消息不变 |

### 1.3 设计原则

1. **结构判据替代词表穷举**：省略主语的本质是"短查询 + 多轮语义依赖"，直接判结构，不枚举表象
2. **沿用检测器既有哲学**：`pronoun_detector.py` docstring 明写"宁可多检测（假阳性只多一次 LLM 消解调用），不能漏检测（假阴性导致缓存永久失效）"——本次改动是该原则的彻底贯彻
3. **只改检测层，不动消解层**：`pronoun_resolver.py`（prompt/历史格式化/降级）零改动

---

## 2. 现状链路与问题

### 2.1 现状调用链

```
redis_semantic_cache._resolve_message(messages, raw, resolve_llm)
  ├─ detect_pronoun(raw, skip_filler=RESOLVE_SKIP_FILLER)   ← 无历史参数
  │    层1 显性指代词（PRONOUNS，固定表）
  │    层2 省略主语：len ≤ 15 ∧ startswith(ELLIPSIS_TRIGGERS)  ← 22 个触发词
  │    层3 纯语气词（FILLER_PHRASES）
  ├─ NEED_RESOLVE → resolve_pronouns(resolve_llm, messages, raw)   ← LLM 补全
  └─ PASS_THROUGH → 原样透传
```

### 2.2 问题

1. **词表不完备**：`ELLIPSIS_TRIGGERS = [有货, 有卖, 多少钱, 价格, 能退, 能换, 支持, 兼容, 怎么, 为什么, 还有, 再, 可以, 能不能, 包邮, 保修, 多久, 什么时候, 哪里, 怎么样]`——"需要""要""带""有没有""需不需要"等高频问法开头词均不在表内；用户换问法即漏检，补词永远追不完
2. **无历史门控**：生产代码 `_resolve_message` 调 `detect_pronoun(raw)` **不传历史信息**——首条消息"多少钱"或"那个按摩椅多少钱"（无历史可补全）也触发 NEED_RESOLVE → LLM 空转一次返回原样（浪费 ~2s 与 token）
3. 触发词表与层 3 存在重叠维护负担（"可以"同时是触发词与语气词，靠顺序与长度区分，易碎）

---

## 3. 改动设计

### 3.1 `pronoun_detector.py`

```python
# 删除：ELLIPSIS_TRIGGERS（22 词）及 ELLIPSIS_MAX_LEN 注释中的触发词说明
# 调整：ELLIPSIS_MAX_LEN = 10（15 → 10；无代词省略句上界即 10 字，
#       11~15 字多为自包含完整查询，收紧避免空转消解）

def detect_pronoun(text: str, skip_filler: bool = True, has_history: bool = False) -> DetectionDecision:
    """
    层1/层2 对称门控：has_history=False（无历史）时两层均降级 PASS_THROUGH，
    不空转消解；has_history=True 时：层1 代词命中 → NEED_RESOLVE，
    否则层2 结构判据 len ≤ ELLIPSIS_MAX_LEN → NEED_RESOLVE
    """
```

- 层 1（显性指代词）：**加 `has_history` 门控**（无历史时命中 → 透传不空转，与层 2 对称）；代词表、子串匹配逻辑不动
- 层 2 判定：`if has_history and len(text) <= ELLIPSIS_MAX_LEN: return NEED_RESOLVE`（替换触发词判据）
- 层 3（纯语气词）**不动**（整句语气词 → SKIP_CACHE，优先级最高）
- 默认 `has_history=False` → 单轮场景（含现有测试调用方式）行为不变

### 3.2 `redis_semantic_cache.py` `_resolve_message`

```python
# 计算 has_history：当前消息之外还有 user/assistant 消息即多轮
history_msgs = [m for m in messages[:-1] if m.get("role") in ("user", "assistant")]
has_history = bool(history_msgs)

decision = detect_pronoun(raw, skip_filler=settings.RESOLVE_SKIP_FILLER, has_history=has_history)
```

- `lookup` / `update` 共用 `_resolve_message`（SPEC 原则三：查写同一逻辑），两处自动同步生效

### 3.3 判定示例

| query | 多轮? | 长度 | 层2 判定 | 说明 |
|---|---|---|---|---|
| 需要充电吗 | ✓ | 5 | NEED_RESOLVE | 修复目标场景 |
| 要充电吗 | ✓ | 4 | NEED_RESOLVE | 换问法 ✓ |
| 带充电口吗 | ✓ | 5 | NEED_RESOLVE | 换问法 ✓ |
| 需不需要充电 | ✓ | 6 | NEED_RESOLVE | 换问法 ✓ |
| 可以充电吗 | ✓ | 5 | NEED_RESOLVE | 原触发词也覆盖（行为不变） |
| 这款按摩椅需要充电吗 | ✓ | 11 | NEED_RESOLVE | 层1 代词命中（行为不变） |
| 那个按摩椅多少钱 | ✗（首条） | 8 | PASS_THROUGH | 层1 代词命中但无历史 → 透传不空转（层1 对称门控**新行为**） |
| 芝华仕按摩椅多少钱 | ✓ | 9 | NEED_RESOLVE | 完整短查询，9 ≤ 10 边界内 → LLM 判自包含后原样返回（规则 3），符合宁可多检测 |
| 京东京造智能门锁保修多久 | ✓ | 12 | PASS_THROUGH | 完整查询 >10 不触发（收紧区间，省空转消解） |
| 需要充电吗 | ✗（首条） | 5 | PASS_THROUGH | 无历史可补全，零成本（**修复空转**） |
| 多少钱 | ✗（首条） | 3 | PASS_THROUGH | 同上（现状生产代码会空转消解） |
| 好的 | ✓ | 2 | SKIP_CACHE | 层3 语气词优先（不变） |

---

## 4. 成本与影响面

### 4.1 消解率与成本

- 现状：~15% 消息触发消解；改后：多轮 + ≤10 字查询全覆盖（预估 25~40%，取决于多轮短查询占比）
- 单次消解成本：DeepSeek 调用 ~2s / 低 token（`RESOLVE_MAX_TOKENS=200`，实际输出多为短句）
- **部分自偿**：消解越全 → 缓存键（消解后消息）越准确 → 缓存命中率上升；消解系统存在目的即缓存质量（SPEC_SEMANTIC_CACHE_RESOLVE.md）
- **净省**：首条消息触发词不再空转消解（现状浪费项）

### 4.2 误补全风险

- 多轮短**非问句**（如"看看""发链接"）会触发消解 → LLM 可能错误补全
- 兜底双保险：① resolver prompt 规则 4"不要添加历史中没出现过的信息，只做补全" ② 消解失败/超时降级原消息（`resolve_pronouns` 现有异常路径）
- 判断依据：此类消息在客服场景占比极低，且即使误补全仅影响检索/缓存键质量，不阻塞主流程

### 4.3 语义缓存影响

- 缓存 key 基于消解后消息（`_get_hash_id(resolved)`），消解规则变化只影响**新写入**的 key；存量键不受影响（TTL 到期自然淘汰）
- 同一用户同一省略问法 → 消解结果一致 → 命中率上升（正收益）

---

## 5. 测试方案

### 5.1 `app/test/test_pronoun_resolve.py`（检测器用例表参数化）

- 现有用例表加 `has_history` 列：`("能退吗", True, NEED_RESOLVE)`、`("还有吗", True, NEED_RESOLVE)`、`("iPhone 15多少钱", False, PASS_THROUGH)`——**单轮语义与现有断言完全一致**（默认 False）
- 代词用例同步标注：`("它支持快充吗", True, NEED_RESOLVE)`；`("那个有货吗", False, PASS_THROUGH)`（层1 对称门控**新行为**）
- 新增用例（多轮 + 换问法全覆盖）：
  - `("需要充电吗", True, NEED_RESOLVE)` / `("要充电吗", True, ...)` / `("带充电口吗", True, ...)` / `("需不需要充电", True, ...)`
- 边界：`("好的", True, SKIP_CACHE)`（层3 优先）、`("多少钱", False, PASS_THROUGH)`（无历史透传）

### 5.2 `app/test/test_entry_cache.py`

- `entry_flow` 复刻逻辑补 `has_history` 传参（与生产对齐）
- **S1（无历史含指代）断言调整**：「那个有货吗」无历史 → 决策从 NEED_RESOLVE 变为 PASS_THROUGH → 缓存 lookup 会执行（mock 返回 None 未命中）→ graph 照常；断言从"跳过 lookup"改为"lookup 执行但未命中、仍走图"

### 5.3 回归

- `uv run pytest -q` 全量

---

## 6. 验证方案（按序执行）

1. `python app/test/test_pronoun_resolve.py` → 全绿（含新增换问法用例）
2. `python app/test/test_entry_cache.py` → 全绿
3. 手动端到端（可选）：两轮对话「这个按摩椅怎么样」→「需要充电吗」，日志确认第二轮 `检测到省略主语` → LLM 消解为"这款按摩椅需要充电吗" → 缓存/检索拿到完整 query

---

## 7. 决策记录

| 决策点 | 决议 |
|---|---|
| 修复路径 | **结构判据化**（多轮 ∧ 短 ∧ 非语气词），删除触发词表——换问法无穷，补词（"需要/要"）是治标 |
| "可以充电吗"等已覆盖问法 | 行为不变（新判据是原判据的超集：所有触发词开头的短句均满足结构判据） |
| 是否 LLM 全量判断（每轮都消解） | **否**——成本不可控；结构判据在"零词表"与"成本收敛"间取得平衡 |
| 历史判定来源 | `_resolve_message` 计算 `messages[:-1]` 中 user/assistant 消息是否存在（与 `_format_history` 同一过滤口径） |
| 层 1 / 层 3 / resolver | 零改动（只动检测层） |
| 短查询阈值 | `ELLIPSIS_MAX_LEN` 15 → **10**：无代词省略句上界即 10 字（「支持米家App连接吗」），11~15 字多为自包含完整查询（重复商品名/型号），收紧避免空转消解；**10 是"漏检≈0"前提下的最紧值**，低于 10 会漏真实省略句；落地后按消解率日志微调 |
| 层 1 历史门控 | **加**（对称化）——无历史时层 1 代词命中降级 PASS_THROUGH，首条消息代词句不再空转消解；代词表与子串匹配逻辑不动 |
| 与 HyDE 交付关系 | 关联工作项，同分支实施；HyDE spec 引用本文件 |

---

## 8. 风险与避坑清单

1. **改动边界**：层 1 仅加 `has_history` 门控（代词表、子串匹配不动）；层 3 零改动（整句语气词 → SKIP_CACHE 优先级最高）；层 2 替换判据
2. **`has_history` 默认 False**：现有测试与未传参调用行为不变；`_resolve_message` 必须显式传值，否则"多轮省略漏检"问题依然存在
3. **messages 结构**：`messages` 最后一条为当前用户消息；`messages[:-1]` 过滤 `role in ("user", "assistant")`（与 `_format_history` 口径一致，跳过 system）
4. **存量缓存键**：消解变化只影响新写入，存量键 TTL 自然淘汰，无需清理脚本
5. **误补全**："看看"类短非问句触发消解的少量误补全由 resolver 规则 4 + 降级兜底；上线后通过日志观察消解率与异常率，若异常可退回（`RESOLVE_ENABLED=false` 一键回滚）
6. **与 HyDE 的边界**：本改动解决"缺主语"（入口层，有 history）；HyDE 解决"口语/模糊"（tool 层，无 history）；两者互补不重叠——缺主语的 query 若仍漏进 tool（如 agent 透传），tool 规则判定后不改写（无实体可桥接），由 agent 层补全
