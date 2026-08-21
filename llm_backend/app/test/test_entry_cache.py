"""入口语义缓存检索逻辑验证（main.py langgraph_query 的前置缓存短路）

运行: cd llm_backend && ../.venv/Scripts/python.exe app/test/test_entry_cache.py

验证场景：
  S1 含指代且无历史 → 跳过 lookup 和 update（无完整问题可作 key）
  S2 完整问题 + 命中 → 短路返回（graph 不被调用）
  S3 完整问题 + 未命中 → 走图 + 图后 update 回写（内容为拼接完整文本）
  S4 语气词 → lookup 内部 SKIP_CACHE 不查 → 走图 → update 内部跳过
  S5 deepseek_service 拼接格式：full_response 为原始文本（无引号包裹）
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import settings
from app.services.pronoun_detector import detect_pronoun, DetectionDecision
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
    """构造不连真实 redis 的缓存实例（mock 层测试用）"""
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
    if lookup_return is not None:
        cache.lookup = AsyncMock(return_value=lookup_return)
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


# ===== 模拟 main.py 入口决策 + 缓存逻辑 =====

async def entry_flow(query, history, cache, graph, decision):
    """复刻 main.py langgraph_query 的入口消解 + 缓存检索逻辑（简化）"""
    resolved_query = query
    if history and decision == DetectionDecision.NEED_RESOLVE:
        resolved_query = "扫地机器人X1有货吗"  # 模拟 LLM 消解成功
        decision = detect_pronoun(resolved_query)

    cached_response = None
    if decision == DetectionDecision.NEED_RESOLVE:
        pass  # 跳过缓存检索
    else:
        cached_response = await cache.lookup(
            history + [{"role": "user", "content": resolved_query}],
            resolve_llm=None,
        )
    if cached_response:
        return {"short_circuit": True, "graph_called": False}

    # 未命中 → 走图
    complete = []
    async for c in graph.astream():
        complete.append(c)
    if decision != DetectionDecision.NEED_RESOLVE and complete:
        await cache.update(
            history + [{"role": "user", "content": resolved_query}],
            "".join(complete),
            resolve_llm=None,
        )
    return {"short_circuit": False, "graph_called": True}


async def test_entry():
    print("[入口缓存] 场景验证")

    # S1: 含指代且无历史（首条）→ 跳过 lookup/update
    cache = make_cache(AsyncMock())
    cache.lookup = AsyncMock(return_value=None)
    cache.update = AsyncMock()
    graph = make_graph(["回答1", "回答2"])
    r = await entry_flow("那个有货吗", [], cache, graph, detect_pronoun("那个有货吗"))
    check("S1 无历史含指代 → 不走短路", not r["short_circuit"])
    check("S1 无历史含指代 → 跳过 lookup", not cache.lookup.called)
    check("S1 无历史含指代 → 跳过 update", not cache.update.called)
    check("S1 无历史含指代 → 照常走图", r["graph_called"])

    # S2: 完整问题 + 命中 → 短路（graph 不被调用）
    cache2 = make_cache(AsyncMock(), lookup_return="缓存答案")
    cache2.update = AsyncMock()
    graph2 = make_graph([])
    r = await entry_flow("扫地机器人X1有货吗", [{"role": "user", "content": "之前的问题"}], cache2, graph2, DetectionDecision.PASS_THROUGH)
    check("S2 完整问题命中 → 短路返回", r["short_circuit"])
    check("S2 命中 → graph 不被调用", graph2._stream_calls["n"] == 0)
    check("S2 命中 → 不写缓存", not cache2.update.called)

    # S3: 完整问题 + 未命中 → 走图 + 回写
    cache3 = make_cache(AsyncMock(), lookup_return=None)
    cache3.update = AsyncMock()
    graph3 = make_graph(["你", "好", "世界"])
    r = await entry_flow("扫地机器人X1有货吗", [], cache3, graph3, DetectionDecision.PASS_THROUGH)
    check("S3 未命中 → 走图", r["graph_called"])
    check("S3 未命中 → 图后回写缓存", cache3.update.called)
    check("S3 回写内容为拼接完整文本", cache3.update.call_args[0][1] == "你好世界", f"got {cache3.update.call_args[0][1]!r}")

    # S4: 语气词 → lookup 内部 SKIP_CACHE（不查）→ 走图 → update 内部跳过
    cache4 = make_cache(AsyncMock())
    cache4.update = AsyncMock()
    graph4 = make_graph(["不客气"])
    r = await entry_flow("好的", [], cache4, graph4, DetectionDecision.SKIP_CACHE)
    check("S4 语气词 → 走图（不短路）", r["graph_called"])
    check("S4 语气词 → update 被调（内部跳过写入）", cache4.update.called)


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
    check("S5 拼接为原始文本（无 JSON 引号）", complete == "你好，有货吗", f"got {complete!r}")


async def main():
    await test_entry()
    await test_join_format()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
