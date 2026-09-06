"""为 jd_smart_furniture.tsv 缺失 sku 的商品行回填商品编码(一次性工具,幂等可重复执行)。

设计约束(见 spec_plan/未完成/SPEC_SKU_ALIGNMENT.md §4.1.3):
- 编码格式 `JD-{CLASS}-{NNN}`: 3 位品类前缀 + 品类内 3 位序号(001 起)
- fill-if-missing: 已有合法 sku 的行永不重写;新行按品类内 max(序号)+1 续编
- **写回行序纪律**: 只逐行原位补 sku 值,禁止整表排序/重排——TSV 行序驱动
  import_product_price_stock.assign_stock"品类第一款=0/少量"库存规则,重排即漂移
- 编码一经固化禁改禁重排(新增商品只能走本工具续编)

用法:
  python scripts/add_sku_column.py            # dry-run:打印待填行与拟定编码
  python scripts/add_sku_column.py --write    # 写回 TSV(推荐先看 dry-run)
"""
import argparse
import csv
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TSV_PATH = PROJECT_ROOT / "scripts" / "data" / "jd_smart_furniture.tsv"

COL_CATEGORY, COL_NAME, COL_SKU = "品类", "商品名称", "sku"

SKU_RE = re.compile(r"^JD-[A-Z]{3}-\d{3}$")

# 品类 → 3 位前缀(与 SPEC_SKU_ALIGNMENT §4.1.2 映射表一致,新增品类需同步扩展)
CLASS_MAP = {
    "电动智能沙发": "SOF",
    "电动升降桌": "DSK",
    "智能门锁": "LCK",
    "智能电动床": "BED",
    "智能床垫": "MTR",
    "智能床头柜": "NST",
    "智能窗帘": "CUR",
    "智能晾衣架": "DRY",
    "按摩椅": "MAS",
}


def load_rows(path: Path) -> tuple[list[dict], list[str]]:
    """读 TSV 返回 (rows, fieldnames);缺 sku 列时抛错提示先加表头。"""
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    if COL_SKU not in fieldnames:
        raise SystemExit(f"TSV 缺 {COL_SKU} 列(当前表头: {fieldnames});请先手工追加表头 {COL_SKU} 后重试")
    return rows, fieldnames


def next_codes(category: str, existing: list[str], count: int) -> list[str]:
    """品类内从 max(序号)+1 起顺次续编 count 个;类别未知/序号越界即报错(保守防错)。"""
    prefix = CLASS_MAP.get(category)
    if prefix is None:
        raise SystemExit(f"未知品类「{category}」,CLASS_MAP 需同步扩展")
    nnn_max = 0
    for code in existing:
        m = SKU_RE.match(code)
        if not m:
            raise SystemExit(f"既有 sku 格式非法(未走本工具固化?): {category} = {code}")
        if code.split("-")[1] != prefix:
            raise SystemExit(f"既有 sku 品类前缀与映射不符: {category} 含 {code}(期望 {prefix})")
        nnn_max = max(nnn_max, int(code.split("-")[2]))
    codes = []
    for i in range(1, count + 1):
        n = nnn_max + i
        if n > 999:
            raise SystemExit(f"品类 {category} 序号越界(>999),需调整编码方案")
        codes.append(f"JD-{prefix}-{n:03d}")
    return codes


def main():
    parser = argparse.ArgumentParser(description="TSV sku 回填生成器(dry-run 默认)")
    parser.add_argument("--write", action="store_true", help="写回 TSV(默认仅打印拟定编码)")
    args = parser.parse_args()

    rows, fieldnames = load_rows(TSV_PATH)

    # 按行序分离:已有合法 sku / 待填(品类内按商品名称升序,保证分配确定性)
    existing: dict[str, list[str]] = {}
    todo: list[tuple[int, dict]] = []
    for idx, row in enumerate(rows):
        sku = (row.get(COL_SKU) or "").strip()
        name = (row.get(COL_NAME) or "").strip()
        if not name:
            continue
        if sku:
            existing.setdefault(row[COL_CATEGORY].strip(), []).append(sku)
        else:
            todo.append((idx, row))

    planned: dict[int, str] = {}
    if todo:
        by_cat: dict[str, list[tuple[int, dict]]] = {}
        for idx, row in todo:
            by_cat.setdefault(row[COL_CATEGORY].strip(), []).append((idx, row))
        for cat, items in sorted(by_cat.items()):
            items.sort(key=lambda p: p[1][COL_NAME].strip())  # 仅决定分配顺序,不影响写回行序
            for (idx, row), code in zip(
                items, next_codes(cat, existing.get(cat, []), len(items))
            ):
                planned[idx] = code

    if not planned:
        print(f"无缺失行(共 {len(rows)} 行,sku 全覆盖)——幂等确认,无需写回")
        return 0

    print(f"待填 {len(planned)} 行:")
    for idx, code in sorted(planned.items()):
        row = rows[idx]
        print(f"  行{idx + 2:<4} {code}  {row[COL_CATEGORY].strip()}  {row[COL_NAME].strip()[:36]}")

    if not args.write:
        print("(dry-run 未写回;确认后加 --write)")
        return 0

    for idx, code in planned.items():
        rows[idx][COL_SKU] = code  # 原位补值,行序不变
    with open(TSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)

    # 写回后自检
    skus = [(r.get(COL_SKU) or "").strip() for r in rows]
    assert all(skus), "存在空 sku(自检失败)"
    assert all(SKU_RE.match(s) for s in skus), "存在格式非法 sku(自检失败)"
    assert len(set(skus)) == len(skus), "sku 重复(自检失败)"
    print(f"写回完成: {len(planned)} 行已补 sku,共 {len(rows)} 行(行序未变)。请 git diff 审阅后提交固化。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
