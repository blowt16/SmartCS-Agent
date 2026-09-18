"""商品知识种子导入:遍历 knowledge_data/ 批量入库(幂等,重复文件命中去重)。

用法: python -m scripts.ingest_knowledge [目录] [user_id]
默认目录: llm_backend/knowledge_data/
默认归属: user_id=6(admin_test 账号) —— 知识库为平台公共内容,统一挂该账号。

归属必须与查看账号一致: 列表接口 GET /api/documents 按 user_id 过滤,
写错归属则前端知识库面板查不到(检索侧不过滤 user_id,故问答不受影响,
问题仅表现为列表空白 —— 2026-09-18 排查的真实故障即为此,原默认值 "1"
对应旧前端硬编码的 user.id,前端改为登录态动态取 id 后失配)。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.logger import get_logger  # noqa: E402
from app.services.indexing_service import IndexingService  # noqa: E402

logger = get_logger(service="ingest_knowledge")

# 知识库归属账号(admin_test)。注意:重跑本脚本的查重键是 (user_id, md5),
# 若归属与既有数据不一致,不会命中去重而是重复入库,污染检索索引。
DEFAULT_OWNER_ID = "6"


async def main(directory: str = "", user_id: str = DEFAULT_OWNER_ID) -> None:
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
        asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OWNER_ID))
    else:
        asyncio.run(main())  # 默认目录 + DEFAULT_OWNER_ID
