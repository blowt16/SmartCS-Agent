"""
手动构建 GraphRAG 索引。

用法：python scripts/build_graphrag_index.py

说明：
- 使用 llm_backend/app/graphrag/settings.yaml 配置
- 对 data/input/ 目录下的所有文件构建索引
- 输出到 data/output/ 目录
"""

import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv

import graphrag.api as api
from graphrag.config.load_config import load_config
from graphrag.config.enums import IndexingMethod
from graphrag.logger.rich_progress import RichProgressLogger

# 路径配置
ROOT_DIR = Path(__file__).parent.parent
GRAPHRAG_DIR = ROOT_DIR / "llm_backend" / "app" / "graphrag"
DATA_DIR = GRAPHRAG_DIR / "data"
SETTINGS_FILE = DATA_DIR / "settings.yaml"  # 实际索引用的是 data/settings.yaml
ENV_FILE = ROOT_DIR / "llm_backend" / ".env"

# 加载 .env 环境变量（settings.yaml 中 ${...} 引用需要）
load_dotenv(ENV_FILE)


async def build_index():
    """构建 GraphRAG 索引"""

    print("=" * 60)
    print("GraphRAG 索引构建")
    print("=" * 60)

    # 检查配置文件
    if not SETTINGS_FILE.exists():
        print(f"错误：找不到配置文件 {SETTINGS_FILE}")
        return

    # 检查输入目录
    input_dir = DATA_DIR / "input"
    if not input_dir.exists():
        print(f"错误：找不到输入目录 {input_dir}")
        return

    # 统计输入文件
    file_count = 0
    for root, dirs, files in os.walk(input_dir):
        for f in files:
            if f.endswith(('.txt', '.csv', '.pdf')):
                file_count += 1

    print(f"\n配置文件：{SETTINGS_FILE}")
    print(f"输入目录：{input_dir}")
    print(f"待处理文件数：{file_count}")
    print("\n开始构建索引...（这可能需要几分钟到几十分钟，取决于文件数量）")

    # 加载配置（root_dir 用 data 目录，因为 settings.yaml 里 base_dir 是相对路径）
    config = load_config(
        DATA_DIR,
        SETTINGS_FILE,
        None
    )

    # 构建索引
    try:
        result = await api.build_index(
            config=config,
            method=IndexingMethod.Standard,
            is_update_run=False,  # 全量重建
            memory_profile=False,
            progress_logger=RichProgressLogger(prefix="graphrag-index")
        )

        print("\n" + "=" * 60)
        print("索引构建完成！")
        print("=" * 60)

        # 输出结果统计
        if hasattr(result, 'stats'):
            print(f"统计信息：{result.stats}")

        # 检查输出目录
        output_dir = DATA_DIR / "output"
        if output_dir.exists():
            parquet_files = list(output_dir.glob("*.parquet"))
            print(f"\n生成的 Parquet 文件：{len(parquet_files)} 个")
            for pf in parquet_files:
                print(f"  - {pf.name}")

    except Exception as e:
        print(f"\n索引构建失败：{e}")
        raise


if __name__ == "__main__":
    asyncio.run(build_index())