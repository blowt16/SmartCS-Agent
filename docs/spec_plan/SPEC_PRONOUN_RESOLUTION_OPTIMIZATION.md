# 指代消解层 1 历史门控（首条消息不空转）实施规格

> **用途**: 为 `pronoun_detector.py` 层 1（显性指代）增加 `has_history` 门控——无历史时代词命中降级 PASS_THROUGH（不再空转调 LLM 消解）；层 2（省略主语触发词）与层 3（纯语气词）**原样保留**；本 plan 只做指代消解的最小优化
> **技术栈**: Python + FastAPI + DeepSeek/Ollama（复用现有消解链路，零新增依赖）
> **状态**: **待实施**（2026-09-01 决策，与 rag_tool HyDE 同分支交付）
> **关联文档**: [[SPEC_SEMANTIC_CACHE_RESOLVE.md]] [[2026-09-01-rag-tool-hyde-design.md]] [[PROJECT_ANALYSIS.md]]

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状链路与问题](#2-现状链路与问题)
3. [改动设计](#3-改动设计)
4. [判定示例](#4-判定示例)
5. [成本与影响面](#5-成本与影响面)
6. [测试方案](#6-测试方案)
7. [验证方案](#7-验证方案)
8. [决策记录](#8-决策记录)
9. [风险与避坑清单](#9-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. 多轮省略主语（"需要充电吗"）的**结构判据化方案经评估后放弃**：长度/触发词闸门判别力低（大多数多轮 query 长度相近，命中率≈100% 即无判别意义）；"有历史即全量 LLM 消解"（LangChain 主流做法）每多轮消息 +1 次 LLM 调用，成本不可接受；免费判别器（疑问句判定/POS 名词检测）存在召回硬伤——详见 §8 决策记录
2. 本 plan 收敛为最小范围：**只做指代（代词）消解优化**，省略主语检测不在范围内

### 1.2 问题

生产代码 `redis_semantic_cache._resolve_message` 调 `detect_pronoun(raw)` **不传历史信息**——首条消息含代词（如"那个按摩椅多少钱"）命中层 1 → NEED_RESOLVE → LLM 空转一次返回原样（无历史可补全，浪费 ~2s 与 token）

### 1.3 目标

| 目标 | 说明 |
|---|---|
| 首条消息代词句零空转 | 无历史时层 1 代词命中 → 跳过层 1，落到层 2 判定 |
| 多轮代词消解行为不变 | 有历史时层 1 判定与现状一致 |
| 层 2/层 3 零改动 | 省略主语触发词表与语气词判定原样保留 |

---

## 2. 现状链路与问题

```
redis_semantic_cache._resolve_message(messages, raw, resolve_llm)
  ├─ detect_pronoun(raw, skip_filler=RESOLVE_SKIP_FILLER)   ← 无历史参数
  │    层1 显性指代词（PRONOUNS，固定表）    ← 无论有无历史，命中即 NEED_RESOLVE
  │    层2 省略主语（ELLIPSIS_TRIGGERS 触发词）
  │    层3 纯语气词（FILLER_PHRASES）
  ├─ NEED_RESOLVE → resolve_pronouns(resolve_llm, messages, raw)   ← LLM 补全
  └─ PASS_THROUGH → 原样透传
```

**问题**：层 1 无历史门控。首条消息"那个按摩椅多少钱"（`detect_pronoun` 返回 NEED_RESOLVE）→ `resolve_pronouns` 拿到空历史 → LLM 规则 3 原样返回 → **空转一次调用**（~2s 延迟 + token 消耗，结果与透传完全一致）。历史不存在的场景下消解必然是 no-op，属于纯浪费。

---

## 3. 改动设计

### 3.1 `pronoun_detector.py`

```python
def detect_pronoun(text: str, skip_filler: bool = True, has_history: bool = False) -> DetectionDecision:
    # 层3 纯语气词（不变，优先级最高）
    if skip_filler and _is_filler(text):
        return DetectionDecision.SKIP_CACHE
    # 那/哪 混淆归一（不变）

    # 层1 显性指代词：加 has_history 门控（无历史 → 跳过层1，落到层2）
    if has_history:
        for pronoun in PRONOUNS:
            if pronoun in text:
                return DetectionDecision.NEED_RESOLVE

    # 层2 省略主语触发词（原样保留，不加 has_history 门控）
    if len(text) <= ELLIPSIS_MAX_LEN and text.startswith(tuple(ELLIPSIS_TRIGGERS)):
        return DetectionDecision.NEED_RESOLVE

    return DetectionDecision.PASS_THROUGH
```

- `PRONOUNS` / `ELLIPSIS_TRIGGERS` / `ELLIPSIS_MAX_LEN=15` / `FILLER_PHRASES`：**全部原样**
- 层 1 在无历史时是"**跳过**"而非"返回 PASS_THROUGH"——控制流继续落到层 2（触发词判定与历史无关，保留现状语义；"那个有货吗"首条消息靠层 2 的"有货"触发词兜住）
- 默认 `has_history=False` → 层 2/层 3 行为与旧代码完全一致；仅层 1 用例需显式传 True

### 3.2 `redis_semantic_cache.py` `_resolve_message`

```python
# 计算 has_history：当前消息之外还有 user/assistant 消息即多轮（与 _format_history 同一过滤口径）
history_msgs = [m for m in messages[:-1] if m.get("role") in ("user", "assistant")]

decision = detect_pronoun(
    raw,
    skip_filler=settings.RESOLVE_SKIP_FILLER,
    has_history=bool(history_msgs),
)
```

- `lookup` / `update` 共用 `_resolve_message`（SPEC 原则三：查写同一逻辑），两处自动同步生效

---

## 4. 判定示例

| query | 多轮 | 层 1 | 层 2 | 结果 | 说明 |
|---|---|---|---|---|---|
| 那个按摩椅多少钱 | ✗ | 命中但无历史→跳过 | 非触发词开头 | PASS_THROUGH | **新行为**：省空转消解 |
| 它支持快充吗 | ✗ | 同上 | 非触发词开头 | PASS_THROUGH | **新行为** |
| 那个按摩椅多少钱 | ✓ | 命中 | — | NEED_RESOLVE | 不变 |
| 那个有货吗 | ✗ | 跳过 | "有货"触发词命中 | NEED_RESOLVE | 现状（层 2 不加门控） |
| 多少钱 | ✗ | — | "多少钱"触发词命中 | NEED_RESOLVE | 现状（已知残余：首条消息触发词仍空转） |
| 多少钱 | ✓ | — | 触发词命中 | NEED_RESOLVE | 不变 |
| 好的 | — | — | 语气词优先 | SKIP_CACHE | 不变 |

---

## 5. 成本与影响面

- **消解率基本不变**（省下的是首条消息代词句的调用，占比小）
- **行为变化**：首条消息代词句不再消解 → 缓存 key=原文、进图检索用原文——与现状最终结果**完全一致**（现状空转后 LLM 也原样返回），只是省掉调用
- **已知残余**（本 plan 不处理，见 §8）：
  1. 首条消息**触发词**空转（"多少钱"，层 2 不加门控）
  2. 多轮省略主语漏检（"需要充电吗"，原问题未解决）

---

## 6. 测试方案

### 6.1 `app/test/test_pronoun_resolve.py`

- 层 2/层 3 用例：**断言不变**（默认 `has_history=False` 时层 2/层 3 判据与旧代码完全一致）
- 层 1 用例加 `has_history` 列：
  - `("它支持快充吗", True, NEED_RESOLVE)`（多轮代词，行为不变）
  - `("它支持快充吗", False, PASS_THROUGH)`（**新行为**）
  - `("那个按摩椅多少钱", False, PASS_THROUGH)`（**新行为**）
- 边界：`("好的", False, SKIP_CACHE)`（层 3 优先，不变）

### 6.2 `app/test/test_entry_cache.py`

- S1（"那个有货吗" 无历史）：层 1 跳过 → **层 2 "有货" 触发词命中** → NEED_RESOLVE → 现有断言（跳过 lookup、照常走图）**不变**——该用例恰好验证"层 2 不加门控"的设计
- 其余用例：补 `has_history` 传参（与生产对齐）

---

## 7. 验证方案（按序执行）

1. `python app/test/test_pronoun_resolve.py` → 全绿（含层 1 新行为用例）
2. `python app/test/test_entry_cache.py` → 全绿
3. 全量回归：`uv run pytest -q`
4. 手动端到端（可选）：首条消息"那个按摩椅多少钱" → 日志确认无消解调用、直接透传

---

## 8. 决策记录

| 决策点 | 决议 |
|---|---|
| 多轮省略主语（"需要充电吗"） | **不在本 plan 解决**。评估结论：① 长度/触发词闸门判别力低（多轮 query 长度普遍相近，命中率≈100% 即无判别意义）；② "有历史全量消解"（LangChain/LlamaIndex 主流做法）每多轮消息 +1 次 LLM 调用，成本不可接受；③ 免费判别器存在召回硬伤——疑问句判定漏"看下价格"类非问句、POS 名词检测漏"多少钱"（钱/n）。**记录为后续演进**：疑问句门控（静态语法词，零维护）+ 消解模型降档（本地/更快模型）组合 |
| 层 1 历史门控 | **加**——无历史时代词命中跳过层 1，修首条消息代词句空转 |
| 层 2 是否也加门控 | **不加**——最小改动原则；首条消息触发词空转（"多少钱"）记录为已知残余 |
| 结构判据化 / ELLIPSIS_MAX_LEN 调整 / 触发词删除 | **全部撤销**（本 plan 不含） |

---

## 9. 风险与避坑清单

1. **默认 `has_history=False` 的语义**：层 2/层 3 行为与旧代码完全一致（层 2 判据不涉历史）；只有层 1 用例需显式传 True
2. **层 1 是"跳过"不是"返回"**：无历史时代词命中必须**继续落到层 2** 判定（"那个有货吗"靠层 2 的"有货"触发词兜住）；若误写成直接 `return PASS_THROUGH`，会导致首条消息省略句漏消解（回归）
3. **已知残余**：① 首条消息触发词空转 ② 多轮省略主语漏检——均记录于 §8，不扩大本 plan 范围
