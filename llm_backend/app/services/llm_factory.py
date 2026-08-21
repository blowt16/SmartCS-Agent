from typing import Union
from app.core.config import settings, ServiceType
from app.services.deepseek_service import DeepseekService
from app.services.ollama_service import OllamaService
class LLMFactory:
    @staticmethod
    def create_chat_service():
        """创建聊天服务实例"""
        if settings.CHAT_SERVICE == ServiceType.DEEPSEEK:
            # 如果.env文件中CHAT_SERVICE设置为DEEPSEEK，则使用DeepseekService
            return DeepseekService()
        else:
            # 否则使用OllamaService
            return OllamaService()