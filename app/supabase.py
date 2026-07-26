"""Caller authentication.

This service reads no rows and writes none: the caller sends the board content
and the derived facts, and the grade goes back for the web app to persist. So
the only thing Supabase is needed for is proving the caller is a real signed-in
user before spending a model call on them.

As in ativscrum-ai-api, runtime calls use the public anon key plus the caller's
own bearer token. There is deliberately no service-role key in this service.
"""

from dataclasses import dataclass

import httpx

from app.config import Settings
from app.errors import AppError


@dataclass(frozen=True, slots=True)
class Session:
    user_id: str
    token: str


class SupabaseGateway:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = settings.supabase_url.rstrip("/")
        self._anon_key = settings.supabase_anon_key
        self._http_client = http_client
        self._transport = transport

    async def validate_session(self, token: str) -> Session:
        response = await self._request("GET", "/auth/v1/user", token)
        if response.status_code in (401, 403):
            raise AppError(401, "authentication_required", "Authentication required.")
        if response.is_error:
            raise AppError(502, "supabase_request_failed", "Upstream request failed.")
        user_id = response.json().get("id")
        if not user_id:
            raise AppError(401, "authentication_required", "Authentication required.")
        return Session(user_id=str(user_id), token=token)

    async def _request(
        self, method: str, path: str, token: str, **kwargs
    ) -> httpx.Response:
        headers = {"apikey": self._anon_key, "Authorization": f"Bearer {token}"}
        if self._http_client is not None:
            return await self._http_client.request(
                method, f"{self._base_url}{path}", headers=headers, **kwargs
            )
        async with httpx.AsyncClient(transport=self._transport, timeout=10) as client:
            return await client.request(
                method, f"{self._base_url}{path}", headers=headers, **kwargs
            )
