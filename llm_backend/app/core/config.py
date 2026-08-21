from pydantic_settings import BaseSettings
from enum import Enum
from pathlib import Path

# 获取项目根目录（llm_backend 的父目录）
ROOT_DIR = Path(__file__).parent.parent.parent.parent
ENV_FILE = ROOT_DIR / ".env"

class ServiceType(str, Enum):
    DEEPSEEK = "deepseek"
    OLLAMA = "ollama"


class EmbeddingServiceType(str, Enum):
    LOCAL = "local"      # 本地 SentenceTransformer（离线）
    OLLAMA = "ollama"    # Ollama HTTP API
    QWEN = "qwen"        # 通义千问 API（OpenAI-compatible）

class Settings(BaseSettings):
    # Deepseek settings
    DEEPSEEK_API_KEY: str
    DEEPSEEK_BASE_URL: str
    DEEPSEEK_MODEL: str
    
    # Vision Model settings (独立配置)
    VISION_API_KEY: str
    VISION_BASE_URL: str
    VISION_MODEL: str
    VISION_MAX_TOKENS: int = 4000                           # 视觉模型最大输出 token
    VISION_TIMEOUT: int = 60                                # 视觉模型请求超时（秒）
    
    # Ollama settings
    OLLAMA_BASE_URL: str
    OLLAMA_CHAT_MODEL: str
    OLLAMA_REASON_MODEL: str
    OLLAMA_EMBEDDING_MODEL: str
    OLLAMA_AGENT_MODEL: str
    # Service selection
    CHAT_SERVICE: ServiceType = ServiceType.DEEPSEEK
    REASON_SERVICE: ServiceType = ServiceType.OLLAMA
    AGENT_SERVICE: ServiceType = ServiceType.DEEPSEEK
    
    # Search settings
    SERPAPI_KEY: str
    SERPAPI_BASE_URL: str = "https://serpapi.com/search"   # SerpAPI 端点
    SEARCH_RESULT_COUNT: int = 3
    SEARCH_LANGUAGE: str = "zh-CN"                          # 搜索语言
    SEARCH_REGION: str = "cn"                               # 搜索地区
    SEARCH_TIMEOUT: int = 15                                # 联网搜索超时（秒）
    
    # Database settings
    DB_HOST: str
    DB_PORT: int
    DB_USER: str
    DB_PASSWORD: str
    DB_NAME: str

    # JWT settings
    SECRET_KEY: str = "your-secret-key"  # 在生产环境中使用安全的密钥
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    
    # Redis settings
    REDIS_HOST: str
    REDIS_PORT: int
    REDIS_DB: int = 0
    REDIS_PASSWORD: str = ""
    REDIS_CACHE_EXPIRE: int = 3600
    REDIS_CACHE_THRESHOLD: float = 0.8
    REDIS_CACHE_PREFIX: str = "cache"                       # 缓存键前缀
    REDIS_CACHE_MAX_SIZE: int = 1000                        # 最大缓存条目数
    REDIS_CACHE_CLEANUP_INTERVAL: int = 3600                # 缓存清理间隔（秒）

    # Semantic cache resolve settings（语义缓存分级指代消解）
    RESOLVE_ENABLED: bool = True                            # 总开关，false 时完全退化为现有行为（一键回滚）
    RESOLVE_MAX_TURNS: int = 5                              # 消解时参考的最大对话轮数（1轮=1用户+1助手）
    RESOLVE_LLM_TEMPERATURE: float = 0.0                   # 消解 LLM 温度（必须为 0，保证同输入同输出）
    RESOLVE_TIMEOUT_MS: int = 2000                          # 消解调用超时（毫秒）——声明性默认，实际值统一由 .env 的 RESOLVE_TIMEOUT_MS 配置（DeepSeek 单次调用约 2s，建议 ≥15000）
    RESOLVE_SKIP_FILLER: bool = True                        # 是否跳过纯语气词（不查不写，避免缓存污染）
    
    # Embedding settings
    EMBEDDING_TYPE: EmbeddingServiceType = EmbeddingServiceType.OLLAMA  # 嵌入服务: local / ollama / qwen
    EMBEDDING_MODEL: str = "text-embedding-v4"      # 本地模型名（TYPE=local 时改回 bge-m3；qwen 类型走 QWEN_EMBEDDING_MODEL）
    EMBEDDING_DIMENSION: int = 1024                 # 向量维度（text-embedding-v4 默认 1024）
    EMBEDDING_TIMEOUT: int = 30                     # Embedding API 请求超时（秒）

    # Qwen Embedding API（OpenAI-compatible / DashScope）
    QWEN_EMBEDDING_API_KEY: str = ""
    QWEN_EMBEDDING_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    QWEN_EMBEDDING_MODEL: str = "text-embedding-v4"
    
    # Vector DB settings (pgvector)
    VECTOR_TABLE_NAME: str = "document_chunks"           # pgvector 文档块表名

    # Chunking settings
    CHUNK_SIZE: int = 500                                   # 文本分块大小
    CHUNK_OVERLAP: int = 50                                 # 分块重叠大小

    # RAG retrieval settings
    BM25_TOP_K: int = 20                                    # BM25 检索候选数
    HYBRID_RETRIEVAL_TOP_K: int = 5                         # 混合检索最终返回数（精排关闭时兜底）
    HYBRID_RETRIEVAL_TOP_N: int = 20                        # 向量检索候选数（混合检索候选）
    RAG_TIMEOUT_SECONDS: int = 30                           # RAG 子图超时（秒），超时降级兜底回答

    # LLM temperature settings
    LLM_TEMPERATURE: float = 0.7                            # 通用 LLM 温度
    ROUTER_TEMPERATURE: float = 0.0                         # 意图识别/路由温度（分类任务，低温保证确定性）

    # Streaming settings
    STREAM_DELAY: float = 0.05                              # 流式响应延迟（秒）

    # Reranker settings
    RERANKER_ENABLED: bool = True                            # 精排开关（替代 LLM 相关性评分）
    RERANKER_MODEL: str = "BAAI/bge-reranker-v2-m3"        # 重排序模型
    RERANKER_TOP_K: int = 5                                  # 重排序返回数
    RERANKER_MAX_LENGTH: int = 512                           # 重排序最大长度
    RERANKER_DEVICE: str = "auto"                            # 精排设备: auto / cuda / cpu
    RERANKER_BATCH_SIZE: int = 8                             # 精排批处理大小
    RERANKER_HALF_PRECISION: bool = True                     # fp16 半精度（6GB 显存必需）

    # Hybrid retrieval settings
    RRF_FUSION_K: int = 60                                   # RRF 融合平滑常数
    RRF_TOP_K: int = 20                                      # RRF 融合后输出候选数（精排输入）

    # Memory & Token settings
    MEMORY_CACHE_TTL: int = 86400                            # 摘要缓存 TTL（秒）

    @property
    def DATABASE_URL(self) -> str:
        """SQLAlchemy 异步连接串（PostgreSQL + psycopg）"""
        return f"postgresql+psycopg://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @property
    def POSTGRES_DSN(self) -> str:
        """psycopg 原生连接串（供 psycopg_pool / LangGraph PostgresSaver 使用）"""
        return f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @property
    def REDIS_URL(self) -> str:
        """构建Redis URL"""
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    class Config:
        env_file = str(ENV_FILE)  # 使用绝对路径
        env_file_encoding = "utf-8"
        case_sensitive = True

settings = Settings() 