"""
从 origin_data/exported_data/ 下的 CSV 文件生成产品知识文档。

每款产品生成一份 txt 文件，包含：
- 产品基本信息（名称、品牌、品类、价格、库存）
- 所属品类说明
- 供应商信息
- 用户评价（好评/差评分类，按评分排序）

输出目录：llm_backend/knowledge_data/product_knowledge/

用法：python scripts/generate_product_knowledge.py
"""

import csv
import os
from pathlib import Path
from collections import defaultdict

# 路径配置
ROOT_DIR = Path(__file__).parent.parent
ORIGIN_DATA_DIR = ROOT_DIR / "llm_backend" / "knowledge_data" / "origin_data" / "exported_data"
OUTPUT_DIR = ROOT_DIR / "llm_backend" / "knowledge_data" / "product_knowledge"


def load_csv(filename: str) -> list[dict]:
    """加载 CSV 文件并返回字典列表"""
    filepath = ORIGIN_DATA_DIR / filename
    with open(filepath, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def generate_product_doc(product: dict, reviews: list[dict], category: dict, supplier: dict) -> str:
    """生成单个产品的知识文档"""

    doc_lines = []

    # 标题
    doc_lines.append(f"# {product['ProductName']} 产品知识文档")
    doc_lines.append("")

    # 产品基本信息
    doc_lines.append("## 产品基本信息")
    doc_lines.append("")
    doc_lines.append(f"- 产品名称：{product['ProductName']}")
    doc_lines.append(f"- 品牌/供应商：{product['SupplierName']}")
    doc_lines.append(f"- 品类：{product['CategoryName']}")
    doc_lines.append(f"- 价格：{product['UnitPrice']} 元")
    doc_lines.append(f"- 包装规格：{product['QuantityPerUnit']}")
    doc_lines.append(f"- 库存状态：{product['UnitsInStock']} 件在库，{product['UnitsOnOrder']} 件待补")
    discontinued = "已停售" if product['Discontinued'] == '1' else "正常销售"
    doc_lines.append(f"- 销售状态：{discontinued}")
    doc_lines.append("")

    # 品类说明
    if category:
        doc_lines.append("## 品类说明")
        doc_lines.append("")
        doc_lines.append(category['Description'])
        doc_lines.append("")

    # 供应商信息
    if supplier:
        doc_lines.append("## 供应商信息")
        doc_lines.append("")
        doc_lines.append(f"- 公司名称：{supplier['CompanyName']}")
        doc_lines.append(f"- 联系人：{supplier['ContactName']}（{supplier['ContactTitle']}）")
        doc_lines.append(f"- 地址：{supplier['Address']}, {supplier['City']}")
        doc_lines.append(f"- 联系电话：{supplier['Phone']}")
        doc_lines.append("")

    # 用户评价
    if reviews:
        doc_lines.append("## 用户评价")
        doc_lines.append("")

        # 按评分分类
        positive_reviews = [r for r in reviews if float(r['Rating']) >= 4.0]
        neutral_reviews = [r for r in reviews if 2.5 <= float(r['Rating']) < 4.0]
        negative_reviews = [r for r in reviews if float(r['Rating']) < 2.5]

        # 好评
        if positive_reviews:
            doc_lines.append("### 好评（评分 >= 4.0）")
            doc_lines.append("")
            for r in sorted(positive_reviews, key=lambda x: float(x['Rating']), reverse=True):
                doc_lines.append(f"- 评分：{r['Rating']} | 日期：{r['ReviewDate']}")
                doc_lines.append(f"  客户：{r['CustomerName']}")
                doc_lines.append(f"  内容：{r['ReviewText']}")
                doc_lines.append("")

        # 中评
        if neutral_reviews:
            doc_lines.append("### 中评（评分 2.5 - 3.9）")
            doc_lines.append("")
            for r in sorted(neutral_reviews, key=lambda x: float(x['Rating']), reverse=True):
                doc_lines.append(f"- 评分：{r['Rating']} | 日期：{r['ReviewDate']}")
                doc_lines.append(f"  客户：{r['CustomerName']}")
                doc_lines.append(f"  内容：{r['ReviewText']}")
                doc_lines.append("")

        # 差评
        if negative_reviews:
            doc_lines.append("### 差评（评分 < 2.5）")
            doc_lines.append("")
            for r in sorted(negative_reviews, key=lambda x: float(x['Rating']), reverse=True):
                doc_lines.append(f"- 评分：{r['Rating']} | 日期：{r['ReviewDate']}")
                doc_lines.append(f"  客户：{r['CustomerName']}")
                doc_lines.append(f"  内容：{r['ReviewText']}")
                doc_lines.append("")

        # 评价统计
        avg_rating = sum(float(r['Rating']) for r in reviews) / len(reviews)
        doc_lines.append("### 评价统计")
        doc_lines.append("")
        doc_lines.append(f"- 总评价数：{len(reviews)} 条")
        doc_lines.append(f"- 平均评分：{avg_rating:.2f}")
        doc_lines.append(f"- 好评数：{len(positive_reviews)} 条")
        doc_lines.append(f"- 中评数：{len(neutral_reviews)} 条")
        doc_lines.append(f"- 差评数：{len(negative_reviews)} 条")
        doc_lines.append("")
    else:
        doc_lines.append("## 用户评价")
        doc_lines.append("")
        doc_lines.append("暂无用户评价数据。")
        doc_lines.append("")

    # 常见问题（从评价中提炼）
    if reviews:
        doc_lines.append("## 常见问题与注意事项")
        doc_lines.append("")

        # 从差评中提炼问题关键词
        issues = []
        for r in negative_reviews:
            text = r['ReviewText'].lower()
            if '故障' in text or '失灵' in text or '异常' in text:
                issues.append("设备故障/异常")
            if '售后' in text or '服务' in text:
                issues.append("售后服务问题")
            if '兼容' in text:
                issues.append("兼容性问题")
            if '价格' in text or '贵' in text:
                issues.append("价格偏高")
            if '噪音' in text or '散热' in text:
                issues.append("噪音/散热问题")

        if issues:
            doc_lines.append("根据用户反馈，该产品可能存在以下问题：")
            doc_lines.append("")
            for issue in set(issues):
                doc_lines.append(f"- {issue}")
            doc_lines.append("")

        # 从好评中提炼优点
        pros = []
        for r in positive_reviews:
            text = r['ReviewText'].lower()
            if '质量' in text or '做工' in text:
                pros.append("做工质量好")
            if '智能' in text:
                pros.append("智能化程度高")
            if '稳定' in text:
                pros.append("运行稳定")
            if '安装' in text or '送货' in text:
                pros.append("安装/配送服务好")

        if pros:
            doc_lines.append("用户认可的产品优点：")
            doc_lines.append("")
            for pro in set(pros):
                doc_lines.append(f"- {pro}")
            doc_lines.append("")

    return "\n".join(doc_lines)


def main():
    """主函数：生成所有产品的知识文档"""

    print("正在加载 CSV 数据...")

    # 加载所有数据
    products = load_csv("products.csv")
    reviews_data = load_csv("reviews.csv")
    categories = load_csv("categories.csv")
    suppliers = load_csv("suppliers.csv")

    # 构建索引
    category_map = {c['CategoryID']: c for c in categories}
    supplier_map = {s['SupplierID']: s for s in suppliers}

    # 按产品 ID 分组评价
    reviews_by_product = defaultdict(list)
    for r in reviews_data:
        reviews_by_product[r['ProductID']].append(r)

    # 创建输出目录
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"共有 {len(products)} 款产品，开始生成知识文档...")

    # 生成每款产品的文档
    for product in products:
        product_id = product['ProductID']
        product_name = product['ProductName']

        # 获取关联数据
        reviews = reviews_by_product.get(product_id, [])
        category = category_map.get(product['CategoryID'])
        supplier = supplier_map.get(product['SupplierID'])

        # 生成文档内容
        doc_content = generate_product_doc(product, reviews, category, supplier)

        # 写入文件
        filename = f"product_{product_id}_{product_name.replace(' ', '_')}.txt"
        filepath = OUTPUT_DIR / filename

        with open(filepath, encoding="utf-8", mode="w") as f:
            f.write(doc_content)

        print(f"  已生成：{filename}（{len(reviews)} 条评价）")

    print(f"\n完成！文档已保存到：{OUTPUT_DIR}")
    print(f"共生成 {len(products)} 个知识文档")


if __name__ == "__main__":
    main()
