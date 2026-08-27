from dataclasses import dataclass, field
from typing import Annotated, Literal, TypedDict, List
from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages


class Router(TypedDict):
    """Classify user query: scenario + risk."""
    logic: str                      # 分类理由（供回答生成参考）
    type: Literal[
        "presale",                  # 售前：商品咨询/参数/价格活动/推荐导购
        "aftersale",                # 售后：退货退款/物流异常/订单查询
        "complaint",                # 投诉安抚：情绪不满/投诉（情绪主导）
        "general",                  # 闲聊（原 general-query）
        "image",                    # 图片（原 image-query）
        "clarify",                  # 意图不明：语义上无法确定用户问什么 → 澄清节点
    ]
    # 售后子场景（return_refund/logistics/order_query）不再由识别层判断——
    # 判断需订单/历史等上下文，下沉到售后 Agent 工作流骨架第一步（简化设计，2026-08-27）
    risk: Literal[
        "none", "violation", "high_risk",
    ]                               # violation=违规咨询拦截；high_risk=高风险操作转人工

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
    router: Router = field(default_factory=lambda: Router(type="general", risk="none", logic=""))
    """The router's classification of the user's query."""
    steps: list[str] = field(default_factory=list)
    """Populated by the retriever. This is a list of documents that the agent can reference."""
    question: str = field(default_factory=str)
    answer: str = field(default_factory=str)
