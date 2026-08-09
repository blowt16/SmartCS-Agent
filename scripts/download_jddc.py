"""
下载 JDDC (京东电商客服对话语料库) 数据并处理为 GraphRAG 可用的 txt 格式。

数据来源：
1. GitHub zhangbo2008/JDDC_for_train_gpt_data - 已从 JDDC 原始数据提取的 ~12 万条多轮对话
2. HuggingFace qgyd2021/e_commerce_customer_service - 电商 FAQ 问答对

处理流程：
- 下载原始数据
- 按电子/家电/智能家居关键词筛选相关对话
- 提取 Q&A 对，展平多轮对话
- 按文件大小分割，保存为 txt 格式
"""

import json
import os
import sys
import random
import requests
from pathlib import Path

# ============================================================
# 配置
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "llm_backend" / "app" / "graphrag" / "data" / "input" / "customer_service_qa"
CACHE_DIR = PROJECT_ROOT / "scripts" / ".cache_jddc"

# JDDC 数据 GitHub 直链 (约 53MB, 含约 12 万条对话)
JDDC_GITHUB_URL = "https://raw.githubusercontent.com/zhangbo2008/JDDC_for_train_gpt_data/main/extract_train.json"

# 筛选关键词 - 电子/家电/智能家居相关（排除过于通用的电商词汇）
ELECTRONICS_KEYWORDS = [
    # 手机数码
    "手机", "华为", "小米手机", "苹果手机", "iPhone", "三星", "OPPO", "vivo", "荣耀手机",
    "平板", "iPad", "耳机", "充电器", "充电宝", "数据线", "蓝牙耳机",
    "像素", "处理器", "运行内存", "存储空间", "SIM卡",
    # 电脑办公
    "电脑", "笔记本", "键盘", "鼠标", "显示器", "打印机", "硬盘", "内存条",
    "联想", "戴尔", "华硕", "ThinkPad", "显卡", "主板", "CPU",
    # 家电（具体品类名）
    "空调", "冰箱", "洗衣机", "电视机", "热水器", "微波炉", "烤箱",
    "电磁炉", "电饭煲", "吸尘器", "净化器", "加湿器", "电风扇",
    "格力", "美的", "海尔", "松下", "索尼", "海信", "TCL",
    "变频", "制冷", "制热", "除霜", "洗衣", "烘干",
    # 智能家居
    "智能", "扫地机器人", "智能音箱", "摄像头", "智能门锁", "路由器", "WiFi",
    "天猫精灵", "小爱同学", "智能家居", "智能手表", "手环",
    # 电子设备通用
    "电器", "电子", "电池", "电源适配", "屏幕", "触屏",
    "保修卡", "发票", "以旧换新", "安装服务",
    "故障码", "报错", "指示灯", "遥控器", "说明书",
]

# 每个 txt 文件最大字符数（约 5000 中文字符 = ~15000 字节）
MAX_CHARS_PER_FILE = 15000

# 最大下载对话条数
MAX_DIALOGUES = 50000

# 最少选取的对话数
MIN_DIALOGUES = 500


def download_file(url, dest, desc=""):
    """下载文件，带进度显示。"""
    print(f"[下载] {desc or dest.name}")
    print(f"  URL: {url}")
    try:
        resp = requests.get(url, stream=True, timeout=300)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(f"\r  进度: {downloaded / 1024 / 1024:.1f}MB / {total / 1024 / 1024:.1f}MB ({pct:.0f}%)", end="", flush=True)
        print(f"\n  完成: {dest.stat().st_size / 1024 / 1024:.1f}MB")
        return True
    except Exception as e:
        print(f"\n  下载失败: {e}")
        return False


def load_jddc_data(cache_dir):
    """加载 JDDC 对话数据 (从 GitHub 下载)。"""
    jddc_file = cache_dir / "extract_train.json"

    if jddc_file.exists():
        print(f"[缓存] 使用已缓存的 JDDC 数据: {jddc_file}")
    else:
        print("=" * 60)
        print("步骤 1: 下载 JDDC 对话数据 (来自 GitHub)")
        print("=" * 60)
        success = download_file(JDDC_GITHUB_URL, jddc_file, "JDDC 对话数据")
        if not success:
            print("[错误] 无法下载 JDDC 数据")
            return []

    print(f"\n[解析] 加载 JSON 数据...")
    with open(jddc_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"  总对话数: {len(data)}")
    return data


def load_huggingface_faq(cache_dir):
    """从 HuggingFace 加载电商 FAQ 数据。"""
    print("\n[加载] 从 HuggingFace 下载电商 FAQ 数据...")
    try:
        from datasets import load_dataset
        ds = load_dataset("qgyd2021/e_commerce_customer_service", split="train", cache_dir=str(cache_dir / "hf_cache"))
        print(f"  FAQ 条目数: {len(ds)}")
        return list(ds)
    except Exception as e:
        print(f"  HuggingFace 加载失败: {e}")
        return []


def is_electronics_related(text):
    """检查文本是否与电子/家电/智能家居相关。"""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in ELECTRONICS_KEYWORDS)


def dialogue_to_qa_pairs(item):
    """
    将一条多轮对话转换为 Q&A 对列表。
    输入格式: {prompt: str, response: str, history: [[q, a], ...]}
    """
    qa_pairs = []

    # 先处理 history 中的历史对话
    history = item.get("history", [])
    if isinstance(history, list):
        for h in history:
            if isinstance(h, (list, tuple)) and len(h) >= 2:
                q, a = str(h[0]).strip(), str(h[1]).strip()
                if q and a:
                    qa_pairs.append((q, a))

    # 处理当前 prompt/response
    prompt = str(item.get("prompt", "")).strip()
    response = str(item.get("response", "")).strip()
    if prompt and response:
        qa_pairs.append((prompt, response))

    return qa_pairs


def format_qa_as_text(qa_pairs, dialogue_id=""):
    """将 Q&A 对列表格式化为文本。"""
    lines = []
    header = f"=== 客服对话 {dialogue_id} ===" if dialogue_id else "=== 客服对话 ==="
    lines.append(header)
    lines.append("")

    for i, (q, a) in enumerate(qa_pairs, 1):
        lines.append(f"客户: {q}")
        lines.append(f"客服: {a}")
        if i < len(qa_pairs):
            lines.append("")

    lines.append("")
    return "\n".join(lines)


def format_faq_as_text(item, idx=""):
    """将 FAQ 条目格式化为文本。"""
    lines = []
    lines.append(f"=== 电商FAQ {idx} ===")
    lines.append("")

    question = item.get("question", item.get("query", item.get("prompt", "")))
    answer = item.get("answer", item.get("response", item.get("reply", "")))

    if isinstance(question, str) and isinstance(answer, str):
        lines.append(f"问题: {question.strip()}")
        lines.append(f"回答: {answer.strip()}")
    else:
        for key, val in item.items():
            if isinstance(val, str) and val.strip():
                lines.append(f"{key}: {val.strip()}")

    lines.append("")
    return "\n".join(lines)


def split_into_files(texts, output_dir, prefix, max_chars=MAX_CHARS_PER_FILE):
    """将文本列表按大小分割到多个文件中。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    files_created = []
    current_content = ""
    file_index = 1

    for text in texts:
        if len(current_content) + len(text) > max_chars and current_content:
            fname = output_dir / f"{prefix}_{file_index:03d}.txt"
            fname.write_text(current_content, encoding="utf-8")
            files_created.append(fname)
            file_index += 1
            current_content = text
        else:
            current_content += text

    if current_content.strip():
        fname = output_dir / f"{prefix}_{file_index:03d}.txt"
        fname.write_text(current_content, encoding="utf-8")
        files_created.append(fname)

    return files_created


def _session_key(item):
    """
    从对话条目中提取 session 标识。
    用 history 的第一个问题作为 session 的唯一标识。
    如果没有 history，用 prompt 作为标识。
    """
    history = item.get("history", [])
    if history and isinstance(history[0], (list, tuple)) and history[0]:
        return str(history[0][0]).strip()[:80]
    return str(item.get("prompt", "")).strip()[:80]


def process_jddc(data):
    """处理 JDDC 对话数据：筛选电子/家电相关对话，转换为文本。"""
    print("\n" + "=" * 60)
    print("步骤 2: 筛选电子/家电/智能家居相关对话")
    print("=" * 60)

    # 第一轮：按关键词筛选相关对话，并按 session 去重（只保留最长版本）
    session_best = {}  # session_key -> (index, qa_pairs, num_turns)
    for i, item in enumerate(data[:MAX_DIALOGUES]):
        if not isinstance(item, dict):
            continue
        full_text = json.dumps(item, ensure_ascii=False)
        if not is_electronics_related(full_text):
            continue
        qa_pairs = dialogue_to_qa_pairs(item)
        if len(qa_pairs) < 2:
            continue

        key = _session_key(item)
        num_turns = len(qa_pairs)
        if key not in session_best or num_turns > session_best[key][2]:
            session_best[key] = (i, qa_pairs, num_turns)

    matched = [(v[0], v[1]) for v in session_best.values()]
    print(f"  去 session 重后，关键词匹配对话数: {len(matched)}")

    # 如果匹配数不足，随机补充通用对话（同样去重）
    if len(matched) < MIN_DIALOGUES:
        print(f"  匹配数不足 {MIN_DIALOGUES}，随机补充通用电商对话...")
        matched_ids = {m[0] for m in matched}
        matched_keys = {_session_key(data[i]) for i, _ in matched if i < len(data)}
        candidates = []
        seen_keys = set()
        for i, item in enumerate(data[:MAX_DIALOGUES]):
            if not isinstance(item, dict) or i in matched_ids:
                continue
            key = _session_key(item)
            if key in matched_keys or key in seen_keys:
                continue
            qa_pairs = dialogue_to_qa_pairs(item)
            if len(qa_pairs) >= 2:
                candidates.append((i, qa_pairs, len(qa_pairs), key))
                seen_keys.add(key)
        # 每个 session 只取最长版本
        cand_best = {}
        for i, qa, n, key in candidates:
            if key not in cand_best or n > cand_best[key][2]:
                cand_best[key] = (i, qa, n, key)
        cand_list = [(v[0], v[1]) for v in cand_best.values()]
        random.shuffle(cand_list)
        needed = MIN_DIALOGUES - len(matched)
        matched.extend(cand_list[:needed])

    print(f"  最终选取对话数: {len(matched)}")

    # 转换为文本
    texts = []
    for idx, (i, qa_pairs) in enumerate(matched):
        text = format_qa_as_text(qa_pairs, dialogue_id=f"JDDC_{i:06d}")
        texts.append(text)

    return texts


def process_faq(faq_data):
    """处理 FAQ 数据。"""
    print(f"\n[处理] 格式化 {len(faq_data)} 条 FAQ 数据...")
    texts = []
    for i, item in enumerate(faq_data):
        text = format_faq_as_text(item, idx=f"FAQ_{i:04d}")
        if len(text) > 30:
            texts.append(text)
    return texts


def generate_synthetic_dialogues():
    """
    生成一些合成的电商客服对话，作为补充。
    基于真实电商场景的常见问题模板。
    """
    synthetic_qa = [
        # 手机相关
        ("这款手机支持5G网络吗？", "是的，这款手机支持全网通5G网络，兼容移动、联通、电信三大运营商的5G频段。"),
        ("手机保修期多长时间？", "手机整机保修一年，电池、充电器等配件保修六个月。建议您保留好购买凭证以便享受售后服务。"),
        ("华为手机怎么截屏？", "您可以同时按住电源键和音量下键进行截屏，也可以在设置中开启三指下滑截屏功能。"),
        ("手机屏幕碎了能保修吗？", "屏幕碎裂属于人为损坏，不在免费保修范围内。您可以联系售后进行付费维修，或者查看是否购买了碎屏险。"),
        ("手机进水了怎么办？", "请立即关机，不要尝试充电。擦干表面水分后尽快送到售后服务中心检测。进水损坏一般不在保修范围内。"),

        # 家电相关
        ("空调安装是免费的吗？", "购买空调后首次安装是免费的，包含标准安装服务。如果需要加长管道、打孔等额外服务，会产生相应费用。"),
        ("冰箱噪音大怎么回事？", "冰箱运行时会有轻微的压缩机工作声音，属于正常现象。如果噪音明显偏大，建议检查冰箱是否放置平稳，或者联系售后上门检测。"),
        ("洗衣机可以洗羽绒服吗？", "部分滚筒洗衣机有羽绒服洗涤程序，可以轻柔洗涤。但波轮洗衣机一般不建议洗羽绒服，可能导致衣物损坏或洗衣机故障。"),
        ("空调多久需要加一次氟？", "正常情况下空调不需要定期加氟。如果制冷效果变差，可能是制冷剂泄漏，需要售后上门检查并补充。"),
        ("冰箱冷藏室结冰怎么办？", "可能是温控器故障或门封不严导致。建议检查门封条是否完好，不要频繁开关门。如问题持续请联系售后。"),

        # 智能家居
        ("扫地机器人能扫地毯吗？", "大部分扫地机器人可以清扫低矮地毯。对于长毛地毯，建议选择带有自动识别地毯并增压吸力的型号。"),
        ("智能音箱需要连接WiFi吗？", "是的，智能音箱首次使用需要通过手机App连接WiFi网络，之后就可以语音控制使用了。"),
        ("智能门锁没电了怎么办？", "智能门锁一般配有USB应急供电接口，可以用充电宝临时供电开门。同时建议定期检查电池电量提醒。"),
        ("智能摄像头怎么安装？", "智能摄像头安装很简单，插电后下载对应App，按提示连接WiFi即可。建议安装在2-3米高度以获得最佳视角。"),
        ("智能家居设备可以联动吗？", "可以。通过智能音箱或智能家居平台，可以设置场景联动，比如开门自动开灯、温度高自动开空调等。"),

        # 电脑数码
        ("笔记本电脑蓝屏怎么办？", "笔记本电脑蓝屏可能是驱动冲突、系统文件损坏或硬件问题。建议先重启尝试，如反复蓝屏请联系售后进行检测维修。"),
        ("机械键盘和薄膜键盘有什么区别？", "机械键盘每个按键都有独立的机械开关，手感更好、寿命更长，适合游戏和长时间打字。薄膜键盘更安静、价格更低。"),
        ("4K显示器需要什么显卡？", "4K显示器建议搭配中高端独立显卡使用，如NVIDIA RTX 3060及以上型号。如果只是办公看视频，入门级显卡也可以胜任。"),
        ("电脑开不了机怎么回事？", "可能是电源问题、内存松动或主板故障。建议先检查电源线是否插好，尝试重新插拔内存条。如仍无法启动请联系售后。"),

        # 售后通用
        ("商品收到有质量问题怎么处理？", "收到商品后如有质量问题，您可以在签收后7天内申请退换货。请拍照保留证据，联系在线客服为您处理。"),
        ("退货的运费谁承担？", "如果是商品质量问题，运费由卖家承担。如果是个人原因退货，运费需要买家自行承担。建议购买运费险以减少损失。"),
        ("订单发货后多久能到？", "一般情况下，京东自营商品下单后1-3天送达，第三方商家发货时间3-7天不等。您可以在订单详情中查看物流信息。"),
        ("可以开发票吗？", "可以的。下单时在结算页面选择需要发票，支持电子发票和纸质发票。电子发票会发送到您的邮箱。"),
        ("商品支持以旧换新吗？", "部分家电和数码产品支持以旧换新服务，您可以查看商品详情页是否有以旧换新标识，或咨询在线客服了解具体政策。"),

        # 家电安装维修
        ("电视挂墙安装需要额外收费吗？", "电视挂墙安装一般需要额外收取安装费，具体费用根据电视尺寸和墙体材质而定。购买时可以查看是否包含安装服务。"),
        ("热水器使用时需要注意什么？", "使用电热水器时要注意：1.定期检查漏电保护器 2.长时间不用要断电 3.每1-2年清洗一次内胆 4.注意水温不要设置过高。"),
        ("空气净化器的滤芯多久换一次？", "一般建议6-12个月更换一次滤芯，具体取决于使用频率和空气质量。大部分净化器有滤芯寿命提醒功能。"),
        ("洗衣机显示故障代码怎么办？", "不同代码代表不同故障，常见的有E1进水超时、E2排水超时等。建议查看说明书对照代码，或联系售后上门维修。"),
    ]

    texts = []
    for i in range(0, len(synthetic_qa), 4):
        batch = synthetic_qa[i:i + 4]
        lines = [f"=== 合成客服对话 SYNTH_{i // 4:03d} ===", ""]
        for q, a in batch:
            lines.append(f"客户: {q}")
            lines.append(f"客服: {a}")
            lines.append("")
        texts.append("\n".join(lines))

    return texts


def main():
    print("=" * 60)
    print("JDDC 电商客服对话数据下载与处理工具")
    print("=" * 60)

    # 创建目录
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_texts = []

    # --- 来源 1: JDDC 对话数据 (GitHub) ---
    jddc_data = load_jddc_data(CACHE_DIR)
    if jddc_data:
        jddc_texts = process_jddc(jddc_data)
        print(f"\n  JDDC 生成文本段数: {len(jddc_texts)}")
        all_texts.extend(jddc_texts)

    # --- 来源 2: HuggingFace FAQ 数据 ---
    faq_data = load_huggingface_faq(CACHE_DIR)
    if faq_data:
        faq_texts = process_faq(faq_data)
        print(f"  FAQ 生成文本段数: {len(faq_texts)}")
        all_texts.extend(faq_texts)

    # --- 来源 3: 合成对话 (作为补充) ---
    print("\n[补充] 生成合成电商客服对话...")
    synthetic_texts = generate_synthetic_dialogues()
    all_texts.extend(synthetic_texts)

    if not all_texts:
        print("\n[错误] 没有获取到任何数据！")
        sys.exit(1)

    # --- 写入文件 ---
    print(f"\n{'=' * 60}")
    print(f"步骤 3: 写入 txt 文件 (每文件不超过 {MAX_CHARS_PER_FILE} 字符)")
    print(f"{'=' * 60}")

    files = split_into_files(all_texts, OUTPUT_DIR, prefix="jddc_dialogue")

    print(f"\n写入完成！共生成 {len(files)} 个文件:")
    for f in files:
        size_kb = f.stat().st_size / 1024
        print(f"  {f.name} ({size_kb:.1f} KB)")

    print(f"\n输出目录: {OUTPUT_DIR}")
    print(f"总文本段数: {len(all_texts)}")
    print("完成！")


if __name__ == "__main__":
    main()
