"""RAGAS 评测的 LLM / embeddings 构造。

三类模型分开构造，评测配置与生产链路隔离（spec §5 隔离原则）：
    - judge LLM / judge embeddings：走 RAGAS_* 独立配置（key 必填，不回退、不混用生产配置）
    - 被测 agent LLM：与生产构造完全一致（lg_builder.create_research_plan 同构），
      评测测的就是生产链路本身

注意：禁止 import llm_backend/main.py（module-level 创建 FastAPI app），
评测所有复用点均从 app.services.* / app.lg_agent.* 导入。

embedding provider 说明：ragas 0.4.x 弃用 LangchainEmbeddingsWrapper（DeprecationWarning），
官方推荐现代 provider（OpenAIEmbeddings(client=openai_client)），且实测 langchain
OpenAIEmbeddings 发往百炼的请求体 400（'contents is neither str nor list of str'），
统一使用 ragas 原生 OpenAIEmbeddings + openai SDK client（与生产 embedding_provider 同格式）。
"""
from openai import OpenAI
from ragas.embeddings import OpenAIEmbeddings

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(service="evaluation.llm_factory")


def build_judge_llm() -> object:
    """构造 ragas 评判 LLM（阿里云百炼 DashScope，OpenAI 兼容接口）。

    ragas 0.4.x 现代 provider（llm_factory，Instructor 结构化输出）——官方推荐路径：
    LangchainLLMWrapper 已弃用且在无 loop 线程（to_thread 合成）里触发 async 调用
    会炸 'Event loop is closed'（实测），现代 provider 内部自管理 sync/async。
    独立 RAGAS_* 配置；temperature=0 保证同题同分（评测可复现）。
    缺失 key 直接报错，绝不静默回落生产配置。
    """
    from openai import OpenAI
    from ragas.llms import llm_factory

    if not settings.RAGAS_JUDGE_API_KEY:
        raise RuntimeError(
            "缺少评测 judge 配置：请在 .env 设置 RAGAS_JUDGE_API_KEY（评测专用百炼 key）"
        )
    client = OpenAI(
        api_key=settings.RAGAS_JUDGE_API_KEY,
        base_url=settings.RAGAS_JUDGE_BASE_URL,
        timeout=settings.RAGAS_JUDGE_TIMEOUT,
        max_retries=2,
    )
    return llm_factory(
        settings.RAGAS_JUDGE_MODEL,
        provider="openai",
        client=client,
        temperature=settings.RAGAS_JUDGE_TEMPERATURE,
        # instructor 结构化输出（场景/评测打分 JSON）较长的：无 max_tokens 时
        # DeepSeek 默认上限截断 → IncompleteOutputException（实测）
        max_tokens=8192,
    )


def build_judge_embeddings() -> object:
    """构造评测 embedding（ragas 现代 provider，与检索 embedding 分离）。

    RAGAS_EMBEDDING_API_KEY 必填；base_url/model 为空时走评测命名空间内的默认值
    （DashScope compatible-mode / text-embedding-v4）。不回落生产 QWEN_EMBEDDING_*——
    生产 embedding 变更不应静默影响评测复现性。
    """
    if not settings.RAGAS_EMBEDDING_API_KEY:
        raise RuntimeError(
            "缺少评测 embedding 配置：请在 .env 设置 RAGAS_EMBEDDING_API_KEY（评测专用百炼 key）"
        )
    client = OpenAI(
        api_key=settings.RAGAS_EMBEDDING_API_KEY,
        base_url=(
            settings.RAGAS_EMBEDDING_BASE_URL
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        timeout=settings.RAGAS_JUDGE_TIMEOUT,
    )
    return DualInterfaceEmbeddings(
        client=client,
        model=settings.RAGAS_EMBEDDING_MODEL or "text-embedding-v4",
    )


class DualInterfaceEmbeddings(OpenAIEmbeddings):
    """双接口 embedding 适配（ragas 0.4.3 指标实现新旧并存，实测踩坑）。

    现代指标（collections 版）走 embed_text/aembed_texts（基类自带批量包装）；
    旧版指标（metrics/_answer_relevance.py:100 等）直接调 embed_query/embed_documents——
    只实现现代接口时旧指标全部 NaN（AttributeError: no attribute 'embed_query'，实测）。
    本类补全 legacy 委托方法，两套指标共用同一实例。
    """

    def embed_query(self, text: str) -> list:
        return self.embed_text(text)

    async def aembed_query(self, text: str) -> list:
        return await self.aembed_text(text)

    def embed_documents(self, texts: list) -> list:
        return [self.embed_text(t) for t in texts]

    async def aembed_documents(self, texts: list) -> list:
        return [await self.aembed_text(t) for t in texts]


def build_agent_llm() -> object:
    """构造被测 agent LLM——与生产构造完全一致（lg_builder.py:416-419 同构）。

    AGENT_SERVICE=deepseek → ChatDeepSeek；否则 ChatOllama（本地）。
    被测方配置取自生产 settings（属"被测对象"而非"评测配置"，见 spec §5.2 隔离边界）。
    """
    from langchain_deepseek import ChatDeepSeek
    from langchain_ollama import ChatOllama

    if settings.AGENT_SERVICE.value == "deepseek":
        return ChatDeepSeek(
            api_key=settings.DEEPSEEK_API_KEY,
            model_name=settings.DEEPSEEK_MODEL,
            temperature=settings.LLM_TEMPERATURE,
            tags=["ragas_eval"],
            extra_body={"thinking": {"type": "disabled"}},
        )
    return ChatOllama(
        model=settings.OLLAMA_AGENT_MODEL,
        base_url=settings.OLLAMA_BASE_URL,
        temperature=settings.LLM_TEMPERATURE,
        tags=["ragas_eval"],
        extra_body={"thinking": {"type": "disabled"}},
    )
