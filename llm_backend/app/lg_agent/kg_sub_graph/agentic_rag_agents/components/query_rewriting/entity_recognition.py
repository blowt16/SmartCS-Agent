"""
实体识别与链接模块

实体识别（Entity Recognition）：
    从用户问题中识别出关键实体（产品名、品牌、类别等）。
    例如："扫地机器人X1多少钱" → 识别出 ["扫地机器人X1"]

实体链接（Entity Linking）：
    将识别出的实体链接到 Neo4j 知识图谱中的具体节点。
    例如："扫地机器人X1" → Node(id=123, type="Product")

意义：
    有了实体 ID 后，下游 Text2Cypher 可以用精准查询
    MATCH (p:Product {id: 123}) 而非模糊匹配，提高检索准确率。
"""

from typing import List, Optional, Dict, Any

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_neo4j import Neo4jGraph
from pydantic import BaseModel, Field

from app.core.logger import get_logger

logger = get_logger(service="entity_recognition")


# ==================== 数据模型 ====================


class EntityItem(BaseModel):
    """单个实体的识别结果"""
    name: str = Field(description="实体名称，如'扫地机器人X1'")
    predicted_type: str = Field(description="预测的实体类型：Product/Category/Customer/Supplier/Unknown")


class EntityRecognitionOutput(BaseModel):
    """LLM 实体识别的结构化输出"""
    entities: List[EntityItem] = Field(default_factory=list, description="识别出的实体列表")


class LinkedEntity(BaseModel):
    """链接后的实体（包含 Neo4j 节点信息）"""
    name: str                    # 用户问题中的原始名称
    node_id: Optional[int]       # Neo4j 节点 ID（如果链接成功）
    node_type: str               # 实际节点类型
    properties: Dict[str, Any]   # 节点属性（用于下游检索）
    linked: bool                 # 是否成功链接到图谱


# ==================== 提示词 ====================


ENTITY_RECOGNITION_PROMPT = """你是一个电商领域的实体识别专家。
从用户问题中识别出关键实体（产品名、类别名、客户名、供应商名等）。

实体类型说明：
- Product: 产品（如"扫地机器人X1"、"智能灯泡"）
- Category: 产品类别（如"智能家居"、"智能照明"）
- Customer: 客户名称
- Supplier: 供应商名称
- Unknown: 无法确定类型

规则：
1. 只识别明确的实体，不要过度推断
2. 如果问题中没有实体，返回空列表
3. 实体名称尽量保持原样，不要改写

示例：
问题: "扫地机器人X1多少钱"
识别: [{{"name": "扫地机器人X1", "predicted_type": "Product"}}]

问题: "智能家居类别有哪些产品"
识别: [{{"name": "智能家居", "predicted_type": "Category"}}]

问题: "你好"
识别: []
"""


# ==================== 核心函数 ====================


async def recognize_entities(
    llm: BaseChatModel,
    question: str,
) -> List[EntityItem]:
    """
    使用 LLM 从用户问题中识别实体。

    Args:
        llm: 语言模型实例
        question: 用户问题

    Returns:
        识别出的实体列表
    """
    if not question.strip():
        return []

    prompt = ChatPromptTemplate.from_messages([
        ("system", ENTITY_RECOGNITION_PROMPT),
        ("human", "问题: {question}\n\n请识别其中的实体。"),
    ])

    chain = prompt | llm.with_structured_output(EntityRecognitionOutput)
    result = await chain.ainvoke({"question": question})

    if result.entities:
        logger.info(f"实体识别完成，识别出 {len(result.entities)} 个实体:")
        for e in result.entities:
            logger.info(f"  - {e.name} ({e.predicted_type})")
    else:
        logger.info("未识别出实体")

    return result.entities


def link_entities_to_graph(
    graph: Neo4jGraph,
    entities: List[EntityItem],
) -> List[LinkedEntity]:
    """
    将识别出的实体链接到 Neo4j 图谱。

    查询策略：
        1. 精确匹配：name == entity.name
        2. 模糊匹配：name CONTAINS entity.name（如果精确匹配失败）

    Args:
        graph: Neo4j 图数据库连接
        entities: LLM 识别出的实体列表

    Returns:
        链接后的实体列表（包含节点 ID 和属性）
    """
    if not entities:
        return []

    linked_entities: List[LinkedEntity] = []

    for entity in entities:
        linked = _link_single_entity(graph, entity)
        linked_entities.append(linked)

    return linked_entities


def _link_single_entity(
    graph: Neo4jGraph,
    entity: EntityItem,
) -> LinkedEntity:
    """
    链接单个实体到图谱。

    查询逻辑：
        1. 根据预测类型确定节点标签（Product/Category 等）
        2. 先精确匹配（name/ProductName/CategoryName == 实体名）
        3. 精确匹配失败则模糊匹配（CONTAINS）

    为什么查多个字段名？
        Northwind 数据集的 Neo4j 节点中，
        Product 用 ProductName，Category 用 CategoryName，
        而 LLM 预测类型可能不 100% 准确，
        同时查多个字段名可以兜底。

    Args:
        graph: Neo4j 连接
        entity: 待链接的实体

    Returns:
        LinkedEntity 对象
    """
    # 根据预测类型确定查询的节点标签
    label_map = {
        "Product": "Product",
        "Category": "Category",
        "Customer": "Customer",
        "Supplier": "Supplier",
    }
    label = label_map.get(entity.predicted_type, None)

    # 尝试精确匹配
    if label:
        cypher = f"""
        MATCH (n:{label})
        WHERE n.name = $name OR n.ProductName = $name OR n.CategoryName = $name
        RETURN elementId(n) as node_id, labels(n)[0] as node_type, n as properties
        LIMIT 1
        """
    else:
        # 未知类型，在所有节点中搜索
        cypher = """
        MATCH (n)
        WHERE n.name = $name OR n.ProductName = $name OR n.CategoryName = $name
        RETURN elementId(n) as node_id, labels(n)[0] as node_type, n as properties
        LIMIT 1
        """

    result = graph.query(cypher, params={"name": entity.name})

    if result:
        row = result[0]
        logger.info(f"实体 '{entity.name}' 精确匹配成功: {row['node_type']}")
        return LinkedEntity(
            name=entity.name,
            node_id=int(row["node_id"]) if row["node_id"] else None,
            node_type=row["node_type"] or "Unknown",
            properties=dict(row["properties"]) if row["properties"] else {},
            linked=True,
        )

    # 精确匹配失败，尝试模糊匹配
    if label:
        cypher = f"""
        MATCH (n:{label})
        WHERE n.name CONTAINS $name OR n.ProductName CONTAINS $name OR n.CategoryName CONTAINS $name
        RETURN elementId(n) as node_id, labels(n)[0] as node_type, n as properties
        LIMIT 1
        """
    else:
        cypher = """
        MATCH (n)
        WHERE n.name CONTAINS $name OR n.ProductName CONTAINS $name OR n.CategoryName CONTAINS $name
        RETURN elementId(n) as node_id, labels(n)[0] as node_type, n as properties
        LIMIT 1
        """

    result = graph.query(cypher, params={"name": entity.name})

    if result:
        row = result[0]
        logger.info(f"实体 '{entity.name}' 模糊匹配到节点: {row['node_type']}")
        return LinkedEntity(
            name=entity.name,
            node_id=int(row["node_id"]) if row["node_id"] else None,
            node_type=row["node_type"] or "Unknown",
            properties=dict(row["properties"]) if row["properties"] else {},
            linked=True,
        )

    # 链接失败
    logger.warning(f"实体 '{entity.name}' 未能链接到图谱")
    return LinkedEntity(
        name=entity.name,
        node_id=None,
        node_type=entity.predicted_type,
        properties={},
        linked=False,
    )


async def recognize_and_link_entities(
    llm: BaseChatModel,
    graph: Neo4jGraph,
    question: str,
) -> List[LinkedEntity]:
    """
    实体识别与链接的主入口：识别 → 链接 一体化。

    流程：
        1. LLM 识别用户问题中的实体（产品名、类别名等）
        2. 在 Neo4j 中查找这些实体，获取节点 ID 和属性
        3. 返回链接结果，供下游 Text2Cypher / 向量检索 使用

    Args:
        llm: 语言模型实例
        graph: Neo4j 图数据库连接
        question: 用户问题

    Returns:
        链接后的实体列表
    """
    # 第一步：LLM 识别实体
    entities = await recognize_entities(llm, question)

    if not entities:
        return []

    # 第二步：链接到图谱
    linked_entities = link_entities_to_graph(graph, entities)

    return linked_entities
