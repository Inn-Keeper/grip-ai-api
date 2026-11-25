"""Which model served a request: one log line per model call and an X-Grip-Model header.
Logs feature, model, tokens, time and outcome only: never user ids or text."""

import logging
from contextvars import ContextVar
from dataclasses import dataclass

log = logging.getLogger("grip.ai")
AI_PREFIX = "/api/v1/ai/"


@dataclass(frozen=True)
class ModelCall:
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    seconds: float
    outcome: str


# A list per request, shared by reference so calls made in another task still land in it.
_calls: ContextVar[list[ModelCall] | None] = ContextVar("model_calls", default=None)


def record(call: ModelCall) -> None:
    calls = _calls.get()
    if calls is not None:
        calls.append(call)


def format_call(feature: str, call: ModelCall) -> str:
    tokens = (
        f"in={call.prompt_tokens} out={call.completion_tokens}"
        if call.prompt_tokens is not None
        else "in=- out=-"
    )
    return f"ai {feature:<20} {call.model:<22} {tokens:<20} {call.seconds:5.1f}s {call.outcome}"


class ModelLog:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        calls: list[ModelCall] = []
        token = _calls.set(calls)

        async def send_with_model(message) -> None:
            if message["type"] == "http.response.start" and calls:
                models = ",".join(dict.fromkeys(call.model for call in calls))
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-grip-model", models.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_model)
        finally:
            _calls.reset(token)
            feature = scope["path"].removeprefix(AI_PREFIX)
            for call in calls:
                log.info(format_call(feature, call))
