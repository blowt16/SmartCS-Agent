"""
多轮消息统一消解器（SPEC_ENTRY_LLM_RESOLUTION §3.2/§4.2）

一次 LLM 调用完成指代消除 + 语义补全；prompt 内自包含出口保证完整问题原样返回。
调用方：main.py 入口（多轮消息无条件消解）与语义缓存内部 _resolve_message（现状旧两段式，
缓存入口改造见 docs/项目问题.md #11），两处共用同一 prompt 与降级路径。

设计原则：
    - 只做补全，不做扩展：LLM 只负责把依赖上下文的成分补全为完整独立的问题
    - 消解失败不阻塞：超时/空结果/异常一律降级为原始消息，保证主流程不中断
    - 与 LLM 后端解耦：通过 generate(messages, temperature=, max_tokens=) 鸭子类型调用，
      DeepseekService / OllamaService 均可（二者签名一致）
    - 三态日志：unchanged（自包含原样）/ changed（补全）/ error（降级），供 no-op 率观测

用法:
    resolved = await resolve_pronouns(llm_service, messages, raw_query)
    # 失败时返回 raw_query 原样
"""

import asyncio
from typing import List, Dict

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(service="pronoun_resolver")

# 单条历史消息的最大截断长度（防止 prompt 过长）
RESOLVE_MAX_CHARS_PER_MSG = 200

# 消解 max_tokens：只需返回一个问题文本
RESOLVE_MAX_TOKENS = 200

RESOLVE_SYSTEM_PROMPT = """你是一个多轮对话的指代消解与语义补全专家。
你的任务是根据对话历史，把用户当前问题中依赖上下文的成分（指代词、省略的主语/宾语、不完整信息）补全为完整、独立的问题。

规则：
1. 如果当前问题包含代词（他/她/它/那个/这个/那件/这件/该产品等），用历史中的实体替换
2. 如果当前问题是省略句（如"有货吗""多少钱""能退吗""需要充电吗"），从历史中补全主语
3. 如果当前问题已完整独立（包含明确主语、不依赖上下文），直接原样返回，不要添加或修改任何信息
4. 如果当前问题是命令式指令（如"查一下价格"），补全为完整的查询意图，不要改写成实体搜索
5. 不要添加历史中没出现过的信息，只做补全，不做扩展
6. 只输出消解后的完整问题文本，不要任何解释"""


def _format_history(messages: List[Dict], max_turns: int) -> str:
    """
    将消息列表格式化为 LLM 可读的对话历史文本。

    只取最近 max_turns 轮（1轮 = 1条用户 + 1条助手），每条截断到
    RESOLVE_MAX_CHARS_PER_MSG 字，避免历史过长导致 prompt 超长。

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
        if len(content) > RESOLVE_MAX_CHARS_PER_MSG:
            content = content[:RESOLVE_MAX_CHARS_PER_MSG]
        lines.append(f"{role}: {content}")

    return "\n".join(lines)


async def resolve_pronouns(llm_service, messages: List[Dict], raw_query: str) -> str:
    """
    指代消解主入口：利用对话历史把当前问题补全为完整、独立的问题。

    Args:
        llm_service: 具备 generate(messages, temperature=, max_tokens=) 的服务
                     （DeepseekService / OllamaService 均可）
        messages: 完整对话消息列表（最后一条为当前用户消息）
        raw_query: 原始用户消息

    Returns:
        消解后的完整问题；任何失败（超时/空/异常）时降级返回 raw_query
    """
    history_text = _format_history(messages, max_turns=settings.RESOLVE_MAX_TURNS)

    prompt_messages = [
        {"role": "system", "content": RESOLVE_SYSTEM_PROMPT},
        {"role": "user", "content": f"对话历史:\n{history_text}\n\n当前问题: {raw_query}\n\n请输出消解后的完整问题："},
    ]

    try:
        resolved = await asyncio.wait_for(
            llm_service.generate(
                prompt_messages,
                temperature=settings.RESOLVE_LLM_TEMPERATURE,
                max_tokens=RESOLVE_MAX_TOKENS,
            ),
            timeout=settings.RESOLVE_TIMEOUT_MS / 1000,
        )
    except Exception as e:
        logger.warning("消解(error 异常/超时)，降级为原始消息: {} | error: {}", raw_query, str(e))
        return raw_query

    if not resolved or not resolved.strip():
        logger.warning("消解(error 返回空)，降级为原始消息: {}", raw_query)
        return raw_query

    resolved_text = resolved.strip()
    if resolved_text == raw_query.strip():
        # unchanged：自包含问题原样返回（no-op 观测——多轮完整问题也必经一次消解调用）
        logger.info("消解(unchanged 自包含): '{}'", raw_query)
    else:
        logger.info("消解(changed): '{}' → '{}'", raw_query, resolved_text)
    return resolved_text
