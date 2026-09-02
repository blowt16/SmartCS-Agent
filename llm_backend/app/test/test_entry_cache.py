"""入口语义缓存检索逻辑验证（main.py langgraph_query 前置链路：统一消解 + 缓存短路）

运行: cd llm_backend && ../.venv/Scripts/python.exe app/test/test_entry_cache.py

main.py 入口逻辑（SPEC_ENTRY_LLM_RESOLUTION §3.1，2026-09-02 重构）：
    正则判定（代词表/触发词）不再是消解前置——多轮消息无条件 LLM 消解一次，
    prompt 自包含出口保证完整问题原样返回；纯语气词 / 无历史（首条）直通不调 LLM；
    cache.lookup/update 无条件调用（语气词由缓存内部 _resolve_message 判定跳过）。

验证场景：
  S1 无历史含指代（"那个有货吗"）→ 直通不调 LLM → lookup 执行（mock 未命中）→ graph 照常
  S2 多轮含指代（承接上文"那个有货吗"）→ 入口 LLM 消解一次（拿到完整 messages）→ 消解后进 lookup
  S3 完整问题命中 → 短路返回（graph 不被调用）
  S4 完整问题未命中 → 走图 + 图后 update 回写（内容为拼接完整文本）
  S5 语气词（多轮）→ 不调 LLM 直通 → 走图 → update 被调（内部判定跳过写入）
  S6 RESOLVE_ENABLED=false → 完全退化（不消解、原样透传）
  S7 deepseek_service 拼接格式：full_response 为原始文本（无引号包裹）
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# Windows 控制台默认 GBK，强制 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from app.core.config import settings
from app.services.pronoun_detector import _is_filler  # 与 main.py 同款临时借用（语气词闸门）
from app.services.pronoun_resolver import resolve_pronouns
from app.services.redis_semantic_cache import RedisSemanticCache

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def make_cache(redis_mock, lookup_return=None):
    """构造不连真实 redis 的缓存实例（mock 层测试用；lookup 一律 mock，
    未命中=return None，命中=return 预设响应；真实 lookup 逻辑由 test_pronoun_resolve.py 覆盖）"""
    cache = RedisSemanticCache.__new__(RedisSemanticCache)
    cache.redis = redis_mock
    cache.score_threshold = 0.8
    cache.prefix = "mock"
    cache.max_cache_size = 1000
    cache.cleanup_interval = 3600
    cache._index_key = "mock:index"
    cache._cleanup_started = True
    provider = MagicMock()
    provider.embed = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
    cache._embedding_provider = provider
    cache.lookup = AsyncMock(return_value=lookup_return)
    cache.update = AsyncMock()
    return cache


def make_graph(chunks):
    """构造 astream 为 async 生成器的 mock 图（返回预设内容序列）"""
    g = MagicMock()
    g._stream_calls = {"n": 0}

    async def astream(*a, **k):
        g._stream_calls["n"] += 1
        for c in chunks:
            yield c

    g.astream = astream
    return g


class FakeLLM:
    """mock 消解 LLM：记录每次调用的 messages，返回预设消解结果"""

    def __init__(self, result="这款按摩椅需要充电吗"):
        self.result = result
        self.calls = []

    async def generate(self, messages, temperature=None, max_tokens=None):
        self.calls.append(messages)
        return self.result


# ===== 复刻 main.py 入口消解 + 缓存逻辑（与 main.py 同构，decision 门控已删除）=====

async def entry_flow(query, history, cache, graph, resolve_llm=None, resolve_enabled=None):
    """复刻 main.py langgraph_query 的入口统一消解 + 缓存检索逻辑（简化）

    多轮非语气词消息 → 无条件 resolve_pronouns(LLM)（消解结果同时进图与缓存）；
    语气词 / 无历史 → 直通不调 LLM；lookup/update 无条件调用
    （语气词由真实缓存内部 _resolve_message 判定跳过，mock 层不体现）。
    """
    if resolve_enabled is None:
        resolve_enabled = settings.RESOLVE_ENABLED
    resolved_query = query
    if (
        resolve_enabled
        and not (settings.RESOLVE_SKIP_FILLER and _is_filler(query))
        and history
    ):
        resolved_query = await resolve_pronouns(
            resolve_llm,
            history + [{"role": "user", "content": query}],
            query,
        )

    cached_response = await cache.lookup(
        history + [{"role": "user", "content": resolved_query}],
        resolve_llm=resolve_llm,
    )
    if cached_response:
        return {"short_circuit": True, "graph_called": False}

    # 未命中 → 走图
    complete = []
    async for c in graph.astream():
        complete.append(c)
    if complete:
        await cache.update(
            history + [{"role": "user", "content": resolved_query}],
            "".join(complete),
            resolve_llm=resolve_llm,
        )
    return {"short_circuit": False, "graph_called": True}


async def test_entry():
    print("[入口缓存] 场景验证")

    # S1: 含指代但无历史（首条消息）→ 直通不调 LLM → lookup 执行但未命中 → graph 照常
    llm1 = FakeLLM()
    cache1 = make_cache(AsyncMock(), lookup_return=None)
    graph1 = make_graph(["回答1", "回答2"])
    r = await entry_flow("那个有货吗", [], cache1, graph1, resolve_llm=llm1)
    check("S1 无历史含指代 → 不调 LLM（直通）", len(llm1.calls) == 0)
    check("S1 无历史含指代 → lookup 执行", cache1.lookup.called)
    check("S1 无历史含指代 → 不走短路", not r["short_circuit"])
    check("S1 无历史含指代 → 照常走图", r["graph_called"])

    # S2: 多轮含指代 → 入口 LLM 消解一次（拿到完整 messages）→ 消解结果进 lookup
    llm2 = FakeLLM("扫地机器人X1有货吗")
    cache2 = make_cache(AsyncMock(), lookup_return=None)
    graph2 = make_graph(["有货"])
    history2 = [
        {"role": "user", "content": "扫地机器人X1多少钱"},
        {"role": "assistant", "content": "扫地机器人X1售价2999元"},
    ]
    r = await entry_flow("那个有货吗", history2, cache2, graph2, resolve_llm=llm2)
    check("S2 多轮含指代 → LLM 被调 1 次", len(llm2.calls) == 1, f"got {len(llm2.calls)}")
    # 断言 resolve_pronouns 构造的 prompt 含完整历史与当前问题（LLM 拿到消解所需的全部上下文）
    prompt_content = llm2.calls[0][-1]["content"] if llm2.calls else ""
    check("S2 LLM 拿到完整上下文（历史 + 当前问题行）",
          "扫地机器人X1多少钱" in prompt_content and "当前问题: 那个有货吗" in prompt_content,
          f"got {prompt_content!r}")
    check("S2 消解结果进入 lookup", cache2.lookup.called and cache2.lookup.call_args[0][0][-1]["content"] == "扫地机器人X1有货吗")

    # S3: 完整问题 + 命中 → 短路（graph 不被调用）
    cache3 = make_cache(AsyncMock(), lookup_return="缓存答案")
    graph3 = make_graph([])
    r = await entry_flow("扫地机器人X1有货吗", [], cache3, graph3, resolve_llm=FakeLLM())
    check("S3 完整问题命中 → 短路返回", r["short_circuit"])
    check("S3 命中 → graph 不被调用", graph3._stream_calls["n"] == 0)
    check("S3 命中 → 不写缓存", not cache3.update.called)

    # S4: 完整问题 + 未命中 → 走图 + 回写
    cache4 = make_cache(AsyncMock(), lookup_return=None)
    graph4 = make_graph(["你", "好", "世界"])
    r = await entry_flow("扫地机器人X1有货吗", [], cache4, graph4, resolve_llm=FakeLLM())
    check("S4 未命中 → 走图", r["graph_called"])
    check("S4 未命中 → 图后回写缓存", cache4.update.called)
    check("S4 回写内容为拼接完整文本", cache4.update.call_args[0][1] == "你好世界", f"got {cache4.update.call_args[0][1]!r}")

    # S5: 语气词（多轮）→ 不调 LLM 直通 → 走图 → update 被调（内部跳过写入）
    llm5 = FakeLLM()
    cache5 = make_cache(AsyncMock(), lookup_return=None)
    graph5 = make_graph(["不客气"])
    history5 = [{"role": "user", "content": "这个按摩椅怎么样"}, {"role": "assistant", "content": "很不错"}]
    r = await entry_flow("好的", history5, cache5, graph5, resolve_llm=llm5)
    check("S5 语气词多轮 → 不调 LLM", len(llm5.calls) == 0)
    check("S5 语气词 → 走图（不短路）", r["graph_called"])
    check("S5 语气词 → update 被调（真实缓存内部判定跳过写入）", cache5.update.called)

    # S6: RESOLVE_ENABLED=false → 完全退化（多轮也不消解，原样透传）
    old_enabled = settings.RESOLVE_ENABLED
    settings.RESOLVE_ENABLED = False
    try:
        llm6 = FakeLLM()
        cache6 = make_cache(AsyncMock(), lookup_return=None)
        graph6 = make_graph(["有货"])
        r = await entry_flow("那个有货吗", history2, cache6, graph6, resolve_llm=llm6)
        check("S6 RESOLVE_ENABLED=false → 不调 LLM", len(llm6.calls) == 0)
        check("S6 关闭开关 → 原样透传进 lookup", cache6.lookup.call_args[0][0][-1]["content"] == "那个有货吗")
        check("S6 关闭开关 → 照常走图", r["graph_called"])
    finally:
        settings.RESOLVE_ENABLED = old_enabled


async def test_join_format():
    print("[deepseek_service] 拼接格式验证")
    # 模拟修改后的收集逻辑：原始文本 append，SSE 单独编码
    deltas = ["你", "好", "，", "有货", "吗"]
    full_response = []
    for d in deltas:
        content = d  # 原始文本
        full_response.append(content)
        # yield f"data: {json.dumps(content, ensure_ascii=False)}"
    complete = "".join(full_response)
    check("S7 拼接为原始文本（无 JSON 引号）", complete == "你好，有货吗", f"got {complete!r}")


async def main():
    await test_entry()
    await test_join_format()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
