"""Refuse oversized request bodies before FastAPI buffers and parses them."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The largest legitimate body is a 1 MB file as base64 (~1.4 MB) plus JSON framing.
MAX_BODY_BYTES = 2_000_000


class BodyLimit:
    """Reads at most MAX_BODY_BYTES, then hands the body to the app in one message."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope["headers"]).get(b"content-length", b"0")
        if length.isdigit() and int(length) > self.max_bytes:
            await self._refuse(scope, receive, send)
            return
        # Chunked bodies carry no length, so count what actually arrives.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await self._refuse(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "request_too_large",
                    "message": "This request is too large.",
                }
            },
        )
        await response(scope, receive, send)
