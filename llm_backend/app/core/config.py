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
    
    # Database settings
    DB_HOST: str
    DB_PORT: int
    DB_USER: str
    DB_PASSWORD: str
    DB_NAME: str
    
    # Neo4j settings
    NEO4J_URL: str = "bolt://localhost:7687"
    NEO4J_USERNAME: str = "neo4j"
    NEO4J_PASSWORD: str = "password"
    NEO4J_DATABASE: str = "neo4j"
    
    # Logging settings
    LOG_LEVEL: str = "INFO"                                 # 日志级别: DEBUG / INFO / WARNING / ERROR

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
    
    # Embedding settings
    EMBEDDING_TYPE: EmbeddingServiceType = EmbeddingServiceType.OLLAMA  # 嵌入服务: local / ollama / qwen
    EMBEDDING_MODEL: str = "bge-m3"                 # 本地/通用 Embedding 模型名
    EMBEDDING_DIMENSION: int = 1024                 # 向量维度（bge-m3=1024）
    EMBEDDING_THRESHOLD: float = 0.90               # 语义相似度阈值

    # Qwen Embedding API（OpenAI-compatible / DashScope）
    QWEN_EMBEDDING_API_KEY: str = ""
    QWEN_EMBEDDING_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    QWEN_EMBEDDING_MODEL: str = "text-embedding-v4"
    
    # Vector DB settings (pgvector)
    VECTOR_TABLE_NAME: str = "document_chunks"           # pgvector 文档块表名

    # Relevance grading settings
    RELEVANCE_GRADING_ENABLED: bool = True                  # 是否启用相关性评分

    # Chunking settings
    CHUNK_SIZE: int = 500                                   # 文本分块大小
    CHUNK_OVERLAP: int = 50                                 # 分块重叠大小

    # RAG retrieval settings
    VECTOR_SEARCH_TOP_K: int = 10                           # 向量检索返回数
    HYBRID_RETRIEVAL_TOP_K: int = 5                         # 混合检索最终返回数
    HYBRID_RETRIEVAL_TOP_N: int = 20                        # 混合检索候选数

    # LLM temperature settings
    LLM_TEMPERATURE: float = 0.7                            # 通用 LLM 温度
    LLM_GRADER_TEMPERATURE: float = 0.0                     # 评分/评估 LLM 温度
    LLM_GENERATION_TEMPERATURE: float = 0.8                 # 测试数据生成温度

    # Streaming settings
    STREAM_DELAY: float = 0.05                              # 流式响应延迟（秒）

    # Reranker settings
    RERANKER_MODEL: str = "BAAI/bge-reranker-v2-m3"        # 重排序模型
    RERANKER_TOP_K: int = 5                                  # 重排序返回数
    RERANKER_MAX_LENGTH: int = 512                           # 重排序最大长度

    # Hybrid retrieval settings
    RRF_FUSION_K: int = 60                                   # RRF 融合参数
    HYBRID_EMBEDDING_MODEL: str = "bge-m3"  # 混合检索向量模型

    # Predefined Cypher settings
    PREDEFINED_CYPHER_SIMILARITY_THRESHOLD: float = 0.5     # 预定义 Cypher 向量匹配阈值

    # Memory & Token settings
    MEMORY_CACHE_TTL: int = 86400                            # 摘要缓存 TTL（秒）
    CONVERSATION_HISTORY_MAX_RECORDS: int = 5                # 对话历史最大记录数

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
    
    @property
    def NEO4J_CONN_URL(self) -> str:
        """构建Neo4j连接URL"""
        return f"{self.NEO4J_URL}"
    
    class Config:
        env_file = str(ENV_FILE)  # 使用绝对路径
        env_file_encoding = "utf-8"
        case_sensitive = True

settings = Settings() 