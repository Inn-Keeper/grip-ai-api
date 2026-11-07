# Grading calibration

Status: prepared, not calibrated. No live generations were run for this repair.

The current rubric targets senior system-design practice. A quotation proves
that the words exist, not that the verdict is correct. The four-word floor uses
Python whitespace splitting: every section with at most four words becomes
`missing`, even a concise meaningful answer. Longer fluent placeholders can
still require model judgment. Unicode acceptance does not establish language
parity. Scores are practice feedback, not validated hiring or readiness measures.

## Fixtures and human review

`tests/fixtures/grading/request.json` is a complete synthetic request.
`tests/fixtures/grading/calibration.json` contains draft matched snippets in
English, Portuguese and Swedish: concise reasoning, verbose empty language,
wrong arithmetic and specific mitigation. They are a starting set, not an
exhaustive benchmark. All human-review fields are deliberately pending.

Before model evaluation, a fluent reviewer checks translation equivalence and
assigns an expected section verdict, supporting evidence, acceptable follow-up,
experience level and ambiguity note. Expand across requirements, API, data model
and tradeoffs, plus different scenarios and concise answers above/below the floor.
Do not infer full-answer quality from these isolated snippets.

## Authorized evaluation procedure

1. Obtain an explicit request to run live inference. Record provider, exact
   model/quantization, prompt revision, configuration, fixture revision and date.
   Agree on the number of calls first; cloud runs spend quota and send content.
2. Keep the existing four-case live suite as the initial check. Integrate reviewed
   snippets into complete six-section answers before expanding the benchmark.
3. Repeat each reviewed answer three times per provider. Record raw model verdicts
   and final floored verdicts separately, quoted evidence and follow-up usefulness.
4. Compare false credit (model awards more than reviewed), false rejection
   (less than reviewed), repeat-run disagreement and useful-follow-up counts.
   Break results down by language, scenario and answer length; report denominators.
5. Investigate every difference caused by the floor and every unsupported quote.
   A fluent but empty answer must not gain credit merely to rescue concise answers.
6. Keep the floor or propose a narrow replacement from this evidence. Review the
   scoring change separately with regression fixtures; do not silently retune it.

No accuracy or fairness claim is justified by the deterministic boundary tests.
