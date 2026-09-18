from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.v1.bili_market import get_product_cover
from app.errors import AppError
from app.models import Base, BiliProduct
from app.services.bili_market.cover import CoverPayload, CoverProxyError, fetch_cover


def _product(*, cluster_id: int = 10000002733, cover_url: str | None = "https://i0.hdslb.com/bfs/mall/cover.png") -> BiliProduct:
    now = datetime.now(timezone.utc)
    return BiliProduct(
        id=str(uuid4()),
        cluster_id=cluster_id,
        title="示例商品",
        cover_url=cover_url,
        detail_url=f"https://mall.bilibili.com/detail.html?clusterId={cluster_id}",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.mark.asyncio
async def test_cover_proxy_returns_image_and_bili_referer():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["referer"] = request.headers.get("referer")
        seen["user_agent"] = request.headers.get("user-agent")
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png-data")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_cover("https://i0.hdslb.com/bfs/mall/cover.png", client)

    assert result == CoverPayload(b"png-data", "image/png")
    assert seen == {
        "referer": "https://mall.bilibili.com/",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://example.com/cover.png", "https://[broken"])
async def test_cover_proxy_rejects_untrusted_or_malformed_url_before_request(url: str):
    with pytest.raises(CoverProxyError) as error:
        await fetch_cover(url)

    assert error.value.code == "PRODUCT_COVER_UNSUPPORTED"
    assert error.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>"),
        httpx.Response(200, headers={"content-type": "image/svg+xml"}, content=b"<svg></svg>"),
        httpx.Response(502, headers={"content-type": "image/png"}, content=b"upstream failure"),
    ],
)
async def test_cover_proxy_rejects_html_and_upstream_failures(response: httpx.Response):
    async def handler(request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CoverProxyError) as error:
            await fetch_cover("https://i0.hdslb.com/bfs/mall/cover.png", client)

    assert error.value.status_code == 502
    assert error.value.code in {"PRODUCT_COVER_UPSTREAM_INVALID", "PRODUCT_COVER_UPSTREAM_FAILED"}


@pytest.mark.asyncio
async def test_cover_proxy_rejects_oversized_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png", "content-length": str(5 * 1024 * 1024 + 1)}, content=b"x")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CoverProxyError) as error:
            await fetch_cover("https://i0.hdslb.com/bfs/mall/cover.png", client)

    assert error.value.code == "PRODUCT_COVER_TOO_LARGE"


@pytest.mark.asyncio
async def test_cover_endpoint_returns_404_when_product_or_cover_is_missing(db):
    with pytest.raises(AppError) as missing_product:
        await get_product_cover(10000002733, db)

    db.add(_product(cover_url=None))
    db.commit()
    with pytest.raises(AppError) as missing_cover:
        await get_product_cover(10000002733, db)

    assert missing_product.value.status_code == 404
    assert missing_cover.value.status_code == 404


@pytest.mark.asyncio
async def test_cover_endpoint_sets_cache_control(db, monkeypatch):
    db.add(_product())
    db.commit()
    monkeypatch.setattr(
        "app.api.v1.bili_market.fetch_cover",
        lambda value: _successful_cover(),
    )

    response = await get_product_cover(10000002733, db)

    assert response.status_code == 200
    assert response.body == b"png-data"
    assert response.media_type == "image/png"
    assert response.headers["cache-control"] == "public, max-age=300, s-maxage=300"
    assert response.headers["x-content-type-options"] == "nosniff"


async def _successful_cover() -> CoverPayload:
    return CoverPayload(b"png-data", "image/png")
