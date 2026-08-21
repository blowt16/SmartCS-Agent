import asyncio
import io
import zipfile
from unittest.mock import AsyncMock, patch

import pytest

from app.services.mineru_client import MinerUError, parse_pdf

# 官方契约(2026-08-21 确认自 https://mineru.net/apiManage/docs):
#   POST /file-urls/batch  → data: {batch_id, file_urls: [url字符串]}
#   PUT url 上传(无 Content-Type),上传后自动提交任务(无需 extract/task/batch)
#   GET  /extract-results/batch/{batch_id} → data.extract_result: 数组
#        [{file_name, state: done/failed/..., full_zip_url, err_msg}]


class FakeResp:
    """真实响应对象:status/json/read/content 均为真实值,规避 AsyncMock 嵌套陷阱。"""

    def __init__(self, status=200, payload=None, content=b""):
        self.status = status
        self._payload = payload
        self.content = content

    async def json(self):
        return self._payload

    async def read(self):
        return self.content


def _ok_resp(**data):
    return FakeResp(200, {"code": 0, "msg": "ok", "data": data})


def _patch_session(fake):
    return patch(
        "app.services.mineru_client.aiohttp.ClientSession",
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=fake),
            __aexit__=AsyncMock(return_value=False),
        ),
    )


@pytest.mark.parametrize("token", ["", "  "])
def test_missing_token_raises_auth_error(tmp_path, token):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-1.4")
    with patch("app.services.mineru_client.settings") as m:
        m.MINERU_API_TOKEN = token
        with pytest.raises(MinerUError) as ei:
            asyncio.run(parse_pdf(str(f)))
        assert ei.value.category == "auth_error"


def test_task_state_failed_maps_to_parse_error(tmp_path):
    """轮询到 state=failed → parse_error(err_msg 带出)。"""
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-1.4")

    fake = AsyncMock()
    fake.post = AsyncMock(return_value=_ok_resp(
        batch_id="b1", file_urls=["https://put.example/x"]
    ))
    fake.put = AsyncMock(return_value=FakeResp(200))
    fake.get = AsyncMock(return_value=_ok_resp(
        batch_id="b1",
        extract_result=[
            {"file_name": "a.pdf", "state": "failed", "err_msg": "文件格式不支持"}
        ],
    ))

    with patch("app.services.mineru_client.settings") as m, _patch_session(fake):
        m.MINERU_API_TOKEN = "tok"
        m.MINERU_BASE_URL = "https://mineru.net/api/v4"
        m.MINERU_POLL_INTERVAL = 0
        m.MINERU_TIMEOUT = 60
        m.MINERU_MAX_RETRIES = 3
        with pytest.raises(MinerUError) as ei:
            asyncio.run(parse_pdf(str(f)))
        assert ei.value.category == "parse_error"


def test_full_markdown_returned_on_done(tmp_path):
    """state=done → 下载 zip 解压取 full.md 返回。"""
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-1.4")

    fake = AsyncMock()
    fake.post = AsyncMock(return_value=_ok_resp(
        batch_id="b1", file_urls=["https://put.example/x"]
    ))
    fake.put = AsyncMock(return_value=FakeResp(200))

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("full.md", "# 产品说明书\n价格 5999")

    async def _get(url, **kw):
        if "/extract-results/batch/" in url:
            return _ok_resp(
                batch_id="b1",
                extract_result=[
                    {"file_name": "a.pdf", "state": "done", "full_zip_url": "https://cdn/x.zip"}
                ],
            )
        return FakeResp(200, content=zbuf.getvalue())  # zip 下载

    fake.get = AsyncMock(side_effect=_get)

    with patch("app.services.mineru_client.settings") as m, _patch_session(fake):
        m.MINERU_API_TOKEN = "tok"
        m.MINERU_BASE_URL = "https://mineru.net/api/v4"
        m.MINERU_POLL_INTERVAL = 0
        m.MINERU_TIMEOUT = 60
        m.MINERU_MAX_RETRIES = 3
        md = asyncio.run(parse_pdf(str(f)))
        assert "5999" in md


def test_network_error_maps_to_api_unreachable(tmp_path):
    """aiohttp 网络异常 → api_unreachable(错误分类对齐 spec)。"""
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-1.4")

    fake = AsyncMock()
    import aiohttp

    async def _boom(*a, **kw):
        raise aiohttp.ClientError("conn refused")

    fake.post = AsyncMock(side_effect=_boom)

    with patch("app.services.mineru_client.settings") as m, _patch_session(fake):
        m.MINERU_API_TOKEN = "tok"
        m.MINERU_BASE_URL = "https://mineru.net/api/v4"
        m.MINERU_MAX_RETRIES = 1
        with pytest.raises(MinerUError) as ei:
            asyncio.run(parse_pdf(str(f)))
        assert ei.value.category == "api_unreachable"
