"""Which model served a request: a log line per model call and an X-Grip-Model header."""

import logging

from app.errors import AppError
from tests.conftest import make_client
from tests.test_poe import AUTH, URL, PoeProvider, ask


def test_model_call_is_logged_and_named_in_a_header(caplog):
    caplog.set_level(logging.INFO, logger="grip.ai")
    origin = {"Origin": "http://localhost:5173"}
    response = make_client(gemini=PoeProvider()).post(
        URL, json=ask(), headers={**AUTH, **origin}
    )
    assert response.headers["x-grip-model"] == "gemini-3.1-flash-lite"
    assert "x-grip-model" in response.headers["access-control-expose-headers"].lower()
    line = next(r.getMessage() for r in caplog.records if r.name == "grip.ai")
    assert line.split()[:3] == ["ai", "poe/chat", "gemini-3.1-flash-lite"]
    assert line.endswith(" ok")


def test_failed_call_is_logged_with_its_error_code(caplog):
    caplog.set_level(logging.INFO, logger="grip.ai")
    limited = AppError(429, "provider_limited", "slow down", retry_after=30)
    response = make_client(gemini=PoeProvider(fail=limited)).post(
        URL, json=ask(), headers=AUTH
    )
    assert response.status_code == 429
    assert response.headers["x-grip-model"] == "gemini-3.1-flash-lite"
    line = next(r.getMessage() for r in caplog.records if r.name == "grip.ai")
    assert line.endswith(" provider_limited")


def test_requests_without_a_model_call_get_no_header():
    response = make_client().get("/health")
    assert "x-grip-model" not in response.headers
