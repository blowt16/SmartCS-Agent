"""
RAG 评估脚本 — 评估 Agent 的工具选择准确率、Cypher 正确率、答案相关性和完整性

使用方式：
    cd llm_backend
    python eval_rag.py

评估维度：
    1. 工具选择准确率：Agent 是否选对了工具
    2. Cypher 正确率：生成的 Cypher 能否正确执行且返回结果
    3. 答案相关性：回答是否包含标准答案关键词
    4. 答案完整性：回答是否覆盖了所有关键信息
"""

import asyncio
import json
import time
from typing import Dict, List, Any

# ============================================================
# 第1部分：测试集定义（Golden Dataset）
# ============================================================

TEST_CASES = [
    {
        "id": "Q1",
        "question": "牛奶的价格是多少",
        "expected_tool": "cypher_query",  # 或 predefined_cypher
        "expected_keywords": ["19"],  # 标准答案关键词
        "expected_items": ["牛奶"],   # 标准答案应包含的信息项
        "difficulty": "简单",
        "category": "产品查询",
    },
    {
        "id": "Q2",
        "question": "有哪些饮料类产品",
        "expected_tool": "cypher_query",
        "expected_keywords": ["牛奶", "果汁", "咖啡", "绿茶", "啤酒"],
        "expected_items": ["牛奶", "果汁", "咖啡豆", "绿茶", "啤酒"],
        "difficulty": "简单",
        "category": "产品查询",
    },
    {
        "id": "Q3",
        "question": "哪些产品库存不足，库存少于20",
        "expected_tool": "predefined_cypher",
        "expected_keywords": ["橄榄油", "辣椒酱", "果汁", "牛奶"],
        "expected_items": ["橄榄油", "辣椒酱", "果汁", "牛奶"],
        "difficulty": "简单",
        "category": "产品查询",
    },
    {
        "id": "Q4",
        "question": "三和贸易下过哪些订单",
        "expected_tool": "cypher_query",
        "expected_keywords": ["10248", "10253"],
        "expected_items": ["10248", "10253"],
        "difficulty": "中等",
        "category": "订单查询",
    },
    {
        "id": "Q5",
        "question": "新鲜食品公司供应了哪些产品",
        "expected_tool": "cypher_query",
        "expected_keywords": ["牛奶", "奶酪"],
        "expected_items": ["牛奶", "奶酪"],
        "difficulty": "中等",
        "category": "供应商查询",
    },
    {
        "id": "Q6",
        "question": "哪个客户购买的总金额最多",
        "expected_tool": "cypher_query",
        "expected_keywords": ["客户", "金额"],
        "expected_items": [],  # 需要计算，答案不确定
        "difficulty": "中等",
        "category": "聚合查询",
    },
    {
        "id": "Q7",
        "question": "展示供应商和产品的供应关系",
        "expected_tool": "cypher_query",
        "expected_keywords": ["新鲜食品", "海洋贸易", "田园农场", "全球饮品"],
        "expected_items": ["新鲜食品公司", "海洋贸易", "田园农场", "全球饮品"],
        "difficulty": "中等",
        "category": "关系查询",
    },
    {
        "id": "Q8",
        "question": "牛奶属于哪个类别，有哪些同类产品",
        "expected_tool": "cypher_query",
        "expected_keywords": ["饮料", "果汁", "咖啡", "绿茶", "啤酒"],
        "expected_items": ["饮料", "果汁", "咖啡豆", "绿茶", "啤酒"],
        "difficulty": "中等",
        "category": "关联查询",
    },
    {
        "id": "Q9",
        "question": "订单10250包含哪些产品，总价是多少",
        "expected_tool": "cypher_query",
        "expected_keywords": ["巧克力", "咖啡", "232", "92"],
        "expected_items": ["巧克力", "咖啡豆"],
        "difficulty": "较难",
        "category": "订单明细",
    },
    {
        "id": "Q10",
        "question": "哪些员工处理了三和贸易的订单",
        "expected_tool": "cypher_query",
        "expected_keywords": ["张伟", "王磊"],
        "expected_items": ["张伟", "王磊"],
        "difficulty": "较难",
        "category": "多跳查询",
    },
]


# ============================================================
# 第2部分：评估函数
# ============================================================

def evaluate_answer(answer: str, expected_keywords: List[str], expected_items: List[str]) -> Dict[str, Any]:
    """
    评估答案的相关性和完整性

    相关性 = 命中的关键词数 / 总关键词数
    完整性 = 覆盖的信息项数 / 总信息项数
    """
    if not answer:
        return {"relevance": 0.0, "completeness": 0.0, "hit_keywords": [], "miss_keywords": [], "hit_items": [], "miss_items": []}

    # 关键词匹配（不区分大小写）
    answer_lower = answer.lower()
    hit_keywords = []
    miss_keywords = []
    for kw in expected_keywords:
        if kw.lower() in answer_lower:
            hit_keywords.append(kw)
        else:
            miss_keywords.append(kw)

    # 信息项匹配
    hit_items = []
    miss_items = []
    for item in expected_items:
        if item.lower() in answer_lower or item in answer:
            hit_items.append(item)
        else:
            miss_items.append(item)

    relevance = len(hit_keywords) / len(expected_keywords) if expected_keywords else 1.0
    completeness = len(hit_items) / len(expected_items) if expected_items else 1.0

    return {
        "relevance": round(relevance, 2),
        "completeness": round(completeness, 2),
        "hit_keywords": hit_keywords,
        "miss_keywords": miss_keywords,
        "hit_items": hit_items,
        "miss_items": miss_items,
    }


def calculate_metrics(results: List[Dict]) -> Dict[str, Any]:
    """
    汇总计算整体指标
    """
    total = len(results)

    # 工具选择准确率
    tool_correct = sum(1 for r in results if r.get("tool_correct"))
    tool_accuracy = tool_correct / total if total > 0 else 0

    # 答案相关性（平均值）
    avg_relevance = sum(r.get("relevance", 0) for r in results) / total if total > 0 else 0

    # 答案完整性（平均值）
    avg_completeness = sum(r.get("completeness", 0) for r in results) / total if total > 0 else 0

    # 成功率（相关性 > 0.5 视为成功）
    success_count = sum(1 for r in results if r.get("relevance", 0) > 0.5)
    success_rate = success_count / total if total > 0 else 0

    # 按难度分组统计
    by_difficulty = {}
    for r in results:
        diff = r.get("difficulty", "未知")
        if diff not in by_difficulty:
            by_difficulty[diff] = {"count": 0, "relevance_sum": 0, "completeness_sum": 0}
        by_difficulty[diff]["count"] += 1
        by_difficulty[diff]["relevance_sum"] += r.get("relevance", 0)
        by_difficulty[diff]["completeness_sum"] += r.get("completeness", 0)

    for diff, data in by_difficulty.items():
        n = data["count"]
        by_difficulty[diff]["avg_relevance"] = round(data["relevance_sum"] / n, 2) if n > 0 else 0
        by_difficulty[diff]["avg_completeness"] = round(data["completeness_sum"] / n, 2) if n > 0 else 0

    return {
        "total_questions": total,
        "tool_accuracy": round(tool_accuracy, 2),
        "avg_relevance": round(avg_relevance, 2),
        "avg_completeness": round(avg_completeness, 2),
        "success_rate": round(success_rate, 2),
        "by_difficulty": by_difficulty,
    }


# ============================================================
# 第3部分：运行评估（两种模式）
# ============================================================

async def run_eval_with_agent():
    """
    模式1：直接连接 Neo4j 执行 Cypher 查询（无需启动后端服务）
    评估 Text2Cypher / 预定义 Cypher 的查询质量
    """
    try:
        from app.core.config import settings
        from langchain_neo4j import Neo4jGraph
    except ImportError as e:
        print(f"导入失败: {e}")
        print("请确保在 llm_backend/ 目录下运行，且已激活 .venv")
        return []

    # 连接 Neo4j
    print(f"连接 Neo4j: {settings.NEO4J_URL}")
    try:
        graph = Neo4jGraph(
            url=settings.NEO4J_URL,
            username=settings.NEO4J_USERNAME,
            password=settings.NEO4J_PASSWORD,
            database=settings.NEO4J_DATABASE,
        )
        print("Neo4j 连接成功！")
    except Exception as e:
        print(f"Neo4j 连接失败: {e}")
        print("请检查 .env 中的 NEO4J 配置")
        return []

    # 为每个测试问题定义对应的 Cypher 查询
    # 这模拟了 Text2Cypher 应该生成的查询
    cypher_queries = {
        "Q1": "MATCH (p:Product {productName: '牛奶'}) RETURN p.productName AS name, p.unitPrice AS price",
        "Q2": "MATCH (p:Product)-[:BELONGS_TO]->(c:Category {categoryName: '饮料'}) RETURN p.productName AS name",
        "Q3": "MATCH (p:Product) WHERE p.unitsInStock < 20 RETURN p.productName AS name, p.unitsInStock AS stock ORDER BY p.unitsInStock",
        "Q4": "MATCH (cu:Customer {companyName: '三和贸易'})-[:PLACED]->(o:Order) RETURN o.orderID AS orderID, o.orderDate AS date",
        "Q5": "MATCH (s:Supplier {companyName: '新鲜食品公司'})-[:SUPPLIES]->(p:Product) RETURN p.productName AS name",
        "Q6": "MATCH (cu:Customer)-[:PLACED]->(o:Order)-[c:CONTAINS]->(p:Product) WITH cu, sum(c.unitPrice * c.quantity) AS total RETURN cu.companyName AS customer, total ORDER BY total DESC LIMIT 1",
        "Q7": "MATCH (s:Supplier)-[:SUPPLIES]->(p:Product) RETURN s.companyName AS supplier, collect(p.productName) AS products",
        "Q8": "MATCH (p:Product {productName: '牛奶'})-[:BELONGS_TO]->(c:Category)<-[:BELONGS_TO]-(p2:Product) RETURN c.categoryName AS category, collect(p2.productName) AS products",
        "Q9": "MATCH (o:Order {orderID: '10250'})-[c:CONTAINS]->(p:Product) RETURN p.productName AS product, c.quantity AS qty, c.unitPrice AS price, c.unitPrice * c.quantity AS subtotal",
        "Q10": "MATCH (e:Employee)-[:PROCESSED]->(o:Order)<-[:PLACED]-(cu:Customer {companyName: '三和贸易'}) RETURN e.firstName + e.lastName AS employee, o.orderID AS orderID",
    }

    results = []

    for tc in TEST_CASES:
        print(f"\n{'='*60}")
        print(f"测试 {tc['id']}: {tc['question']}")
        print(f"预期工具: {tc['expected_tool']} | 难度: {tc['difficulty']}")

        start_time = time.time()
        cypher = cypher_queries.get(tc["id"], "")

        try:
            print(f"Cypher: {cypher}")
            records = graph.query(cypher)
            elapsed = round(time.time() - start_time, 2)

            # 把查询结果转为字符串用于关键词匹配
            answer = json.dumps(records, ensure_ascii=False)
            print(f"查询结果: {answer[:200]}")
            print(f"响应时间: {elapsed}s")

            # 判断 Cypher 是否执行成功（返回非空结果）
            cypher_success = len(records) > 0
            print(f"Cypher 执行: {'成功' if cypher_success else '无结果'}")

            # 评估答案
            eval_result = evaluate_answer(answer, tc["expected_keywords"], tc["expected_items"])

            result = {
                "id": tc["id"],
                "question": tc["question"],
                "expected_tool": tc["expected_tool"],
                "cypher": cypher,
                "answer": answer[:500],
                "elapsed": elapsed,
                "tool_correct": True,  # 直接用 Cypher 测试，工具选择默认正确
                "cypher_success": cypher_success,
                "relevance": eval_result["relevance"],
                "completeness": eval_result["completeness"],
                "hit_keywords": eval_result["hit_keywords"],
                "miss_keywords": eval_result["miss_keywords"],
                "hit_items": eval_result["hit_items"],
                "miss_items": eval_result["miss_items"],
                "difficulty": tc["difficulty"],
                "category": tc["category"],
                "error": None,
            }

        except Exception as e:
            elapsed = round(time.time() - start_time, 2)
            result = {
                "id": tc["id"],
                "question": tc["question"],
                "expected_tool": tc["expected_tool"],
                "cypher": cypher,
                "answer": "",
                "elapsed": elapsed,
                "tool_correct": False,
                "cypher_success": False,
                "relevance": 0,
                "completeness": 0,
                "hit_keywords": [],
                "miss_keywords": tc["expected_keywords"],
                "hit_items": [],
                "miss_items": tc["expected_items"],
                "difficulty": tc["difficulty"],
                "category": tc["category"],
                "error": str(e),
            }
            print(f"错误: {e}")

        results.append(result)

    return results


async def run_eval_manual():
    """
    模式2：手动评估（不需要启动服务）
    你手动运行 Agent 查询，把回答粘贴到这里评估
    """
    print("=" * 60)
    print("手动评估模式")
    print("对每个问题，请粘贴 Agent 的回答（直接回车跳过）：")
    print("=" * 60)

    results = []

    for tc in TEST_CASES:
        print(f"\n{tc['id']}: {tc['question']}")
        print(f"预期工具: {tc['expected_tool']} | 难度: {tc['difficulty']}")
        answer = input("粘贴回答 > ").strip()

        if not answer:
            answer = ""

        eval_result = evaluate_answer(answer, tc["expected_keywords"], tc["expected_items"])

        print(f"  相关性: {eval_result['relevance']} | 完整性: {eval_result['completeness']}")
        if eval_result["miss_keywords"]:
            print(f"  缺失关键词: {eval_result['miss_keywords']}")

        results.append({
            "id": tc["id"],
            "question": tc["question"],
            "expected_tool": tc["expected_tool"],
            "answer": answer[:500],
            "relevance": eval_result["relevance"],
            "completeness": eval_result["completeness"],
            "hit_keywords": eval_result["hit_keywords"],
            "miss_keywords": eval_result["miss_keywords"],
            "hit_items": eval_result["hit_items"],
            "miss_items": eval_result["miss_items"],
            "difficulty": tc["difficulty"],
            "category": tc["category"],
        })

    return results


# ============================================================
# 第4部分：输出评估报告
# ============================================================

def print_report(results: List[Dict], metrics: Dict[str, Any]):
    """打印评估报告"""
    print("\n")
    print("=" * 70)
    print("                    RAG 评估报告")
    print("=" * 70)

    print(f"\n📊 整体指标:")
    print(f"  测试问题数:     {metrics['total_questions']}")
    print(f"  工具选择准确率:  {metrics['tool_accuracy']:.0%}")
    print(f"  答案相关性:     {metrics['avg_relevance']:.0%}")
    print(f"  答案完整性:     {metrics['avg_completeness']:.0%}")
    print(f"  成功率(相关性>50%): {metrics['success_rate']:.0%}")

    print(f"\n📈 按难度分组:")
    print(f"  {'难度':<8} {'题数':<6} {'相关性':<10} {'完整性':<10}")
    print(f"  {'-'*34}")
    for diff, data in metrics.get("by_difficulty", {}).items():
        print(f"  {diff:<8} {data['count']:<6} {data['avg_relevance']:<10} {data['avg_completeness']:<10}")

    print(f"\n📋 逐题结果:")
    print(f"  {'ID':<5} {'问题':<25} {'相关性':<8} {'完整性':<8} {'耗时':<8} {'状态'}")
    print(f"  {'-'*75}")
    for r in results:
        status = "✓ 通过" if r.get("relevance", 0) > 0.5 else "✗ 失败"
        if r.get("error"):
            status = f"✗ 错误: {r['error'][:30]}"
        elapsed = f"{r.get('elapsed', 0)}s"
        print(f"  {r['id']:<5} {r['question'][:23]:<25} {r.get('relevance', 0):<8} {r.get('completeness', 0):<8} {elapsed:<8} {status}")

    # 失败分析
    failures = [r for r in results if r.get("relevance", 0) <= 0.5]
    if failures:
        print(f"\n❌ 失败分析 ({len(failures)} 题):")
        for r in failures:
            print(f"  {r['id']} {r['question']}")
            if r.get("miss_keywords"):
                print(f"    缺失关键词: {r['miss_keywords']}")
            if r.get("miss_items"):
                print(f"    缺失信息: {r['miss_items']}")

    # 保存详细结果到 JSON
    with open("eval_results.json", "w", encoding="utf-8") as f:
        json.dump({"results": results, "metrics": metrics}, f, ensure_ascii=False, indent=2)
    print(f"\n💾 详细结果已保存到 eval_results.json")


# ============================================================
# 第5部分：主入口
# ============================================================

async def main():
    print("RAG 评估工具")
    print("1. 自动评估（需要启动后端服务）")
    print("2. 手动评估（粘贴 Agent 回答）")

    choice = input("选择模式 (1/2): ").strip()

    if choice == "1":
        results = await run_eval_with_agent()
    else:
        results = await run_eval_manual()

    if results:
        metrics = calculate_metrics(results)
        print_report(results, metrics)


if __name__ == "__main__":
    asyncio.run(main())
