"""基于词向量的查询匹配工具，用于将用户问题匹配到预定义的Cypher查询"""

import numpy as np
from typing import Dict, List, Tuple, Any, Optional
from sklearn.metrics.pairwise import cosine_similarity
from app.core.config import settings
from app.services.embedding_provider import get_embedding_provider
from app.core.logger import get_logger

logger = get_logger(service="predefined_cypher_utils")


class VectorQueryMatcher:
    """基于词向量的查询匹配器，用于将用户问题匹配到预定义的Cypher查询"""

    def __init__(
        self,
        predefined_cypher_dict: Dict[str, str],
        query_descriptions: Dict[str, str],
        similarity_threshold: float = None,
    ):
        if similarity_threshold is None:
            similarity_threshold = settings.PREDEFINED_CYPHER_SIMILARITY_THRESHOLD

        self.predefined_cypher_dict = predefined_cypher_dict
        self.query_descriptions = query_descriptions
        self.similarity_threshold = similarity_threshold
        self._provider = get_embedding_provider()

        # 预计算查询向量
        self.query_vectors = self._compute_query_vectors()

    def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        """使用统一 Embedding Provider 将文本转换为向量"""
        try:
            return self._provider.embed_sync(texts)
        except Exception as e:
            logger.error(f"生成embedding时出错: {str(e)}")
            return [[0.0] * self._provider.dimension] * len(texts)

    def _compute_query_vectors(self) -> Dict[str, np.ndarray]:
        """预计算所有预定义查询的向量表示"""
        query_texts = []
        query_keys = []

        for query_name, cypher in self.predefined_cypher_dict.items():
            description = self.query_descriptions.get(query_name, "")
            query_text = f"{query_name} {description}"
            query_texts.append(query_text)
            query_keys.append(query_name)

        vectors = self._embed_texts(query_texts)
        return {key: np.array(vector) for key, vector in zip(query_keys, vectors)}

    def match_query(self, user_question: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """将用户问题匹配到最相似的预定义查询"""
        question_vector = np.array(self._embed_texts([user_question])[0])

        similarities = []
        for query_name, query_vector in self.query_vectors.items():
            similarity = cosine_similarity([question_vector], [query_vector])[0][0]
            similarities.append((query_name, similarity))

        similarities.sort(key=lambda x: x[1], reverse=True)

        results = []
        for query_name, similarity in similarities[:top_k]:
            if similarity >= self.similarity_threshold:
                results.append({
                    "query_name": query_name,
                    "similarity": float(similarity),
                    "cypher": self.predefined_cypher_dict[query_name]
                })

        return results

    def extract_parameters(self, user_question: str, query_name: str, llm=None) -> Dict[str, str]:
        """从用户问题中提取参数"""
        if query_name not in self.predefined_cypher_dict:
            return {}

        cypher_template = self.predefined_cypher_dict[query_name]
        import re
        param_names = re.findall(r'\$(\w+)', cypher_template)

        if llm is not None:
            return self._extract_parameters_with_llm(user_question, param_names, query_name, llm)
        return self._extract_parameters_with_rules(user_question, param_names)

    def _extract_parameters_with_rules(self, user_question: str, param_names: List[str]) -> Dict[str, str]:
        """使用规则从用户问题中提取参数"""
        params = {}
        import re

        for param_name in param_names:
            if param_name == "product_name":
                product_match = re.search(r'[关于|查询|找|有关][\s]*([\w\s]+?)[\s]*[的|是|多少]', user_question)
                if product_match:
                    params[param_name] = product_match.group(1)
            elif param_name == "category_name":
                category_match = re.search(r'[类别|分类|种类|类型][\s]*([\w\s]+?)[\s]*[的|是|有]', user_question)
                if category_match:
                    params[param_name] = category_match.group(1)
            elif param_name == "order_id":
                order_match = re.search(r'订单[\s]*([0-9]+)', user_question)
                if order_match:
                    params[param_name] = order_match.group(1)

        return params

    def _extract_parameters_with_llm(self, user_question: str, param_names: List[str],
                                    query_name: str, llm: Any) -> Dict[str, str]:
        """使用LLM从用户问题中提取参数"""
        from langchain_core.prompts import ChatPromptTemplate

        prompt = ChatPromptTemplate.from_messages([
            ("system", """你是参数提取专家。你的任务是从用户问题中提取指定参数。
            只返回JSON格式的参数值，不要添加任何解释。
            如果无法提取某个参数，则该参数值为空字符串。"""),
            ("human", f"""
            用户问题: {user_question}
            查询类型: {query_name}
            需要提取的参数: {', '.join(param_names)}

            请提取这些参数并以JSON格式返回，格式如: {{"参数名": "参数值", ...}}
            """)
        ])

        response = llm.invoke(prompt)
        import json
        import re

        try:
            json_match = re.search(r'{.*}', response.content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
        except Exception as e:
            logger.error(f"无法解析LLM响应为JSON: {str(e)}")

        return {}


def create_vector_query_matcher(
    predefined_cypher_dict: Dict[str, str],
    query_descriptions: Optional[Dict[str, str]] = None
) -> VectorQueryMatcher:
    """创建并返回VectorQueryMatcher实例"""
    if query_descriptions is None:
        query_descriptions = {
            name: name.replace('_', ' ')
            for name in predefined_cypher_dict.keys()
        }
    return VectorQueryMatcher(predefined_cypher_dict, query_descriptions)
