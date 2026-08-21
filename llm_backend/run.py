import sys
import uvicorn
from app.core.logger import get_logger
import os
from pathlib import Path

logger = get_logger(service="server")

if sys.platform == "win32":
    # psycopg 异步驱动不支持 Windows 默认的 ProactorEventLoop。
    # uvicorn 在创建事件循环时（auto 模式）会延迟导入该工厂函数，因此在此处
    # 于 uvicorn.run 之前打补丁，替换为 SelectorEventLoop（Docker/Linux 无此问题）
    import uvicorn.loops.asyncio as _uv_asyncio_loop
    import asyncio as _asyncio

    def _selector_loop_factory(use_subprocess: bool = False):
        return _asyncio.SelectorEventLoop

    _uv_asyncio_loop.asyncio_loop_factory = _selector_loop_factory

def start_server():
    # 确保工作目录正确
    os.chdir(Path(__file__).parent)
    
    logger.info("Starting server...")
    logger.info("Working directory: {}", os.getcwd())
    
    uvicorn.run(
        "main:app",        # 使用模块路径
        host="0.0.0.0",
        port=8000,
        access_log=False,
        log_level="error",
        reload=True        #开发模式下启用热重载
    )
 # http://127.0.0.1:8000/
if __name__ == "__main__":
    start_server() 