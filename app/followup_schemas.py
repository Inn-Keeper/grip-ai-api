"""Contracts for grading answers to interviewer follow-ups. Each answer is a
"section" keyed q1..qN, so the evidence and scoring guards apply unchanged."""

from uuid import UUID

from pydantic import Field, model_validator

from app.schemas import ScenarioFacts, SectionGrade, ShortText, StrictModel

MAX_FOLLOWUPS = 6


class FollowupAnswer(StrictModel):
    question: ShortText
    # Blank is allowed: an unanswered question is graded missing in code.
    answer: str = Field(default="", max_length=4_000)


class FollowupGradeRequest(StrictModel):
    board_id: UUID
    facts: ScenarioFacts
    followups: list[FollowupAnswer] = Field(min_length=1, max_length=MAX_FOLLOWUPS)


class AnswerGrade(SectionGrade):
    """SectionGrade keyed by answer id (q1, q2, ...); evidence rule inherited."""

    section: str = Field(pattern=r"^q[1-9]$")


class FollowupSuggestion(StrictModel):
    """What the model may return. Deliberately contains no score."""

    sections: list[AnswerGrade] = Field(min_length=1, max_length=MAX_FOLLOWUPS)

    @model_validator(mode="after")
    def grade_each_answer_once(self):
        seen = [item.section for item in self.sections]
        if len(set(seen)) != len(seen):
            raise ValueError("an answer was graded more than once")
        return self


class FollowupGradeResponse(StrictModel):
    request_id: UUID
    model: str
    board_id: UUID
    # Computed from the verdicts, never taken from the model.
    score: int = Field(ge=0, le=100)
    # One per submitted follow-up, in the order sent.
    answers: list[AnswerGrade]
