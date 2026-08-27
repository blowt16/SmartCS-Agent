"""JDDC 真实客服对话 → 智能家具评测集构建器。

从 JDDC GitHub 镜像（scripts/.cache_jddc/repo/extract_train.json，64k 条真实京东客服对话）
提取智能家具品类 QA 对，输出 ragas 兼容 jsonl（user_input/reference/reference_contexts），
供 python -m evaluation --skip-synthesize --testset-file <文件> 直接评测。

筛选原则（真实客服场景）：
    - 品类词命中（沙发/按摩椅/床/门锁/灯具等，与知识库智能家具品类对齐）
    - 优先"品类+属性"产品知识类问题（规格/价格/售后/保修/退换/安装）
    - 中文占多数、去订单号/占位符噪音、去重、过滤纯安抚话术回答

用法: cd llm_backend && python -m evaluation.jddc_builder [--count 50] [--output ...]
"""
import argparse
import json
import re
import sys
from pathlib import Path

# 允许 python -m evaluation.jddc_builder（llm_backend 下）与直接脚本两种运行方式
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 品类词（与知识库"京东智能家具"对齐：沙发/按摩椅/床/门锁/灯具/晾晒等）
CATEGORY_KEYWORDS = [
    "沙发", "按摩椅", "床垫", "电动床", "智能床", "床", "门锁",
    "台灯", "吊灯", "灯具", "灯", "晾衣架", "衣柜", "桌椅", "桌子",
    "椅子", "柜子", "梳妆台", "床头柜", "窗帘", "卫浴", "马桶",
    "花洒", "浴室柜", "家具",
]

# 属性词（产品知识类问题信号：规格/价格/售后等，优先抽取）
ATTRIBUTE_KEYWORDS = [
    "规格", "尺寸", "价格", "多少钱", "材质", "真皮", "布艺", "电动",
    "功能", "颜色", "售后", "保修", "质保", "退换", "退货", "换货",
    "安装", "送货", "发货", "优惠", "活动", "怎么", "区别", "哪个好",
    "质量", "坏了", "故障", "维修", "没电",
]

# 纯安抚话术开头（参考回答质量差，跳过）
TEMPLATE_OPENERS = (
    "请您稍等", "正在为您核实", "马上为您", "正在帮您查", "请您提供",
    "麻烦您提供", "请问有什么问题", "亲爱的您好", "亲，您好", "亲您好",
)

# JDDC 脱敏占位符
_PLACEHOLDER_RE = re.compile(r"\[[^\]]*x\]")
# 会话分隔符噪音（答案中混入的 #E-s[数字x] 之类）
_SESSION_SEP_RE = re.compile(r"#[A-Za-z-]+\[[^\]]*x\]")
# 订单号/数字噪音：开头 ≥5 位连续数字（如 "1186710退货1186710台灯按钮不灵敏"）
_ORDER_NO_PREFIX_RE = re.compile(r"^\d{5,}")
# 中文占多数判定
def _is_zh(q: str) -> bool:
    q = q.replace(" ", "")
    if not q:
        return False
    return sum(1 for c in q if "一" <= c <= "鿿") / len(q) >= 0.6


def clean_question(q: str) -> str:
    """清洗 JDDC 问题文本：去占位符、去开头订单号/非汉字字符、去首尾空白。"""
    q = _PLACEHOLDER_RE.sub("", q)
    q = _ORDER_NO_PREFIX_RE.sub("", q)
    q = q.lstrip("*×xX·,，。. 　\t")
    return q.strip("。？?！! 　,，").strip()


def clean_answer(a: str) -> str:
    """清洗客服回答：去会话分隔符与脱敏占位符噪音。"""
    a = _SESSION_SEP_RE.sub("", a)
    a = _PLACEHOLDER_RE.sub("", a)
    return a.strip()


def load_jddc_data() -> list:
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "scripts/.cache_jddc/repo/extract_train.json"
    )
    if not src.exists():
        raise RuntimeError(
            f"JDDC 数据不存在: {src}（请先用 git -c http.sslBackend=schannel clone "
            "https://github.com/zhangbo2008/JDDC_for_train_gpt_data.git "
            "scripts/.cache_jddc/repo）"
        )
    with open(src, encoding="utf-8") as f:
        return json.load(f)


def extract_qa_pairs(data: list) -> list:
    """展平全部对话为 (question, answer, dialogue_id) 三元组（含 history 轮次）。"""
    pairs = []
    for idx, d in enumerate(data):
        qas = []
        for h in d.get("history") or []:
            if isinstance(h, (list, tuple)) and len(h) >= 2:
                qas.append((str(h[0]), str(h[1])))
        qas.append((str(d.get("prompt", "")), str(d.get("response", ""))))
        for q, a in qas:
            if q and a:
                pairs.append((q, a, idx))
    return pairs


def build_testset(count: int, output: Path) -> list:
    data = load_jddc_data()
    pairs = extract_qa_pairs(data)

    def score(q: str) -> int:
        """排序分：品类+属性 2 分 > 品类 1 分；同分按问答长度质量微调。"""
        return (2 if any(k in q for k in ATTRIBUTE_KEYWORDS) else 1)

    candidates = []
    seen_q = set()
    for q_raw, a_raw, idx in pairs:
        q = clean_question(q_raw)
        a = clean_answer(a_raw)
        if not (any(k in q for k in CATEGORY_KEYWORDS)):
            continue
        if not _is_zh(q) or not (5 <= len(q) <= 60):
            continue
        if a.startswith(TEMPLATE_OPENERS) or len(a) < 10:
            continue
        key = q
        if key in seen_q:
            continue
        seen_q.add(key)
        candidates.append(
            {"_score": score(q), "user_input": q, "reference": a, "dialogue_id": idx}
        )

    candidates.sort(key=lambda r: (-r["_score"], -len(r["reference"])))
    selected = candidates[:count]

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        for r in selected:
            row = {
                "user_input": r["user_input"],
                "reference": r["reference"],
                "reference_contexts": [],  # 占位：后续按知识库检索补全或手动填写
                "source": "jddc",
                "dialogue_id": r["dialogue_id"],
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"[完成] 候选 {len(candidates)} 条 → 抽取 {len(selected)} 条 → {output}")
    return selected


def main():
    p = argparse.ArgumentParser(description="JDDC 智能家具真实客服 QA 评测集构建")
    p.add_argument("--count", type=int, default=50, help="抽取条数（默认 50）")
    p.add_argument(
        "--output", type=str,
        default=str(Path(__file__).resolve().parent / "testsets" / "testset_jddc_50.jsonl"),
        help="输出 jsonl 路径",
    )
    args = p.parse_args()
    build_testset(args.count, Path(args.output))


if __name__ == "__main__":
    main()
