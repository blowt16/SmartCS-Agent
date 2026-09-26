"""
多轮消息统一消解器（SPEC_ENTRY_LLM_RESOLUTION §3.2/§4.2）

一次 LLM 调用完成指代消除 + 语义补全；prompt 内自包含出口保证完整问题原样返回。
调用方：main.py 入口（多轮消息无条件消解）与语义缓存内部 _resolve_message（现状旧两段式，
缓存入口改造见 docs/项目问题.md #11），两处共用同一 prompt 与降级路径。

设计原则：
    - 只做补全，不做扩展：LLM 只负责把依赖上下文的成分补全为完整独立的问题
    - 消解失败不阻塞：超时/空结果/异常一律降级为原始消息，保证主流程不中断
    - 与 LLM 后端解耦：通过 generate(messages, temperature=, max_tokens=, reasoning_effort=)
      鸭子类型调用，DeepseekService / OllamaService 均可（二者签名一致）
    - 三态日志：unchanged（自包含原样）/ changed（补全）/ error（降级），供 no-op 率观测

用法:
    resolved = await resolve_pronouns(llm_service, messages, raw_query)
    # 失败时返回 raw_query 原样
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import List, Dict

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(service="pronoun_resolver")


@dataclass
class ResolveResult:
    """消解结果（SPEC_MULTI_CANDIDATE_REFERENCE §4.1.1）。

    candidates 为空 = 无歧义，query 即正常消解结果；
    candidates ≥2 = 指代有多个同等候选，**不擅自选定**，query 为原消息，下游据此反问用户。
    """

    query: str
    candidates: List[str] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) >= 2


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)

# 候选列表上限。prompt 已要求"最多 5 个、只取最近一条助手回复"，此处再兜一道：
# 实测候选会随轮次滚雪球（3→6→7），而列出 7 个选项用户同样没法回答。
# 截断**不静默**——打 WARNING 记录被丢弃的部分（避免"看起来都列全了"）。
_MAX_CANDIDATES = 5


def _parse_resolve_output(text: str, raw_query: str) -> ResolveResult:
    """解析消解输出。

    **纯文本向后兼容是硬要求**：模型未按 JSON 输出（或旧格式）时，整体作为 query、
    候选为空——保证既有消解行为在任何解析失败下都不劣化于改造前。
    """
    stripped = (text or "").strip()
    if not stripped:
        return ResolveResult(query=raw_query)

    # 模型常把 JSON 包在 ```json 围栏里；先剥围栏再试
    candidate_text = stripped
    fence = _JSON_FENCE.search(stripped)
    if fence:
        candidate_text = fence.group(1).strip()

    try:
        obj = json.loads(candidate_text)
    except (ValueError, TypeError):
        return ResolveResult(query=stripped)          # 纯文本兼容

    if not isinstance(obj, dict):
        return ResolveResult(query=stripped)

    resolved = obj.get("resolved")
    query = resolved.strip() if isinstance(resolved, str) and resolved.strip() else raw_query

    raw_cands = obj.get("candidates")
    candidates: List[str] = []
    if isinstance(raw_cands, list):
        for c in raw_cands:
            if isinstance(c, str) and c.strip():
                candidates.append(c.strip())

    return ResolveResult(query=query, candidates=candidates)

RESOLVE_SYSTEM_PROMPT = """你是一个多轮对话的指代消解与语义补全专家。
你的任务是根据对话历史，把用户当前问题中依赖上下文的成分（指代词、省略的主语/宾语、不完整信息）补全为完整、独立的问题。

规则：
1. 如果当前问题包含代词（他/她/它/那个/这个/那件/这件/该产品等），用历史中的实体替换
2. 如果当前问题是省略句（如"有货吗""多少钱""能退吗""需要充电吗"），从历史中补全主语
3. 如果当前问题已完整独立（包含明确主语、不依赖上下文），直接原样返回，不要添加或修改任何信息
4. 如果当前问题是命令式指令（如"查一下价格"），补全为完整的查询意图，不要改写成实体搜索
5. 不要添加历史中没出现过的信息，只做补全，不做扩展
6. 补全所需的信息优先取自"用户"说过的话；用户没说时，可以取"助手"回复里的商品实体
   （商品名/型号/品牌/品类）。例：助手推荐过"小米智能门锁2"，用户问"那个有货吗"
   → "小米智能门锁2有货吗"
7. **严禁把"助手"的话抄进结果**：助手的询问、澄清话术、选项列表（如"您是想了解商品信息、
   售后问题还是其他呢？"）是助手在问用户，不是用户说的话——不得整体复制，也不得当作
   补全内容。助手把同一句话重复问过多次，同样不算"用户提到过"
8. 如果用户只是在回应、接话（如"嗯""好的""我想问下"）而没有提供任何可补全的实体，
   原样返回，不要猜测、不要拼凑
9. **多候选检测（重要）**：如果用户当前问题用了指代词或省略，而上文中有**多个同等合理**
   的候选对象（典型场景：助手上一条回复里一次列了多款商品），无法唯一确定指代目标——
   **此时不要擅自选定一个**，把这些候选放进 candidates；其余情况 candidates 必须为空数组。
   · 候选必须是**助手回复里确实出现过的具体对象**（商品名/型号），不得编造、不得臆测
   · 候选**只取自助手最近一条回复里列出的对象**，不要从更早的对话里翻找——否则候选会
     随轮次越滚越多（实测 3→6→7），澄清一次比一次更没法回答
   · 候选最多 5 个；超过时只保留最近一条回复里最先出现的 5 个
   · **用户上一轮已经指明过具体商品、且此后没有引入新商品时，指代目标就是唯一的**——
     这种情况属于候选唯一，正常消解，candidates 为空（不要因为历史上曾出现多款商品就一味判歧义）
   · 用户当前问题里已指明目标时（如"小米那款保修多久"），candidates 为空，正常消解
   · 有多候选歧义时，resolved 直接**原样返回用户消息**（不锁定任何对象），
     下游会据此反问用户，不会用 resolved 去检索

输出 JSON（不要任何解释、不要 markdown 围栏）：
{"resolved": "消解后的完整问题", "candidates": ["候选1", "候选2"]}"""


def _format_history(messages: List[Dict], max_turns: int) -> str:
    """
    将消息列表格式化为 LLM 可读的对话历史文本。

    只取最近 max_turns 轮（1轮 = 1条用户 + 1条助手），每条截断到
    settings.RESOLVE_MAX_CHARS_PER_MSG 字，避免历史过长导致 prompt 超长。

    Args:
        messages: 完整对话消息列表（最后一条为待消解的当前用户消息）
        max_turns: 最多保留的对话轮数

    Returns:
        格式化的对话历史字符串，如 "用户: xxx\n助手: xxx\n..."
    """
    # 筛选出用户和助手的消息，跳过 system 等角色
    chat_msgs = [m for m in messages if m.get("role") in ("user", "assistant")]

    # 只保留最近 max_turns*2 条消息（每轮一问一答）；当前消息是待消解对象，不参与历史
    chat_msgs = chat_msgs[-(max_turns * 2):-1] if len(chat_msgs) > 1 else []

    lines = []
    for msg in chat_msgs:
        role = "用户" if msg["role"] == "user" else "助手"
        content = msg["content"]
        if len(content) > settings.RESOLVE_MAX_CHARS_PER_MSG:
            content = content[:settings.RESOLVE_MAX_CHARS_PER_MSG]
        lines.append(f"{role}: {content}")

    return "\n".join(lines)


async def resolve_pronouns_ex(
    llm_service, messages: List[Dict], raw_query: str
) -> ResolveResult:
    """
    指代消解主入口（结构化版）：补全当前问题，并检测"多候选指代"。

    Args:
        llm_service: 具备 generate(messages, temperature=, max_tokens=) 的服务
                     （DeepseekService / OllamaService 均可）
        messages: 完整对话消息列表（最后一条为当前用户消息）
        raw_query: 原始用户消息

    Returns:
        ResolveResult；任何失败（超时/空/异常）降级为
        ResolveResult(query=raw_query, candidates=[])——候选为空即"不触发澄清"，
        与改造前行为一致。
    """
    history_text = _format_history(messages, max_turns=settings.RESOLVE_MAX_TURNS)

    prompt_messages = [
        {"role": "system", "content": RESOLVE_SYSTEM_PROMPT},
        {"role": "user", "content": f"对话历史:\n{history_text}\n\n当前问题: {raw_query}\n\n请按 JSON 格式输出消解结果："},
    ]

    try:
        raw = await asyncio.wait_for(
            llm_service.generate(
                prompt_messages,
                temperature=settings.RESOLVE_LLM_TEMPERATURE,
                max_tokens=settings.RESOLVE_MAX_TOKENS,
                reasoning_effort=settings.RESOLVE_REASONING_EFFORT or None,
            ),
            timeout=settings.RESOLVE_TIMEOUT_MS / 1000,
        )
    except Exception as e:
        logger.warning("消解(error 异常/超时)，降级为原始消息: {} | error: {}", raw_query, str(e))
        return ResolveResult(query=raw_query)

    if not raw or not raw.strip():
        logger.warning("消解(error 返回空)，降级为原始消息: {}", raw_query)
        return ResolveResult(query=raw_query)

    result = _parse_resolve_output(raw, raw_query)

    if len(result.candidates) > _MAX_CANDIDATES:
        logger.warning("消解候选过多({} 个)，截断为 {} 个；被丢弃: {}",
                       len(result.candidates), _MAX_CANDIDATES,
                       result.candidates[_MAX_CANDIDATES:])
        result.candidates = result.candidates[:_MAX_CANDIDATES]

    if result.ambiguous:
        # 多候选：不擅自选定，下游会路由到澄清节点反问用户
        logger.info("消解(多候选 {}): '{}' → 候选取自上文 {}", len(result.candidates),
                    raw_query, result.candidates)
    elif result.query == raw_query.strip():
        # unchanged：自包含问题原样返回（no-op 观测——多轮完整问题也必经一次消解调用）
        logger.info("消解(unchanged 自包含): '{}'", raw_query)
    else:
        logger.info("消解(changed): '{}' → '{}'", raw_query, result.query)
    return result


async def resolve_pronouns(llm_service, messages: List[Dict], raw_query: str) -> str:
    """兼容包装：只取消解后的文本。

    既有调用点（redis_semantic_cache / evaluation.runner / 多处测试）无需改动；
    需要多候选信息的调用点（main.py 入口）改用 `resolve_pronouns_ex`。
    """
    return (await resolve_pronouns_ex(llm_service, messages, raw_query)).query
