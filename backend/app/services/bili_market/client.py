import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import httpx

from app.core.config import Settings

BILI_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://mall.bilibili.com/neul-next/resell/home.html",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
}


@dataclass
class BiliRequestAttempt:
    id: str
    cluster_id: int
    status_code: Optional[int]
    success: bool
    duration_ms: float
    created_at: datetime
    persisted: bool = False


class BiliAPIError(Exception):
    def __init__(self, code: str, message: str, status_code: Optional[int] = None, events: Optional[List[BiliRequestAttempt]] = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.events = events or []
        super().__init__(message)


class BiliMarketClient:
    def __init__(self, settings: Settings, http_client: Optional[httpx.AsyncClient] = None):
        self.settings = settings
        self._client = http_client
        self._owns_client = http_client is None
        self._semaphore = asyncio.Semaphore(settings.bili_max_concurrency)
        self._inflight: Dict[int, asyncio.Task[Tuple[Dict[str, Any], List[BiliRequestAttempt]]]] = {}
        self.total_requests = 0
        self.successful_requests = 0
        self.rate_limited_requests = 0
        self.total_duration_seconds = 0.0

    async def start(self) -> None:
        if self._client is None:
            limits = httpx.Limits(max_connections=self.settings.bili_max_concurrency * 2, max_keepalive_connections=self.settings.bili_max_concurrency)
            self._client = httpx.AsyncClient(timeout=self.settings.bili_request_timeout_seconds, limits=limits)

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    async def fetch(self, cluster_id: int) -> Dict[str, Any]:
        payload, _ = await self.fetch_with_events(cluster_id)
        return payload

    async def fetch_with_events(self, cluster_id: int) -> Tuple[Dict[str, Any], List[BiliRequestAttempt]]:
        await self.start()
        existing = self._inflight.get(cluster_id)
        if existing is not None and not existing.done():
            return await asyncio.shield(existing)
        task = asyncio.create_task(self._fetch_uncached(cluster_id))
        self._inflight[cluster_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._inflight.get(cluster_id) is task:
                self._inflight.pop(cluster_id, None)

    async def _fetch_uncached(self, cluster_id: int) -> Tuple[Dict[str, Any], List[BiliRequestAttempt]]:
        assert self._client is not None
        url = self.settings.bili_api_base_url.rstrip("/") + "/mall-search-items/items_detail/cluster_info"
        payload = {"clusterId": str(cluster_id)}
        last_error: Optional[Exception] = None
        events: List[BiliRequestAttempt] = []
        async with self._semaphore:
            started = time.perf_counter()
            try:
                for attempt in range(3):
                    attempt_started = time.perf_counter()
                    try:
                        self.total_requests += 1
                        # The B站市集 edge rejects the default httpx user agent
                        # with 412. These are the same non-sensitive browser
                        # headers used by the market page itself; no cookies or
                        # account credentials are required for this endpoint.
                        response = await self._client.post(url, json=payload, headers=BILI_BROWSER_HEADERS)
                        if response.status_code == 429:
                            self.rate_limited_requests += 1
                            events.append(self._event(cluster_id, response.status_code, False, attempt_started))
                            retry_after = response.headers.get("Retry-After")
                            try:
                                delay = min(float(retry_after), 30.0) if retry_after else float(2 ** attempt)
                            except ValueError:
                                delay = float(2 ** attempt)
                            await asyncio.sleep(delay)
                            last_error = BiliAPIError("BILI_RATE_LIMITED", "B站接口限流", 429, events)
                            continue
                        if response.status_code >= 500 and attempt < 2:
                            events.append(self._event(cluster_id, response.status_code, False, attempt_started))
                            await asyncio.sleep(1.0 if attempt == 0 else 3.0)
                            last_error = BiliAPIError("BILI_API_UNAVAILABLE", "B站接口暂时不可用", response.status_code, events)
                            continue
                        if response.status_code == 404:
                            events.append(self._event(cluster_id, response.status_code, False, attempt_started))
                            raise BiliAPIError("PRODUCT_NOT_FOUND", "未找到该 B 站市集商品", 404, events)
                        response.raise_for_status()
                        try:
                            data = response.json()
                        except ValueError as exc:
                            events.append(self._event(cluster_id, response.status_code, False, attempt_started))
                            raise BiliAPIError("BILI_API_INVALID_RESPONSE", "B站接口返回格式无效", response.status_code, events) from exc
                        if not isinstance(data, dict):
                            events.append(self._event(cluster_id, response.status_code, False, attempt_started))
                            raise BiliAPIError("BILI_API_INVALID_RESPONSE", "B站接口返回格式无效", response.status_code, events)
                        events.append(self._event(cluster_id, response.status_code, True, attempt_started))
                        self.successful_requests += 1
                        return data, events
                    except BiliAPIError:
                        raise
                    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                        events.append(self._event(cluster_id, None, False, attempt_started))
                        last_error = exc
                        if attempt < 2:
                            await asyncio.sleep(1.0 if attempt == 0 else 3.0)
                            continue
                    except httpx.HTTPStatusError as exc:
                        events.append(self._event(cluster_id, exc.response.status_code, False, attempt_started))
                        raise BiliAPIError("BILI_API_UNAVAILABLE", "B站接口请求失败", exc.response.status_code, events) from exc
            finally:
                self.total_duration_seconds += time.perf_counter() - started
        if isinstance(last_error, BiliAPIError):
            raise last_error
        raise BiliAPIError("BILI_API_UNAVAILABLE", "B站接口暂时不可用", events=events) from last_error

    @staticmethod
    def _event(cluster_id: int, status_code: Optional[int], success: bool, started: float) -> BiliRequestAttempt:
        return BiliRequestAttempt(
            id=str(uuid4()),
            cluster_id=cluster_id,
            status_code=status_code,
            success=success,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            created_at=datetime.now(timezone.utc),
        )

    def metrics(self) -> Dict[str, Any]:
        return {
            "requests": self.total_requests,
            "successful": self.successful_requests,
            "rate_limited": self.rate_limited_requests,
            "average_duration_ms": round((self.total_duration_seconds / self.total_requests) * 1000, 2) if self.total_requests else 0,
        }
