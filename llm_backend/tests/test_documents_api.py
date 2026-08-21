import io
import pytest
from httpx import ASGITransport, AsyncClient

from main import app


async def test_upload_unsupported_ext_400(test_user_id, cleanup_test_data):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/api/upload", files={"file": ("x.exe", io.BytesIO(b"MZ"), "application/octet-stream")}, data={"user_id": test_user_id})
    assert resp.status_code == 400
    assert "不支持" in resp.json()["detail"]


async def test_upload_and_list_and_delete(test_user_id, cleanup_test_data):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # 上传
        resp = await c.post("/api/upload", files={"file": ("产品.md", io.BytesIO("# 章\n价格 5999".encode()), "text/markdown")}, data={"user_id": test_user_id})
        assert resp.status_code == 200
        body = resp.json()
        assert body["index_result"]["status"] == "success"
        md5 = body["index_result"]["md5"]

        # 列表
        lst = await c.get("/api/documents", params={"user_id": test_user_id})
        docs = lst.json()["documents"]
        assert any(d["md5"] == md5 and d["file_type"] == "md" for d in docs)

        # 重复上传 → duplicate
        again = await c.post("/api/upload", files={"file": ("产品.md", io.BytesIO("# 章\n价格 5999".encode()), "text/markdown")}, data={"user_id": test_user_id})
        assert again.json()["index_result"]["status"] == "duplicate"

        # 删除
        dele = await c.delete(f"/api/documents/{md5}", params={"user_id": test_user_id})
        assert dele.status_code == 200

        # 再删 → 404
        dele2 = await c.delete(f"/api/documents/{md5}", params={"user_id": test_user_id})
        assert dele2.status_code == 404
