from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.services.bili_market.client import BILI_BROWSER_HEADERS

MAX_COVER_BYTES = 5 * 1024 * 1024
COVER_TIMEOUT_SECONDS = 5.0
ALLOWED_COVER_HOSTS = frozenset({
    "i0.hdslb.com",
    "i1.hdslb.com",
    "i2.hdslb.com",
    "i3.hdslb.com",
})
ALLOWED_COVER_CONTENT_TYPES = frozenset({
    "image/avif",
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
})
COVER_CACHE_CONTROL = "public, max-age=300, s-maxage=300"


class CoverProxyError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class CoverPayload:
    content: bytes
    content_type: str


def validate_cover_url(value: Optional[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoverProxyError("PRODUCT_COVER_NOT_FOUND", "未找到商品封面", 404)
    try:
        parsed = urlparse(value.strip())
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        port = parsed.port
    except (AttributeError, UnicodeError, ValueError) as exc:
        raise CoverProxyError("PRODUCT_COVER_UNSUPPORTED", "商品封面地址不受支持", 404) from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname not in ALLOWED_COVER_HOSTS
        or parsed.username
        or parsed.password
        or port is not None
        or not parsed.path
    ):
        raise CoverProxyError("PRODUCT_COVER_UNSUPPORTED", "商品封面地址不受支持", 404)
    return parsed.geturl()


async def _fetch_cover_with_client(client: httpx.AsyncClient, url: str) -> CoverPayload:
    try:
        async with client.stream(
            "GET",
            url,
            headers={
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": "https://mall.bilibili.com/",
                "User-Agent": BILI_BROWSER_HEADERS["User-Agent"],
            },
        ) as response:
            if response.status_code != 200:
                raise CoverProxyError("PRODUCT_COVER_UPSTREAM_FAILED", "商品封面暂时无法获取", 502)
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in ALLOWED_COVER_CONTENT_TYPES:
                raise CoverProxyError("PRODUCT_COVER_UPSTREAM_INVALID", "商品封面响应不是图片", 502)
            try:
                content_length = int(response.headers.get("content-length", "0"))
            except ValueError:
                content_length = 0
            if content_length > MAX_COVER_BYTES:
                raise CoverProxyError("PRODUCT_COVER_TOO_LARGE", "商品封面文件过大", 502)

            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_COVER_BYTES:
                    raise CoverProxyError("PRODUCT_COVER_TOO_LARGE", "商品封面文件过大", 502)
            if not content:
                raise CoverProxyError("PRODUCT_COVER_UPSTREAM_INVALID", "商品封面响应为空", 502)
            return CoverPayload(bytes(content), content_type)
    except CoverProxyError:
        raise
    except (httpx.HTTPError, TimeoutError) as exc:
        raise CoverProxyError("PRODUCT_COVER_UPSTREAM_FAILED", "商品封面暂时无法获取", 502) from exc


async def fetch_cover(value: Optional[str], client: Optional[httpx.AsyncClient] = None) -> CoverPayload:
    url = validate_cover_url(value)
    if client is not None:
        return await _fetch_cover_with_client(client, url)
    timeout = httpx.Timeout(COVER_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as owned_client:
        return await _fetch_cover_with_client(owned_client, url)
