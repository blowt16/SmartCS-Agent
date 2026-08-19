import sys
import os
from pathlib import Path

# 将项目根目录添加到 Python 路径
root_dir = Path(__file__).parent.parent.parent
sys.path.append(str(root_dir))


from app.lg_agent.lg_states import InputState
from app.lg_agent.utils import new_uuid
from app.lg_agent.lg_builder import graph, init_checkpointer, close_checkpointer
from app.core.logger import get_logger
import asyncio
import builtins

logger = get_logger(service="lg_agent_cli")

thread = {"configurable": {"thread_id": new_uuid()}}


async def process_query(query):
    logger.info("CLI 查询开始: {}", query[:100])
    try:
        inputState = InputState(messages=query)

        async for c, metadata in graph.astream(input=inputState, stream_mode="messages", config=thread):
            if c.content and "research_plan" not in metadata.get("tags", []):
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
