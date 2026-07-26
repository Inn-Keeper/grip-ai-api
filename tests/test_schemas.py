"""The structural anti-pushover guards.

These are the tests that matter most in this service. They assert that a model
*cannot* express a generous grade without grounding it, regardless of what the
prompt says or how the model behaves on any given day.
"""

import pytest
from pydantic import ValidationError

from app.schemas import SECTION_IDS, GradeSuggestion, SectionGrade


def graded(section: str, verdict: str, evidence: str = "", gap: str = "And then?"):
    return {"section": section, "verdict": verdict, "evidence": evidence, "gap": gap}


def full_set(verdict: str = "covered", evidence: str = "quoted span"):
    return [graded(section, verdict, evidence) for section in SECTION_IDS]


class TestEvidenceRule:
    def test_credit_without_a_quote_is_rejected(self):
        with pytest.raises(ValidationError, match="without quoting evidence"):
            SectionGrade(**graded("scale", "covered", evidence=""))

    def test_thin_also_requires_a_quote(self):
        with pytest.raises(ValidationError, match="without quoting evidence"):
            SectionGrade(**graded("scale", "thin", evidence="   "))

    def test_missing_must_not_quote_anything(self):
        # A section graded "missing" that somehow quotes text is incoherent —
        # either the text exists or it doesn't.
        with pytest.raises(ValidationError, match="quotes evidence"):
            SectionGrade(**graded("scale", "missing", evidence="something"))

    def test_a_grounded_verdict_is_accepted(self):
        grade = SectionGrade(**graded("scale", "covered", evidence="8M x 60 / 86400"))
        assert grade.verdict == "covered"


class TestCoverage:
    def test_every_section_must_be_graded(self):
        with pytest.raises(ValidationError):
            GradeSuggestion(sections=full_set()[:5], hardest_followup="?")

    def test_grading_one_section_twice_is_rejected(self):
        sections = full_set()[:5] + [graded("scale", "thin", "dup")]
        with pytest.raises(ValidationError, match="exactly once"):
            GradeSuggestion(sections=sections, hardest_followup="?")

    def test_a_complete_set_is_accepted(self):
        suggestion = GradeSuggestion(sections=full_set(), hardest_followup="Why?")
        assert len(suggestion.sections) == len(SECTION_IDS)


class TestNoModelSuppliedScore:
    def test_the_model_cannot_return_a_score(self):
        # extra="forbid" — if a future prompt tempts the model to add one, the
        # response is rejected rather than quietly trusted.
        with pytest.raises(ValidationError):
            GradeSuggestion(
                sections=full_set(),
                hardest_followup="?",
                score=95,
            )

    def test_a_gap_is_always_required(self):
        with pytest.raises(ValidationError):
            SectionGrade(section="scale", verdict="covered", evidence="x", gap="")
