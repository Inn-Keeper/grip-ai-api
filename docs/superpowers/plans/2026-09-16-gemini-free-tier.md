# Gemini Free Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make grip-ai-api fully usable with `AI_PROVIDER=gemini` on the Gemini free tier: it configures cleanly, fails with clear JSON errors, tells clients when quota returns, and does not spend quota on repeated work.

**Architecture:** No new modules and no new dependencies. Settings gain a `.env.local` layer. Error handling becomes one envelope for every failure. The Gemini client reads the exhausted quota from its 429 body. `GradeService` gets a small in-memory LRU cache keyed by everything the model sees.

**Tech Stack:** Python 3.11, FastAPI 0.139, pydantic-settings 2.14, httpx 0.28, pytest 9 + pytest-asyncio, ruff 0.15.21.

**Spec:** No separate spec file. The requirements come from the 2026-09-16 verification of `grip/` (defect #5, "bare 500s when Supabase fails") and the follow-up request "make the ai api fully functional with Gemini free tier". Context an implementer needs:

- **The key goes unused.** `GEMINI_API_KEY` lives in `.env.local`, but `Settings` reads only `.env`, so the key is never loaded. `.env.local` also sets `AI_MODEL_GRADE=gemini-3.5-flash` without `AI_PROVIDER`, and lists `AI_MAX_OUTPUT_TOKENS` and `AI_TIMEOUT_SECONDS` twice.
- **Defect #5.**
  - When Supabase is unreachable, misconfigured or answers with HTML, the API returns a bare `500 Internal Server Error` instead of the `{"error": {...}}` envelope.
  - A malformed body returns FastAPI's default 422 shape, not the envelope.
- **Free-tier limits.** The limit is 20 requests/day per model per project (observed), and it resets at midnight America/Los_Angeles. A 429 body names the exhausted quota (`GenerateRequestsPerDayPerProjectPerModel-FreeTier` vs `...PerMinute...`) and carries a `retryDelay`. Today every 429 becomes `provider_limited` with no hint of when to retry.
- **Wasted quota.**
  - A read timeout is retried, although Google may already have counted the request.
  - A quote that differs only in line breaks is rejected with a 502, and that 502 costs a request.
  - Re-rendering a board regrades unchanged reasoning.
- **The live suite's opt-in is too broad.** `RUN_LIVE_AI=1` runs against whatever provider the settings select. Once `.env.local` is loaded, that would silently spend Gemini quota.

## Global Constraints

- Python 3.11 (`requires-python = ">=3.11"`); the Docker base is `python:3.11-slim`. `zoneinfo` works there because tzdata is present in the image (checked 2026-09-16).
- No new runtime or dev dependencies.
- Free-tier budget: about 20 Gemini requests/day per model. No task other than Task 9 may call Gemini. Offline tests must never reach the network.
- Never print, log or commit secrets. Show `.env`/`.env.local` only as key names (`cut -d= -f1 .env.local`).
- Commits: conventional-commit style (`feat:`, `fix:`, `docs:`, `ci:`, `test:`). Author: InnKeeper only. **No `Co-Authored-By` trailer and no "Generated with" footer.**
- Baseline: the working tree already contains the uncommitted placeholder-floor change (`apply_placeholder_floor` in `app/service.py`). Commit it before starting (`fix: grade placeholder sections as missing`); Task 7's code builds on it.
- Commands run from `grip-ai-api/` with the project venv: `.venv/bin/python -m pytest ...`, `.venv/bin/ruff ...`.
- **exFAT note:** on the T7 drive, delete AppleDouble files before running (`find . -name '._*' -not -path './.git/*' -delete`). If pytest hangs in uninterruptible IO while another heavy job is running on the drive, wait for that job to finish.
- Validation: every code block below was applied to a copy of the repo and run. Final state: `82 passed, 7 skipped`, `ruff check` and `ruff format --check` clean.

## File map

| File | Change | Task |
| --- | --- | --- |
| `app/config.py` | `.env.local` layer; reject Gemini model under Ollama; `ai_grade_cache_size` | 1, 7 |
| `app/main.py` | no import-time app; shared `error_response`; validation and catch-all handlers; `Retry-After` | 1, 4, 5 |
| `Dockerfile` | `uvicorn --factory` | 1 |
| `tests/test_pushover.py` | `RUN_LIVE_AI=gemini` opt-in | 2 |
| `app/supabase.py` | auth failures become `AppError`; own 10 s timeout | 3 |
| `app/errors.py` | `retry_after` | 5 |
| `app/gemini.py` | quota-aware 429; retry only unsent requests; `clock` | 5 |
| `app/service.py` | whitespace-insensitive evidence; `GradeCache` | 6, 7 |
| `tests/conftest.py` | `settings(**overrides)`, `make_client(..., **overrides)` | 7 |
| `tests/test_grade.py` | `post(token=...)`; new cases | 5, 6, 7 |
| `tests/test_config.py`, `test_auth.py`, `test_errors.py`, `test_cache.py` | new | 1, 3, 4, 7 |
| `tests/test_gemini.py` | quota and retry cases | 5 |
| `.github/workflows/ci.yml` | new | 8 |
| `README.md`, `.env.example`, `.dockerignore`, `pyproject.toml` | docs for each task | 1–7 |

---

### Task 1: Load `.env.local` and keep the app out of import time

**Files:**
- Modify: `app/config.py`
- Modify: `app/main.py` (last lines)
- Modify: `Dockerfile` (last line)
- Modify: `.dockerignore`, `README.md`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings` reads `(".env", ".env.local")`, and later files win. `app.main` no longer exposes `app`; it is served with `uvicorn app.main:create_app --factory`.

**Why the import change:** once `.env.local` is loaded, `app = create_app()` at import time would make every test that imports `app.main` depend on the developer's local file. A broken `.env.local` would then fail the offline suite.

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py`

```python
"""Where settings come from, and combinations that must fail at startup."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

SET_BY_FILES = ("AI_PROVIDER", "AI_MODEL_GRADE", "GEMINI_API_KEY", "AI_TIMEOUT_SECONDS")


def test_env_local_overrides_env(tmp_path, monkeypatch):
    for name in SET_BY_FILES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AI_PROVIDER=ollama\nAI_TIMEOUT_SECONDS=180\n")
    (tmp_path / ".env.local").write_text(
        "AI_PROVIDER=gemini\nGEMINI_API_KEY=local-key\nAI_TIMEOUT_SECONDS=60\n"
    )

    settings = Settings()

    assert settings.ai_provider == "gemini"
    assert settings.gemini_api_key == "local-key"
    assert settings.ai_timeout_seconds == 60


def test_a_gemini_model_under_the_ollama_provider_fails_at_startup():
    with pytest.raises(ValidationError, match="AI_PROVIDER=gemini"):
        Settings(
            _env_file=None, ai_provider="ollama", ai_model_grade="gemini-3.5-flash"
        )


def test_importing_the_app_reads_no_local_env_files(tmp_path):
    # A broken .env.local must not stop the test suite from importing the app.
    (tmp_path / ".env.local").write_text("AI_PROVIDER=nonsense\n")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run them and watch all three fail**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`
Expected: 2 failed:
- the provider is still `ollama`, because `.env.local` is ignored;
- no `ValidationError` is raised.

The import test passes for now, because `.env.local` is not read yet. It guards Step 3: it fails if the config change lands without removing `app = create_app()`.

- [ ] **Step 3: Implement**

In `app/config.py`, replace the `model_config` line:

```python
    # .env.local (gitignored, like .env) overrides .env for this machine.
    model_config = SettingsConfigDict(env_file=(".env", ".env.local"), extra="ignore")
```

In `configure_provider`, make this the first check inside the Ollama branch:

```python
        if self.ai_provider == "ollama":
            if self.ai_model_grade.startswith("gemini-"):
                raise ValueError(
                    "AI_MODEL_GRADE is a Gemini model; set AI_PROVIDER=gemini to use it"
                )
            if not self.ollama_url.startswith(("http://", "https://")):
```

In `app/main.py`, delete the last two lines (and the blank lines before them):

```python
app = create_app()
```

In `Dockerfile`, change the last line to:

```dockerfile
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: all tests pass, 7 skipped; ruff clean.

Check factory mode: run `.venv/bin/uvicorn app.main:create_app --factory --port 18001` in one shell and `curl -s localhost:18001/health` in another. Expected: `{"status":"ok"}`. Then stop the server.

**Heads-up:** from now on, the real `.env.local` (Gemini model, no `AI_PROVIDER`) makes the app refuse to start with "AI_MODEL_GRADE is a Gemini model; set AI_PROVIDER=gemini". That refusal is intended; Task 9 fixes the file.

- [ ] **Step 5: Docs**

- `.dockerignore`: add a line `.env.local` after `.env`.
- `README.md`, in *Local setup*:
  - Change the sentence starting "Fill in `SUPABASE_URL`…" to: "Fill in `SUPABASE_URL` and `SUPABASE_ANON_KEY` in `.env`. Settings are read from `.env`, then `.env.local`, whose values win; both are gitignored, so keep machine-specific values such as `GEMINI_API_KEY` in `.env.local`."
  - Change the run command to `uvicorn app.main:create_app --factory --reload --port 8000`.

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/main.py Dockerfile .dockerignore README.md tests/test_config.py
git commit -m "feat: read .env.local and build the app in a factory"
```

---

### Task 2: Spend Gemini quota in the live suite only on explicit opt-in

**Files:**
- Modify: `tests/test_pushover.py` (module docstring, `live` marker, the provider guard in the `grade` helper)
- Modify: `pyproject.toml` (the `live` marker description), `README.md`

**Interfaces:**
- Produces: `RUN_LIVE_AI=1` runs the live suite against Ollama and skips if the settings select Gemini. `RUN_LIVE_AI=gemini` runs it against Gemini and costs 4 requests.

- [ ] **Step 1: Prove the current risk without spending quota.** A dead proxy guarantees no request leaves the machine:

```bash
env HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 AI_PROVIDER=gemini \
  GEMINI_API_KEY=dummy AI_MODEL_GRADE=gemini-3.5-flash RUN_LIVE_AI=1 AI_MAX_RETRIES=0 \
  .venv/bin/python -m pytest -m live -q -rs
```

Expected (the bug): the tests *try* to call Gemini and fail with `provider_unavailable`. Without the proxy, they would have spent quota.

- [ ] **Step 2: Implement** in `tests/test_pushover.py`

Docstring usage lines:

```
    RUN_LIVE_AI=1 pytest -m live -s          # local Ollama
    RUN_LIVE_AI=gemini pytest -m live -s     # spends 4 free-tier Gemini requests
```

Marker:

```python
live = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_AI") not in ("1", "gemini"),
    reason="live provider test; set RUN_LIVE_AI=1 (Ollama) or RUN_LIVE_AI=gemini",
)
```

Replace the provider guard after `settings = Settings()`:

```python
    if settings.ai_provider == "gemini":
        # .env.local may select Gemini; only an explicit opt-in spends its quota.
        if os.environ.get("RUN_LIVE_AI") != "gemini":
            pytest.skip(
                "AI_PROVIDER=gemini: set RUN_LIVE_AI=gemini to spend 4 requests"
            )
        if not settings.gemini_api_key:
            pytest.fail("GEMINI_API_KEY is required when AI_PROVIDER=gemini")
```

- [ ] **Step 3: Verify both modes with the dead proxy.** Run the Step 1 command twice, once with `RUN_LIVE_AI=1` and once with `RUN_LIVE_AI=gemini`.
Expected:
- `RUN_LIVE_AI=1`: all live tests **skipped** with "set RUN_LIVE_AI=gemini to spend 4 requests".
- `RUN_LIVE_AI=gemini`: they fail with `provider_unavailable`. That failure is correct: the proxy blocked the call.

Then run `.venv/bin/python -m pytest -q`. Expected: the offline suite is still green.

- [ ] **Step 4: Docs**

- `pyproject.toml` marker: `"live: calls the model provider; skipped unless RUN_LIVE_AI is 1 (Ollama) or gemini",`
- `README.md`, *Tests*: replace the "To compare Gemini…" block with:

````markdown
To run it against Gemini, set its key in `.env.local`, then:

```bash
AI_PROVIDER=gemini AI_MODEL_GRADE=gemini-3.5-flash AI_MAX_OUTPUT_TOKENS=8000 RUN_LIVE_AI=gemini pytest -m live -s
```

This sends the synthetic fixtures to Gemini and spends four requests of the
daily quota. `RUN_LIVE_AI=1` never calls Gemini: if the settings select it, the
live tests skip.
````

  Keep the existing sentence "Switching providers does not automatically fall back to the cloud."

- [ ] **Step 5: Commit**

```bash
git add tests/test_pushover.py pyproject.toml README.md
git commit -m "test: require RUN_LIVE_AI=gemini before spending Gemini quota"
```

---

### Task 3: Supabase failures return the error envelope (defect #5)

**Files:**
- Modify: `app/supabase.py` (`validate_session`, `_request`, a new constant)
- Create: `tests/test_auth.py`
- Modify: `README.md` (errors table)

**Interfaces:**
- Consumes: `tests.conftest.make_client(supabase=...)` and `tests.test_grade.post(client)`, both existing.
- Produces: `app.supabase.AUTH_TIMEOUT_SECONDS = 10`. New error codes: `not_configured` (503), `auth_unavailable` (503), `supabase_request_failed` (502, now also for non-JSON bodies).

- [ ] **Step 1: Write the failing tests** in `tests/test_auth.py`

```python
"""Caller authentication against Supabase, with HTTP stubbed at the transport."""

import httpx
import pytest

from app.config import Settings
from app.supabase import SupabaseGateway
from tests.conftest import make_client
from tests.test_grade import post

CONFIGURED = {
    "supabase_url": "https://example.supabase.co",
    "supabase_anon_key": "anon",
}


def gateway(handler, **settings) -> SupabaseGateway:
    config = Settings(_env_file=None, **{**CONFIGURED, **settings})
    return SupabaseGateway(config, transport=httpx.MockTransport(handler))


def unreachable(request):
    raise httpx.ConnectError("unreachable", request=request)


def responds(status, **body):
    return lambda request: httpx.Response(status, **body)


@pytest.mark.parametrize(
    "handler,settings,status,code",
    [
        (unreachable, {}, 503, "auth_unavailable"),
        (
            responds(200, text="<html>proxy page</html>"),
            {},
            502,
            "supabase_request_failed",
        ),
        (responds(500, json={}), {}, 502, "supabase_request_failed"),
        (responds(401, json={}), {}, 401, "authentication_required"),
        (responds(200, json={}), {}, 401, "authentication_required"),
        (responds(200, json={"id": "u"}), {"supabase_url": ""}, 503, "not_configured"),
        (
            responds(200, json={"id": "u"}),
            {"supabase_anon_key": ""},
            503,
            "not_configured",
        ),
    ],
)
def test_auth_failures_keep_the_error_envelope(handler, settings, status, code):
    response = post(make_client(supabase=gateway(handler, **settings)))
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert error["request_id"]


async def test_auth_does_not_inherit_the_long_model_timeout():
    # The shared client waits up to AI_TIMEOUT_SECONDS (180 s) for a grade;
    # a stuck Supabase call must fail long before that.
    seen = {}

    def handler(request):
        seen.update(request.extensions["timeout"])
        return httpx.Response(200, json={"id": "user-1"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(timeout=180, transport=transport) as shared:
        auth = SupabaseGateway(
            Settings(_env_file=None, **CONFIGURED), http_client=shared
        )
        session = await auth.validate_session("token")

    assert session.user_id == "user-1"
    assert seen["read"] == 10
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_auth.py -q`
Expected:
- The unreachable, HTML and `not_configured` cases fail: the exception escapes through `TestClient` (in production, a bare 500).
- The timeout test fails because the read timeout is 180, not 10.
- The existing 401/500/missing-id cases pass.

- [ ] **Step 3: Implement** in `app/supabase.py`

Below the imports:

```python
AUTH_TIMEOUT_SECONDS = 10
```

Replace `validate_session`:

```python
    async def validate_session(self, token: str) -> Session:
        if not self._base_url or not self._anon_key:
            raise AppError(
                503, "not_configured", "SUPABASE_URL and SUPABASE_ANON_KEY must be set."
            )
        try:
            response = await self._request("GET", "/auth/v1/user", token)
        except httpx.TransportError as exc:
            raise AppError(
                503, "auth_unavailable", "Supabase could not be reached."
            ) from exc
        if response.status_code in (401, 403):
            raise AppError(401, "authentication_required", "Authentication required.")
        if response.is_error:
            raise AppError(502, "supabase_request_failed", "Upstream request failed.")
        try:
            user_id = response.json().get("id")
        except (ValueError, AttributeError) as exc:
            # A proxy or captive portal answering with HTML is still an upstream failure.
            raise AppError(
                502, "supabase_request_failed", "Upstream request failed."
            ) from exc
        if not user_id:
            raise AppError(401, "authentication_required", "Authentication required.")
        return Session(user_id=str(user_id), token=token)
```

Replace the body of `_request` after the `headers = ...` line:

```python
        url = f"{self._base_url}{path}"
        # The shared client waits up to AI_TIMEOUT_SECONDS for a model; auth must not.
        if self._http_client is not None:
            return await self._http_client.request(
                method, url, headers=headers, timeout=AUTH_TIMEOUT_SECONDS, **kwargs
            )
        async with httpx.AsyncClient(
            transport=self._transport, timeout=AUTH_TIMEOUT_SECONDS
        ) as client:
            return await client.request(method, url, headers=headers, **kwargs)
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: all pass; ruff clean. If ruff format complains, run `.venv/bin/ruff format app tests` and rerun.

- [ ] **Step 5: Docs.** In the `README.md` errors table, add these rows after `model_not_found`:

```markdown
| `supabase_request_failed` | 502 | Supabase answered with an error or a non-JSON body. |
| `not_configured` | 503 | `SUPABASE_URL` or `SUPABASE_ANON_KEY` is not set. |
| `auth_unavailable` | 503 | Supabase could not be reached. |
```

- [ ] **Step 6: Commit**

```bash
git add app/supabase.py tests/test_auth.py README.md
git commit -m "fix: return the error envelope when Supabase auth fails"
```

---

### Task 4: One error envelope for malformed requests and unexpected failures

**Files:**
- Modify: `app/main.py` (imports, a module-level helper, handlers inside `create_app`)
- Create: `tests/test_errors.py`
- Modify: `README.md` (errors section)

**Interfaces:**
- Produces: `app.main.error_response(request, status_code, code, message) -> JSONResponse`, which Task 5 uses. New codes: `invalid_request` (422) and `internal_error` (500).

- [ ] **Step 1: Write the failing tests** in `tests/test_errors.py`

```python
"""Every error the API returns has the same envelope, whatever raised it."""

from fastapi.testclient import TestClient

from app.main import create_app
from app.service import GradeService
from tests.conftest import StubGemini, StubSupabase, make_client, settings
from tests.test_grade import post


def test_a_malformed_request_uses_the_error_envelope():
    response = make_client().post(
        "/api/v1/ai/grade-talk-track",
        headers={"Authorization": "Bearer token"},
        json={"board_id": "not-a-uuid"},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert "board_id" in error["message"]
    assert error["request_id"]


def test_an_unexpected_failure_uses_the_error_envelope():
    config = settings()
    broken = StubGemini(fail=RuntimeError("provider adapter bug"))
    service = GradeService(config, StubSupabase(), broken)
    client = TestClient(
        create_app(config, grade_service=service), raise_server_exceptions=False
    )

    response = post(client)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert "provider adapter bug" not in error["message"]
    assert error["request_id"]
```

`raise_server_exceptions=False` is needed because `TestClient` otherwise re-raises the exception instead of returning the 500 response.

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_errors.py -q`
Expected: 2 failed:
- the 422 body is FastAPI's `{"detail": [...]}`, so `KeyError: 'error'`;
- the 500 body is plain text, so the JSON decode fails.

- [ ] **Step 3: Implement** in `app/main.py`

Imports: add `import logging` at the top, and `from fastapi.exceptions import RequestValidationError` after the `fastapi` import.

Below `bearer = HTTPBearer(auto_error=False)`:

```python
logger = logging.getLogger(__name__)


def error_response(
    request: Request, status_code: int, code: str, message: str
) -> JSONResponse:
    """The single error shape clients can rely on."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": getattr(request.state, "request_id", None),
            }
        },
    )
```

Replace the existing `handle_app_error`, and add two handlers after it:

```python
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_invalid_request(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"])
        return error_response(
            request, 422, "invalid_request", f"{field}: {first['msg']}"
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Logged with the traceback; the caller only gets an opaque message.
        logger.exception("Unhandled error on %s", request.url.path)
        return error_response(request, 500, "internal_error", "Something went wrong.")
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: all pass; ruff clean.

- [ ] **Step 5: Docs** (`README.md`, *Errors*)

- Change the first line to: "Every error returns `{"error": {"code", "message", "request_id"}}`, including malformed requests and unexpected failures."
- Add a row after `authentication_required`, and one at the end of the table:

```markdown
| `invalid_request` | 422 | The body does not match the schema; the message names the first bad field. |
| `internal_error` | 500 | Unexpected failure; logged with its traceback. |
```

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_errors.py README.md
git commit -m "fix: use the error envelope for invalid requests and unexpected failures"
```

---

### Task 5: Quota-aware 429s, and retry only requests that never left

**Files:**
- Modify: `app/errors.py`, `app/gemini.py`, `app/main.py` (`handle_app_error`)
- Test: `tests/test_gemini.py`, `tests/test_grade.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `error_response` (Task 4).
- Produces:
  - `AppError(..., retry_after: int | None = None)`.
  - `app.gemini.seconds_until_quota_reset(now: datetime) -> int`.
  - `app.gemini.rate_limit_error(body: str, now: datetime, retries: int) -> AppError`.
  - `GeminiClient(..., clock: Callable[[], datetime])`.
  - New code `provider_quota_exhausted` (429). Every 429 response carries `Retry-After`.

**Why only unsent requests are retried:** after a `ReadTimeout`, Google may already have processed the request and counted it. A retry would spend a second request out of 20 for one grade. `ConnectError`, `ConnectTimeout` and `PoolTimeout` mean the request never left.

- [ ] **Step 1: Write the failing tests** in `tests/test_gemini.py`

Add `from datetime import datetime, timezone` after `import json`. Change the import to `from app.gemini import GeminiClient, seconds_until_quota_reset`. Replace the `gemini` helper and add the fixtures:

```python
# 08:00 Pacific daylight time.
PACIFIC_MORNING = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)

# Shape of a Gemini 429 on the OpenAI-compatible endpoint (trimmed).
DAILY_QUOTA_BODY = json.dumps(
    [
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "You exceeded your current quota.",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                "quotaValue": "20",
                            }
                        ],
                    },
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "48s",
                    },
                ],
            }
        }
    ]
)
MINUTE_QUOTA_BODY = DAILY_QUOTA_BODY.replace("PerDay", "PerMinute")


async def no_sleep(_seconds: float) -> None:
    pass


def gemini(handler, now: datetime = PACIFIC_MORNING) -> GeminiClient:
    return GeminiClient(
        Settings(_env_file=None, ai_provider="gemini", gemini_api_key="key"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=no_sleep,
        clock=lambda: now,
    )
```

Change the `generate` helper to pass the clock through:

```python
async def generate(handler, now: datetime = PACIFIC_MORNING) -> AppError:
    with pytest.raises(AppError) as raised:
        await gemini(handler, now).generate(
            "gemini-3.5-flash", "system", {}, GradeSuggestion
        )
    return raised.value
```

Append:

```python
async def test_a_spent_daily_quota_says_when_it_resets():
    error = await generate(lambda request: httpx.Response(429, text=DAILY_QUOTA_BODY))
    assert error.code == "provider_quota_exhausted"
    assert error.retry_after == 16 * 3600  # 08:00 to midnight Pacific


async def test_a_per_minute_limit_passes_on_the_providers_retry_delay():
    error = await generate(lambda request: httpx.Response(429, text=MINUTE_QUOTA_BODY))
    assert (error.code, error.retry_after) == ("provider_limited", 48)


def test_the_daily_reset_accounts_for_the_end_of_daylight_saving():
    # 1 Nov 2026, 00:30 PDT: clocks go back tonight, so midnight is 24.5 hours away.
    now = datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc)
    assert seconds_until_quota_reset(now) == 24 * 3600 + 30 * 60


async def test_a_request_that_may_have_reached_gemini_is_not_retried():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    error = await generate(handler)
    assert (error.code, calls) == ("provider_unavailable", 1)


async def test_a_request_that_never_left_is_retried():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused", request=request)

    error = await generate(handler)
    assert (error.code, calls) == ("provider_unavailable", 2)
```

In `tests/test_grade.py`, class `TestRefusals`, add before `test_every_error_carries_a_request_id`:

```python
    def test_a_rate_limit_tells_the_client_when_to_retry(self):
        limited = AppError(
            429, "provider_quota_exhausted", "Used up.", retry_after=3600
        )
        response = post(make_client(gemini=StubGemini(fail=limited)))
        assert response.status_code == 429
        assert response.headers["retry-after"] == "3600"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_gemini.py tests/test_grade.py -q`
Expected: a collection error, `cannot import name 'seconds_until_quota_reset'`. After the import is fixed, the new tests fail on the missing `clock`/`retry_after` or on the wrong call counts.

- [ ] **Step 3: Implement**

In `app/errors.py`, add the parameter `retry_after: int | None = None,` after `retries`, and at the end of `__init__`:

```python
        # Seconds the client should wait before retrying; sent as Retry-After.
        self.retry_after = retry_after
```

In `app/gemini.py`, imports: add `import logging`, `import re`, `from datetime import datetime, time, timedelta, timezone` and `from zoneinfo import ZoneInfo`. Below `GEMINI_OPENAI_ENDPOINT`:

```python
logger = logging.getLogger(__name__)

# Free-tier requests-per-day quotas reset at midnight Pacific time.
QUOTA_RESET_ZONE = ZoneInfo("America/Los_Angeles")
RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"(\d+)')
DEFAULT_RETRY_AFTER_SECONDS = 60
# Failures where the request never reached Gemini, so a retry spends no quota.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def seconds_until_quota_reset(now: datetime) -> int:
    local = now.astimezone(QUOTA_RESET_ZONE)
    midnight = datetime.combine(
        local.date() + timedelta(days=1), time(), QUOTA_RESET_ZONE
    )
    # Subtract in UTC: same-zone arithmetic ignores a DST change in between.
    remaining = midnight.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    return max(1, int(remaining.total_seconds()))


def rate_limit_error(body: str, now: datetime, retries: int) -> AppError:
    """Tell a spent daily quota apart from a per-minute limit.

    Gemini names the exhausted quota in the 429 body, for example
    GenerateRequestsPerDayPerProjectPerModel-FreeTier. Its retryDelay only
    means something for per-minute limits; a daily quota returns at midnight
    Pacific time.
    """
    if "PerDay" in body:
        return AppError(
            429,
            "provider_quota_exhausted",
            "Today's free Gemini quota is used up; it resets at midnight Pacific time.",
            retries=retries,
            retry_after=seconds_until_quota_reset(now),
        )
    delay = RETRY_DELAY.search(body)
    return AppError(
        429,
        "provider_limited",
        "The AI provider is rate limited.",
        retries=retries,
        retry_after=int(delay.group(1)) if delay else DEFAULT_RETRY_AFTER_SECONDS,
    )
```

In `GeminiClient.__init__`, add the parameter `clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),` after `random`, and set `self.clock = clock`.

In the request loop of `generate`, replace the transport `except` and the 429 branch:

```python
            except NOT_SENT as exc:
                if retries >= self.settings.ai_max_retries:
                    raise self._unavailable(retries) from exc
                await self._retry_delay(retries)
                retries += 1
                continue
            except httpx.TransportError as exc:
                # It may have reached Gemini and counted against the daily quota.
                raise self._unavailable(retries) from exc

            if response.status_code == 429:
                # No secrets in the body: quota ids, model name, retry delay.
                logger.warning("Gemini rate limited: %s", response.text[:500])
                raise rate_limit_error(response.text, self.clock(), retries)
```

In `app/main.py`, `handle_app_error` becomes:

```python
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        response = error_response(request, exc.status_code, exc.code, exc.message)
        if exc.retry_after is not None:
            response.headers["Retry-After"] = str(exc.retry_after)
        return response
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: all pass; ruff clean.

Check the timezone in the image once: `docker run --rm python:3.11-slim python -c "import zoneinfo; zoneinfo.ZoneInfo('America/Los_Angeles')"`. Expected: no output, exit 0.

- [ ] **Step 5: Docs** (`README.md` errors table). Replace the `provider_limited` row and add one after it:

```markdown
| `provider_limited` | 429 | Short-term provider rate limit; `Retry-After` says when to retry. |
| `provider_quota_exhausted` | 429 | Today's Gemini quota is used up; `Retry-After` points at midnight Pacific time. |
```

- [ ] **Step 6: Commit**

```bash
git add app/errors.py app/gemini.py app/main.py tests/test_gemini.py tests/test_grade.py README.md
git commit -m "feat: report when Gemini quota returns and stop retrying sent requests"
```

---

### Task 6: Accept quotes that differ only in whitespace

**Files:**
- Modify: `app/service.py` (`validate_evidence`)
- Test: `tests/test_grade.py`
- Modify: `README.md` (*What makes the grade trustworthy*)

**Interfaces:**
- Produces: `app.service.squash_whitespace(text: str) -> str`.

- [ ] **Step 1: Write the failing test** in `TestRefusals`, before `test_requires_a_bearer_token`:

```python
    def test_a_quote_differing_only_in_line_breaks_is_accepted(self):
        # Models often quote a line break as a space; a 502 would waste quota.
        wrapped = "The database at 17k/s.\nCache-aside on Redis,\nthen read replicas."
        sections = {**REAL_ANSWER, "bottleneck": wrapped}
        quoted = {**sections, "bottleneck": "Cache-aside on Redis, then read replicas."}
        gemini = StubGemini(suggestion(["covered"] * 6, quoted))
        assert post(make_client(gemini=gemini), sections=sections).status_code == 200
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_grade.py -q`
Expected: 1 failed (`502 != 200`).

- [ ] **Step 3: Implement** in `app/service.py`

```python
def squash_whitespace(text: str) -> str:
    return " ".join(text.split())


def validate_evidence(suggestion: GradeSuggestion, sections: dict) -> None:
    """Reject model quotations that are absent from the candidate's section.

    Whitespace is compared loosely: models often quote a line break as a space.
    """
    for grade in suggestion.sections:
        evidence = squash_whitespace(grade.evidence)
        if evidence and evidence not in squash_whitespace(sections[grade.section]):
```

(The `raise AppError(502, "invalid_model_response", ...)` body below stays unchanged.)

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: all pass. The existing fabricated-quote test still returns 502.

- [ ] **Step 5: Docs.** In `README.md`, replace "Production currently checks presence only; source matching remains a WIP guard, so schema-valid output alone does not establish that a grade is grounded." (the service already matches quotes) with:

```markdown
quote occurs verbatim in the corresponding input section, and so does the
service: a quotation that is not in the candidate's section is rejected with
`invalid_model_response`. Whitespace is compared loosely, because models often
quote a line break as a space.
```

- [ ] **Step 6: Commit**

```bash
git add app/service.py tests/test_grade.py README.md
git commit -m "fix: match evidence quotes regardless of line breaks"
```

---

### Task 7: Cache grades in memory so unchanged reasoning costs nothing

**Files:**
- Modify: `app/config.py` (`ai_grade_cache_size`), `app/service.py` (docstring, imports, `GradeCache`, `grade_cache_key`, `GradeService`)
- Modify: `tests/conftest.py` (`settings`, `make_client`), `tests/test_grade.py` (`post`)
- Create: `tests/test_cache.py`
- Modify: `README.md`, `.env.example`

**Interfaces:**
- Consumes: `apply_placeholder_floor` (baseline) and `validate_evidence` (Task 6).
- Produces:
  - `Settings.ai_grade_cache_size: int` (default 256, `>= 0`).
  - `GradeCache(size)` with `get(key) -> GradeSuggestion | None` and `put(key, grade)`.
  - `grade_cache_key(user_id, model, context) -> str`.
  - `settings(**overrides)` and `make_client(supabase=None, gemini=None, **overrides)`.
  - `post(client, sections=None, self_rating=None, token="token")`.

**Design:**
- **The key.** It is the user id, the model, the system prompt and the exact context sent. A prompt change or any edit therefore regrades, and users never share grades.
- **What is stored.** Only successful, validated, floored suggestions are stored, never errors. `request_id` is still fresh per request.
- **Limits.** `ponytail:` the cache is per process and lost on restart. Persisting through `arch_boards.talk_grade` is the upgrade path, once the client integration exists.

- [ ] **Step 1: Test helpers.** In `tests/conftest.py`, change `def settings() -> Settings:` to `def settings(**overrides) -> Settings:` and add `**overrides,` as the last argument of its `Settings(...)` call. Change `make_client` to:

```python
def make_client(supabase=None, gemini=None, **overrides) -> TestClient:
    from app.service import GradeService

    config = settings(**overrides)
```

In `tests/test_grade.py`, change `post` to take `token="token"` and send `headers={"Authorization": f"Bearer {token}"}`.

- [ ] **Step 2: Write the failing tests** in `tests/test_cache.py`

```python
"""Unchanged reasoning is graded once, so resubmitting it costs no quota."""

from app.errors import AppError
from app.supabase import Session
from tests.conftest import StubGemini, make_client
from tests.test_grade import REAL_ANSWER, post, suggestion


class TokenIsUser:
    """Each bearer token is its own user, so one client can act as several."""

    async def validate_session(self, token: str) -> Session:
        return Session(user_id=token, token=token)


def test_resubmitting_unchanged_reasoning_does_not_call_the_model_again():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(gemini=gemini)

    first, second = post(client).json(), post(client).json()

    assert len(gemini.calls) == 1
    assert first["score"] == second["score"] == 50
    assert first["request_id"] != second["request_id"]


def test_edited_reasoning_is_graded_again():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(gemini=gemini)
    edited = {**REAL_ANSWER, "tradeoff": REAL_ANSWER["tradeoff"] + " Revisit at 10x."}

    post(client)
    post(client, sections=edited)

    assert len(gemini.calls) == 2


def test_grades_are_not_shared_between_users():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(supabase=TokenIsUser(), gemini=gemini)

    post(client, token="alice")
    post(client, token="bob")

    assert len(gemini.calls) == 2


def test_failures_are_not_cached():
    gemini = StubGemini(fail=AppError(429, "provider_limited", "Rate limited."))
    client = make_client(gemini=gemini)

    post(client)
    post(client)

    assert len(gemini.calls) == 2


def test_the_least_recently_used_grade_is_evicted_first():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(supabase=TokenIsUser(), gemini=gemini, ai_grade_cache_size=2)

    for token in ("alice", "bob", "alice", "carol", "alice"):
        post(client, token=token)

    # alice was reused before carol arrived, so bob's grade made room.
    assert len(gemini.calls) == 3


def test_a_zero_size_cache_always_calls_the_model():
    gemini = StubGemini(suggestion(["thin"] * 6))
    client = make_client(gemini=gemini, ai_grade_cache_size=0)

    post(client)
    post(client)

    assert len(gemini.calls) == 2
```

- [ ] **Step 3: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_cache.py -q`
Expected: 2 failed, the unchanged-reasoning test (2 calls, not 1) and the LRU test (5 calls, not 3).

The edited, per-user, failures and zero-size tests already pass: with no cache, every request calls the model, and `Settings` ignores the unknown `ai_grade_cache_size`. They guard against over-caching once the cache exists.

- [ ] **Step 4: Implement**

In `app/config.py`, after `ai_reasoning_effort`:

```python
    # Recent grades kept in memory so unchanged reasoning is not re-graded; 0 disables.
    ai_grade_cache_size: int = Field(default=256, ge=0)
```

(`Field` is already imported from `pydantic`.)

In `app/service.py`, replace the first paragraph of the module docstring:

```
Nothing here is persisted, matching ativscrum-ai-api. The caller sends the
board's reasoning plus the facts derived from it, and gets a grade back to store
itself through its own Supabase session. Recent grades are only cached in
memory, so resubmitting unchanged reasoning costs no provider quota.
```

Add the imports `import hashlib`, `import json` and `from collections import OrderedDict`. Before `class GradeService`:

```python
class GradeCache:
    """The most recent successful grades, keyed by everything the model sees.

    ponytail: in-process LRU, lost on restart and not shared between workers;
    persist through arch_boards.talk_grade once the client integration lands.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self._grades: OrderedDict[str, GradeSuggestion] = OrderedDict()

    def get(self, key: str) -> GradeSuggestion | None:
        grade = self._grades.get(key)
        if grade is not None:
            self._grades.move_to_end(key)
        return grade

    def put(self, key: str, grade: GradeSuggestion) -> None:
        self._grades[key] = grade
        self._grades.move_to_end(key)
        if len(self._grades) > self.size:
            self._grades.popitem(last=False)


def grade_cache_key(user_id: str, model: str, context: dict) -> str:
    # The rubric is part of the key, so a prompt change regrades everything.
    material = json.dumps([user_id, model, SYSTEM_PROMPT, context], sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()
```

With size 0, `put` inserts and immediately evicts, so no special case is needed.

In `GradeService.__init__`, add `self.cache = GradeCache(settings.ai_grade_cache_size)`. In `grade()`:
- Change `await self.supabase.validate_session(token)` to `session = await self.supabase.validate_session(token)`.
- Replace everything from the `self.provider.generate(...)` call through `suggestion = apply_placeholder_floor(...)` with:

```python
        model = self.settings.ai_model_grade
        context = build_context(
            payload.facts.model_dump(), sections, payload.self_rating
        )
        key = grade_cache_key(session.user_id, model, context)
        suggestion = self.cache.get(key)
        if suggestion is None:
            result = await self.provider.generate(
                model, SYSTEM_PROMPT, context, GradeSuggestion
            )
            validate_evidence(result.value, sections)
            suggestion = apply_placeholder_floor(result.value, sections)
            self.cache.put(key, suggestion)
```

Finally, change `model=self.settings.ai_model_grade` in the `GradeResponse(...)` call to `model=model`.

- [ ] **Step 5: Run the suite, then check that the LRU test catches a FIFO slip**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: `82 passed, 7 skipped`; ruff clean.

Mutation check: temporarily delete the two lines in `GradeCache.get` that call `move_to_end`, then run `.venv/bin/python -m pytest tests/test_cache.py -q`. Expected: `1 failed` (the LRU test). Restore the lines with `git checkout -p app/service.py` or by hand, and rerun the suite.

- [ ] **Step 6: Docs**

- `.env.example`, after `AI_REASONING_EFFORT=low`:

```
AI_GRADE_CACHE_SIZE=256
# Machine-specific values (GEMINI_API_KEY) go in .env.local, which overrides this file.
```

- `README.md`:
  - The first line becomes "FastAPI service that grades…" (drop "Stateless").
  - Replace "The service is otherwise stateless. It stores nothing: no prompts, no reasoning, no grades." with "The service stores nothing durable: no prompts, no reasoning, no grades on disk. It keeps recent grades in memory (see *Gemini free tier*) and forgets them on restart."
  - Configuration table, a new last row:

    ```markdown
    | `AI_GRADE_CACHE_SIZE` | no | Grades kept in memory per process; defaults to `256`, `0` disables. |
    ```
  - Under *Not built yet*, replace the content-hash cache bullet with: "Persisting grades (`arch_boards.talk_grade`), so a grade survives restarts instead of living only in the in-memory cache."
  - Before `## Container`, add:

```markdown
## Gemini free tier

The free tier limits requests per project and per model. At the time of
writing a Flash model allowed 20 requests a day; check the current numbers in
Google AI Studio. The daily count resets at midnight Pacific time.

- **Identical requests are free.** A grade is cached in memory, keyed by user,
  model, rubric and the exact reasoning and facts sent. Re-rendering a board or
  retrying a click does not spend quota; editing any section does.
- **Quota errors say when to retry.** A spent daily quota returns
  `provider_quota_exhausted` with `Retry-After` set to the reset; a per-minute
  limit returns `provider_limited` with Google's suggested delay. Neither is
  retried by the service.
- **Only unsent requests are retried.** A connection failure is retried
  (`AI_MAX_RETRIES`); a read timeout is not, because Google may already have
  counted it.
- **The live suite costs 4 requests** and runs only with `RUN_LIVE_AI=gemini`.
- **The cache is per process.** Restarts and extra workers start empty. Run one
  worker on the free tier.
```

- Check that nothing stale remains: `grep -rn -e 'app.main:app' -e 'tateless' -e 'presence only' --exclude-dir=.git --exclude-dir=.venv --exclude-dir=docs .` should print nothing.

- [ ] **Step 7: Commit**

```bash
git add app/config.py app/service.py tests/conftest.py tests/test_grade.py tests/test_cache.py README.md .env.example
git commit -m "feat: cache grades in memory so unchanged reasoning spends no quota"
```

---

### Task 8: CI for the offline suite

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:** none. CI needs no secrets, and the live tests skip there because `RUN_LIVE_AI` is unset.

- [ ] **Step 1: Write the workflow**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install
        run: python -m pip install ".[dev]"

      - name: Lint
        run: |
          ruff check .
          ruff format --check .

      # Offline suite only: live tests skip without RUN_LIVE_AI, so CI needs no
      # Gemini key and spends no quota.
      - name: Test
        run: pytest -q
```

- [ ] **Step 2: Reproduce the job locally in a clean venv, on internal disk** (the exFAT drive breaks the wheel build; see the README):

```bash
rm -rf /tmp/grip-ci && mkdir /tmp/grip-ci
rsync -a --exclude '._*' --exclude .venv --exclude .git --exclude '.env*' ./ /tmp/grip-ci/src/
python3.11 -m venv /tmp/grip-ci/venv
cd /tmp/grip-ci/src && /tmp/grip-ci/venv/bin/python -m pip install ".[dev]"
. /tmp/grip-ci/venv/bin/activate && ruff check . && ruff format --check . && pytest -q
```

Expected: ruff clean; `82 passed, 7 skipped`. Then `rm -rf /tmp/grip-ci`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run lint and the offline suite"
```

After pushing, confirm the run is green: `gh run list --limit 1`.

---

### Task 9: Budgeted live verification (manual, 5 Gemini requests)

Do this on a day with quota left. **Budget: 4 requests for the live suite + 1 for the end-to-end grade.** Do not rerun anything casually. If a `provider_quota_exhausted` arrives, stop: `Retry-After` says when the quota resets.

**Files:** `.env.local` only (gitignored; never commit or print it).

- [ ] **Step 1: Fix `.env.local`.** Edit it in an editor; do not `cat` it.
  - Add `AI_PROVIDER=gemini`.
  - Keep one `AI_MAX_OUTPUT_TOKENS`, set to `8000`.
  - Keep one `AI_TIMEOUT_SECONDS`.
  - Keep `AI_MODEL_GRADE=gemini-3.5-flash`.
  - Check the result by key name only: `cut -d= -f1 .env.local | sort | uniq -d` must print nothing.
  - Then `.venv/bin/python -c "from app.config import Settings; s = Settings(); print(s.ai_provider, s.ai_model_grade, s.ai_max_output_tokens, bool(s.gemini_api_key))"` should print `gemini gemini-3.5-flash 8000 True`.

  From now on, local Ollama runs need an explicit override:
  `AI_PROVIDER=ollama AI_MODEL_GRADE=grip-grader AI_MAX_OUTPUT_TOKENS=2048 RUN_LIVE_AI=1 .venv/bin/python -m pytest -m live -s`.

- [ ] **Step 2: Offline suite, unchanged.** Run `.venv/bin/python -m pytest -q`. Expected: `82 passed, 7 skipped`, with no network use.

- [ ] **Step 3: Live suite (4 requests).** Run `RUN_LIVE_AI=gemini .venv/bin/python -m pytest -m live -s`.
Expected: all 7 live tests pass, using 4 generations. Write down the printed latencies and verdicts in the PR description.

- [ ] **Step 4: One end-to-end grade, then a cache hit (1 request)**
  1. Start the API: `.venv/bin/uvicorn app.main:create_app --factory --port 8000`.
  2. Sign in to the web app and copy an access token from the browser session. Keep it in a shell variable only (`read -s TOKEN`). Never paste it into a file or chat.
  3. Send the request from the README *Endpoint* example: a real `board_id` UUID, the catalog facts, and six short but real sections. Send it with `curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d @/tmp/grade.json localhost:8000/api/v1/ai/grade-talk-track`.
     Expected: 200 with `score`, six verdicts and quoted evidence.
  4. Send the identical request again. Expected: 200, the same `score`, a different `request_id`, and an instant response (no Gemini call; the server log shows no outbound request).
  5. Stop the server, `unset TOKEN`, and `rm /tmp/grade.json`.

- [ ] **Step 5: Record the outcome.** Note in the PR:
  - the quota spent (5);
  - the reset time (midnight Pacific; 09:00 CEST in summer);
  - any 429 body's `quotaId`, as logged by the `Gemini rate limited` warning.

---

## Out of scope

- **Switching to `gemini-3.8-flash`.** Google's model page lists `gemini-3.5-flash` as the legacy Flash model. Quota is per model, so switching would add a fresh 20/day. But `STRICT_GEMINI_MODELS` must only list models verified to support strict JSON output, so treat that as its own change, with its own live run.
- **Persisting grades or sharing the cache between workers.** This waits for `arch_boards.talk_grade`.
- **Client UI for `Retry-After`.** The web/mobile integration is not built yet.
