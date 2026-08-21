"""
将 scripts/data/jd_smart_furniture.tsv（真实京东在售智能家具商品清单）转换为 docx
商品知识文档，供手动上传索引构建 / 检索测试。

TSV 列: 品类 | 商品名称 | 品牌 | 参考价格(元) | 主要参数与卖点 | 来源
每个商品生成一份 docx: H1=商品名称, 章节=商品信息(品牌/品类/参考价格)/主要参数与卖点/参考来源

用法:
  python scripts/build_smart_furniture_docx.py [--limit 50]

输出: llm_backend/knowledge_data/product_knowledge_docx/ (gitignore 内, 不会入库)
"""

import argparse
import csv
import sys
from pathlib import Path

from docx import Document

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TSV_PATH = PROJECT_ROOT / "scripts" / "data" / "jd_smart_furniture.tsv"
OUTPUT_DIR = PROJECT_ROOT / "llm_backend" / "knowledge_data" / "product_knowledge_docx"

COLS = ["品类", "商品名称", "品牌", "参考价格(元)", "主要参数与卖点", "来源"]


def sanitize_filename(name: str, max_len: int = 30) -> str:
    """文件名安全化（去非法字符, 截断）。"""
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return name[:max_len].strip("_")


def build_docx(row: dict, out_path: Path) -> bool:
    """单个商品生成 docx。商品名称为空视为失败。"""
    title = row["商品名称"].strip()
    if not title:
        return False

    doc = Document()
    doc.add_heading(title, level=1)

    doc.add_heading("商品信息", level=2)
    if row["品类"].strip():
        doc.add_paragraph(f"品类：{row['品类'].strip()}")
    if row["品牌"].strip():
        doc.add_paragraph(f"品牌：{row['品牌'].strip()}")
    if row["参考价格(元)"].strip():
        doc.add_paragraph(f"参考价格：{row['参考价格(元)'].strip()} 元（活动/参考价，以京东实时结算为准）")

    if row["主要参数与卖点"].strip():
        doc.add_heading("主要参数与卖点", level=2)
        for item in row["主要参数与卖点"].split("；"):
            item = item.strip()
            if item:
                doc.add_paragraph(item, style="List Bullet")

    if row["来源"].strip():
        doc.add_heading("参考来源", level=2)
        doc.add_paragraph(row["来源"].strip())

    doc.save(str(out_path))
    return True


def main():
    parser = argparse.ArgumentParser(description="京东智能家具商品清单 → docx 知识文档")
    parser.add_argument("--limit", type=int, default=50, help="最多生成多少份 docx（默认 50）")
    args = parser.parse_args()

    with open(TSV_PATH, encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    print(f"清单共 {len(rows)} 条（目标 {args.limit}）")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    created = skipped = 0
    for i, row in enumerate(rows[: args.limit], start=1):
        out = OUTPUT_DIR / f"{i:03d}_{sanitize_filename(row['商品名称'])}.docx"
        if build_docx(row, out):
            created += 1
        else:
            skipped += 1

    print(f"完成: 生成 {created} 份 docx, 跳过 {skipped} 条(缺商品名称), 输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
