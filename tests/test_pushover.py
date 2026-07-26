"""Does the grader actually refuse bad answers?

Everything else in this suite proves the *plumbing* is honest: a grade cannot be
returned without evidence, and the score is computed rather than trusted. None
of that proves the model applies the rubric rather than being agreeable.

Only a real call can prove that, so these are marked `live` and skipped unless
GEMINI_API_KEY is set — the same arrangement ativscrum-ai-api uses for its live
tenant tests. Run them before trusting a grade, and again after any prompt edit:

    GEMINI_API_KEY=... pytest -m live

Each fixture is deliberately bad in a different way. If any of them starts
passing as "covered", the rubric has gone soft and the feature is misleading.
"""

import os

import pytest

from app.config import Settings
from app.gemini import GeminiClient
from app.prompts import SYSTEM_PROMPT, build_context
from app.schemas import GradeSuggestion
from tests.conftest import CATALOG_FACTS


live = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="live provider test; set GEMINI_API_KEY to run",
)

pytestmark = [pytest.mark.live, live]


# Reads like an answer, says nothing. The classic thing a lenient judge rewards.
CONFIDENT_NONSENSE = {
    "requirements": "We need a highly scalable, robust and performant system with excellent user experience and strong reliability guarantees across the board.",
    "scale": "The system must handle very high traffic at massive scale with significant storage requirements as the product grows.",
    "api": "We will expose a clean, well-designed RESTful API following industry best practices and standard conventions.",
    "dataModel": "We will use an appropriate database with a well-normalised schema designed for our access patterns.",
    "bottleneck": "The main bottleneck will be handled by scaling horizontally and adding caching where appropriate.",
    "tradeoff": "There are tradeoffs between consistency and availability and we have chosen a sensible balance.",
}

# Right shape, wrong arithmetic: derived peak is ~16,700/s, this claims 50/s.
WRONG_NUMBERS = {
    "requirements": "Product pages under 200ms p99, four nines availability. Price edits can lag a minute. Checkout is out of scope.",
    "scale": "8 million users doing 60 requests a day is about 50 requests per second at peak, so a single database handles it comfortably.",
    "api": "GET /products/{id} returns product and stock. GET /search?q= returns a page of ids.",
    "dataModel": "Product keyed by product_id, partitioned on product_id since all reads are by id.",
    "bottleneck": "Nothing really bottlenecks at 50 requests per second, one Postgres box is plenty.",
    "tradeoff": "Chose a single database over sharding because the load is low. Would shard if traffic grew.",
}

# Barely there. Should be near-zero.
PLACEHOLDERS = {
    "requirements": "fast and reliable",
    "scale": "lots of users",
    "api": "REST",
    "dataModel": "postgres",
    "bottleneck": "the db",
    "tradeoff": "cache vs no cache",
}


async def grade(sections: dict) -> GradeSuggestion:
    settings = Settings(gemini_api_key=os.environ["GEMINI_API_KEY"])
    client = GeminiClient(settings)
    result = await client.generate(
        settings.ai_model_grade,
        SYSTEM_PROMPT,
        build_context(CATALOG_FACTS, sections, None),
        GradeSuggestion,
    )
    return result.value


def verdicts(suggestion: GradeSuggestion) -> dict[str, str]:
    return {item.section: item.verdict for item in suggestion.sections}


async def test_confident_nonsense_is_not_rewarded():
    result = verdicts(await grade(CONFIDENT_NONSENSE))
    covered = [section for section, verdict in result.items() if verdict == "covered"]
    assert not covered, f"graded fluent hand-waving as covered: {covered}"


async def test_wrong_arithmetic_fails_the_estimate():
    # 50/s against a derived 16,700/s is two orders of magnitude out. Whatever
    # happens elsewhere, the scale section must not pass.
    result = verdicts(await grade(WRONG_NUMBERS))
    assert result["scale"] != "covered", "accepted an estimate 300x under the truth"


async def test_wrong_arithmetic_undermines_the_bottleneck_too():
    # The bottleneck answer is only coherent given the wrong number, so a
    # grader that checks against ground truth should not pass it either.
    result = verdicts(await grade(WRONG_NUMBERS))
    assert result["bottleneck"] != "covered"


async def test_placeholders_score_near_zero():
    result = verdicts(await grade(PLACEHOLDERS))
    assert "covered" not in result.values()
    thin_or_worse = sum(1 for v in result.values() if v in ("thin", "missing"))
    assert thin_or_worse == 6


async def test_every_section_gets_a_next_question():
    suggestion = await grade(PLACEHOLDERS)
    for item in suggestion.sections:
        assert item.gap.strip()
        assert item.gap.strip().lower() not in ("none", "nothing", "n/a")


async def test_evidence_is_quoted_verbatim_from_the_answer():
    suggestion = await grade(WRONG_NUMBERS)
    for item in suggestion.sections:
        if item.verdict == "missing":
            continue
        source = WRONG_NUMBERS[item.section]
        assert item.evidence.strip() in source, (
            f"{item.section}: evidence was not copied from the candidate's text"
        )
