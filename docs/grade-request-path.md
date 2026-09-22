# How a talk-track grade is requested

One request, from the Arch Board to a stored grade. The service validates the
caller, asks a model six section verdicts, computes the score itself, and keeps
nothing on disk. It does hold two things in memory: recent grades, so unchanged
reasoning is not re-graded, and the last provider rate limit, so a spent quota
is refused here instead of at the provider.

The web client is built (`apps/web/src/archBoard`). Mobile is not.

```
┌─ CLIENT (grip-apps, apps/web) ─────────────────────────────────────┐
│                                                                    │
│  1. User writes the six sections in the Arch Board                 │
│         TalkTrack.tsx holds them; gradeSections() fills blanks     │
│                                                                    │
│  2. Client derives the ground truth ITSELF                         │
│         buildGradeFacts()       packages/core/src/talkGrade.js     │
│         deriveScale(scale)      → peak_qps, storage_gb             │
│         evaluate() checks       → checks_passed / checks_failed    │
│         board nodes             → node_types, partition_keys       │
│         scenario.pushback       → pushback (read, not derived)     │
│      ⚠ the API never re-derives these. See README "Trust boundary" │
│                                                                    │
│  3. GET /api/v1/ai/status  → is the provider answering right now?  │
│         "unavailable" disables the button; no token, no quota spent│
│                                                                    │
│  4. Grab the Supabase session access token                         │
└────────────────────────────────┬───────────────────────────────────┘
                                 │
                   5. POST /api/v1/ai/grade-talk-track
                      Authorization: Bearer <user token>
                      { board_id, facts, sections, self_rating }
                                 │
┌────────────────────────────────▼───────────────────────────────────┐
│ FastAPI  app/main.py                                               │
│                                                                    │
│  6. CORSMiddleware      origin in ALLOWED_ORIGINS, else blocked    │
│  7. assign_request_id   uuid4 → echoed in every AppError           │
│  8. GradeRequest        pydantic strict; 8000 chars/section        │
│        ⚠ a malformed body fails HERE, as FastAPI's own             │
│          {"detail": [...]} — no code, no request_id                │
└────────────────────────────────┬───────────────────────────────────┘
                                 │
┌────────────────────────────────▼───────────────────────────────────┐
│ GradeService.grade   app/service.py                                │
│                                                                    │
│  9. token missing? ───────────────────────► 401 authentication_…   │
│                                                                    │
│ 10. SupabaseGateway.validate_session(token)                        │
│        └─► GET <SUPABASE_URL>/auth/v1/user                         │
│            apikey: ANON_KEY  +  Authorization: caller's token      │
│            (RLS stays the boundary; no service-role key here)      │
│                                                                    │
│ 11. a remembered rate limit still holding? ► 429 provider_quota_…  │
│        the last 429 is kept until it lapses, so this costs nothing │
│                                                                    │
│ 12. every section blank? ─────────────────► 422 nothing_to_grade   │
│                                                                    │
│ 13. build_context(facts, sections, self_rating)                    │
│        ⚠ self_rating is DROPPED — never shown to the model         │
│                                                                    │
│ 14. grade_cache_key(user, model, rubric, context)                  │
│        a hit skips the provider entirely ──► step 19               │
└────────────────────────────────┬───────────────────────────────────┘
                                 │
          15. provider.generate(...)   ← AI_PROVIDER picks one
                                 │
              ┌──────────────────┴──────────────────┐
              ▼                                     ▼
      ┌───────────────┐                     ┌───────────────┐
      │ ollama.py     │                     │ gemini.py     │
      │ POST /api/chat│                     │ strict JSON   │
      │ local Qwen    │                     │ hosted        │
      │ rubric + JSON │                     │ retries only  │
      │ schema        │                     │ unsent calls  │
      │   ↓           │                     │   ↓           │
      │ GradeSuggestion                     │ GradeSuggestion
      └───────┬───────┘                     └───────┬───────┘
              └──────────────────┬──────────────────┘
                                 │
┌────────────────────────────────▼───────────────────────────────────┐
│ Back in GradeService                                               │
│                                                                    │
│ 16. validate_evidence()      a quote that is not a verbatim span   │
│                              of that section → 502                 │
│ 17. apply_placeholder_floor  ≤4 words → "missing", model overruled │
│ 18. cache.put(key)           only validated, floored grades; a 429 │
│                              is remembered instead (step 11)       │
│ 19. score_from_verdicts      covered 100 / thin 50 / missing 0,    │
│                              averaged.  THE MODEL NEVER SCORES.    │
│ 20. divergence_from          (self_rating/5*100) − score           │
│                              positive = overconfident              │
└────────────────────────────────┬───────────────────────────────────┘
                                 │
        21. 200  { request_id, model, board_id, score, divergence,
                   suggestion: { sections[6]{verdict, evidence, gap},
                                 hardest_followup } }
                                 │
┌────────────────────────────────▼───────────────────────────────────┐
│ CLIENT again                                                       │
│ 22. Persists through its OWN Supabase session                      │
│     → arch_boards.talk_grade   (migration 0015; the score only)    │
│     The verdicts, quotes and follow-ups are not persisted: they    │
│     live in the service's memory and in the open board.            │
└────────────────────────────────────────────────────────────────────┘
```

## What the client has to handle

Every failure raised by the service returns the same envelope:
`{"error": {"code", "message", "request_id"}}`. A malformed request body is the
exception: it is rejected by FastAPI before any of this runs and comes back as
`{"detail": [...]}`, with no code and no request id. One envelope for both is
Task 4 of `docs/superpowers/plans/2026-09-16-gemini-free-tier.md`, unimplemented.

| Code | Status | What the caller does |
| --- | --- | --- |
| `authentication_required` | 401 | Refresh the session and retry once. |
| `nothing_to_grade` | 422 | Nothing was written. Prompt, don't retry. |
| `context_too_large` | 413 | Over `AI_CONTEXT_MAX_CHARS`. Ask for a shorter answer. |
| `provider_limited` | 429 | Short-term limit. Wait for `Retry-After`, then retry. |
| `provider_quota_exhausted` | 429 | Today's free Gemini quota is gone. `Retry-After` points at midnight Pacific; don't retry before it. |
| `invalid_model_response` | 502 | Retry once; a second failure is a real bug. |
| `response_truncated` | 502 | Raise `AI_MAX_OUTPUT_TOKENS`. |
| `provider_error` | 502 | The provider rejected the request. Check credentials. |
| `provider_unavailable` | 503 | Ollama not running, or the network is down. |
| `model_not_found` | 503 | `ollama create grip-grader -f Modelfile` |

Both 429s can come from step 11 without touching the provider, once one real
refusal has been seen.

## The three steps worth staring at

**Step 2** — the caller computes the numbers it is then graded against.
Deliberate, and safe only while the person who could be misled is the one doing
the cheating. Move fact derivation server-side before anyone else reads a grade.

**Step 14** — the cache key is the user, the model, the rubric and the exact
context. A prompt change regrades everything, an edit regrades that board, and
no two users ever share a grade.

**Step 19** — the model classifies; the service scores. Every trust property in
this service rests on that split.
