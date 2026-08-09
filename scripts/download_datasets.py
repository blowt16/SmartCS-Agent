"""
下载并清洗中文电商客服数据集，输出适合 GraphRAG 的纯文本文件。

数据来源:
1. qgyd2021/e_commerce_customer_service (faq.json - 电商客服问答)
2. OpenStellarTeam/Chinese-EcomQA (中文电商概念问答)

输出目录: llm_backend/app/graphrag/data/input/ecommerce_faq/
每个 txt 文件不超过 ~5000 字符，包含清晰的 Q&A 格式。
"""

import json
import os
import re
import sys
import time
from pathlib import Path

# ── 路径配置 ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "llm_backend" / "app" / "graphrag" / "data" / "input" / "ecommerce_faq"

MAX_CHARS_PER_FILE = 5000  # 每个 txt 文件的最大字符数


def ensure_datasets_library():
    """确保 datasets 库已安装。"""
    try:
        import datasets
        return datasets
    except ImportError:
        print("[INFO] 正在安装 datasets 库...")
        import subprocess
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "datasets", "-q"
        ])
        import datasets
        return datasets


def download_with_retry(repo_id, filename=None, repo_type="dataset", max_retries=3):
    """
    带重试的数据集下载，应对 HuggingFace 的 429 限流。

    参数:
        repo_id: HuggingFace 数据集 ID
        filename: 要下载的特定文件名（None 则下载整个数据集）
        repo_type: 仓库类型，默认 dataset
        max_retries: 最大重试次数

    返回:
        下载到本地的文件路径
    """
    from huggingface_hub import hf_hub_download

    for attempt in range(max_retries):
        try:
            if filename:
                path = hf_hub_download(
                    repo_id=repo_id,
                    filename=filename,
                    repo_type=repo_type,
                )
            else:
                path = hf_hub_download(
                    repo_id=repo_id,
                    repo_type=repo_type,
                )
            return path
        except Exception as e:
            if "429" in str(e) or "Too Many Requests" in str(e):
                wait = 10 * (attempt + 1)
                print(f"[WARN] HuggingFace 限流，等待 {wait}s 后重试 ({attempt + 1}/{max_retries})...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"下载 {repo_id}/{filename} 失败，已重试 {max_retries} 次")


def load_faq_dataset():
    """
    加载 qgyd2021/e_commerce_customer_service 的 FAQ 数据。

    这个数据集的 faq.json 是一个 JSON 数组，每个元素是问答对，
    通常包含 question / answer 或类似字段。

    返回:
        list[dict]: FAQ 问答对列表
    """
    print("=" * 60)
    print("[1/2] 下载电商客服 FAQ 数据集 (qgyd2021/e_commerce_customer_service)")
    print("=" * 60)

    from datasets import load_dataset

    # 尝试用 load_dataset API 直接加载
    try:
        print("[INFO] 尝试通过 datasets.load_dataset 加载...")
        ds = load_dataset("qgyd2021/e_commerce_customer_service", split="train", trust_remote_code=True)
        records = list(ds)
        print(f"[OK] 成功加载 {len(records)} 条记录")
        if records:
            print(f"[INFO] 字段: {list(records[0].keys())}")
            print(f"[INFO] 示例: {str(records[0])[:300]}")
        return records
    except Exception as e:
        print(f"[WARN] load_dataset 失败: {e}")
        print("[INFO] 尝试直接下载 faq.json...")

    # 备用方案：直接下载原始文件
    try:
        faq_path = download_with_retry(
            "qgyd2021/e_commerce_customer_service",
            filename="data/faq.json"
        )
        with open(faq_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            print(f"[OK] 从 faq.json 加载 {len(data)} 条记录")
            if data:
                print(f"[INFO] 字段: {list(data[0].keys()) if isinstance(data[0], dict) else type(data[0])}")
            return data
        elif isinstance(data, dict):
            print(f"[INFO] 字典结构，键: {list(data.keys())}")
            records = []
            for k, v in data.items():
                if isinstance(v, list):
                    records.extend(v)
            print(f"[OK] 从 faq.json 展开得到 {len(records)} 条记录")
            return records
    except Exception as e:
        print(f"[WARN] 直接下载也失败: {e}")

    return []


def load_ecomqa_dataset():
    """
    加载 OpenStellarTeam/Chinese-EcomQA 数据集。

    JSONL 格式，每行包含:
      - system_prompt: 任务描述（含示例）
      - prompt: 具体问题（格式: ***query***：...***答案***：）
      - gt: 参考答案（ground truth）
      - task: 任务类别代码（如 BC=品牌知识, PS=产品规格等）

    返回:
        list[dict]: 问答对列表
    """
    print()
    print("=" * 60)
    print("[2/2] 下载中文电商概念问答数据集 (OpenStellarTeam/Chinese-EcomQA)")
    print("=" * 60)

    from datasets import load_dataset

    try:
        print("[INFO] 尝试通过 datasets.load_dataset 加载...")
        ds = load_dataset("OpenStellarTeam/Chinese-EcomQA", split="train")
        records = list(ds)
        print(f"[OK] 成功加载 {len(records)} 条记录")
        if records:
            print(f"[INFO] 字段: {list(records[0].keys())}")
            sample = records[0]
            prompt_text = sample.get("prompt", "")
            print(f"[INFO] prompt 示例: {prompt_text[:200]}")
            print(f"[INFO] gt 示例: {sample.get('gt', '')[:200]}")
            print(f"[INFO] task 示例: {sample.get('task', '')}")
        return records
    except Exception as e:
        print(f"[WARN] load_dataset 失败: {e}")
        print("[INFO] 尝试直接下载 ChineseEcomQA.jsonl...")

    try:
        jsonl_path = download_with_retry(
            "OpenStellarTeam/Chinese-EcomQA",
            filename="ChineseEcomQA.jsonl"
        )
        records = []
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        print(f"[OK] 从 JSONL 加载 {len(records)} 条记录")
        return records
    except Exception as e:
        print(f"[ERROR] Chinese-EcomQA 下载也失败: {e}")
        return []


# ── 数据清洗函数 ──────────────────────────────────────────

def clean_text(text):
    """清理文本中的多余空白和特殊标记。"""
    if not text:
        return ""
    # 去掉 ***对话偏好*** 等标记（PC 类别的用户画像前缀）
    text = re.sub(r"\*{3,}对话偏好\*{3,}[：:]", "", text)
    # 去掉 ***query*** 和 ***答案*** 标记
    text = re.sub(r"\*{3,}query\*{3,}[：:]", "", text)
    text = re.sub(r"\*{3,}答案\*{3,}[：:]?", "", text)
    # 去掉其他 ***xxx*** 格式的标记
    text = re.sub(r"\*{3,}[^*]+\*{3,}[：:]?", "", text)
    # 去掉多余星号
    text = re.sub(r"\*{2,}", "", text)
    # 压缩连续空白
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text


def extract_core_question(prompt, task=""):
    """
    从 prompt 中提取核心问题文本。

    PC 类别的 prompt 包含很长的用户画像 JSON + 商品列表，
    我们只保留有价值的上下文信息，截断过长的 JSON 部分。
    """
    # 去掉标记符号
    text = clean_text(prompt)

    # 对于 PC（个性化推荐）类别，prompt 可能包含用户画像 JSON
    if task == "PC" and len(text) > 800:
        # PC 类别的 gt 通常是简短的选项（如 A/B/C/D），
        # prompt 中的用户画像 JSON 对 GraphRAG 价值不大，
        # 保留前 500 字符作为摘要即可
        text = text[:500] + "...(用户画像数据已截断)"

    return text


def extract_qa_from_faq(records):
    """
    从 FAQ 数据集提取问答对。
    自动适配不同的字段名称。

    返回:
        list[tuple(str, str)]: (question, answer) 列表
    """
    qa_pairs = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        # 常见字段名: question/问题/q, answer/回答/ans/a
        q = (rec.get("question") or rec.get("问题") or rec.get("q")
             or rec.get("query") or rec.get("prompt") or "")
        a = (rec.get("answer") or rec.get("回答") or rec.get("ans") or rec.get("a")
             or rec.get("response") or rec.get("gt") or "")

        # 如果没有标准字段，尝试用第一个和第二个值
        if not q and not a:
            values = list(rec.values())
            if len(values) >= 2:
                q, a = str(values[0]), str(values[1])

        q = clean_text(str(q))
        a = clean_text(str(a))
        if q and a:
            qa_pairs.append((q, a))
    return qa_pairs


def extract_qa_from_ecomqa(records):
    """
    从 Chinese-EcomQA 数据集提取问答对。
    prompt 字段包含问题（带标记），gt 字段是参考答案。

    返回:
        list[tuple(str, str, str)]: (question, answer, task_category) 列表
    """
    qa_pairs = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        prompt = rec.get("prompt", "")
        gt = rec.get("gt", "")
        task = rec.get("task", "")

        # 从 prompt 中提取问题文本（去掉标记，处理超长内容）
        question = extract_core_question(prompt, task)
        answer = clean_text(str(gt))
        if question and answer:
            qa_pairs.append((question, answer, task))
    return qa_pairs


# ── 任务类别映射 ──────────────────────────────────────────

TASK_LABELS = {
    "BC": "品牌知识",
    "RLC": "商品推荐列表",
    "IC": "商品分类",
    "SC": "搜索推荐",
    "AC": "属性对比",
    "CC": "概念对比",
    "PC": "个性化推荐",
    "IDC": "行业分类",
    "ITC": "意图分类",
    "RVC": "商品评价",
}


# ── 文件输出函数 ──────────────────────────────────────────

def format_faq_block(q, a):
    """格式化单个 FAQ 问答对为文本块。"""
    return f"问题：{q}\n答案：{a}"


def format_ecomqa_block(q, a, task=""):
    """格式化 Chinese-EcomQA 问答对为文本块。"""
    task_label = TASK_LABELS.get(task, task)
    header = f"【{task_label}】" if task_label else ""
    return f"{header}\n问题：{q}\n答案：{a}"


def write_txt_files(blocks, prefix, output_dir):
    """
    将文本块列表写入多个 txt 文件，每个文件不超过 MAX_CHARS_PER_FILE。

    参数:
        blocks: list[str] 文本块列表
        prefix: 文件名前缀
        output_dir: 输出目录 Path

    返回:
        int: 写入的文件数量
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    file_index = 1
    current_content = ""
    files_written = 0

    for i, block in enumerate(blocks):
        # 单个 block 就超过限制时，截断它
        if len(block) > MAX_CHARS_PER_FILE:
            block = block[:MAX_CHARS_PER_FILE - 50] + "\n\n...(内容已截断)"

        # 计算添加这个 block 后的内容长度
        separator = "\n\n" + "=" * 50 + "\n\n" if current_content else ""
        new_content = current_content + separator + block

        if len(new_content) > MAX_CHARS_PER_FILE and current_content:
            # 当前文件已满，先写入
            filename = f"{prefix}_{file_index:03d}.txt"
            filepath = output_dir / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(current_content)
            files_written += 1
            print(f"  -> {filename} ({len(current_content)} 字符)")

            # 开始新文件
            file_index += 1
            current_content = block
        else:
            current_content = new_content

    # 写入最后一个文件
    if current_content:
        filename = f"{prefix}_{file_index:03d}.txt"
        filepath = output_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(current_content)
        files_written += 1
        print(f"  -> {filename} ({len(current_content)} 字符)")

    return files_written


# ── 主流程 ────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  电商客服数据集下载 & 清洗工具")
    print("  输出目录:", OUTPUT_DIR)
    print("=" * 60)

    # 确保依赖
    ensure_datasets_library()

    total_files = 0
    total_qa = 0

    # ── 数据集 1: 电商客服 FAQ ──
    try:
        faq_records = load_faq_dataset()
        if faq_records:
            qa_pairs = extract_qa_from_faq(faq_records)
            print(f"\n[INFO] 从 FAQ 数据集提取了 {len(qa_pairs)} 个问答对")
            if qa_pairs:
                print(f"[INFO] 示例 Q: {qa_pairs[0][0][:80]}")
                print(f"[INFO] 示例 A: {qa_pairs[0][1][:80]}")

            blocks = [format_faq_block(q, a) for q, a in qa_pairs]
            count = write_txt_files(blocks, "faq_ecommerce", OUTPUT_DIR)
            total_files += count
            total_qa += len(qa_pairs)
        else:
            print("[WARN] FAQ 数据集为空")
    except Exception as e:
        print(f"[ERROR] 处理 FAQ 数据集时出错: {e}")

    # ── 数据集 2: Chinese-EcomQA ──
    try:
        ecomqa_records = load_ecomqa_dataset()
        if ecomqa_records:
            qa_pairs = extract_qa_from_ecomqa(ecomqa_records)
            print(f"\n[INFO] 从 Chinese-EcomQA 提取了 {len(qa_pairs)} 个问答对")
            if qa_pairs:
                print(f"[INFO] 示例 Q: {qa_pairs[0][0][:80]}")
                print(f"[INFO] 示例 A: {qa_pairs[0][1][:80]}")
                print(f"[INFO] 类别: {qa_pairs[0][2]}")

            blocks = [format_ecomqa_block(q, a, t) for q, a, t in qa_pairs]
            count = write_txt_files(blocks, "ecomqa_concept", OUTPUT_DIR)
            total_files += count
            total_qa += len(qa_pairs)
        else:
            print("[WARN] Chinese-EcomQA 数据集为空")
    except Exception as e:
        print(f"[ERROR] 处理 Chinese-EcomQA 数据集时出错: {e}")

    # ── 汇总 ──
    print()
    print("=" * 60)
    print(f"  完成！共生成 {total_files} 个文件，包含 {total_qa} 个问答对")
    print(f"  输出目录: {OUTPUT_DIR}")
    print("=" * 60)

    # 列出生成的文件
    if OUTPUT_DIR.exists():
        files = sorted(OUTPUT_DIR.glob("*.txt"))
        if files:
            print("\n生成的文件:")
            for f in files:
                print(f"  {f.name} ({f.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
