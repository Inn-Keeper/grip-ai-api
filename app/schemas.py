"""Request and response contracts for talk-track grading.

Two structural guards live here rather than in the prompt, because a prompt can
be ignored and a schema cannot:

1. A section graded anything other than "missing" MUST carry a verbatim quote
   from the candidate's own text. A model that wants to be generous has to cite
   something first, and ungrounded praise stops being expressible.

2. The model never returns a score. It returns per-section verdicts; the score
   is computed from those in `service.py`. Leniency can therefore only show up
   as a generous verdict on a specific section — which the fixtures test — and
   never as a quietly inflated number.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


ShortText = Annotated[str, Field(min_length=1, max_length=500)]

# Must stay in step with TALK_TRACK_SECTIONS in packages/core/src/talkTrack.js.
SectionId = Literal[
    "requirements",
    "scale",
    "api",
    "dataModel",
    "bottleneck",
    "tradeoff",
]

SECTION_IDS: tuple[str, ...] = (
    "requirements",
    "scale",
    "api",
    "dataModel",
    "bottleneck",
    "tradeoff",
)

Verdict = Literal["covered", "thin", "missing"]

# Points a verdict is worth when the service computes the score.
VERDICT_POINTS: dict[str, int] = {"covered": 100, "thin": 50, "missing": 0}


class ScenarioFacts(StrictModel):
    """Ground truth the grader checks answers against.

    Computed by the caller from packages/core (see README, "Trust boundary"):
    the arithmetic lives in JavaScript and is not duplicated here, so this
    service treats the facts as given rather than as something to re-derive.
    """

    scenario_id: str = Field(min_length=1, max_length=120)
    name: ShortText
    brief: str = Field(min_length=1, max_length=2_000)
    dau: int = Field(ge=0)
    payload_kb: float = Field(ge=0)
    retention_days: int = Field(ge=0)
    peak_qps: float = Field(ge=0)
    storage_gb: float = Field(ge=0)
    checks_passed: list[ShortText] = Field(default_factory=list, max_length=40)
    checks_failed: list[ShortText] = Field(default_factory=list, max_length=40)
    node_types: list[str] = Field(default_factory=list, max_length=40)
    partition_keys: list[str] = Field(default_factory=list, max_length=40)
    pushback: str = Field(default="", max_length=1_000)


class GradeRequest(StrictModel):
    board_id: UUID
    facts: ScenarioFacts
    sections: dict[SectionId, str]
    self_rating: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def cap_section_length(self):
        for section_id, text in self.sections.items():
            if len(text) > 8_000:
                raise ValueError(f"section {section_id} exceeds 8000 characters")
        return self


class SectionGrade(StrictModel):
    section: SectionId
    verdict: Verdict
    # Verbatim span from the candidate's text. Empty only when nothing was said.
    evidence: str = Field(default="", max_length=600)
    # What a senior interviewer would still ask after reading this section.
    gap: ShortText

    @model_validator(mode="after")
    def require_evidence_for_credit(self):
        if self.verdict != "missing" and not self.evidence.strip():
            raise ValueError(
                f"section {self.section} graded '{self.verdict}' without quoting evidence"
            )
        if self.verdict == "missing" and self.evidence.strip():
            raise ValueError(
                f"section {self.section} graded 'missing' but quotes evidence"
            )
        return self


class GradeSuggestion(StrictModel):
    """What the model is allowed to return. Deliberately contains no score."""

    sections: list[SectionGrade] = Field(min_length=6, max_length=6)
    hardest_followup: ShortText

    @model_validator(mode="after")
    def cover_every_section_once(self):
        seen = [item.section for item in self.sections]
        if sorted(seen) != sorted(SECTION_IDS):
            raise ValueError("every talk-track section must be graded exactly once")
        return self


class GradeResponse(StrictModel):
    request_id: UUID
    model: str
    board_id: UUID
    # Computed here from the verdicts, never taken from the model.
    score: int = Field(ge=0, le=100)
    # Positive when the candidate rated themselves higher than the grade.
    divergence: int | None = None
    suggestion: GradeSuggestion
