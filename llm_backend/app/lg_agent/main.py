import sys
import os
from pathlib import Path

# 将项目根目录添加到 Python 路径
root_dir = Path(__file__).parent.parent.parent
sys.path.append(str(root_dir))


from app.lg_agent.lg_states import AgentState, InputState
from app.lg_agent.utils import new_uuid
from app.lg_agent.lg_builder import graph, init_checkpointer, close_checkpointer
from app.core.logger import get_logger
from langgraph.types import Command
import asyncio
import time
import builtins

logger = get_logger(service="lg_agent_cli")

thread = {"configurable": {"thread_id": new_uuid()}}


async def process_query(query):
    logger.info("CLI 查询开始: {}", query[:100])
    try:
        inputState = InputState(messages=query)

        async for c, metadata in graph.astream(input=inputState, stream_mode="messages", config=thread):
            # if c.additional_kwargs.get("tool_calls"):
            #     print(c.additional_kwargs.get("tool_calls")[0]["function"].get("arguments"), end="", flush=True)

            if c.content and "research_plan" not in metadata.get("tags", []):
                print(c.content, end="", flush=True)
            # if c.content:
            #     print(c.content, end="", flush=True)
        # async for c in graph.astream(input=inputState, stream_mode="values", config=thread):
        #     print(c, end="", flush=True)

        state = await graph.aget_state(thread)
        if len(state[-1]) > 0:
            if len(state[-1][0].interrupts) > 0:
                response = input('\n响应可能包含不确定信息。重试生成？如果是，按"y"：')
                if response.lower() == 'y':
                    async for c, metadata in graph.astream(Command(resume=response), stream_mode="messages", config=thread):
                        if c.additional_kwargs.get("tool_calls"):
                            print(c.additional_kwargs.get("tool_calls")[0]["function"].get("arguments"), end="")
                        if c.content:
                            time.sleep(0.05)
                            print(c.content, end="", flush=True)
    except Exception as e:
        logger.exception("CLI 查询处理失败: {}", str(e))
        raise

    logger.info("CLI 查询完成: {}", query[:100])


async def main():
    await init_checkpointer()
    try:
        input = builtins.input
        while True:
            query = input("> ")
            if query.strip().lower() == "q":
                print("Exiting...")
                break
            await process_query(query)
    finally:
        await close_checkpointer()


if __name__ == "__main__":
    asyncio.run(main())
