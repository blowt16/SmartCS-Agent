from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.logger import log_structured, request_id_var
import uuid
import time


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 生成唯一请求追踪 ID
        request_id = str(uuid.uuid4())
        request_id_var.set(request_id)
        request.state.request_id = request_id

        start_time = time.time()

        response = await call_next(request)

        # 计算处理时间
        process_time = time.time() - start_time

        # 返回 X-Request-ID 给客户端，便于前后端关联
        response.headers["X-Request-ID"] = request_id

        # 结构化日志记录
        log_structured("http_request", {
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round(process_time * 1000, 2),
            "client_ip": request.client.host if request.client else "-",
        })

        return response
