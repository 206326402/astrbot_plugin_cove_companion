from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Mapping

import aiohttp

PROTOCOL = "astrbot-cove/1"


class CoveAPIError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 0, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


class CoveAmbiguousError(CoveAPIError):
    pass


class CoveClient:
    def __init__(self, base_url: str, token_file: str, *, timeout_seconds: int = 20):
        self.base_url = base_url.rstrip("/")
        self.token_file = Path(token_file)
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=5)
        self.session: aiohttp.ClientSession | None = None

    def token(self) -> str:
        try:
            return self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CoveAPIError("token_unavailable", f"无法读取 Cove 桥接令牌：{exc}") from exc

    async def start(self) -> None:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=self.timeout)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def _request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None, body: Mapping[str, Any] | None = None) -> Any:
        await self.start()
        assert self.session is not None
        headers = {"authorization": "Bearer " + self.token()}
        try:
            async with self.session.request(method, self.base_url + path, params=params, json=body, headers=headers) as response:
                raw = await response.text()
                try:
                    payload = json.loads(raw) if raw else {}
                except ValueError:
                    payload = {"error": {"code": "invalid_json", "message": raw[:300]}}
                if response.status == 410 and isinstance(payload, dict) and payload.get("code") == "cursor_expired":
                    return payload
                if response.status >= 400:
                    error = payload.get("error") if isinstance(payload, dict) else None
                    code = str((error or {}).get("code") or f"http_{response.status}")
                    message = str((error or {}).get("message") or raw[:500] or code)
                    retryable = bool((error or {}).get("retryable", response.status >= 500))
                    if response.status >= 500 or code in {"remote_result_unknown", "lease_expired"}:
                        raise CoveAmbiguousError(code, message, response.status, retryable)
                    raise CoveAPIError(code, message, response.status, retryable)
                return payload
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise CoveAmbiguousError("cove_unreachable", f"Cove 通信失败：{type(exc).__name__}", retryable=True) from exc

    async def bootstrap(self) -> dict[str, Any]:
        return await self._request("GET", "/astrbot/v1/bootstrap")

    async def snapshot(self, fresh: bool = False) -> dict[str, Any]:
        return await self._request("GET", "/astrbot/v1/snapshot", params={"fresh": "1" if fresh else "0"})

    async def events(self, after: int, limit: int = 100, wait: int = 0) -> dict[str, Any]:
        return await self._request("GET", "/astrbot/v1/events", params={"after": after, "limit": limit, "wait": wait})

    async def search(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        response = await self._request("GET", "/astrbot/v1/search", params={"query": query, "limit": limit})
        return list(response.get("songs") or [])

    async def candidates(self, limit: int = 40) -> list[dict[str, Any]]:
        response = await self._request("GET", "/astrbot/v1/candidates", params={"limit": limit})
        return list(response.get("songs") or [])

    async def catalog(self, kind: str, query: str = '', ident: str = '', limit: int = 20) -> dict[str, Any]:
        return await self._request('GET', '/astrbot/v1/catalog', params={'kind':kind,'query':query,'id':ident,'limit':min(30,limit)})

    async def tracks(self, song_ids: list[str]) -> list[dict[str, Any]]:
        ids = [str(item) for item in song_ids if str(item).isdigit()][:50]
        if not ids:
            return []
        response = await self._request("GET", "/astrbot/v1/tracks", params={"ids": ",".join(ids)})
        return list(response.get("songs") or [])

    async def lyrics(self, song_id: str) -> dict[str, Any]:
        return await self._request("GET", "/astrbot/v1/lyrics", params={"song_id": song_id})

    async def claim(self, holder: str) -> dict[str, Any]:
        response = await self._request("POST", "/astrbot/v1/controller/claim", body={"holder": holder})
        return dict(response.get("lease") or {})

    async def renew(self, lease_id: str, fencing_token: int) -> dict[str, Any]:
        response = await self._request("POST", "/astrbot/v1/controller/renew", body={"lease_id": lease_id, "fencing_token": fencing_token})
        return dict(response.get("lease") or {})

    async def release(self, lease_id: str, fencing_token: int) -> bool:
        response = await self._request("POST", "/astrbot/v1/controller/release", body={"lease_id": lease_id, "fencing_token": fencing_token})
        return bool(response.get("released"))

    async def command(self, request: Mapping[str, Any]) -> dict[str, Any]:
        response = await self._request("POST", "/astrbot/v1/commands", body=dict(request))
        return dict(response.get("operation") or {})

    async def operation(self, operation_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/astrbot/v1/operations/{operation_id}")
        return dict(response.get("operation") or {})

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/astrbot/v1/health/ready")

    async def wait_operation(self, operation_id: str, *, deadline_seconds: float = 30) -> dict[str, Any]:
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            operation = await self.operation(operation_id)
            state = str(operation.get("state") or "unknown")
            if state in {"confirmed", "rejected", "cancelled", "unknown"}:
                return operation
            await asyncio.sleep(0.35)
        return {"operation_id": operation_id, "state": "unknown", "error": {"code": "operation_wait_timeout", "message": "操作在等待窗口内没有终态", "retryable": False}}

    async def playlist(self, playlist_id: str, limit: int = 30):
        r=await self._request("GET","/astrbot/v1/playlist",params={"id":playlist_id,"limit":limit})
        return list(r.get("songs") or [])

    async def catalog(self, kind, query='', ident='', limit=20):
        return await self._request('GET','/astrbot/v1/catalog',params={'kind':kind,'query':query,'id':ident,'limit':limit})

    async def auth_status(self, fresh=False):
        return await self._request("GET","/astrbot/v1/auth/status",params={"fresh":"1" if fresh else "0"})

    async def auth_action(self, action, ident=None):
        if action not in {"qr/start","qr/check","activate"}:raise ValueError("invalid_auth_action")
        return await self._request("POST","/astrbot/v1/auth/"+action,body={"id":ident} if ident else {})

    async def availability(self,song_id: str) -> bool:
        result=await self._request("GET","/astrbot/v1/availability",params={"id":song_id})
        return result.get("available") is True
