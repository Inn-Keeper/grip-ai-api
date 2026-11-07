# AI Service Product UX Repairs Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task. Track progress with checkboxes. Commit only when the user asks.

**Goal:** Make imports complete, failures actionable, local waiting bounded, and grading limitations clear without expanding the service into a job-processing platform.

**Architecture:** Keep FastAPI, Pydantic, existing provider adapters and transient in-process state. Reconcile import source coverage in Python, normalize validation failures at the API boundary, and serve validated cached grades before applying provider limits. Preserve client-owned persistence.

**Tech stack:** Python, FastAPI, Pydantic, httpx, pytest/pytest-asyncio and Ruff.

**Spec:** The six findings from the November 7, 2026 evaluate-product review, captured below as acceptance criteria. Implementation choices here are proposals; this document does not authorize implementation or deployment.

## Scope and constraints

- Implementation stays in `grip-ai-api`. Any client UX changes requiring `grip-apps` are explicit follow-ups, not silently included work.
- No new production dependencies, durable job queue, progress-streaming protocol, persistence service or automatic cloud fallback.
- Keep existing endpoint success shapes and error envelope compatible. Add optional safe validation details rather than exposing submitted values.
- Preserve authentication, user-scoped caching, verbatim evidence checks, deterministic scoring, withholding self-rating from the model, and existing provider retry restrictions.
- Reproduce each finding on the implementation revision before editing. Offline tests establish behavior, not model quality or live integration.
- Live model tests remain opt-in. Default verification uses fixtures and stub HTTP transports, without quota spending or external writes.
- Existing review baseline: 137 offline tests passed, 7 live tests excluded. The installed venv uses Python 3.14; repeat compatibility checks on the documented Python 3.11 target when available.

## Proposed approach

Prefer small repairs to existing boundaries. Import coverage is a deterministic bookkeeping problem; do not ask a model to certify its own completeness. Bound local lock acquisition rather than adding a worker system. Preserve all-or-nothing import responses for now, but state their recovery implications explicitly.

For grading fairness, first expand offline fixtures and document the current word-count rule. Do not replace it with a new unvalidated heuristic or silently loosen grading. A later authorized calibration run decides whether it should change.

## Delivery order

| Phase | Tasks | Exit condition |
| --- | --- | --- |
| Now | 1 to 2 | Every import source line is accounted for; invalid requests have actionable, safe errors |
| Next | 3 to 4 | Local waiting is bounded; cached feedback remains available during provider limits |
| Then | 5 to 6 | Grading limitations are explicit; setup and integration documentation reflect reality |

Tasks are independently reviewable. Task 6 incorporates the final contracts from Tasks 1 to 5. Task 5's live calibration is a separately authorized follow-up, not a prerequisite to shipping the deterministic recovery fixes.

## Review focus

1. Model omissions, wrong chunk references and overlong source lines must not silently drop or misattribute input (Task 1).
2. Validation errors must not echo private application text, bearer tokens or uploaded content (Task 2).
3. Cancelled or timed-out queue waiters must not release another request's lock or invoke the model later (Task 3).
4. Cache hits must remain user-isolated and must not bypass authentication, schema validation or context limits (Task 4).
5. Concise and translated answers must not be presented as calibrated assessments without evidence (Task 5).

## Task 1: Account for every import line

**Finding:** High; reproduced successful import with an omitted line absent from both rows and `unplaced`.

**Files:** `app/import_service.py`, `app/ledger.py`, `tests/test_import.py`, `tests/test_ledger.py`.

**Contract:** Every nonblank original source line appears in an accepted row's source or `unplaced`. A model row may reference only line IDs actually supplied in its chunk. Rejected rows leave their lines available for recovery. Return unplaced source text in original order, once per source line. Preserve the current `ImportResponse` shape.

- [ ] Add failing tests: model omits one/all lines; invalid stage; out-of-range and cross-chunk references; duplicate IDs; row/unplaced overlap; multiple rows sharing source; multiline records; empty model output.
- [ ] Add a chunk helper returning numbered whole-line chunks plus their allowed global line IDs. Do not recover IDs by parsing arbitrary candidate text after generation.
- [ ] Reject a single numbered source line exceeding the chunk budget before any model call, using `413 import_line_too_long` and a safe message asking the user to split that paragraph. Current arbitrary character splitting loses line identity; do not preserve that behavior.
- [ ] Validate each row's entire reference set against the chunk. Reject invalid rows rather than silently discarding bad IDs and accepting the remaining attribution. Count a line as covered only after accepting its row.
- [ ] Reconcile all source IDs at the end: `unplaced = all IDs minus accepted coverage`, plus valid explicit unplaced IDs not already covered. Reject unknown explicit IDs; keep their omission from results from hiding real input.
- [ ] Handle conflicting row references conservatively: rows sharing a line are withheld and their source goes to `unplaced`, avoiding duplicate applications with uncertain attribution.
- [ ] Run focused tests and verify text/redaction/source restoration still round-trip without changing original line content.

**Acceptance:** The two-line omission reproduction returns one row plus the omitted original line in `unplaced`. No model output can create a successful response with unaccounted-for input.

## Task 2: Normalize request validation errors

**Finding:** Medium; reproduced `422 {detail: ...}` instead of the service error envelope.

**Files:** `app/main.py`, `tests/test_grade.py`, `tests/test_import.py`, `tests/test_postings_endpoint.py`; add `tests/test_validation.py` for shared contract cases.

**Contract:** Request validation returns HTTP 422 with `error.code = invalid_request`, a concise corrective `message`, and the middleware-generated `request_id`. Optional `error.fields` contains only safe locations and stable validation types. Never serialize Pydantic `input`, arbitrary exception context, or raw submitted values.

- [ ] Add tests for invalid UUID, unknown section key, section over 8,000 characters, missing facts, malformed JSON, text/file conflicts, and invalid posting list sizes.
- [ ] Register a `RequestValidationError` handler. Preserve existing `AppError` handling and `Retry-After`; do not convert genuine internal exceptions into input errors.
- [ ] Assert the same envelope and request-ID format across all three POST routes. Add private sentinel strings to inputs and assert none appears in responses or captured logs.
- [ ] Verify the current shared client adapter can display the top-level corrective message without requiring a client release. Field highlighting is an optional client follow-up.

**Acceptance:** Invalid input is distinguishable from provider failure and includes safe guidance and a support identifier.

## Task 3: Bound local queue waiting and explain import failure

**Finding:** High under concurrent use; source-backed unbounded lock wait and all-or-nothing multi-chunk import.

**Files:** `app/config.py`, `app/ollama.py`, `tests/test_ollama.py`, `tests/test_import.py`, `.env.example`, `README.md`.

**Contract:** Add `OLLAMA_QUEUE_TIMEOUT_SECONDS`, default `5`, positive float. Waiting for the local generation lock beyond that deadline returns `503 provider_busy` with `Retry-After: 5`, before sending any provider request. Generation retains its existing timeout and no automatic local retries. The queue deadline bounds waiting only; it does not promise a total generation duration.

- [ ] Extend the existing event-controlled concurrency test: first generation holds the lock, second times out, no second provider request occurs, first still succeeds, and a subsequent request can acquire the lock.
- [ ] Test cancellation while waiting and provider exceptions while holding the lock. Release only a lock acquired by that invocation, including timeout/cancellation races.
- [ ] Implement bounded acquisition with `asyncio.wait_for` and `try/finally` around the acquired lock. Keep one active local generation per process.
- [ ] Test a two-chunk import where the second generation fails: return no successful import response, no rows are stored, and the error remains actionable. Preserve the original error code and retry hint; add safe import context that earlier work was not returned and input must be retried.
- [ ] Document all-or-nothing parsing, possible repeated model work after retry, and that browser cancellation does not prove an already-sent model call stopped.
- [ ] Document that per-generation timeouts do not bound the whole multi-chunk import. Do not advertise resumability or partial progress.

**Acceptance:** A queued local request gets a bounded, retryable busy outcome without increasing inference concurrency. A failed import never appears partially saved or resumable when it is not.

**Client follow-up:** In `grip-apps`, distinguish busy from quota exhaustion, preserve input on error and explain long imports before submission. Inspect current client handling during implementation; list any necessary client changes separately.

## Task 4: Serve cached grades during provider limits

**Finding:** Medium; reproduced cached successful request returning 429 after another request triggered a limit.

**Files:** `app/service.py`, `app/ollama.py`, `app/gemini.py` only if a shared context-validation boundary is needed, `tests/test_cache.py`, `tests/test_quota.py`.

**Contract:** Authenticate → validate reasoning/context → look up user-scoped cache → apply provider limit only on cache miss → generate. Do not alter user/model/rubric/context cache keys or persist new data. `/status` continues to describe permission to generate, not whether a particular result is cached.

- [ ] Add regression: cache A; request B triggers limit; A returns 200 with the same feedback and a fresh request ID; B remains 429; provider call count stays unchanged during both retries.
- [ ] Test another user cannot read A; empty reasoning is still rejected; cache disabled still respects limits; oversized context cannot bypass the configured cap through an old cache entry.
- [ ] Move the limit check after the validated cache lookup. Keep deterministic context validation independent from provider invocation so cache hits receive the same input limits.
- [ ] Preserve self-rating exclusion from the cache key and recompute divergence per request; test changed self-rating reuses feedback without another generation.
- [ ] Update status documentation to distinguish generation availability from cached-result availability. Do not make a provider health probe or spend quota to improve `/status`.

**Acceptance:** A remembered quota limit blocks new inference, not an already available authenticated result.

**Client follow-up:** The board currently disables grading under a remembered limit. API repair alone does not make cached recovery reachable there. Plan a small `grip-apps` change allowing explicit retry of the current request; the service returns cached success or the existing 429. No cache-discovery endpoint is required.

## Task 5: Make grading limits explicit and prepare calibration

**Finding:** Medium; the four-word floor is reproduced, while semantic harm and language disparities remain hypotheses.

**Files:** `app/service.py`, `tests/test_scoring.py`, `tests/test_pushover.py`, `README.md`; create `docs/grading-calibration.md` and synthetic fixtures under `tests/fixtures/grading/`.

**Contract:** Keep current verdict points and floor until calibration justifies a replacement. Document the floor as a known limitation and the rubric as senior-level practice feedback, not a validated readiness or hiring assessment. Do not silently claim language support because JSON accepts Unicode.

- [ ] Add deterministic tests showing the actual boundary: empty, four-word and five-word sections; punctuation, whitespace and Unicode. These pin the current limitation rather than certify it as fair.
- [ ] Create matched synthetic cases: concise meaningful, verbose empty, incorrect arithmetic, good reasoning, and near-boundary responses. Include English, Portuguese and Swedish equivalents reviewed for meaning before comparison.
- [ ] Define human review fields: expected section verdicts, supporting evidence, acceptable follow-up, answer level/language and ambiguity notes. No real candidate data.
- [ ] Write a calibration procedure comparing false credit, false rejection, verdict consistency across repeat runs and usefulness of the next question by language and scenario. Record provider, model, prompt revision and settings.
- [ ] Mark fixture creation and offline contract tests complete independently of live calibration. Run no model calls without a separate explicit request.
- [ ] After authorized calibration, choose whether to retain the floor with clearer guidance or replace it with a narrowly tested rule. Ship a scoring change only with reviewed fixtures showing its intended improvement and no reward for fluent placeholders.

**Acceptance:** Current limits are discoverable; meaningful calibration cases exist; the report distinguishes deterministic safeguards from model-quality evidence. Fairness is not declared solved by adding tests around the old rule.

## Task 6: Reconcile setup and capability documentation

**Finding:** Low; contradictory persistence and mobile integration statements.

**Files:** `README.md`, `docs/grade-request-path.md`, `.env.example`; verify adjacent client sources read-only.

- [ ] Recheck current web/mobile grading hooks and client save behavior. Describe score persistence, transient detailed feedback, and service-owned cache separately; remove stale “integration not built” statements.
- [ ] Add one complete synthetic grade request and representative response with a placeholder bearer token. Include token acquisition guidance using a dedicated test account without printing credentials or adding an auth bypass.
- [ ] Give a shortest first-success path for local Ollama and a separate explicit Gemini path. Distinguish `/health`, configuration-only `/ready`, remembered-limit `/status`, and a real inference check.
- [ ] Document new validation, queue, import-completeness and cached-limit behavior. Include all supported errors and their caller recovery action.
- [ ] Replace or clearly date unverified external price/quota/model claims. If updating them as current facts, verify official sources at implementation time.
- [ ] Review examples against OpenAPI and offline fixtures. Manually verify instructions on a configured safe environment when available; disclose anything not exercised.

**Acceptance:** A developer can identify supported clients, who persists what, and how to reach a first grade without resolving contradictory instructions.

## Verification and release

- [ ] For each task, run its focused tests first using `.venv/bin/python -m pytest -q <test files>` through `rtk proxy`.
- [ ] Run required checks from the repository root:
  - `rtk proxy .venv/bin/python -m pytest -q`
  - `rtk proxy .venv/bin/ruff check .`
  - `rtk proxy .venv/bin/ruff format --check .`
  - `rtk git diff --check`
- [ ] Confirm live tests stay skipped; do not set `RUN_LIVE_AI=1` during routine checks.
- [ ] Verify ordinary, missing-auth, invalid-input, remembered-limit, cache-hit, queued-busy, partial-model-output and later-chunk-failure paths using stubbed providers/auth.
- [ ] Smoke-test client interpretation of additive errors using synthetic responses when client access is available. API-only completion must explicitly list remaining client work from Tasks 3 to 4.
- [ ] Record Python version, passed/failed/skipped checks, files changed, known calibration gaps and manual follow-ups. No deployment or commit without the user's instruction.

**Done:** The six findings have implementation or explicit validation evidence; any hypothesis not yet resolved is recorded as such. Import completeness and API recovery fixes can ship before model calibration. No claim of full user-facing recovery until connected client limitations are checked.


## Execution record, 2026-11-07

- Tasks 1 to 4 implemented with failing regressions observed before fixes. Full suite:
  160 passed, 7 live tests skipped on the installed Python 3.14 venv. Ruff lint,
  format check and `git diff --check` passed before final review.
- Task 5: boundary tests, synthetic multilingual draft fixtures and calibration
  procedure added. Human translation/verdict review and live calibration remain
  pending; no scoring change or model-quality claim.
- Task 6: setup, API errors, queue behavior, client capabilities and persistence
  documentation updated; complete synthetic request and first-grade guide added.
  No live first-grade request or deployed-client check performed.
- Ruling: use a safe validation-category list rather than arbitrary field paths;
  dictionary keys can contain user text. Input shape remains documented by OpenAPI.
- Ruling: unplaced lines are derived from accepted coverage, not the model's
  unplaced list. This makes omitted and invalid references recoverable by default.
- Related defect: redaction could consume newlines and break attribution. Added a
  failing regression, restricted redaction matches to a single source line, and
  replaced the unused character-splitting helper with numbered whole-line chunks.
- Client follow-ups remain separate: busy/quota messaging and reaching cached
  grading while the UI disables new requests. Durable import jobs are not added.
- User instruction: no commits. All changes remain in the existing checkout.

### Final verification and review

- Fresh independent read-only review found validation guidance too generic and
  persistence wording stale. Added two failing guidance regressions, then static
  safe recovery messages; corrected the wording. No remaining material findings.
- Final required checks: 162 tests passed, 7 live tests skipped; Ruff lint and
  format check passed; `git diff --check` passed. One existing Starlette/httpx
  TestClient deprecation warning remains.
- Python 3.11 is installed but has no pytest/dependencies. No environment install
  was performed; compatibility on that interpreter remains unverified.
- No live inference, live auth, deployed client, human calibration or translation
  acceptance checks. No commits, pushes or deployments.


### Breaking-change review follow-up, 2026-11-07

- Restored Unicode horizontal whitespace support in phone redaction while keeping
  CR/LF source boundaries intact. Added failing regressions for non-breaking,
  narrow non-breaking and thin spaces; all now pass alongside line-identity tests.
- Full API verification: 165 tests passed, 7 live tests skipped; Ruff lint and
  formatting checks passed. No live provider calls or commits.
