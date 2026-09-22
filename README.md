# grip-ai-api

FastAPI service that grades the **talk track** of a Grip Arch Board
round — the written reasoning a candidate produces alongside a system design.

The default provider is **local Qwen through Ollama** (`grip-grader`). Gemini
remains available with `AI_PROVIDER=gemini` for comparison. No new Python
dependency is required: both adapters use the existing HTTP client.

The board itself already scores deterministically: which components exist, how
they are wired, whether the numbers work. That is checked in
[`packages/core`](../grip-apps/packages/core) and needs no model. What a
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
`missing` without nonempty evidence, and `validate_evidence` in `service.py`
rejects the whole response when a quote does not occur verbatim in the
corresponding input section. A grade can therefore only be given for words the
candidate actually wrote.

**Grading is against ground truth, not vibes.** The request carries the
scenario's derived figures — peak requests per second, storage over the
retention window, which design checks passed, which partition keys were
declared. The rubric asks the model to compare the candidate's arithmetic to
those numbers rather than to judge whether it "looks right".

The rubric also grades by absence: every section must come back with the next
question an interviewer would ask. "What is missing" is far harder to answer
generously than "how good is this".

**Placeholders score zero in code.** A section of four words or fewer
("REST", "the db") is graded `missing` whatever the model says: the local model
rated such placeholders `thin`, which is half marks.

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

The service stores nothing durable: no prompts, no reasoning, no grades on
disk. It keeps recent grades in memory (see *Gemini free tier*) and forgets them
on restart. The planned client integration will persist the returned
grade through its own Supabase session. That integration is not built yet;
this service needs no database access.

## Requirements

- Python 3.11
- a Supabase project (only `/auth/v1/user` is called, to validate the caller)
- native Ollama with `grip-grader` installed (or a Gemini API key for comparison)

## Local setup

From this repository, with the Ollama macOS app running:

```bash
ollama pull qwen3.5:9b
ollama create grip-grader -f Modelfile
curl http://localhost:11434/api/tags
```

If you already created `grip-grader`, skip the pull/create commands. The API
supplies the full rubric and JSON schema on every request. It uses 8192 context
tokens, 2048 output tokens, and `think=false` by default. The input character cap
is a practical limit for short English talk tracks, not an exact tokenizer;
keep enough context space for the rubric and output when changing these values.
Requests run one at a time per API process; use a single Uvicorn worker on the
16 GB M1. `OLLAMA_KEEP_ALIVE=0` unloads the model after every grade to return its
memory to macOS. Local timeouts are not retried automatically.

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install ".[dev]"
cp .env.example .env
```

Fill in `SUPABASE_URL` and `SUPABASE_ANON_KEY` in `.env`. The grading endpoint
still requires a signed-in user's bearer token; no Gemini key is needed.
If a virtual environment is already installed, skip its creation and install.

```bash
uvicorn app.main:app --reload --port 8000
```

```bash
curl http://localhost:8000/health   # {"status":"ok"} — no config needed
curl http://localhost:8000/ready    # configuration check only
```

`/ready` names missing configuration; it does not contact Ollama or Supabase.
Check `/api/tags` and run the live suite below to verify actual local inference.

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
RUN_LIVE_AI=1 pytest -m live -s
```

This runs four sequential generations through the selected provider, prints
latency and verdicts, and makes no Supabase requests or database writes. The
ordinary suite never calls a model, even if credentials are present.

`tests/test_pushover.py` sends three deliberately bad talk tracks — fluent
hand-waving, wrong arithmetic, and bare placeholders — plus a strong answer.
Checks cover inappropriate credit, fabricated evidence, useful follow-ups and
recognizing a good answer. **Run these after prompt, model or quantization
changes.** Passing this small suite is an initial check, not a full calibration.

To compare Gemini, set its key in `.env`, then run:

```bash
AI_PROVIDER=gemini AI_MODEL_GRADE=gemini-3.5-flash AI_MAX_OUTPUT_TOKENS=8000 RUN_LIVE_AI=1 pytest -m live -s
```

This explicitly sends the synthetic fixtures to Gemini and consumes provider
quota. Switching providers does not automatically fall back to the cloud.

## Endpoint

`POST /api/v1/ai/grade-talk-track`, bearer token required.

`GET /api/v1/ai/status` says whether grading would go through, with no token and
no provider call: `{"grading":"available"}`, or `{"grading":"unavailable",
"code","message","retry_after"}` while a remembered limit holds. Google publishes
no remaining-quota figure, so "available" means nothing has refused us yet, not
that quota remains.

[`docs/grade-request-path.md`](docs/grade-request-path.md) walks one request
end to end, from the board to the stored grade.

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
| `provider_limited` | 429 | Short-term provider rate limit; `Retry-After` says when to retry. |
| `invalid_model_response` | 502 | Model returned an ungradeable or ungrounded response. |
| `response_truncated` | 502 | Model ran out of output budget mid-answer; raise `AI_MAX_OUTPUT_TOKENS`. |
| `provider_error` | 502 | Provider rejected the request. |
| `provider_unavailable` | 503 | Transport failure or repeated 5xx. |
| `provider_quota_exhausted` | 429 | Today's free Gemini quota is spent; `Retry-After` points at midnight Pacific. |
| `model_not_found` | 503 | Ollama could not find the model; pull or create it. |

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `APP_ENV` | no | Environment label; defaults to `development`. |
| `ALLOWED_ORIGINS` | yes in deployment | Comma-separated exact frontend origins allowed by CORS. |
| `SUPABASE_URL` | yes | Supabase project URL. |
| `SUPABASE_ANON_KEY` | yes | Public anon key; authorization still comes from the caller's token. |
| `AI_PROVIDER` | no | `ollama` (default) or `gemini`. |
| `OLLAMA_URL` | no | Ollama server; defaults to `http://localhost:11434`. |
| `OLLAMA_CONTEXT_LENGTH` | no | Context tokens; defaults to `8192`. |
| `OLLAMA_THINK` | no | Enable Qwen thinking; defaults to `false`. Re-evaluate output budget and latency if enabled. |
| `OLLAMA_KEEP_ALIVE` | no | How long Ollama keeps the model loaded; defaults to `0`, which unloads it after each grade. |
| `GEMINI_API_KEY` | Gemini only | Server-only Gemini credential. |
| `AI_MODEL_GRADE` | no | Defaults to `grip-grader` for Ollama, `gemini-3.5-flash` for Gemini. Explicit names must match the selected provider. |
| `AI_TIMEOUT_SECONDS` | no | Provider request timeout; defaults to `180` seconds, excluding local queue wait. |
| `AI_MAX_RETRIES` | no | Gemini transient retries; defaults to `1`. Ollama never automatically retries. |
| `AI_CONTEXT_MAX_CHARS` | no | Hard cap on serialized candidate context; defaults to `12000`. |
| `AI_MAX_OUTPUT_TOKENS` | no | Defaults to `2048` for local non-thinking output. Set `8000` for Gemini, whose thinking shares the output budget. |
| `AI_REASONING_EFFORT` | no | Gemini only: `low`, `medium` or `high`; defaults to `low`. |
| `AI_GRADE_CACHE_SIZE` | no | Grades kept in memory per process; defaults to `256`, `0` disables. |

The model ids in `STRICT_GEMINI_MODELS` were copied from `ativscrum-ai-api`.
Confirm them against the provider's current model list before deploying.

## Gemini free tier

The free tier limits requests per project and per model. At the time of writing
a Flash model allowed 20 requests a day; check the current numbers in Google AI
Studio. The daily count resets at midnight Pacific time.

- **Identical requests are free.** A grade is cached in memory, keyed by user,
  model, rubric and the exact reasoning and facts sent. Re-rendering a board or
  retrying a click does not spend quota; editing any section does. Size it with
  `AI_GRADE_CACHE_SIZE`.
- **Only successful grades are cached.** A rate limit or a rejected response is
  never stored, so a retry after a failure really does retry.
- **A refusal is remembered.** The 429 is the only quota signal Google gives, so
  the service holds on to it: until it lapses, `/api/v1/ai/status` reports the
  block and further grade requests are refused here, without spending a request
  to be told the same thing. A spent day returns `provider_quota_exhausted`
  with `Retry-After` set to midnight Pacific; a per-minute limit returns
  `provider_limited` with Google's own `retryDelay`.
- **Only unsent requests are retried.** A connection failure is retried
  (`AI_MAX_RETRIES`); a read timeout is not, because Google may already have
  counted it.
- **The cache and the remembered limit are per process.** Restarts and extra
  workers start empty. Run one worker on the free tier.
- **The live suite costs 4 requests** and runs only with `RUN_LIVE_AI=1`.

## Container

```bash
docker build -t grip-ai-api:dev .
docker run --rm --env-file .env -p 8000:8000 grip-ai-api:dev
```

Two-stage image, runs as a non-root user on port 8000.
For Docker Desktop on the Mac, set `OLLAMA_URL=http://host.docker.internal:11434`;
container localhost is not the host's Ollama server. Ollama must accept connections
from Docker. Native Uvicorn is the simpler local setup and needs no binding change.

## Deployment

Deployed to Render from [`render.yaml`](render.yaml): Dashboard → Blueprints →
New Blueprint Instance, pointed at this repo. It prompts for `SUPABASE_URL`,
`SUPABASE_ANON_KEY` and `GEMINI_API_KEY`; everything else is in the file. Set
`ALLOWED_ORIGINS` there to your exact Vercel origin first.

Afterwards set `VITE_AI_URL` to the service URL in Vercel and redeploy the web
app, because Vite bakes that value in at build time. `curl <url>/ready` names
any configuration that is still missing.

The container binds `$PORT`, which Render injects, and falls back to 8000
locally.

**What the free plan costs.** The service sleeps after 15 minutes without
traffic and takes about a minute to wake, so the first grade of a session waits
for the container before it waits for the model. Both in-memory stores start
empty after a sleep: unchanged reasoning is graded again, and a spent daily
quota costs one request to rediscover. Within a working session, where the
caches matter most, the service stays warm. The $7 Starter plan stops the
sleeping if that becomes annoying.

The local Ollama setup keeps inference on the Mac. A remotely hosted API cannot
reach it using `localhost`; it needs a reachable inference server or an explicit
switch to Gemini. The Python image does not bundle Qwen or Ollama. Supabase
authentication still uses the network.

The free tier is per Google project, so a deployed instance and a laptop sharing
one key eat each other's 20 requests a day. A second project for local
development is the cheap fix.

The Gemini API free tier may use submitted content to improve Google products
(see [Gemini API terms](https://ai.google.dev/gemini-api/terms)). Talk-track
entries are personal interview-prep notes and can name target companies. That
tradeoff was accepted deliberately for this project; re-read the terms before
pointing this at anything else.

## Callers

The web Arch Board grades from the talk-track card. It needs `VITE_AI_URL`
pointing here, and a saved board, because the request carries the board's UUID.
The returned score is stored in `arch_boards.talk_grade` by the web app itself;
this service still writes nothing.

## Not built yet

- The mobile UI. Only the web board can grade today.
- Persisting grades (`arch_boards.talk_grade` holds the score, but the verdicts
  and follow-ups live only in the in-memory cache and are lost on restart).
