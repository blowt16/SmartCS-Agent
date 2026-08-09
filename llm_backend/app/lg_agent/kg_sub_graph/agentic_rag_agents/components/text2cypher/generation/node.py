from typing import Any, Callable, Coroutine, Dict

from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_neo4j import Neo4jGraph

from ....components.text2cypher.generation.prompts import (
    create_text2cypher_generation_prompt_template,
)
from ....retrievers.cypher_examples.base import BaseCypherExampleRetriever
from ....components.agent_safety import CypherSafetyValidator
from ..state import CypherInputState
from app.core.logger import get_logger

logger = get_logger(service="text2cypher_generation")

# 定义text2cypher generation prompt
generation_prompt = create_text2cypher_generation_prompt_template()


def create_text2cypher_generation_node(
    llm: BaseChatModel,
    graph: Neo4jGraph,
    cypher_example_retriever: BaseCypherExampleRetriever,
) -> Callable[[CypherInputState], Coroutine[Any, Any, dict[str, Any]]]:
    text2cypher_chain = generation_prompt | llm | StrOutputParser()

    async def generate_cypher(state: CypherInputState) -> Dict[str, Any]:
        """
        Generates a cypher statement based on the provided schema and user input
        """
        task = state.get("task", "")
        # 获取针对当前任务的cypher示例, 选择 k 个
        examples: str = cypher_example_retriever.get_examples(
            **{"query": task[0] if isinstance(task, list) else task, "k": 3}
        )
        generated_cypher = await text2cypher_chain.ainvoke(
            {
                "question": state.get("task", ""),
                "fewshot_examples": examples,
                "schema": graph.schema,
            }
        )

        # ② Cypher 安全校验：生成后执行前检查危险操作
        validator = CypherSafetyValidator()
        is_safe, reason = validator.validate(generated_cypher)
        if not is_safe:
            logger.warning(f"Cypher 安全校验失败: {reason}, 原查询: {generated_cypher[:100]}")
            return {
                "statement": "",
                "steps": ["generate_cypher_blocked"],
                "errors": [f"Cypher 安全校验失败: {reason}"],
            }

        return {"statement": generated_cypher, "steps": ["generate_cypher"]}

    return generate_cypher
