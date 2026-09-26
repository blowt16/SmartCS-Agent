from dataclasses import dataclass, field
from typing import Annotated, Literal, TypedDict, List
from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages


class Router(TypedDict):
    """Classify user query: scenario + aftersale sub-scenario + risk."""
    logic: str                      # 分类理由（供回答生成参考）
    type: Literal[
        "presale",                  # 售前：商品咨询/参数/价格活动/推荐导购
        "aftersale",                # 售后：退货退款/物流异常/订单查询
        "complaint",                # 投诉安抚：情绪不满/投诉（情绪主导）
        "general",                  # 闲聊（原 general-query）
        "image",                    # 图片（原 image-query）
        "clarify",                  # 意图不明：语义上无法确定用户问什么 → 澄清节点
    ]
    # 售后二级场景（2026-09-26 恢复该维度，推翻 2026-08-27 决策 #13）——
    # 原决策"子场景判断需订单/历史等上下文"混淆了识别与执行两个环节：
    # 识别层只回答"用户说的是哪类诉求"（纯语义分类，对话文本足够）；
    # "查订单/算差价/发起退货"等执行动作仍归售后 Agent。
    # 本次只落 state + 日志 + 评测，不参与路由（route_query 未改）。
    sub_type: Literal[
        "logistics_query",          # 物流查询
        "return_refund",            # 退货退款
        "exchange",                 # 换货
        "reship",                   # 补发
        "order_query",              # 订单查询
        "other",                    # 其他/兜底：确定是售后但归不出细类
        "none",                     # 非 aftersale 时必须为 none
    ]
    risk: Literal[
        "none", "violation", "high_risk",
    ]                               # violation=违规咨询拦截；high_risk=高风险操作转人工
    source: Literal["rule", "llm"]  # 判定来源：规则层短路 / LLM 识别（供日志与评测统计）
    # 模型自评置信度 [0,1]。**只记录、不参与路由**（2026-09-26 实测决策）：
    # 81 条样本中位数 0.92、仅 3 条低于 0.75，且 4 条判错全落在高置信区
    # （模型"自信地判错"），阈值 0.75 需误伤 2 条判对的才拦下 1 条判错的，净收益为负。
    # 故先按 source=llm 攒真实流量分布，攒够再定阈值。
    # 取值约定：规则层/经营范围预检短路=1.0（确定性命中）；结构化输出失败降级=0.0。
    confidence: float

# @dataclass(kw_only=True)： 强制要求数据类中的所有字段必须以关键字参数的形式提供。即不能以位置参数的方式传递。
@dataclass(kw_only=True)
class InputState:
    """Represents the input state for the agent.

    This class defines the structure of the input state, which includes
    the messages exchanged between the user and the agent. 
    """

    messages: Annotated[list[AnyMessage], add_messages]
    
    """Messages track the primary execution state of the agent.

    Typically accumulates a pattern of Human/AI/Human/AI messages; if
    you were to combine this template with a tool-calling ReAct agent pattern,
    it may look like this:

    1. HumanMessage - user input
    2. AIMessage with .tool_calls - agent picking tool(s) to use to collect
         information
    3. ToolMessage(s) - the responses (or errors) from the executed tools
    
        (... repeat steps 2 and 3 as needed ...)
    4. AIMessage without .tool_calls - agent responding in unstructured
        format to the user.

    5. HumanMessage - user responds with the next conversational turn.

        (... repeat steps 2-5 as needed ... )
    

    Merges two lists of messages, updating existing messages by ID.

    By default, this ensures the state is "append-only", unless the
    new message has the same ID as an existing message.
    

    Returns:
        A new list of messages with the messages from `right` merged into `left`.
        If a message in `right` has the same ID as a message in `left`, the
        message from `right` will replace the message from `left`."""
    

# @dataclass(kw_only=True)： 强制要求数据类中的所有字段必须以关键字参数的形式提供。即不能以位置参数的方式传递。
@dataclass(kw_only=True)
class AgentState(InputState):
    """State of the retrieval graph / agent."""
    router: Router = field(default_factory=lambda: Router(
        type="general", sub_type="none", risk="none", logic="",
        source="llm", confidence=0.0))
    """The router's classification of the user's query."""
    steps: list[str] = field(default_factory=list)
    """Populated by the retriever. This is a list of documents that the agent can reference."""
    question: str = field(default_factory=str)
    answer: str = field(default_factory=str)
