"""管理端鉴权测试(SPEC_ADMIN_CONSOLE §8.2 / §5.1 三态语义)。

越权是本模块的主要风险:普通用户令牌命中任一 /api/admin/* 端点必须 403。
"""
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# ⚠️ main 必须在【collection 阶段】导入,不能只写成函数内 import:
# 从仓库根跑 pytest 时,根目录另有一个无关的 D:\SmartCS-Agent\main.py,用例执行阶段
# 它排在 sys.path[0],此时函数内 `from main import app` 会命中错文件(实测报
# ImportError: cannot import name 'app' from 'main'),连 conftest 的 _login 也会栽在
# 同一处。这里先把 llm_backend 提到最前导入一次,正确的 main 进 sys.modules 后,
# 后续 conftest 与本文件的 import 全都命中缓存。
# 代价:main.py 末尾的 StaticFiles(frontend/dist) 在 dist 缺失时构造即抛 RuntimeError,
# 那样本文件会 collection 报错——不过同一环境下 conftest 的令牌 fixture 本来也不可用。
_BACKEND = str(Path(__file__).resolve().parent.parent)
if _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

from main import app  # noqa: E402

# 5 个管理端模块各取 1 个端点(组级依赖一条声明覆盖全组,故 5 个抽查点应行为一致)
ADMIN_ENDPOINTS = [
    "/api/admin/console/stats",
    "/api/admin/products",
    "/api/admin/orders",
    "/api/admin/knowledge",
    "/api/admin/tickets",
]


@asynccontextmanager
async def _client():
    """ASGI 直连(不起 uvicorn)。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_admin_endpoint_without_token_401():
    async with _client() as c:
        r = await c.get("/api/admin/console/stats")
    assert r.status_code == 401


async def test_admin_endpoint_forged_token_401():
    async with _client() as c:
        r = await c.get("/api/admin/console/stats", headers=_bearer("invalid.token.here"))
    assert r.status_code == 401


@pytest.mark.parametrize("endpoint", ADMIN_ENDPOINTS)
async def test_normal_user_forbidden_403(endpoint, normal_token):
    """普通用户令牌访问管理端 5 个模块 → 403(不是 401,也不是 500)。"""
    async with _client() as c:
        r = await c.get(endpoint, headers=_bearer(normal_token))
    assert r.status_code == 403, f"{endpoint} 期望 403,实际 {r.status_code}: {r.text}"
    assert "需要管理员权限" in r.json()["detail"]


async def test_normal_user_can_still_access_users_me(normal_token):
    """既有端点不受管理端鉴权影响。"""
    async with _client() as c:
        r = await c.get("/api/users/me", headers=_bearer(normal_token))
    assert r.status_code == 200


async def test_admin_token_can_access_console_stats(admin_token):
    async with _client() as c:
        r = await c.get("/api/admin/console/stats", headers=_bearer(admin_token))
    assert r.status_code == 200
    assert "products" in r.json()


async def _post_token(c: AsyncClient, user: dict) -> dict:
    r = await c.post("/api/token", json={"email": user["email"], "password": user["password"]})
    assert r.status_code == 200, r.text
    return r.json()


async def test_token_response_contains_role(admin_user, normal_user):
    """POST /api/token 的响应体带 role,前端据此决定进哪个端。"""
    async with _client() as c:
        assert (await _post_token(c, admin_user))["role"] == "admin"
        assert (await _post_token(c, normal_user))["role"] == "user"


async def test_users_me_response_contains_role(admin_user, normal_user):
    async with _client() as c:
        for user, role in ((admin_user, "admin"), (normal_user, "user")):
            token = (await _post_token(c, user))["access_token"]
            r = await c.get("/api/users/me", headers=_bearer(token))
            assert r.status_code == 200
            assert r.json()["role"] == role
