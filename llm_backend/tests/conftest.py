"""pytest 公共 fixtures:确保 app 可导入,提供测试数据清理。"""
import asyncio
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

if sys.platform == "win32":
    # psycopg 异步驱动不支持 Windows 默认的 ProactorEventLoop。
    # 必须在 pytest-asyncio 创建测试事件循环前切换策略(database.py 的
    # 设置在测试循环创建后才生效,迟于此)。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

LLM_BACKEND = str(Path(__file__).resolve().parent.parent)
# ⚠️ 必须【无条件移到最前】,不能写成 `if LLM_BACKEND not in sys.path: insert(0, ...)`:
# 从仓库根跑 `python -m pytest` 时,CWD(D:\SmartCS-Agent)排在 sys.path[0],而根目录另有一个
# 无关的 uv 脚手架 main.py(只有 `def main()`,没有 `app`)。若 llm_backend 已在 sys.path 的
# 靠后位置,那个 `if` 就什么都不做 → `from main import app` 命中根目录的 stub →
# ImportError: cannot import name 'app' from 'main'。
# 症状很有迷惑性:与 test_admin_auth.py / test_documents_api.py(模块顶层 import main)
# 同会话跑就"碰巧能过"(正确的 main 已进 sys.modules),单独跑则必挂。
if LLM_BACKEND in sys.path:
    sys.path.remove(LLM_BACKEND)
sys.path.insert(0, LLM_BACKEND)



@pytest.fixture
def test_user_id() -> str:
    """每个测试独立的 user_id,避免与生产数据/测试间互扰。"""
    return f"test_{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def cleanup_test_data(test_user_id):
    """测试结束删除该 user 的 documents/chunks(含失败注入的残留)。"""
    yield
    from sqlalchemy import delete
    from app.core.database import AsyncSessionLocal
    from app.models.document import Document
    from app.models.document_chunk import DocumentChunk

    async with AsyncSessionLocal() as s:
        await s.execute(delete(DocumentChunk).where(DocumentChunk.user_id == test_user_id))
        await s.execute(delete(Document).where(Document.user_id == test_user_id))
        await s.commit()


# ==================== 管理端:临时账号与令牌 ====================

TEST_PASSWORD = "TestAdmin123"


@asynccontextmanager
async def _temp_user(role: str):
    """建一个临时账号,退出时删除。明文密码 TEST_PASSWORD(前端传明文,见 spec §2.3B)。"""
    from sqlalchemy import delete

    from app.core.database import AsyncSessionLocal
    from app.core.hashing import get_password_hash
    from app.models.user import User

    email = f"{role}_{uuid.uuid4().hex[:8]}@test.com"
    async with AsyncSessionLocal() as s:
        u = User(username=email.split("@")[0], email=email,
                 password_hash=get_password_hash(TEST_PASSWORD), role=role)
        s.add(u)
        await s.commit()
        await s.refresh(u)
        uid = u.id
    try:
        yield {"id": uid, "email": email, "password": TEST_PASSWORD}
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(User).where(User.id == uid))
            await s.commit()


@pytest.fixture
async def admin_user():
    """临时管理员(role='admin')。"""
    async with _temp_user("admin") as u:
        yield u


@pytest.fixture
async def normal_user():
    """临时普通用户(role='user'),用于越权测试。"""
    async with _temp_user("user") as u:
        yield u


def _import_app():
    """确保 `main` 解析到 llm_backend/main.py,而不是仓库根的无关 stub。

    ⚠️ 两个独立的坑,都会让 `from main import app` 拿到错的东西:

    1) 【顶层不能 import main】main.py 末尾的 StaticFiles(directory=frontend/dist) 在目录
       不存在时【构造即抛】RuntimeError。frontend/dist 被 gitignore,没构建过的环境里,
       conftest 顶层 import 会让【整套测试】(含与本次无关的 test_cleaner/test_rrf)
       在 collection 阶段全灭。所以只能函数内导入。

    2) 【sys.path 会在 collection 与用例执行两个阶段之间被重排】(实测:conftest 导入时
       llm_backend 在 sys.path[0],到 fixture 执行时仓库根被重新插到了 [0])。而仓库根有
       一个 uv 脚手架 main.py(只有 `def main()`,没有 `app`)。
       → ImportError: cannot import name 'app' from 'main' (D:\SmartCS-Agent\main.py)
       这个坑还会伪装:与模块顶层 import main 的文件(test_admin_auth / test_documents_api)
       同会话跑就"碰巧能过"(正确的 main 已进 sys.modules),单独跑则必挂。

    所以:导入前当场把路径摆正,并把已缓存的错误 main 清出去。两行都不能省。
    """
    if LLM_BACKEND in sys.path:
        sys.path.remove(LLM_BACKEND)
    sys.path.insert(0, LLM_BACKEND)

    cached = sys.modules.get("main")
    if cached is not None and not str(getattr(cached, "__file__", "")).startswith(LLM_BACKEND):
        del sys.modules["main"]

    import main as main_module
    return main_module.app


async def _login(email: str, password: str) -> str:
    from httpx import ASGITransport, AsyncClient

    app = _import_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/token", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]


@pytest.fixture
async def admin_token(admin_user):
    return await _login(admin_user["email"], admin_user["password"])


@pytest.fixture
async def normal_token(normal_user):
    return await _login(normal_user["email"], normal_user["password"])
