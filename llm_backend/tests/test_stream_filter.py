"""出口流式闸门测试(纯函数,不连库)——2026-09-18 流式整改。

背景:原过滤 `"research_plan" not in metadata["tags"]` 打在售前子图的**共享模型实例**上
(planner 与 summarize 共用),把唯一给用户看的 summarize 一并挡掉,售前退化为整段一次性
返回(实测:285 chunk 仅 1 个放行)。改为按 langgraph_node 精确判定。

覆盖:内部节点(router/planner)不外泄 / 用户可见 LLM 放行 / 售前容器节点去重与超时兜底 /
空内容与 tool_call 分片丢弃 / 真实观测序列的整段回归。
"""
from app.lg_agent.stream_filter import StreamChunkFilter


class _Chunk:
    """duck-typing 替代 langchain 消息:过滤器只读 content / additional_kwargs。"""

    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.additional_kwargs = {"tool_calls": tool_calls} if tool_calls else {}


def _meta(node):
    return {"langgraph_node": node, "tags": []}


def _feed(node, content="", tool_calls=None, filt=None):
    """跑一个分片,返回 (放行文本 | None, 过滤器)。"""
    filt = filt or StreamChunkFilter()
    return filt.select(_Chunk(content, tool_calls), _meta(node)), filt


# ==================== 内部推理不外泄 ====================

def test_router_node_not_leaked():
    """analyze_and_route_query 的结构化输出属内部推理,即便有 content 也不外泄。"""
    out, _ = _feed("analyze_and_route_query", '{"type": "presale"}')
    assert out is None


def test_planner_node_not_leaked():
    """planner 拆解任务同为内部推理(旧实现靠 tag 挡,现按节点挡)。"""
    out, _ = _feed("planner", '{"tasks": [...]}')
    assert out is None


# ==================== 用户可见输出放行 ====================

def test_general_query_streams():
    out, filt = _feed("respond_to_general_query", "亲~您好")
    assert out == "亲~您好"
    assert filt.streamed is True


def test_clarify_and_image_stream():
    assert _feed("clarify_node", "请问您是想问…")[0] == "请问您是想问…"
    assert _feed("create_image_query", "图片里是…")[0] == "图片里是…"


def test_static_placeholder_nodes_pass():
    """risk/转人工/售后占位是节点直接返回的静态话术,必须放行(否则用户看不到)。"""
    for node in ("risk_intercept", "transfer_human", "aftersale_placeholder", "complaint_placeholder"):
        assert _feed(node, "抱歉,无法为您处理")[0] == "抱歉,无法为您处理"


# ==================== 售前:summarize 流式 + 容器节点去重 ====================

def test_summarize_streams():
    """售前唯一对用户可见的 LLM——旧实现被 tag 黑名单误伤,现须放行。"""
    out, _ = _feed("summarize", "亲～您好呀")
    assert out == "亲～您好呀"


def test_research_container_skipped_when_already_streamed():
    """容器节点回填的整段答复与 summarize 流式内容同源,重复放行会让前端出两遍。"""
    filt = StreamChunkFilter()
    _feed("summarize", "亲～您好呀", filt=filt)
    out, _ = _feed("create_research_plan", "亲～您好呀", filt=filt)
    assert out is None


def test_research_container_passes_when_nothing_streamed():
    """超时/失败降级时 summarize 无 token 流出,容器话术是唯一出口,必须放行。"""
    out, _ = _feed("create_research_plan", "抱歉，系统处理超时，请稍后再试。")
    assert out == "抱歉，系统处理超时，请稍后再试。"


# ==================== 分片级丢弃规则 ====================

def test_empty_content_skipped():
    assert _feed("summarize", "")[0] is None


def test_tool_call_chunk_skipped():
    """带 tool_calls 的分片是工具调用参数,不是给用户看的文本。"""
    assert _feed("respond_to_general_query", "", tool_calls=[{"id": "call_1"}])[0] is None


def test_missing_metadata_node_tolerated():
    """metadata 缺 langgraph_node 时不得抛异常(框架版本差异兜底)。"""
    assert StreamChunkFilter().select(_Chunk("正文"), {}) == "正文"


# ==================== 真实观测序列回归(2026-09-18 presale 实测) ====================

def test_presale_answer_delivered_exactly_once():
    """复刻实测分布:router 64 空 + planner 38 空 + summarize 148 有字 + 容器 1 整段。

    旧实现仅放行最后 1 个整段(不流式);修复后应流出 148 片、且拼接结果与答案逐字相等
    ——不重复、不丢失。
    """
    answer = "亲～关于米家智能晾衣机2的价格，帮您查到以下信息：" * 5
    per_token = [answer[i:i + 1] for i in range(len(answer))]

    filt = StreamChunkFilter()
    out = []
    for _ in range(64):                                   # router:结构化输出,无 content
        out.append(filt.select(_Chunk(""), _meta("analyze_and_route_query")))
    for _ in range(38):                                   # planner:同上
        out.append(filt.select(_Chunk(""), _meta("planner")))
    for t in per_token:                                   # summarize:逐 token 流式
        out.append(filt.select(_Chunk(t), _meta("summarize")))
    out.append(filt.select(_Chunk(answer), _meta("create_research_plan")))  # 容器整段回填

    passed = [c for c in out if c]
    assert len(passed) == len(per_token)                  # 逐 token 流式(不再整段)
    assert "".join(passed) == answer                      # 逐字相等:不重复不丢失
