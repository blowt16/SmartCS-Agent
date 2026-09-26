"""端到端多轮对话测试（进程内，复刻 main.py 入口顺序）。

用法: cd llm_backend && ../.venv/Scripts/python.exe -m scripts.e2e_multi_turn

## 为什么需要它（不要删）

节点级评测（`eval_intent_golden.py`）直接调 `analyze_and_route_query`，且多轮的
assistant 回复是**手写的**（还刻意含关键名词，等于给模型送分）。真实场景中
① 回复由系统生成、指代消解更难；② 入口的 LLM 指代消解根本没被覆盖；
③ 走的是完整图 + 检查点 + 线程恢复。

它已接连挖出三个节点级评测全绿的缺陷：
    · 项目问题.md #20 消解把助手话术抄进用户消息
    · 项目问题.md #21 助手列多商品时用户指代被擅自选定
    · 项目问题.md #22 澄清节点 LLM 路径从未生效（format 缺参静默降级）

## 复刻的入口顺序（main.py）

    _is_filler 闸门 → resolve_pronouns_ex（多轮无条件 LLM 消解）→ graph.astream
（语义缓存已在 .env 关闭，不参与）

## 环境

需 PostgreSQL + Redis 已启动（docker compose up -d）；会真实调用 LLM 与检索。
每次运行使用随机 thread_id，不污染既有会话。
"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import app.core.database  # noqa: E402,F401 —— Windows 必选：Selector 事件循环补丁
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.lg_agent.lg_builder import close_checkpointer, graph, init_checkpointer  # noqa: E402
from app.lg_agent.stream_filter import StreamChunkFilter  # noqa: E402
from app.lg_agent.utils import new_uuid  # noqa: E402
from app.services.pronoun_detector import _is_filler  # noqa: E402
from app.services.pronoun_resolver import resolve_pronouns_ex  # noqa: E402

# ===== 真实多轮对话（含省略/指代/话题切换/情绪升级/多候选/已指明主体）=====
CONVERSATIONS = {
    "对话1·指代与省略密集": [
        "你们有智能门锁吗",
        "多少钱",              # 省略主语
        "能便宜点吗",           # 规则层可命中
        "那安装呢",             # 规则层不命中 → 靠历史
    ],
    "对话2·售前转售后": [
        "推荐一款扫地机器人",
        "这款保修多久",         # 多候选指代（助手会列多款）→ 期望追问
        "我想退货",
        "运费谁承担",
    ],
    "对话3·情绪升级": [
        "我的快递怎么还没到",
        "都三天了还没动",
        "你们太差劲了",
        "我要投诉",             # 期望 high_risk 转人工
    ],
    "对话4·从澄清中恢复": [
        "在吗",
        "嗯",
        "我想问下",
        "算了，给我推荐个摄像头吧",
    ],
    # ⚠️ 关键反例（SPEC_MULTI_CANDIDATE_REFERENCE §7.3 的澄清频率风险）：
    # 用户**上一轮已指明具体商品**，此后未引入新商品 → 指代目标唯一，
    # **不得**判为多候选而反复澄清。若这里出现澄清即为设计过于激进。
    "对话5·已指明主体再用指代（反例）": [
        "小米智能门锁2 指静脉版多少钱",   # 用户自己锁定型号
        "那个保修多久",                  # 指代唯一 → 应正常消解，不得澄清
        "有货吗",                       # 省略主语，仍唯一 → 不得澄清
        "那算了，退货吧",                # 场景切换
    ],
}


def _history_from(msgs):
    """从图状态取历史，转成 main.py 用的 [{'role','content'}] 形式。"""
    out = []
    for m in msgs:
        if isinstance(m, HumanMessage):
            out.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage) and m.content:
            out.append({"role": "assistant", "content": m.content})
    return out


async def run_conversation(title, turns):
    thread_id = f"e2e-{new_uuid()}"
    config = {"configurable": {"thread_id": thread_id}}
    stats = {"clarify": 0, "multi_candidate": 0, "turns": 0}
    print(f"\n{'=' * 100}\n{title}   thread={thread_id}\n{'=' * 100}")

    for i, query in enumerate(turns, 1):
        t0 = time.time()
        state = await graph.aget_state(config)
        history = _history_from(state.values.get("messages", [])) if state.values else []

        # ---- 入口：语气词闸门 + 多轮无条件 LLM 消解（复刻 main.py）----
        resolved, candidates, skipped = query, [], False
        if settings.RESOLVE_ENABLED and history:
            if settings.RESOLVE_SKIP_FILLER and _is_filler(query):
                skipped = True
            else:
                try:
                    r = await resolve_pronouns_ex(
                        _get_resolve_llm(),
                        history + [{"role": "user", "content": query}],
                        query,
                    )
                    resolved, candidates = r.query, r.candidates
                except Exception as e:
                    print(f"    [消解异常] {str(e)[:70]}")

        # ---- 进图 ----
        filt, buf = StreamChunkFilter(), []
        try:
            async for c, md in graph.astream(
                input={"messages": resolved, "ref_candidates": candidates},
                stream_mode="messages", config=config):
                txt = filt.select(c, md)
                if txt:
                    buf.append(txt)
        except Exception as e:
            print(f"  T{i} '{query}' → 图执行异常: {str(e)[:120]}")
            continue

        st = await graph.aget_state(config)
        r = (st.values or {}).get("router", {}) or {}
        answer = "".join(buf).strip() or "(无可见输出)"
        dt = time.time() - t0
        stats["turns"] += 1
        # ⚠️ 澄清计数**不能**按 router["type"]=="clarify"：多候选路径的 type 由规则层/LLM
        # 判定（常为 presale），但 route_query 以候选优先改走 clarify_node。
        # 按实际落点统计，口径与 route_query 保持一致。
        went_clarify = len(candidates) >= 2 and r.get("risk") == "none"
        if not went_clarify and r.get("type") == "clarify" and r.get("risk") == "none":
            went_clarify = True
        if went_clarify:
            stats["clarify"] += 1
        if candidates:
            stats["multi_candidate"] += 1

        print(f"\n  T{i} 用户: {query}")
        if skipped:
            print("      [语气词跳过消解]")
        elif candidates:
            print(f"      [多候选 {len(candidates)}] {candidates}")
        elif resolved != query:
            print(f"      [已消解] → '{resolved}'")
        print(f"      判定: type={r.get('type')} sub_type={r.get('sub_type')} "
              f"risk={r.get('risk')} source={r.get('source')}"
              + ("  → 实走 clarify_node（多候选）" if went_clarify and r.get('type') != 'clarify' else ""))
        print(f"      回答({dt:.1f}s): {answer[:130]}")

    print(f"\n  ── 本对话统计：{stats['turns']} 轮 | 澄清 {stats['clarify']} 次 | "
          f"多候选 {stats['multi_candidate']} 次")
    return stats


def _get_resolve_llm():
    from main import _get_resolve_llm as _g
    return _g()


async def main():
    if not settings.RESOLVE_ENABLED:
        print("!! RESOLVE_ENABLED=false，入口消解不会执行（覆盖不到）")
    await init_checkpointer()
    total = {"turns": 0, "clarify": 0, "multi_candidate": 0}
    try:
        for title, turns in CONVERSATIONS.items():
            s = await run_conversation(title, turns)
            for k in total:
                total[k] += s[k]
    finally:
        await close_checkpointer()
    print(f"\n{'=' * 100}")
    print(f"总计：{total['turns']} 轮 | 澄清 {total['clarify']} 次 | "
          f"多候选 {total['multi_candidate']} 次")


if __name__ == "__main__":
    asyncio.run(main())
