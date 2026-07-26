# grip-ai-api

Stateless FastAPI service that grades the **talk track** of a Grip Arch Board
round — the written reasoning a candidate produces alongside a system design.

The board itself already scores deterministically: which components exist, how
they are wired, whether the numbers work. That is checked in
[`packages/core`](../tech-refresh/packages/core) and needs no model. What a
diagram cannot score is whether the reasoning behind it would survive an
interviewer. That is what this service does.

Built on the same shape as `ativscrum-ai-api`: Supabase token validation, the
anon key plus the caller's bearer token so RLS stays the boundary, strict JSON
output, and no service-role key anywhere in the service.

## What makes the grade trustworthy

An LLM asked to "rate this answer" returns a flattering number for almost
anything. That failure mode would be worse than no grader at all, because it
looks objective. Three things are arranged so leniency has nowhere to hide.

**The model never returns a score.** It classifies each of the six talk-track
sections as `covered` / `thin` / `missing`. The score is computed from those
verdicts in `service.py`. Generosity can therefore only appear as a specific
wrong verdict on a specific section — which is testable — never as a quietly
inflated number.

**Credit requires a quotation.** `SectionGrade` rejects any verdict other than
`missing` that does not carry a verbatim span from the candidate's own text.
This is a schema rule, not a prompt request: an ungrounded grade cannot be
represented, so the provider response fails validation and the caller gets
`invalid_model_response` instead of praise.

**Grading is against ground truth, not vibes.** The request carries the
scenario's derived figures — peak requests per second, storage over the
retention window, which design checks passed, which partition keys were
declared. The rubric asks the model to compare the candidate's arithmetic to
those numbers rather than to judge whether it "looks right".

The rubric also grades by absence: every section must come back with the next
question an interviewer would ask. "What is missing" is far harder to answer
generously than "how good is this".

## Trust boundary

**The caller supplies the ground-truth facts.** The arithmetic that produces
peak QPS and storage lives in `packages/core/src/estimation.js`. This service
does not re-derive it, because a second implementation in Python would drift
from the first and there would be no way to know which one was right.

The consequence is deliberate and worth stating plainly: a caller can send
facts that make its own answer look correct. For a personal interview-prep tool
this is not a threat — the only person who can be misled is the one doing the
cheating. Do not reuse this service in a context where the grade is an
assessment of someone by someone else without moving fact derivation
server-side first.

The service is otherwise stateless. It stores nothing: no prompts, no
reasoning, no grades. The web app persists the returned grade through its own
Supabase session, so RLS governs the write and this service needs no database
access at all.

## Requirements

- Python 3.11
- a Supabase project (only `/auth/v1/user` is called, to validate the caller)
- a Google AI Studio (Gemini) API key

## Local setup

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

```bash
curl http://localhost:8000/health   # {"status":"ok"} — no config needed
curl http://localhost:8000/ready    # names any missing secret
```

Set `ALLOWED_ORIGINS` to the exact frontend origins that may call the API. Do
not use `*`.

> **Working on an exFAT volume?** macOS writes AppleDouble sidecars (`._*`)
> that break the wheel build with "multiple .dist-info directories". Install
> the dependencies directly instead of the project
> (`pip install fastapi==... pytest==...`) and run pytest from the project
> root; the package does not need to be installed to test or serve. Docker
> builds are unaffected because they run on Linux.

## Tests

```bash
pytest -q          # offline: schema guards, scoring, endpoint behaviour
ruff check .
ruff format --check .
```

The offline suite proves the *plumbing* is honest. It cannot prove the model
applies the rubric rather than being agreeable — only a real call can do that:

```bash
GEMINI_API_KEY=... pytest -m live
```

Budget those calls. The free tier allows **20 requests per day per model** and
the live suite spends 6, so there are three runs in a day and ad-hoc debugging
scripts come out of the same pool. A 429 here reports `retryDelay: 48s`, which
is misleading — the daily quota does not reset for hours.

`tests/test_pushover.py` sends three deliberately bad talk tracks — fluent
hand-waving, right-shape-wrong-arithmetic, and bare placeholders — and asserts
none of them are graded `covered`. **Run these after any prompt edit and before
trusting a grade.** If a fixture starts passing, the rubric has gone soft and
the feature is actively misleading.

## Endpoint

`POST /api/v1/ai/grade-talk-track`, bearer token required.

```jsonc
{
  "board_id": "uuid",
  "facts": {
    "scenario_id": "catalog",
    "name": "Read-heavy product catalog",
    "brief": "...",
    "dau": 8000000,
    "payload_kb": 4,
    "retention_days": 1825,
    "peak_qps": 16666.7,          // derived in packages/core
    "storage_gb": 1168,           // derived in packages/core
    "checks_passed": ["..."],
    "checks_failed": ["..."],
    "node_types": ["client", "cdn", "service", "sql"],
    "partition_keys": [],
    "pushback": "..."
  },
  "sections": { "requirements": "...", "scale": "...", "api": "...",
                "dataModel": "...", "bottleneck": "...", "tradeoff": "..." },
  "self_rating": 4
}
```

Returns a computed `score`, a `divergence` (how many points the candidate
over-rated themselves, positive when overconfident), and per-section verdicts
with quoted evidence and the next question.

The self-rating is **not** sent to the model — telling a grader the candidate
scored themselves 5/5 anchors it toward agreement. Comparing the two is the
caller's job.

### Errors

Every error returns `{"error": {"code", "message", "request_id"}}`.

| Code | Status | Meaning |
| --- | --- | --- |
| `authentication_required` | 401 | Missing or rejected bearer token. |
| `nothing_to_grade` | 422 | Every section was blank. |
| `context_too_large` | 413 | Reasoning exceeds `AI_CONTEXT_MAX_CHARS`. |
| `provider_limited` | 429 | Provider rate limit — retry later. |
| `invalid_model_response` | 502 | Model returned an ungradeable or ungrounded response. |
| `response_truncated` | 502 | Model ran out of output budget mid-answer; raise `AI_MAX_OUTPUT_TOKENS`. |
| `provider_error` | 502 | Provider rejected the request. |
| `provider_unavailable` | 503 | Transport failure or repeated 5xx. |

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `APP_ENV` | no | Environment label; defaults to `development`. |
| `ALLOWED_ORIGINS` | yes in deployment | Comma-separated exact frontend origins allowed by CORS. |
| `SUPABASE_URL` | yes | Supabase project URL. |
| `SUPABASE_ANON_KEY` | yes | Public anon key; authorization still comes from the caller's token. |
| `GEMINI_API_KEY` | yes | Server-only Gemini credential. |
| `AI_MODEL_GRADE` | no | Must support strict JSON output; rejected at startup otherwise. |
| `AI_TIMEOUT_SECONDS` | no | Provider timeout; defaults to `30`. |
| `AI_MAX_RETRIES` | no | Retries for transient provider failures; defaults to `1`. |
| `AI_CONTEXT_MAX_CHARS` | no | Hard cap on serialized context; defaults to `24000`. |
| `AI_MAX_OUTPUT_TOKENS` | no | Maximum provider output tokens; defaults to `8000`. The model's reasoning is drawn from this same budget, so it must cover thinking *and* the JSON — a value near the size of the answer alone truncates most grades. |
| `AI_REASONING_EFFORT` | no | `low`, `medium` or `high`; defaults to `low`. Bounds the thinking budget so it cannot consume the answer. Raise it if the pushover fixtures start passing. |

The model ids in `STRICT_GEMINI_MODELS` were copied from `ativscrum-ai-api`.
Confirm them against the provider's current model list before deploying.

## Container

```bash
docker build -t grip-ai-api:dev .
docker run --rm --env-file .env -p 8000:8000 grip-ai-api:dev
```

Two-stage image, runs as a non-root user on port 8000.

## Deployment notes

Deployable on a free Koyeb instance the same way as `ativscrum-ai-api`:
Dockerfile build, HTTP port `8000`, health check path `/health`, every runtime
variable set as an environment variable or secret, and `ALLOWED_ORIGINS` set to
the exact production origin.

Free instances scale to zero, so the first grade after an idle period
cold-starts. The frontend should say so rather than appear hung.

The Gemini API free tier may use submitted content to improve Google products
(see [Gemini API terms](https://ai.google.dev/gemini-api/terms)). Talk-track
entries are personal interview-prep notes and can name target companies. That
tradeoff was accepted deliberately for this project; re-read the terms before
pointing this at anything else.

## Not built yet

- The `arch_boards.talk_grade` column and the web/mobile UI that calls this.
- A content-hash cache so unchanged reasoning is not re-graded, which protects
  the free-tier daily quota and keeps a grade stable between renders.
- No remote is configured for this repository yet.
