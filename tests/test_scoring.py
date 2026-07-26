"""Scoring is computed here, never taken from the model."""

from app.schemas import SECTION_IDS, GradeSuggestion, SectionGrade
from app.service import divergence_from, score_from_verdicts


def suggestion(*verdicts: str) -> GradeSuggestion:
    sections = [
        SectionGrade(
            section=section,
            verdict=verdict,
            evidence="" if verdict == "missing" else "quoted",
            gap="And then?",
        )
        for section, verdict in zip(SECTION_IDS, verdicts, strict=True)
    ]
    return GradeSuggestion(sections=sections, hardest_followup="Why that key?")


class TestScoreFromVerdicts:
    def test_all_covered_is_full_marks(self):
        assert score_from_verdicts(suggestion(*["covered"] * 6)) == 100

    def test_all_missing_is_zero(self):
        assert score_from_verdicts(suggestion(*["missing"] * 6)) == 0

    def test_thin_is_worth_half(self):
        assert score_from_verdicts(suggestion(*["thin"] * 6)) == 50

    def test_a_mixed_answer_lands_between(self):
        # 3 covered, 2 thin, 1 missing -> (300 + 100 + 0) / 6
        score = score_from_verdicts(
            suggestion("covered", "covered", "covered", "thin", "thin", "missing")
        )
        assert score == 67

    def test_one_strong_section_cannot_carry_the_rest(self):
        score = score_from_verdicts(
            suggestion("covered", "missing", "missing", "missing", "missing", "missing")
        )
        assert score == 17


class TestDivergence:
    def test_overconfidence_is_positive(self):
        # Rated 5/5 (100) against a graded 50.
        assert divergence_from(5, 50) == 50

    def test_agreement_is_zero(self):
        assert divergence_from(3, 60) == 0

    def test_underconfidence_is_reported_not_clamped(self):
        assert divergence_from(2, 80) == -40

    def test_no_self_rating_means_no_divergence(self):
        assert divergence_from(None, 80) is None
