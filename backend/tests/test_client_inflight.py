import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.config import Settings
from app.services.bili_market.client import BiliMarketClient


@pytest.mark.asyncio
async def test_same_cluster_requests_share_one_inflight_http_call():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "available.json").read_text(encoding="utf-8"))
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=fixture)

    transport = httpx.MockTransport(handler)
    settings = Settings(bili_global_min_interval_seconds=10)
    http_client = httpx.AsyncClient(transport=transport)
    client = BiliMarketClient(settings, http_client)
    try:
        await asyncio.gather(client.fetch(10000002733), client.fetch(10000002733))
    finally:
        await http_client.aclose()
    assert calls == 1


@pytest.mark.asyncio
async def test_fetch_uses_market_browser_headers_to_avoid_412():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "available.json").read_text(encoding="utf-8"))
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update({key.lower(): value for key, value in request.headers.items()})
        if seen.get("user-agent", "").startswith("python-httpx"):
            return httpx.Response(412)
        return httpx.Response(200, json=fixture)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = BiliMarketClient(Settings(), http_client)
    try:
        payload = await client.fetch(10000002733)
    finally:
        await http_client.aclose()

    assert payload["code"] == 0
    assert seen["accept"] == "application/json, text/plain, */*"
    assert seen["referer"].endswith("/neul-next/resell/home.html")
    assert seen["user-agent"].startswith("Mozilla/5.0")


@pytest.mark.asyncio
async def test_fetch_with_events_records_rate_limit_and_retry_success(monkeypatch):
    fixture = json.loads((Path(__file__).parent / "fixtures" / "available.json").read_text(encoding="utf-8"))
    responses = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json=fixture),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    transport = httpx.MockTransport(handler)
    settings = Settings(bili_global_min_interval_seconds=10)
    http_client = httpx.AsyncClient(transport=transport)
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.bili_market.client.asyncio.sleep", sleep)
    client = BiliMarketClient(settings, http_client)
    try:
        payload, events = await client.fetch_with_events(10000002733)
    finally:
        await http_client.aclose()

    assert payload["code"] == 0
    assert [(event.status_code, event.success) for event in events] == [(429, False), (200, True)]
    assert sleep.await_count == 1
