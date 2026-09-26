"""语义缓存分级指代消解验证（SPEC_SEMANTIC_CACHE_RESOLVE.md §10）

运行: cd llm_backend && ../.venv/Scripts/python.exe app/test/test_pronoun_resolve.py

覆盖:
  §10.1 检测器 10 用例 + 边界词
  消解器: 正常 / 超时降级 / 空结果降级 / 异常降级 / 参数（temperature=0 / max_tokens / reasoning_effort 均取 settings）
  LLM 服务: reasoning_effort 鸭子类型签名 + DeepseekService 透传（stub client）/ 历史截断长度配置化
  缓存层: SKIP_CACHE 不查不写 / NEED_RESOLVE 消解后查找 / 命中返回 / key 基于消解后消息
  真实 Redis 冒烟: lookup/update 全链路（独立 prefix，测试后清理）
"""

import asyncio
import hashlib
import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# 保证 app 包可导入（脚本位于 app/test/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# Windows 控制台默认 GBK，强制 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from app.core.config import settings
from app.services.deepseek_service import DeepseekService
from app.services.llm_factory import LLMFactory
from app.services.ollama_service import OllamaService
from app.services.pronoun_detector import detect_pronoun, DetectionDecision
from app.services.pronoun_resolver import resolve_pronouns, _format_history
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
        ("你们有那些比较好的沙发", DetectionDecision.PASS_THROUGH),   # 疑问词"哪些"误写"那些"（非指代）
        ("你们有哪些比较好的沙发", DetectionDecision.PASS_THROUGH),   # 标准疑问词
        ("那些沙发有货吗", DetectionDecision.NEED_RESOLVE),           # 句首"那些"仍是远指代词
        ("是那些吗", DetectionDecision.NEED_RESOLVE),                 # "是那些"属歧义句，宁可消解不漏检
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
    """mock LLM：记录 temperature/max_tokens/reasoning_effort，返回预设结果"""

    def __init__(self, result="扫地机器人X1有货吗"):
        self.result = result
        self.received_temperature = None
        self.received_max_tokens = None
        self.received_reasoning_effort = None

    async def generate(self, messages, temperature=None, max_tokens=None, reasoning_effort=None):
        self.received_temperature = temperature
        self.received_max_tokens = max_tokens
        self.received_reasoning_effort = reasoning_effort
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
    check(f"max_tokens 取配置值 settings.RESOLVE_MAX_TOKENS={settings.RESOLVE_MAX_TOKENS}",
          llm.received_max_tokens == settings.RESOLVE_MAX_TOKENS, f"got {llm.received_max_tokens}")
    check(f"reasoning_effort 取配置值 {settings.RESOLVE_REASONING_EFFORT!r}",
          llm.received_reasoning_effort == settings.RESOLVE_REASONING_EFFORT,
          f"got {llm.received_reasoning_effort}")

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


# ==================== 2c. 助手话术泄漏防线（问题 A，2026-09-26 端到端测出） ====================
#
# 背景（docs/项目问题.md #20）：真实多轮里助手（澄清节点）问
# "亲～请问您想咨询商品信息、售后问题还是其他呢？"，用户答"我想问下"，
# 消解结果把**助手的话术整段搬进用户消息**："我想问下商品信息、售后问题还是其他呢？"
# → 下游意图识别命中"售后"兜底词 → 回"售后处理服务正在升级中"，答非所问。
#
# 本段必须用**真实 LLM** 跑——被改动的是 prompt，只有真调用能验证其行为。
# 断言口径：消解结果不得引入**用户原话中不存在**的助手话术片段。
#
# ⚠️ 触发条件（实测定位，2026-09-26）：**助手把同一句澄清话术重复问了 ≥2 遍**。
# 实测对照：助手只问 1 次 → 不泄漏；问 2 次/3 次 → 泄漏；两次问的是不同的话 → 不泄漏。
# 机理：同一句在历史里出现两次，模型把它当成"用户反复谈到的话题"，于是补进消解结果。
# 故 A-1/A-2 必须用**重复话术**的历史，否则测不出该缺陷（首版测试用单轮历史，误判为已修复）。

_CLARIFY = "亲～请问您想咨询商品信息、售后问题还是其他呢？"

# (用例名, 历史, 当前消息, 不得出现在消解结果中的助手话术片段)
LEAK_CASES = [
    ("A-1 澄清话术被重复两次（E2E 真实序列）",
     [{"role": "user", "content": "在吗"}, {"role": "assistant", "content": _CLARIFY},
      {"role": "user", "content": "嗯"}, {"role": "assistant", "content": _CLARIFY}],
     "我想问下", ["售后问题", "商品信息"]),
    ("A-2 澄清话术被重复三次",
     [{"role": "user", "content": "在吗"}, {"role": "assistant", "content": _CLARIFY},
      {"role": "user", "content": "嗯"}, {"role": "assistant", "content": _CLARIFY},
      {"role": "user", "content": "那个"}, {"role": "assistant", "content": _CLARIFY}],
     "我想问下", ["售后问题", "商品信息"]),
    ("A-3 罗列选项的澄清话术重复两次",
     [{"role": "user", "content": "你好"},
      {"role": "assistant", "content": "亲～您是想了解商品价格、产品参数，还是售后政策呢？"},
      {"role": "user", "content": "嗯"},
      {"role": "assistant", "content": "亲～您是想了解商品价格、产品参数，还是售后政策呢？"}],
     "我问个事", ["产品参数", "售后政策"]),
    # 单轮不触发（实测 ok），保留为边界对照——防止修复后反向误伤
    ("A-4 澄清话术只出现一次（边界对照）",
     [{"role": "user", "content": "在吗"},
      {"role": "assistant", "content": _CLARIFY}],
     "我想问下", ["售后问题", "商品信息"]),
]

# 既有能力回归（修复 prompt 不得损伤这些）
# (用例名, 历史, 当前消息, 必须包含的片段, 不得包含的片段)
REGRESSION_CASES = [
    ("R-1 指代替换",
     [{"role": "user", "content": "扫地机器人X1多少钱"},
      {"role": "assistant", "content": "扫地机器人X1售价2999元"}],
     "那个有货吗", ["扫地机器人X1"], ["售价", "2999"]),
    ("R-2 省略主语补全",
     [{"role": "user", "content": "这款智能门锁支持指纹吗"},
      {"role": "assistant", "content": "支持指纹和密码双重认证哦"}],
     "多少钱", ["智能门锁"], ["双重认证"]),
]


async def test_resolve_no_assistant_leak():
    """问题 A 防线 + 既有消解能力回归（真实 LLM 调用）。"""
    print("[消解器] 助手话术泄漏防线 + 回归（真实 LLM）")
    llm = LLMFactory.create_chat_service()

    async def _resolve(history, query):
        return await resolve_pronouns(
            llm, history + [{"role": "user", "content": query}], query)

    for name, history, query, forbidden in LEAK_CASES:
        try:
            r = await _resolve(history, query)
        except Exception as e:
            check(f"{name}（LLM 不可用，未验证）", False, f"{type(e).__name__}: {str(e)[:60]}")
            continue
        leaked = [f for f in forbidden if f in r]
        check(f"{name}: '{query}' → '{r}'", not leaked, f"泄漏助手话术片段={leaked}")

    for name, history, query, must, must_not in REGRESSION_CASES:
        try:
            r = await _resolve(history, query)
        except Exception as e:
            check(f"{name}（LLM 不可用，未验证）", False, f"{type(e).__name__}: {str(e)[:60]}")
            continue
        missing = [m for m in must if m not in r]
        leaked = [m for m in must_not if m in r]
        check(f"{name}: '{query}' → '{r}'", not missing and not leaked,
              f"缺失={missing} 泄漏={leaked}")


def test_history_truncation_configurable():
    """历史单条截断长度取配置值（临时改小 settings 值即可观测，证明未被硬编码）"""
    print("[消解器] 历史截断长度配置化")
    old = settings.RESOLVE_MAX_CHARS_PER_MSG
    settings.RESOLVE_MAX_CHARS_PER_MSG = 5
    try:
        hist = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "商" * 50},
            {"role": "user", "content": "追问"},
        ]
        text = _format_history(hist, max_turns=5)
        body = text.split("助手: ")[1].strip()
        check("助手消息按 settings.RESOLVE_MAX_CHARS_PER_MSG=5 截断",
              len(body) == 5, f"got {len(body)} 字")
    finally:
        settings.RESOLVE_MAX_CHARS_PER_MSG = old


# ==================== 2b. LLM 服务参数透传（方案A/B 回归防线） ====================


def test_llm_service_signature():
    """鸭子类型契约：两个 LLM 服务的 generate 都必须接受 reasoning_effort"""
    print("[LLM 服务] generate 签名（鸭子类型契约）")
    for cls in (DeepseekService, OllamaService):
        params = inspect.signature(cls.generate).parameters
        check(f"{cls.__name__}.generate 接受 reasoning_effort",
              "reasoning_effort" in params, f"got {list(params)}")


async def test_deepseek_generate_passthrough():
    """DeepseekService.generate 把 reasoning_effort 透传给 API（stub client，不联网）"""
    print("[LLM 服务] DeepseekService.generate 参数透传")
    svc = DeepseekService.__new__(DeepseekService)  # 跳过 __init__，不创建真实 client
    svc.model = "stub"
    captured = {}

    class _StubCompletions:
        async def create(self, **kwargs):
            captured.clear()
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    svc.client = SimpleNamespace(chat=SimpleNamespace(completions=_StubCompletions()))
    msg = [{"role": "user", "content": "hi"}]

    await svc.generate(msg, temperature=0.0, max_tokens=1024, reasoning_effort="none")
    check("reasoning_effort='none' 透传到 API",
          captured.get("reasoning_effort") == "none", f"got {captured.get('reasoning_effort')}")
    check("max_tokens 透传到 API", captured.get("max_tokens") == 1024, f"got {captured.get('max_tokens')}")

    await svc.generate(msg, temperature=0.0)
    check("未配置 reasoning_effort 时不传该参数（不影响其他调用方）",
          "reasoning_effort" not in captured, f"got {captured.get('reasoning_effort')}")
    check("未配置 max_tokens 时不传该参数", "max_tokens" not in captured)


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
    await test_resolve_no_assistant_leak()
    test_history_truncation_configurable()
    test_llm_service_signature()
    await test_deepseek_generate_passthrough()
    await test_cache_skip()
    await test_cache_resolve()
    await test_cache_hit()
    await test_update_resolve()
    await test_redis_smoke()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
