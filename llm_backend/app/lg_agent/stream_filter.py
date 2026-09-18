"""LangGraph messages 流 → SSE 文本的出口闸门（内部推理不外泄 + 容器节点不重复）。

背景（2026-09-18 流式整改）：
`stream_mode="messages"` 无差别广播图内所有 chat model 的 token，框架不区分"给用户看的答复"
与"内部管道推理"。原实现用 langchain run tag 作黑名单（售前子图共享实例上的
`tags=["research_plan"]`，已随本次整改移除），但该 tag 打在**共享模型实例**上——planner 与
summarize 共用、分不出内部与外发，把唯一对用户可见的 summarize 一并挡掉——实测售前 query
的 285 个分片仅 1 个放行，链路退化为"十几秒后整段蹦出"。

现改为按 `metadata["langgraph_node"]` 精确判定（节点名实测稳定可取值）：
- `INTERNAL_NODES`（路由/拆解节点）：结构化输出的内部推理，不外泄；
- `RESEARCH_CONTAINER_NODE`：售前容器节点，其 messages 是 summarize 答复的整段回填——
  仅在"本轮尚无 token 流出"时放行（超时/异常降级话术），否则与流式内容重复。

两个消费通道共用本模块（HTTP `/api/langgraph/query` 与 `app/lg_agent/main.py` CLI），
避免同源过滤逻辑再次分叉漂移。
"""

# 内部推理节点：结构化输出只产 tool-call 分片（实测 content 恒为空），显式拦一道防漏
INTERNAL_NODES = frozenset({"analyze_and_route_query", "planner"})

# 售前容器节点（lg_builder.create_research_plan）：回填整段答案，需与流式内容去重
RESEARCH_CONTAINER_NODE = "create_research_plan"


class StreamChunkFilter:
    """有状态分片过滤器（每请求一个实例）。

    `select()` 返回应发给前端的文本；返回 None 表示该分片丢弃。
    """

    def __init__(self) -> None:
        self.streamed = False  # 本轮是否已有 token 流出（容器节点去重依据）

    def select(self, chunk, metadata) -> str | None:
        content = getattr(chunk, "content", "") or ""
        # 无正文（结构化输出的空壳分片）或工具调用参数：都不是给用户看的文本
        if not content or chunk.additional_kwargs.get("tool_calls"):
            return None

        node = (metadata or {}).get("langgraph_node")
        if node in INTERNAL_NODES:
            return None
        if node == RESEARCH_CONTAINER_NODE and self.streamed:
            return None

        self.streamed = True
        return content
