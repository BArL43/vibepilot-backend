from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


@dataclass(slots=True)
class VibeResponse:
    data: dict[str, Any]
    request_id: str | None
    http_status: int


class VibeAPIError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        self.details = details or {}
        self.retry_after = retry_after


class VibeClient:
    """Small, typed boundary around the public VibeMarketolog Agent API."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        headers = {"Accept": "application/json", "User-Agent": "VibePilot/0.3"}
        if settings.vibe_api_token:
            headers["Authorization"] = f"Bearer {settings.vibe_api_token}"
        self._client = httpx.AsyncClient(
            base_url=settings.vibe_api_base,
            headers=headers,
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            transport=transport,
        )

    async def __aenter__(self) -> VibeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retryable: bool = True,
    ) -> VibeResponse:
        attempts = self.settings.retry_attempts if retryable else 1
        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method, path, json=json, headers=headers
                )
            except httpx.HTTPError as exc:
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(2**attempt, 4))
                    continue
                raise VibeAPIError(
                    status_code=502,
                    code="network_error",
                    message=(
                        f"VibeMarketolog network request failed: {type(exc).__name__}"
                    ),
                ) from exc

            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {"data": payload}

            request_id = payload.get("request_id") or response.headers.get(
                "X-Request-Id"
            )
            if response.is_success:
                return VibeResponse(
                    data=payload,
                    request_id=str(request_id) if request_id else None,
                    http_status=response.status_code,
                )

            retry_after_raw = payload.get("retry_after") or response.headers.get(
                "Retry-After"
            )
            try:
                retry_after = float(retry_after_raw) if retry_after_raw else None
            except (TypeError, ValueError):
                retry_after = None
            if response.status_code in {429, 503} and attempt + 1 < attempts:
                await asyncio.sleep(min(retry_after or 2**attempt, 8))
                continue

            raise VibeAPIError(
                status_code=response.status_code,
                code=str(payload.get("error") or "upstream_error"),
                message=str(
                    payload.get("message")
                    or payload.get("detail")
                    or f"VibeMarketolog returned HTTP {response.status_code}"
                ),
                request_id=str(request_id) if request_id else None,
                details=payload,
                retry_after=retry_after,
            )

        raise AssertionError("unreachable")

    async def capabilities(self) -> VibeResponse:
        return await self._request("GET", "/capabilities")

    async def me(self) -> VibeResponse:
        return await self._request("GET", "/me")

    async def balance(self) -> VibeResponse:
        return await self._request("GET", "/balance")

    async def estimate(self, payload: dict[str, Any]) -> VibeResponse:
        return await self._request("POST", "/generate/estimate", json=payload)

    async def generate(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> VibeResponse:
        return await self._request(
            "POST",
            "/generate",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
            retryable=True,
        )

    async def generation_status(self, generation_id: int | str) -> VibeResponse:
        return await self._request("GET", f"/generation/{generation_id}/status")

    async def webhook_test(self, callback_url: str) -> VibeResponse:
        return await self._request(
            "POST",
            "/webhook-test",
            json={"callback_url": callback_url},
            retryable=False,
        )
