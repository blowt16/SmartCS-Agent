"""MinerU 云端 API 客户端(标准 API v4,字段已按官方文档 2026-08-21 确认)。

流程(官方: https://mineru.net/apiManage/docs):
    POST /file-urls/batch       申请签名上传 URL(请求含 files[].name/data_id/is_ocr,
                                 响应 data.file_urls 为字符串数组)
    PUT  上传 URL(无 Content-Type)→ 服务端自动提交解析任务(无需 extract/task/batch)
    GET  /extract-results/batch/{batch_id}  轮询 data.extract_result 数组
                                 按 file_name 匹配本文件,state=done 取 full_zip_url
    下载 zip 解压取 full.md

失败统一抛 MinerUError(category 对齐 spec 错误分类):
    auth_error / unsupported_format / api_timeout / parse_error / api_unreachable
"""
import asyncio
import io
import zipfile
from pathlib import Path

import aiohttp

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(service="mineru")


class MinerUError(Exception):
    def __init__(self, category: str, detail: str = ""):
        self.category = category
        self.detail = detail
        super().__init__(f"[{category}] {detail}")


async def parse_pdf(file_path: str) -> str:
    """上传 PDF 并返回解析后的 full.md 文本。"""
    token = settings.MINERU_API_TOKEN.strip()
    if not token:
        raise MinerUError("auth_error", "MINERU_API_TOKEN 未配置")

    base = settings.MINERU_BASE_URL.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    filename = Path(file_path).name

    async with aiohttp.ClientSession() as session:
        # 1. 申请签名上传 URL(提交失败按 MINERU_MAX_RETRIES 重试)
        batch_id, upload_url = await _request_upload_urls(session, base, headers, filename)

        # 2. PUT 上传文件(不带 Content-Type——OSS 预签名 URL 不含该头,
        #    aiohttp 对 bytes 默认加 application/octet-stream 会致签名不匹配 403),
        #    上传后自动提交解析任务
        with open(file_path, "rb") as f:
            put_resp = await session.put(
                upload_url, data=f.read(), skip_auto_headers={"Content-Type"}
            )
        if put_resp.status != 200:
            raise MinerUError("parse_error", f"文件上传失败: HTTP {put_resp.status}")

        # 3. 轮询解析结果
        elapsed = 0.0
        while elapsed < settings.MINERU_TIMEOUT:
            await asyncio.sleep(settings.MINERU_POLL_INTERVAL)
            elapsed += settings.MINERU_POLL_INTERVAL
            poll = await session.get(
                f"{base}/extract-results/batch/{batch_id}", headers=headers
            )
            poll_data = await _checked(poll)
            item = _find_result(poll_data, filename)
            state = item.get("state")
            if state == "done":
                zip_url = item.get("full_zip_url")
                if not zip_url:
                    raise MinerUError("parse_error", "任务完成但缺少 full_zip_url")
                return await _download_full_md(session, zip_url)
            if state == "failed":
                raise MinerUError(
                    "parse_error", item.get("err_msg") or "MinerU 任务解析失败"
                )

        raise MinerUError("api_timeout", f"轮询超时({settings.MINERU_TIMEOUT}s)")


async def _request_upload_urls(session, base: str, headers: dict, filename: str) -> tuple[str, str]:
    """POST /file-urls/batch:申请签名上传 URL,返回 (batch_id, upload_url)。"""
    last_exc: MinerUError | None = None
    for attempt in range(max(1, settings.MINERU_MAX_RETRIES)):
        try:
            resp = await session.post(
                f"{base}/file-urls/batch",
                headers=headers,
                json={
                    "files": [
                        {
                            "name": filename,
                            "data_id": filename,
                            "is_ocr": True,   # 扫描件 OCR(官方: 仅 pipeline/vlm 有效)
                            "language": "ch",
                        }
                    ],
                    "model_version": "pipeline",
                },
            )
            data = await _checked(resp)
            urls = data["data"].get("file_urls") or []
            if not urls:
                raise MinerUError("parse_error", "file-urls/batch 未返回上传链接")
            return data["data"]["batch_id"], urls[0]
        except MinerUError:
            raise
        except aiohttp.ClientError as e:
            last_exc = MinerUError("api_unreachable", f"MinerU API 不可达: {e}")
            if attempt < settings.MINERU_MAX_RETRIES - 1:
                logger.warning("提交上传请求失败,{}s 后重试({}/{})",
                               2**attempt, attempt + 1, settings.MINERU_MAX_RETRIES)
                await asyncio.sleep(2**attempt)
    assert last_exc is not None
    raise last_exc


def _find_result(poll_data: dict, filename: str) -> dict:
    """从 data.extract_result 数组按 file_name 匹配本文件(官方契约)。"""
    results = (poll_data.get("data") or {}).get("extract_result") or []
    for item in results:
        if item.get("file_name") == filename:
            return item
    raise MinerUError("parse_error", f"轮询结果未找到文件: {filename}")


async def _checked(resp) -> dict:
    """统一响应校验:HTTP 层与业务层(code!=0 / success=false)。"""
    if resp.status == 401:
        raise MinerUError("auth_error", f"鉴权失败: HTTP {resp.status}")
    if resp.status == 400:
        raise MinerUError("unsupported_format", f"格式不支持: HTTP {resp.status}")
    if resp.status >= 500:
        raise MinerUError("api_timeout", f"服务异常: HTTP {resp.status}")
    data = await resp.json()
    if data.get("code") != 0 and data.get("code") is not None:
        raise MinerUError("parse_error", f"业务错误: {data}")
    if data.get("success") is False:
        raise MinerUError("auth_error", f"网关错误: {data}")
    return data


async def _download_full_md(session, zip_url: str) -> str:
    """下载结果 zip,解压取 full.md。"""
    resp = await session.get(zip_url)
    if resp.status != 200:
        raise MinerUError("parse_error", f"结果下载失败: HTTP {resp.status}")
    with zipfile.ZipFile(io.BytesIO(await resp.read())) as zf:
        for name in zf.namelist():
            if name.endswith("full.md"):
                md = zf.read(name).decode("utf-8", errors="replace")
                if not md.strip():
                    raise MinerUError("parse_error", "full.md 为空")
                return md
    raise MinerUError("parse_error", "zip 中未找到 full.md")
