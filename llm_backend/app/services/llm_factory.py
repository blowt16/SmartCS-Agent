from typing import Union
from app.core.config import settings, ServiceType
from app.services.deepseek_service import DeepseekService
from app.services.ollama_service import OllamaService
class LLMFactory:
    @staticmethod
    def create_chat_service():
        """创建聊天服务实例（消解专用 LLM 的唯一创建点：main 入口 / 评测入口）

        RESOLVE_MODEL 配置后（消解降档，空 = 沿用 CHAT_SERVICE 模型不变）：
        同 provider 换模型名——DeepseekService 显式传 model；Ollama 链路暂不支持降档
        （其生成模型由 OLLAMA_CHAT_MODEL 决定，需降档时配置该项）。
        """
        resolve_model = settings.RESOLVE_MODEL or None
        if settings.CHAT_SERVICE == ServiceType.DEEPSEEK:
            # 如果.env文件中CHAT_SERVICE设置为DEEPSEEK，则使用DeepseekService
            return DeepseekService(model=resolve_model)
        else:
            # 否则使用OllamaService
            return OllamaService()