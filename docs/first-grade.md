# First synthetic grade

Follow README local setup first. Native Ollama is the default; the Gemini path
requires an explicit provider/model/key/output-budget configuration. Never silently
fall back from local inference to cloud inference.

1. With Ollama running, create `grip-grader` using the README commands. Check
   `/api/tags` for the configured model. This does not verify generation.
2. Configure Supabase auth and exact CORS origins, then start Uvicorn on port 8000.
   `/health` checks the process, `/ready` checks configuration only, and
   `/api/v1/ai/status` reports remembered provider limits only. None proves that
   the model is reachable or has remaining quota.
3. Use a dedicated test account in your configured Grip client. Obtain its current
   Supabase session access token using your existing development tools. Treat it
   as a credential: do not paste it into chat, commit it or print it in logs.
   The API has no auth bypass. The synthetic board UUID is accepted because this
   service does not look up or save the board; normal clients use a saved board.
4. Load that token into `GRIP_TEST_ACCESS_TOKEN` through your local secret workflow,
   without shell tracing or echoing it. From the repository root, send:

   ```bash
   curl --fail-with-body http://localhost:8000/api/v1/ai/grade-talk-track \
     -H "Authorization: Bearer ${GRIP_TEST_ACCESS_TOKEN}" \
     -H 'Content-Type: application/json' \
     --data-binary @tests/fixtures/grading/request.json
   ```

   This performs real inference. With Gemini selected it sends the synthetic
   fixture to that provider and spends quota. Do this only when live testing is
   requested. Do not save private requests or tokens to fixture files.
5. Expect HTTP 200 containing `request_id`, `model`, `board_id`, computed `score`,
   `divergence` and `suggestion`. The suggestion contains all six sections exactly
   once, each with `section`, `verdict`, `evidence`, `gap`, plus `hardest_followup`.
   For example, a deliberately illustrative result may contain:

   ```json
   {"section":"bottleneck","verdict":"thin","evidence":"The database at 17k/s.","gap":"How do you prevent cache misses from overwhelming SQL?"}
   ```

   This is one section, not a complete response or a promised model output. If all
   six verdicts are `thin`, score is 50; self-rating 4 produces divergence 30.
   Check that every credited quotation occurs in its submitted section. A 200
   response establishes integration, not a well-calibrated grade.

For an offline full endpoint example, run
`.venv/bin/python -m pytest -q tests/test_grade.py`: auth and provider are stubbed.
For failures, use README's error table. `provider_busy` means wait for the retry
hint; `invalid_request` means correct the payload; provider limits block new
generation but valid user-scoped cached feedback remains available. Retrying an
identical successful request uses the in-memory cache until eviction/restart.
