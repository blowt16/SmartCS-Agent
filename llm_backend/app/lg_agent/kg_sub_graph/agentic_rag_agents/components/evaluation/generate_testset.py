"""
测试集生成器

从 Neo4j 知识图谱中抽取真实产品数据，用 LLM 批量生成测试问答对。

生成策略：
    1. 从 Neo4j 查询真实产品、类别、评价数据
    2. 按类别构造 prompt，让 LLM 生成多样化的问答对
    3. 每个类别 40 条，共 200 条
    4. 输出为 JSON 文件，供评估脚本使用

类别分布（共 200 条）：
    - product_query:    40 条  产品信息查询
    - stock_price:      40 条  库存/价格查询
    - comparison:       40 条  产品对比推荐
    - policy:           40 条  售后政策/流程
    - edge_case:        40 条  错别字/模糊/超范围
"""

import json
import asyncio
from typing import List, Dict, Any
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logger import get_logger
from app.lg_agent.kg_sub_graph.kg_neo4j_conn import get_neo4j_graph

logger = get_logger(service="testset_generator")


# ===== 结构化输出定义 =====

class QAPair(BaseModel):
    """单条测试问答对"""
    question: str = Field(description="用户问题（自然语言）")
    expected_answer: str = Field(description="期望的标准答案（基于知识图谱数据）")
    difficulty: str = Field(description="难度：easy / medium / hard")
    requires_cypher: bool = Field(description="是否需要 Cypher 查询（vs 纯文档检索）")


class QABatch(BaseModel):
    """一批测试问答对"""
    items: List[QAPair]


# ===== Prompt 模板 =====

PRODUCT_QUERY_PROMPT = """你是电商客服测试数据生成专家。
基于以下真实产品数据，生成 {count} 条【产品信息查询】类的测试问答对。

要求：
1. 问题要多样化：单产品查询、多产品查询、按类别查询、按属性查询
2. 答案必须严格基于给定的数据，不要编造
3. 难度分布：easy 50%, medium 30%, hard 20%
4. 部分问题使用口语化表达（如"有啥""咋样""多少钱"）

产品数据：
{data}
"""

STOCK_PRICE_PROMPT = """你是电商客服测试数据生成专家。
基于以下真实产品数据，生成 {count} 条【库存和价格查询】类的测试问答对。

要求：
1. 涵盖：单价查询、库存状态、批量价格、促销相关
2. 部分问题包含多个产品或条件筛选
3. 答案必须严格基于给定数据
4. 难度分布：easy 40%, medium 40%, hard 20%

产品数据：
{data}
"""

COMPARISON_PROMPT = """你是电商客服测试数据生成专家。
基于以下真实产品数据，生成 {count} 条【产品对比和推荐】类的测试问答对。

要求：
1. 涵盖：同类别对比、跨类别推荐、性价比分析、按需求推荐
2. 问题要有明确的对比维度（价格、库存、评分等）
3. 答案必须基于给定数据给出客观对比
4. 难度分布：easy 20%, medium 50%, hard 30%

产品数据：
{data}
"""

POLICY_PROMPT = """你是电商客服测试数据生成专家。
生成 {count} 条【售后政策和流程】类的测试问答对。

要求：
1. 涵盖：退换货流程、保修政策、配送时间、支付方式、发票问题
2. 答案要完整清晰，模拟真实客服的回答风格
3. 包含常见的追问场景
4. 难度分布：easy 40%, medium 40%, hard 20%

常见电商售后政策参考：
- 7天无理由退换货（未拆封）
- 质量问题15天内免费换新
- 保修期：小家电1年，大家电3年
- 配送：一线城市次日达，其他3-5天
- 支持支付宝、微信、信用卡
"""

EDGE_CASE_PROMPT = """你是电商客服测试数据生成专家。
生成 {count} 条【边缘case】类的测试问答对，用于测试系统的鲁棒性。

要求覆盖以下子类型（均匀分配）：
1. 错别字/输入错误（如"扫第机器人""智neng音箱"）：约10条
2. 模糊/不完整问题（如"有吗""那个多少钱"）：约10条
3. 超范围问题（服装、食品等非智能家居）：约10条
4. 复杂多轮/多条件查询：约10条
5. 难度：hard 为主

不需要严格基于数据，但要模拟真实用户的输入习惯。
"""


# ===== 数据抽取 =====

def fetch_product_data() -> str:
    """从 Neo4j 抽取产品数据，用于构造生成 prompt"""
    graph = get_neo4j_graph()
    if graph is None:
        logger.error("Neo4j 连接失败")
        return ""

    queries = {
        "products": """
            MATCH (p:Product)
            RETURN p.ProductName as name, p.UnitPrice as price,
                   p.UnitsInStock as stock, p.CategoryName as category
            LIMIT 50
        """,
        "categories": """
            MATCH (c:Category)
            RETURN c.CategoryName as name, c.Description as desc
        """,
        "reviews": """
            MATCH (p:Product)<-[:ABOUT]-(r:Review)
            RETURN p.ProductName as product, avg(toFloat(r.Rating)) as avg_rating,
                   count(r) as review_count
            ORDER BY review_count DESC
            LIMIT 20
        """,
    }

    result_parts = []
    for label, query in queries.items():
        try:
            records = graph.query(query)
            if records:
                result_parts.append(f"## {label}\n{json.dumps(records, ensure_ascii=False, indent=2)}")
        except Exception as e:
            logger.warning(f"查询 {label} 失败: {e}")

    return "\n\n".join(result_parts)


# ===== 生成主流程 =====

async def generate_batch(
    llm: BaseChatModel,
    prompt_template: str,
    data: str,
    count: int,
) -> List[Dict[str, Any]]:
    """用 LLM 生成一批测试问答对"""
    prompt = prompt_template.format(count=count, data=data)

    chain = llm.with_structured_output(QABatch)
    result = await chain.ainvoke(prompt)

    logger.info(f"生成了 {len(result.items)} 条测试数据")
    return [item.model_dump() for item in result.items]


async def generate_testset(
    output_path: str = "evaluation/testset.json",
    total_count: int = 200,
) -> str:
    """
    生成完整测试集。

    Args:
        output_path: 输出文件路径
        total_count: 总条数（按类别均分）

    Returns:
        输出文件的绝对路径
    """
    # 初始化 LLM
    if settings.AGENT_SERVICE == settings.ServiceType.DEEPSEEK:
        from langchain_deepseek import ChatDeepSeek
        llm = ChatDeepSeek(
            api_key=settings.DEEPSEEK_API_KEY,
            model_name=settings.DEEPSEEK_MODEL,
            temperature=0.8,
        )
    else:
        from langchain_ollama import ChatOllama
        llm = ChatOllama(
            model=settings.OLLAMA_AGENT_MODEL,
            base_url=settings.OLLAMA_BASE_URL,
            temperature=0.8,
        )

    # 从 Neo4j 抽取数据
    logger.info("从 Neo4j 抽取产品数据...")
    product_data = fetch_product_data()

    if not product_data:
        logger.warning("Neo4j 数据为空，使用空数据继续（政策/边缘case 类不依赖数据）")

    # 按类别生成
    per_category = total_count // 5
    categories = [
        ("product_query", PRODUCT_QUERY_PROMPT, True),
        ("stock_price", STOCK_PRICE_PROMPT, True),
        ("comparison", COMPARISON_PROMPT, True),
        ("policy", POLICY_PROMPT, False),
        ("edge_case", EDGE_CASE_PROMPT, False),
    ]

    all_items = []

    for category_name, prompt_template, needs_data in categories:
        logger.info(f"生成 {category_name} 类测试数据 ({per_category} 条)...")
        data = product_data if needs_data else ""

        try:
            items = await generate_batch(llm, prompt_template, data, per_category)
            for item in items:
                item["category"] = category_name
            all_items.extend(items)
        except Exception as e:
            logger.error(f"生成 {category_name} 失败: {e}")

    # 写入文件
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    testset = {
        "metadata": {
            "total": len(all_items),
            "categories": {cat: per_category for cat, _, _ in categories},
            "description": "SmartCS-Agent 电商客服系统评估测试集",
        },
        "items": all_items,
    }

    with open(output, "w", encoding="utf-8") as f:
        json.dump(testset, f, ensure_ascii=False, indent=2)

    logger.info(f"测试集已生成: {output.absolute()}, 共 {len(all_items)} 条")
    return str(output.absolute())


if __name__ == "__main__":
    asyncio.run(generate_testset())
