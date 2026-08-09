FROM python:3.11-slim

WORKDIR /app

# 系统依赖（sentence-transformers 需要 gcc 编译）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# 先复制 requirements，利用 Docker 缓存层
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY llm_backend/ ./llm_backend/

# 创建必要目录
RUN mkdir -p /app/llm_backend/uploads /app/llm_backend/logs /app/llm_backend/static

WORKDIR /app/llm_backend

EXPOSE 8000

# 启动前先初始化数据库表，再启动服务
CMD ["sh", "-c", "python -m scripts.init_db && uvicorn main:app --host 0.0.0.0 --port 8000"]
