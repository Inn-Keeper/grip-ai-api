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
A scenario with no figures (a free board, a custom scenario) leaves the five
numbers out; the model then checks the arithmetic against the candidate's own
stated assumptions instead of against zeros.

The rubric also grades by absence: every section must come back with the next
question an interviewer would ask. "What is missing" is far harder to answer
generously than "how good is this".

**Placeholders score zero in code.** A section of four whitespace-separated words or fewer
("REST", "the db") is graded `missing` whatever the model says: the local model
rated such placeholders `thin`, which is half marks. This also rejects terse
meaningful answers: it is a known heuristic, not proof that the reasoning is
absent. The rubric targets senior-level practice; language parity and grading
accuracy have not been established. [Calibration procedure](docs/grading-calibration.md).

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
on restart. The web and mobile clients persist the returned score and its
detailed verdicts, evidence and follow-up through their own Supabase session
(`arch_boards.talk_grade` and `talk_assessment`). Public share links never
return the detailed feedback. This service needs no database-row access.

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
16 GB M1. Waiting for that slot is capped by `OLLAMA_QUEUE_TIMEOUT_SECONDS`
(default 5 seconds); a busy slot returns `503 provider_busy` and `Retry-After: 5`.
The generation timeout starts after the slot is acquired, so the queue cap is
not a total request deadline. `OLLAMA_KEEP_ALIVE=0` unloads the model after every grade to return its
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
    "storage_gb": 1168,           // derived in packages/core; all five figures are optional
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

For a complete synthetic example and safe first-success procedure, see
[first-grade.md](docs/first-grade.md).

Returns a computed `score`, a `divergence` (how many points the candidate
over-rated themselves, positive when overconfident), and per-section verdicts
with quoted evidence and the next question.

The self-rating is **not** sent to the model — telling a grader the candidate
scored themselves 5/5 anchors it toward agreement. Comparing the two is the
caller's job.

### Errors

Application and request-validation errors return
`{"error": {"code", "message", "request_id"}}`. Validation errors may also include
`validation_types`, never submitted values or dictionary keys. Unexpected server
failures are not converted into input errors.

| Code | Status | Meaning |
| --- | --- | --- |
| `invalid_request` | 422 | Check required fields, types and schema length limits; correct the input rather than retrying unchanged. |
| `provider_busy` | 503 | Local generation slot is busy; wait for `Retry-After`, then retry. |
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

## Ledger import

`POST /api/v1/ai/import/parse`, bearer token required. Turns a free-form list of
job applications into rows the Quest import screen shows for review. The web
app saves the rows the user confirms; this service stores nothing.

Request, one of:

```json
{ "text": "applied to acme for FE role, waiting" }
{ "filename": "ledger.docx", "content_base64": "..." }
```

Only `.docx` and `.md`. The browser sends the file as base64 so no multipart
dependency is needed, and `.docx` text is read with the standard library
(`zipfile` + `xml.etree`).

How it works (`app/ledger.py`, `app/import_service.py`):

1. **Redact.** Links, emails, phone numbers and salaries become `[LINK_1]`,
   `[EMAIL_1]` and so on before anything reaches the model. Dates are kept.
   Names of people written in plain text are not detected.
2. **Chunk.** Lines are numbered and split into chunks of at most 8,000
   characters, parsed one at a time. Text over 40,000 characters is refused.
3. **Parse.** The model returns rows that point at their source by line number,
   which keeps its output small. Status and dates are checked in code; a row
   with an unknown status goes to `unplaced`, an unreadable date becomes `null`.
4. **Restore.** Placeholders are swapped back, the first link becomes `link`,
   and `source` holds the user's original lines. Each row also carries up to 5
   `must_have_techs` the lines name.

Every nonblank source line appears in an accepted row or `unplaced`, including
lines the model omits. Rows with invalid or conflicting source references are
withheld for review. Numbered lines stay whole within an 8,000-character chunk;
split a longer paragraph before importing.

Parsing is all-or-nothing: a later chunk failure returns an error and no earlier
results. Keep the original input and retry the full import; completed chunks may
be generated again. The API saves no rows. Multi-chunk requests can take several
generation timeouts; there is no total import deadline or resumable job. Cancelling
a browser request does not prove an already-sent generation stopped.

Import uses the configured provider and shares grading's quota memory, so a
spent daily quota blocks both with the same `429`. On the Gemini free tier,
Google may use the redacted text to improve its products.

| Code | Status | Meaning |
| --- | --- | --- |
| `unsupported_file` | 415 | Not a `.docx` or `.md` file. |
| `unreadable_file` | 422 | Broken `.docx` or invalid base64. |
| `file_too_large` | 413 | Over 1 MB, or a `.docx` that unpacks to over 10 MB. |
| `ledger_too_long` | 413 | Over 40,000 characters of text. |
| `import_line_too_long` | 413 | One numbered paragraph exceeds the 8,000-character chunk budget; split it into shorter lines. |
| `nothing_to_import` | 422 | The file or text was empty. |

## Posting links

`POST /api/v1/ai/import/postings`, bearer token required. Body
`{ "urls": [...] }` (1 to 10). Returns `{ "postings": [{ url, status, text }] }`
in order; `status` is `ok`, `blocked`, `unreachable`, `not_html`, `too_large`
or `empty`, and `text` is the page's visible text (max 20,000 chars) when `ok`.
The web app detects techs in it; no model is called.

Guards (`app/posting_reader.py`): `https` on the default port only, no
`user:pass@`; every resolved address must be public (not private, loopback,
link local, reserved or multicast) and the connection is pinned to the checked
address; at most 3 redirects, each checked again; `GET` without cookies,
`Referer` or the user's token; 5 s per link, 3 at a time, 20 s per request;
HTML or plain text only, cut at 1 MB. Each user may read 30 links per 10
minutes (`429 rate_limited` with `Retry-After`). URLs and page text are never
logged or stored; logs keep counts per status.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `APP_ENV` | no | Environment label; defaults to `development`. |
| `ALLOWED_ORIGINS` | yes in deployment | Comma-separated exact frontend origins allowed by CORS. |
| `SUPABASE_URL` | yes | Supabase project URL. |
| `SUPABASE_ANON_KEY` | yes | Public anon key; authorization still comes from the caller's token. |
| `AI_PROVIDER` | no | `ollama` (default) or `gemini`. |
| `OLLAMA_URL` | no | Ollama server; defaults to `http://localhost:11434`. |
| `OLLAMA_QUEUE_TIMEOUT_SECONDS` | no | Maximum wait for the local generation slot; defaults to `5`, then returns `provider_busy`. |
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
- **Cached grades survive a remembered limit.** Authenticated, valid requests for
  cached feedback still return it without inference. `/status` describes new
  generation availability, not whether a particular answer is cached. The current
  Grip UI may disable Grade during a limit; exposing cached retries is client work.
- **Only successful grades are cached.** A rate limit or a rejected response is
  never stored, so a retry after a failure really does retry.
- **A refusal is remembered.** The 429 is the only quota signal Google gives, so
  the service holds on to it: until it lapses, `/api/v1/ai/status` reports the
  block and uncached grade requests are refused here, without spending a request
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
caches matter most, the service stays warm. A paid always-on plan can avoid idle suspension; verify current hosting terms
before choosing one.

The local Ollama setup keeps inference on the Mac. A remotely hosted API cannot
reach it using `localhost`; it needs a reachable inference server or an explicit
switch to Gemini. The Python image does not bundle Qwen or Ollama. Supabase
authentication still uses the network.

The free tier is per Google project, so a deployed instance and a laptop sharing
one key consume the same project quota (the historical example above was 20/day). A second project for local
development is the cheap fix.

The Gemini API free tier may use submitted content to improve Google products
(see [Gemini API terms](https://ai.google.dev/gemini-api/terms)). Talk-track
entries are personal interview-prep notes and can name target companies. That
tradeoff was accepted deliberately for this project; re-read the terms before
pointing this at anything else.

## Callers

Web and mobile Arch Board integrate grading from the talk-track UI. The web app needs `VITE_AI_URL`
pointing here, and a saved board, because the request carries the board's UUID.
The clients store the returned score in `arch_boards.talk_grade` and the full
response, with a fingerprint of the graded inputs, in `arch_boards.talk_assessment`.
They show that feedback again only while the board still matches those inputs.
This service still writes nothing.

## Not built yet

- Assessment history: a board keeps only its latest grade and feedback.
- Client-specific busy messaging and explicit cached retries while `/status`
  reports a limit. Those require a separate `grip-apps` change.
- Calibrated grading across scenarios and languages. See
  [grading-calibration.md](docs/grading-calibration.md).
