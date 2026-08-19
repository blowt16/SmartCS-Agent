from loguru import logger
import logging
import sys
from pathlib import Path
import os
import contextvars

# ============================================================================
# 日志目录：基于项目根目录的绝对路径，不受 CWD 影响
# logger.py 位于 llm_backend/app/core/logger.py → parent×4 = 项目根目录
# ============================================================================
LOG_DIR = Path(__file__).parent.parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ============================================================================
# 动态日志级别：通过环境变量 LOG_LEVEL 控制，默认 INFO
# 用法：LOG_LEVEL=DEBUG python run.py
# ============================================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ============================================================================
# 请求追踪 ID：基于 contextvars 实现全链路自动注入
# 所有模块的日志无需任何修改，自动携带当前请求的 request_id
# ============================================================================
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

# 移除默认的控制台输出
logger.remove()

# --------------------------------------------------------------------------
# 控制台输出（人类可读，带颜色）
# --------------------------------------------------------------------------
logger.add(
    sys.stdout,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<yellow>{extra[request_id]}</yellow> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    ),
    level=LOG_LEVEL,
)

# --------------------------------------------------------------------------
# 文件输出（JSON 结构化，便于 ELK / Loki 等日志系统索引）
# --------------------------------------------------------------------------
logger.add(
    LOG_DIR / "app.log",
    rotation="500 MB",
    retention="10 days",
    compression="zip",
    format=(
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <8} | "
        "{extra[request_id]} | "
        "{name}:{function}:{line} - "
        "{message}"
    ),
    level=LOG_LEVEL,
    encoding="utf-8",
    serialize=True,  # JSON 结构化输出，便于机器解析
)

# --------------------------------------------------------------------------
# 错误日志（人类可读格式，便于快速排查，仅记录 ERROR）
# --------------------------------------------------------------------------
logger.add(
    LOG_DIR / "error.log",
    rotation="100 MB",
    retention="30 days",
    compression="zip",
    format=(
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <8} | "
        "{extra[request_id]} | "
        "{name}:{function}:{line} - "
        "{message}"
    ),
    level="ERROR",
    encoding="utf-8",
)

# ============================================================================
# Patcher：自动注入 request_id 到每条日志记录的 extra 字段
# 关键：patcher 在日志写入时执行（而非 logger 创建时），因此 contextvars
#       中当前请求的 request_id 会被正确读取
# ============================================================================
logger.configure(patcher=lambda record: record["extra"].update(
    request_id=request_id_var.get()
))


def get_logger(service: str):
    """获取带有服务名称的 logger"""
    return logger.bind(service=service)


def log_structured(event_type: str, data: dict):
    """结构化日志记录 — 自动附加 request_id 和服务信息"""
    logger.bind(event_type=event_type).info(data)


# ============================================================================
# stdlib logging → loguru 桥接
# uvicorn / SQLAlchemy 等第三方库使用标准 logging 模块输出日志，
# 若不桥接会绕过统一管理器直接写 stderr。InterceptHandler 把全部
# stdlib 日志转发到 loguru，统一进入控制台 / app.log / error.log。
# ============================================================================
class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _uv_logger = logging.getLogger(_name)
    _uv_logger.handlers = [InterceptHandler()]
    _uv_logger.propagate = False
