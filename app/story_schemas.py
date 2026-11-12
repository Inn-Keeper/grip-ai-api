"""Contracts for STAR story grading. Same guards as schemas.py: credit needs a
verbatim quote, and the model returns verdicts, never a score."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas import SectionGrade, ShortText, StrictModel

# Must stay in step with the story columns in grip-apps (stories table).
StorySectionId = Literal["situation", "task", "action", "result"]

STORY_SECTION_IDS: tuple[str, ...] = ("situation", "task", "action", "result")


class StoryGradeRequest(StrictModel):
    story_id: UUID
    competency: str = Field(min_length=1, max_length=60)
    title: str = Field(min_length=1, max_length=200)
    sections: dict[StorySectionId, str]

    @model_validator(mode="after")
    def cap_section_length(self):
        for section_id, text in self.sections.items():
            if len(text) > 8_000:
                raise ValueError(f"section {section_id} exceeds 8000 characters")
        return self


class StorySectionGrade(SectionGrade):
    """SectionGrade with STAR ids; the evidence rule is inherited."""

    section: StorySectionId


class StoryGradeSuggestion(StrictModel):
    """What the model may return. Deliberately contains no score."""

    sections: list[StorySectionGrade] = Field(min_length=4, max_length=4)
    hardest_followup: ShortText

    @model_validator(mode="after")
    def cover_every_section_once(self):
        seen = [item.section for item in self.sections]
        if sorted(seen) != sorted(STORY_SECTION_IDS):
            raise ValueError("every STAR section must be graded exactly once")
        return self


class StoryGradeResponse(StrictModel):
    request_id: UUID
    model: str
    story_id: UUID
    # Computed from the verdicts, never taken from the model.
    score: int = Field(ge=0, le=100)
    suggestion: StoryGradeSuggestion
