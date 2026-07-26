"""Provider-client failure modes, offline.

A response cut off mid-JSON and a response that broke the evidence rule are
different failures, and this service cannot afford to conflate them: its whole
claim is that `invalid_model_response` means the rubric guard fired. If a
truncated answer arrives under the same code, "the grader refused you" and "we
did not give the model room to finish" become indistinguishable.
"""

import json

import httpx
import pytest

from app.config import Settings
from app.errors import AppError
from app.gemini import GeminiClient
from app.schemas import GradeSuggestion


def gemini(handler) -> GeminiClient:
    return GeminiClient(
        Settings(gemini_api_key="key"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def responds(content: str, finish_reason: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": finish_reason, "message": {"content": content}}
                ]
            },
        )

    return handler


async def generate(handler) -> AppError:
    with pytest.raises(AppError) as raised:
        await gemini(handler).generate(
            "gemini-3.5-flash", "system", {}, GradeSuggestion
        )
    return raised.value


async def test_truncated_response_says_it_was_truncated():
    # What the provider actually returns when reasoning tokens eat the output
    # budget: valid JSON as far as it goes, stopped mid-object.
    cut_off = '{\n  "sections": [\n    {\n      "section": "requirements",'
    error = await generate(responds(cut_off, "length"))
    assert error.code == "response_truncated"


async def test_reasoning_is_bounded_so_it_cannot_crowd_out_the_answer():
    # The truncation above is only half-fixed by a bigger cap: Gemini's thinking
    # budget is dynamic and expands into whatever room it is given. Capping the
    # effort is what actually guarantees the JSON has somewhere to go.
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    await generate(handler)
    assert sent["reasoning_effort"] == "low"


async def test_complete_but_ungrounded_response_is_still_invalid():
    # The evidence rule firing must keep its own code, or the distinction above
    # buys nothing.
    ungrounded = GradeSuggestion.model_construct(
        sections=[], hardest_followup="?"
    ).model_dump_json()
    error = await generate(responds(ungrounded, "stop"))
    assert error.code == "invalid_model_response"
