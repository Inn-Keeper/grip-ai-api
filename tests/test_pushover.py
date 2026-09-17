"""Does the grader actually refuse bad answers?

Everything else in this suite proves the *plumbing* is honest: a grade cannot be
returned without evidence, and the score is computed rather than trusted. None
of that proves the model applies the rubric rather than being agreeable.

These checks call the selected provider only when explicitly enabled. They
exercise grading without Supabase credentials or writes:

    RUN_LIVE_AI=1 pytest -m live -s

Each fixture is deliberately bad in a different way. If any of them starts
passing as "covered", the rubric has gone soft and the feature is misleading.
"""

import os
import time

import pytest
import pytest_asyncio

from app.config import Settings
from app.gemini import GeminiClient
from app.ollama import OllamaClient
from app.prompts import SYSTEM_PROMPT, build_context
from app.schemas import GradeSuggestion
from app.service import apply_placeholder_floor, score_from_verdicts
from tests.conftest import CATALOG_FACTS


live = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_AI") != "1",
    reason="live provider test; set RUN_LIVE_AI=1 to run",
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


GOOD_ANSWER = {
    "requirements": "Serve product details and search under 200ms p99 at 99.99% availability. Price changes may take 60 seconds to propagate; stale stock is acceptable for browsing. Checkout and inventory reservation are out of scope.",
    "scale": "8 million DAU times 60 reads gives 480 million reads/day. Divide by 86400 for 5556 reads/sec average; a 3x peak is about 16667/sec. With 0.02 writes/user/day, 4KB per write and 1825 days retention, storage is 8e6 * 0.02 * 4 / 1e6 * 1825 = 1168GB, excluding indexes and replicas.",
    "api": "GET /products/{product_id} returns title, price, stock and version, with 404 for unknown ids. GET /search?q=...&cursor=... returns product summaries and a next cursor. PATCH /products/{product_id} accepts price and expected_version; staff auth is required and version conflicts return 409.",
    "dataModel": "Product(product_id primary key, title, price, stock, version) lives in SQL. Reads by product_id make it a suitable hash partition key when growth requires sharding; initially keep a single primary and read replicas. Index title in a separate search index, updated from SQL changes.",
    "bottleneck": "The uncached SQL reads are the first bottleneck at 16667 peak requests/sec. Add Redis cache-aside for product_id reads, invalidate on price updates and enforce a 60s TTL. Coalesce cache misses for hot products and rate-limit DB fallback during cache failure. Load-test the primary and replicas before selecting their sizes.",
    "tradeoff": "Choose cache-aside over write-through: reads dominate and it avoids making every write depend on Redis availability. Accept up to 60s stale browse data, but use versioned invalidations to limit races. If prices must be immediately consistent, move price reads to authoritative storage and revisit write-through with explicit consistency handling.",
}


async def grade(sections: dict) -> GradeSuggestion:
    settings = Settings()
    if settings.ai_provider == "gemini" and not settings.gemini_api_key:
        pytest.fail("GEMINI_API_KEY is required when AI_PROVIDER=gemini")
    client = (OllamaClient if settings.ai_provider == "ollama" else GeminiClient)(
        settings
    )
    result = await client.generate(
        settings.ai_model_grade,
        SYSTEM_PROMPT,
        build_context(CATALOG_FACTS, sections, None),
        GradeSuggestion,
    )
    return result.value


@pytest_asyncio.fixture(scope="module")
async def graded_cases():
    # Each input is generated once; all assertions inspect that same grade.
    cases = {}
    for name, sections in {
        "nonsense": CONFIDENT_NONSENSE,
        "wrong": WRONG_NUMBERS,
        "placeholders": PLACEHOLDERS,
        "good": GOOD_ANSWER,
    }.items():
        started = time.monotonic()
        cases[name] = await grade(sections)
        print(f"\n{name}: {time.monotonic() - started:.1f}s, {verdicts(cases[name])}")
    return cases


def verdicts(suggestion: GradeSuggestion) -> dict[str, str]:
    return {item.section: item.verdict for item in suggestion.sections}


def test_confident_nonsense_is_not_rewarded(graded_cases):
    result = verdicts(graded_cases["nonsense"])
    covered = [section for section, verdict in result.items() if verdict == "covered"]
    assert not covered, f"graded fluent hand-waving as covered: {covered}"


def test_wrong_arithmetic_fails_the_estimate(graded_cases):
    # 50/s against a derived 16,700/s is two orders of magnitude out. Whatever
    # happens elsewhere, the scale section must not pass.
    result = verdicts(graded_cases["wrong"])
    assert result["scale"] != "covered", "accepted an estimate 300x under the truth"


def test_wrong_arithmetic_undermines_the_bottleneck_too(graded_cases):
    # The bottleneck answer is only coherent given the wrong number, so a
    # grader that checks against ground truth should not pass it either.
    result = verdicts(graded_cases["wrong"])
    assert result["bottleneck"] != "covered"


def test_placeholders_score_near_zero(graded_cases):
    result = verdicts(graded_cases["placeholders"])
    assert "covered" not in result.values()
    # Score as the service does: the model alone rated these "thin" (50/100).
    graded = apply_placeholder_floor(graded_cases["placeholders"], PLACEHOLDERS)
    assert score_from_verdicts(graded) == 0


def test_every_section_gets_a_next_question(graded_cases):
    for suggestion in graded_cases.values():
        for item in suggestion.sections:
            assert item.gap.strip()
            assert item.gap.strip().lower() not in ("none", "nothing", "n/a")


def test_evidence_is_quoted_verbatim_from_the_answer(graded_cases):
    sources = {
        "nonsense": CONFIDENT_NONSENSE,
        "wrong": WRONG_NUMBERS,
        "placeholders": PLACEHOLDERS,
        "good": GOOD_ANSWER,
    }
    for name, suggestion in graded_cases.items():
        for item in suggestion.sections:
            if item.verdict == "missing":
                continue
            assert item.evidence.strip() in sources[name][item.section], (
                f"{name}/{item.section}: evidence was not copied from the candidate's text"
            )


def test_good_answers_receive_credit(graded_cases):
    result = verdicts(graded_cases["good"])
    assert list(result.values()).count("covered") >= 4
    assert "missing" not in result.values()
