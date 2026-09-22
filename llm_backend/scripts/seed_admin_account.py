"""确保管理员账号存在且 role='admin'(幂等,可重复执行)。

用法:
  python scripts/seed_admin_account.py

⚠️ get_password_hash 接受的是【明文】——hashing.py 的 docstring 说"前端已做过 SHA256"
是过期的,前端实际传明文(见 SPEC_ADMIN_CONSOLE §2.3B)。绝不能先算一遍 SHA256,
那样这个账号永远登不上。
"""
import asyncio
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent          # llm_backend
sys.path.insert(0, str(ROOT_DIR))
import app.core.database  # noqa: E402 —— Windows Selector 事件循环补丁

from sqlalchemy import select  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.hashing import get_password_hash  # noqa: E402
from app.core.logger import get_logger  # noqa: E402
from app.models.user import User  # noqa: E402

logger = get_logger(service="seed_admin_account")

ADMIN_EMAIL = "admin_test@test.com"
ADMIN_USERNAME = "admin_test"
ADMIN_PASSWORD = "admin"


async def main() -> int:
    async with AsyncSessionLocal() as s:
        user = (await s.execute(
            select(User).where(User.email == ADMIN_EMAIL)
        )).scalar_one_or_none()

        if user is not None:
            # 已存在:只改 role,不碰 password_hash —— 避免把人已经用顺手的密码改掉
            user.role = "admin"
            await s.commit()
            logger.info("管理员账号已存在,已确保 role='admin'")
            logger.info("email={} role={} id={} (密码未改动，沿用原密码)",
                        user.email, user.role, user.id)
            return 0

        user = User(
            username=ADMIN_USERNAME,
            email=ADMIN_EMAIL,
            password_hash=get_password_hash(ADMIN_PASSWORD),   # 明文,不是 SHA256
            role="admin",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        logger.info("管理员账号已新建")
        logger.info("email={} role={} id={} password={}",
                    user.email, user.role, user.id, ADMIN_PASSWORD)
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
