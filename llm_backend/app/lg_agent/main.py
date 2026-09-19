import sys
import os
from pathlib import Path

# 将项目根目录添加到 Python 路径
root_dir = Path(__file__).parent.parent.parent
sys.path.append(str(root_dir))


from app.lg_agent.lg_states import InputState
from app.lg_agent.utils import new_uuid
from app.lg_agent.lg_builder import graph, init_checkpointer, close_checkpointer
from app.lg_agent.stream_filter import StreamChunkFilter
from app.core.logger import get_logger, log_round_start, log_round_end
import asyncio
import builtins

logger = get_logger(service="lg_agent_cli")

thread = {"configurable": {"thread_id": new_uuid()}}


async def process_query(query):
    # 轮次边界与耗时用与服务入口同一套 helper（core/logger.py），避免两处格式漂移
    t0 = log_round_start(query, channel="cli")
    n_chunks = 0
    try:
        inputState = InputState(messages=query)

        # 出口闸门与 HTTP 入口共用，防同源过滤逻辑再次分叉（app/lg_agent/stream_filter.py）
        chunk_filter = StreamChunkFilter()
        async for c, metadata in graph.astream(input=inputState, stream_mode="messages", config=thread):
            text = chunk_filter.select(c, metadata)
            if text:
                n_chunks += 1
                print(text, end="", flush=True)
    except Exception as e:
        logger.exception("CLI 查询处理失败: {}", str(e))
        log_round_end(t0, "异常")
        raise

    log_round_end(t0, f"完成（{n_chunks} 分片）")


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
