"""商品知识种子导入:遍历 knowledge_data/ 批量入库(幂等,重复文件命中去重)。

用法: python -m scripts.ingest_knowledge [目录] [user_id]
默认目录: llm_backend/knowledge_data/
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.logger import get_logger  # noqa: E402
from app.services.indexing_service import IndexingService  # noqa: E402

logger = get_logger(service="ingest_knowledge")


async def main(directory: str = "", user_id: str = "1") -> None:
    target = Path(directory) if directory else ROOT / "knowledge_data"
    if not target.exists():
        logger.error("目录不存在: {}", target)
        sys.exit(1)

    svc = IndexingService()
    result = await svc.process_directory(str(target), user_id=user_id)
    logger.info("处理完成: {} 个文件", result["processed_files"])
    summary = {"success": 0, "duplicate": 0, "failed": 0}
    for r in result["results"]:
        summary[r.get("status", "failed")] += 1
        if r.get("status") == "failed":
            logger.error("失败: {} → {}: {}",
                         r.get("original_filename", "?"), r.get("error"), r.get("detail"))
    logger.info("汇总: success={} duplicate={} failed={}",
                summary["success"], summary["duplicate"], summary["failed"])


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # 显式目录: python -m scripts.ingest_knowledge <目录> [user_id]
        asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "1"))
    else:
        asyncio.run(main())  # 默认目录 + user_id=1
