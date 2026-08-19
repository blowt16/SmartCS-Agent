"""语义缓存分级指代消解验证（SPEC_SEMANTIC_CACHE_RESOLVE.md §10）

运行: cd llm_backend && ../.venv/Scripts/python.exe app/test/test_pronoun_resolve.py

覆盖:
  §10.1 检测器 10 用例 + 边界词
  消解器: 正常 / 超时降级 / 空结果降级 / 异常降级 / 参数（temperature=0, max_tokens=200）
  缓存层: SKIP_CACHE 不查不写 / NEED_RESOLVE 消解后查找 / 命中返回 / key 基于消解后消息
  真实 Redis 冒烟: lookup/update 全链路（独立 prefix，测试后清理）
"""

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# 保证 app 包可导入（脚本位于 app/test/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# Windows 控制台默认 GBK，强制 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from app.core.config import settings
from app.services.pronoun_detector import detect_pronoun, DetectionDecision
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


# ==================== 1. 检测器（§10.1） ====================


def test_detector():
    print("[检测器] §10.1 用例")
    cases = [
        # (当前消息, 期望检测结果)
        ("那个有货吗", DetectionDecision.NEED_RESOLVE),      # 显性指代-指示代词
        ("它支持快充吗", DetectionDecision.NEED_RESOLVE),    # 显性指代-人称代词
        ("能退吗", DetectionDecision.NEED_RESOLVE),          # 省略主语-短问句
        ("还有吗", DetectionDecision.NEED_RESOLVE),          # 省略主语-追问
        ("iPhone 15 256G价格", DetectionDecision.PASS_THROUGH),      # 无指代-完整问题
        ("iPhone 15 256G蓝色有现货吗", DetectionDecision.PASS_THROUGH),  # 无指代-长句
        ("好的", DetectionDecision.SKIP_CACHE),              # 纯语气词-确认
        ("知道了", DetectionDecision.SKIP_CACHE),            # 纯语气词-反馈
        ("那个...", DetectionDecision.NEED_RESOLVE),         # 消解降级场景（检测仍命中）
        ("iPhone 15价格", DetectionDecision.PASS_THROUGH),   # 首条消息
    ]
    for text, expect in cases:
        got = detect_pronoun(text)
        check(f"'{text}' → {expect.value}", got == expect, f"got {got.value}")

    print("[检测器] 边界用例")
    check("'可以' → SKIP_CACHE（整句语气词）", detect_pronoun("可以") == DetectionDecision.SKIP_CACHE)
    check("'可以退吗' → NEED_RESOLVE（问句开头）", detect_pronoun("可以退吗") == DetectionDecision.NEED_RESOLVE)
    check("skip_filler=False 时 '好的' → PASS_THROUGH", detect_pronoun("好的", skip_filler=False) == DetectionDecision.PASS_THROUGH)
    check("'该产品支持快充吗' → NEED_RESOLVE（书面指代）", detect_pronoun("该产品支持快充吗") == DetectionDecision.NEED_RESOLVE)
    check("'还有类似的产品吗' → NEED_RESOLVE", detect_pronoun("还有类似的产品吗") == DetectionDecision.NEED_RESOLVE)
    check("'好的，谢谢' 混合句 → PASS_THROUGH（不在语气词表）", detect_pronoun("好的，谢谢") == DetectionDecision.PASS_THROUGH)
    check("空串 → PASS_THROUGH", detect_pronoun("") == DetectionDecision.PASS_THROUGH)


# ==================== 2. 消解器（§5） ====================


class FakeLLM:
    """mock LLM：记录 temperature/max_tokens，返回预设结果"""

    def __init__(self, result="扫地机器人X1有货吗"):
        self.result = result
        self.received_temperature = None
        self.received_max_tokens = None

    async def generate(self, messages, temperature=None, max_tokens=None):
        self.received_temperature = temperature
        self.received_max_tokens = max_tokens
        return self.result


HISTORY = [
    {"role": "user", "content": "扫地机器人X1多少钱"},
    {"role": "assistant", "content": "扫地机器人X1售价2999元"},
    {"role": "user", "content": "那个有货吗"},
]


async def test_resolver():
    print("[消解器] 正常/降级路径")
    llm = FakeLLM()
    r = await resolve_pronouns(llm, HISTORY, "那个有货吗")
    check("正常消解返回完整问题", r == "扫地机器人X1有货吗", f"got {r}")
    check("temperature=0.0 传入 LLM", llm.received_temperature == 0.0, f"got {llm.received_temperature}")
    check("max_tokens=200 传入 LLM", llm.received_max_tokens == 200, f"got {llm.received_max_tokens}")

    r = await resolve_pronouns(FakeLLM(""), HISTORY, "那个有货吗")
    check("空结果 → 降级为原始消息", r == "那个有货吗")

    class ErrLLM:
        async def generate(self, *a, **k):
            raise RuntimeError("llm down")
    r = await resolve_pronouns(ErrLLM(), HISTORY, "那个有货吗")
    check("异常 → 降级为原始消息", r == "那个有货吗")

    old_timeout = settings.RESOLVE_TIMEOUT_MS
    settings.RESOLVE_TIMEOUT_MS = 100
    try:
        class SlowLLM:
            async def generate(self, *a, **k):
                await asyncio.sleep(5)
                return "x"
        r = await resolve_pronouns(SlowLLM(), HISTORY, "那个有货吗")
        check("超时(100ms) → 降级为原始消息", r == "那个有货吗")
    finally:
        settings.RESOLVE_TIMEOUT_MS = old_timeout


# ==================== 3. 缓存层（mock redis） ====================


async def _no_scan_keys(match=None):
    """空结果的 async 生成器，供 redis.scan_iter mock 使用"""
    if False:
        yield b""


def make_cache(redis_mock, embed_fn, vec_dim=3):
    """构造不连接真实 redis 的缓存实例（仅 mock 层测试用）"""
    cache = RedisSemanticCache.__new__(RedisSemanticCache)
    cache.redis = redis_mock
    cache.score_threshold = 0.8
    cache.prefix = "mock"
    cache.max_cache_size = 1000
    cache.cleanup_interval = 3600
    cache._index_key = "mock:index"
    cache._cleanup_started = True
    provider = MagicMock()
    provider.embed = AsyncMock(side_effect=lambda texts: [embed_fn(texts[0])])
    cache._embedding_provider = provider
    return cache


async def test_cache_skip():
    print("[缓存层] SKIP_CACHE 不查不写")
    redis_mock = AsyncMock()
    embed_calls = []
    cache = make_cache(redis_mock, lambda t: embed_calls.append(t) or [1.0, 0.0, 0.0])

    r = await cache.lookup([{"role": "user", "content": "好的"}], resolve_llm=FakeLLM())
    check("lookup 纯语气词 → None", r is None)
    check("lookup 纯语气词 → 未调用 embedding", len(embed_calls) == 0)
    check("lookup 纯语气词 → 未调用 redis", redis_mock.zrange.call_count == 0)

    await cache.update([{"role": "user", "content": "知道了"}], "不客气", resolve_llm=FakeLLM())
    check("update 纯语气词 → 未调用 redis.set", not redis_mock.set.called)


async def test_cache_resolve():
    print("[缓存层] NEED_RESOLVE 消解后查找")
    redis_mock = AsyncMock()
    redis_mock.zcard = AsyncMock(return_value=0)              # 空索引 → 触发重建
    redis_mock.scan_iter = _no_scan_keys                      # 无存量键
    redis_mock.zrange = AsyncMock(return_value=[])
    embed_inputs = []
    cache = make_cache(redis_mock, lambda t: embed_inputs.append(t) or [1.0, 0.0, 0.0])
    llm = FakeLLM("扫地机器人X1有货吗")

    r = await cache.lookup(HISTORY, resolve_llm=llm)
    check("lookup 含指代 → None（无匹配缓存）", r is None)
    check("embedding 输入为消解后消息", embed_inputs == ["扫地机器人X1有货吗"], f"got {embed_inputs}")
    check("触发了 LLM 消解", llm.received_temperature == 0.0)

    # RESOLVE_ENABLED=false → 完全退化：不消解，用原始消息查找
    old_enabled = settings.RESOLVE_ENABLED
    settings.RESOLVE_ENABLED = False
    try:
        embed_inputs.clear()
        r = await cache.lookup(HISTORY, resolve_llm=llm)
        check("RESOLVE_ENABLED=false → 不消解（embedding 输入为原始消息）",
              embed_inputs == ["那个有货吗"], f"got {embed_inputs}")
    finally:
        settings.RESOLVE_ENABLED = old_enabled


async def test_cache_hit():
    print("[缓存层] 命中返回")
    hash_id = hashlib.md5("扫地机器人X1有货吗".encode()).hexdigest()
    redis_mock = AsyncMock()
    redis_mock.zcard = AsyncMock(return_value=1)
    redis_mock.zrange = AsyncMock(return_value=[hash_id])

    async def fake_get(key):
        if key == f"mock:vec:{hash_id}":
            return json.dumps([1.0, 0.0, 0.0]).encode()
        if key == f"mock:resp:{hash_id}":
            return "扫地机器人X1售价2999元".encode()
        if key == f"mock:meta:{hash_id}":
            return json.dumps({"access_count": 1}).encode()
        return None
    redis_mock.get = AsyncMock(side_effect=fake_get)

    cache = make_cache(redis_mock, lambda t: [1.0, 0.0, 0.0])

    # 含指代消息消解后命中
    r = await cache.lookup(HISTORY, resolve_llm=FakeLLM("扫地机器人X1有货吗"))
    check("含指代消息 → 消解后命中缓存", r == "扫地机器人X1售价2999元", f"got {r}")
    check("命中后 zadd 更新索引访问时间", redis_mock.zadd.called)

    # 完整问题直接命中同一缓存（同 key）
    r2 = await cache.lookup([{"role": "user", "content": "扫地机器人X1有货吗"}])
    check("完整问题 → 命中同一缓存（同源消解）", r2 == "扫地机器人X1售价2999元", f"got {r2}")


async def test_update_resolve():
    print("[缓存层] update key 基于消解后消息")
    redis_mock = AsyncMock()
    cache = make_cache(redis_mock, lambda t: [1.0, 0.0, 0.0])

    await cache.update(HISTORY, "有货", resolve_llm=FakeLLM("扫地机器人X1有货吗"))
    sets = {c.args[0] for c in redis_mock.set.call_args_list}
    hash_id = hashlib.md5("扫地机器人X1有货吗".encode()).hexdigest()
    check("vec key 基于消解后消息", f"mock:vec:{hash_id}" in sets, f"got {sets}")
    check("resp key 基于消解后消息", f"mock:resp:{hash_id}" in sets)
    check("zadd 维护有序索引", redis_mock.zadd.called)


# ==================== 4. 真实 Redis 冒烟 ====================


async def test_redis_smoke():
    print("[真实 Redis] 全链路冒烟（独立 prefix，结束清理）")
    try:
        cache = RedisSemanticCache(prefix="smoke_resolve_test")
        await cache.redis.ping()
    except Exception as e:
        print(f"  ! redis 不可用，跳过冒烟: {e}")
        return

    llm = FakeLLM("扫地机器人X1有货吗")
    # 写入：含指代消息 → 消解后存储
    await cache.update(HISTORY, "有货", resolve_llm=llm)
    # 诊断：直接检查写入的向量是否有效（非全 0）
    members = await cache.redis.zrange(cache._index_key, 0, -1)
    first = members[0].decode("utf-8") if members and isinstance(members[0], bytes) else (members[0] if members else None)
    vec_raw = await cache.redis.get(f"smoke_resolve_test:vec:{first}") if first else None
    vec = json.loads(vec_raw.decode()) if vec_raw else []
    norm = (sum(v * v for v in vec) ** 0.5) if vec else 0
    print(f"  (诊断) 索引成员数={len(members)} 向量维度={len(vec)} L2范数={norm:.4f}")
    # 查找：同一消息 → 命中
    r = await cache.lookup(HISTORY, resolve_llm=llm)
    check("写入消解后 + 查找同消息 → 命中", r == "有货", f"got {r}")
    # 查找：完整问题（PASS_THROUGH 透传）→ 命中同一缓存
    r2 = await cache.lookup([{"role": "user", "content": "扫地机器人X1有货吗"}])
    check("完整问题透传 → 命中同一缓存（原则三）", r2 == "有货", f"got {r2}")
    # 纯语气词 → 不查
    r3 = await cache.lookup([{"role": "user", "content": "好的"}], resolve_llm=llm)
    check("纯语气词 → 不查缓存", r3 is None)
    # 无指代不相关问题 → 未命中
    r4 = await cache.lookup([{"role": "user", "content": "小米手环8多少钱"}])
    check("不相关完整问题 → 未命中", r4 is None, f"got {r4}")

    # 清理冒烟数据
    await cache.redis.delete(cache._index_key)
    async for k in cache.redis.scan_iter(match="smoke_resolve_test:*"):
        await cache.redis.delete(k)
    print("  ✓ 冒烟数据已清理")


async def main():
    test_detector()
    await test_resolver()
    await test_cache_skip()
    await test_cache_resolve()
    await test_cache_hit()
    await test_update_resolve()
    await test_redis_smoke()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
