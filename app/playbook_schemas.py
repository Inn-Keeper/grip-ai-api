"""Contracts for the first-run playbook: interview questions as sample STAR
stories plus one scored Arch Board scenario. Caps mirror FIRST_RUN_LIMITS in
packages/core/src/firstRun.js."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.errors import AppError
from app.import_schemas import StrictModel

Short = Annotated[str, Field(max_length=40)]
Note = Annotated[str, Field(max_length=500)]
Star = Annotated[str, Field(max_length=1200)]


class PlaybookRequest(StrictModel):
    stack: list[Short] = Field(default_factory=list, max_length=30)
    goal: Literal["technical", "stories", "design", "explore"]
    brief: str = Field(default="", max_length=500)
    role: str = Field(default="", max_length=120)
    job_text: str = Field(default="", max_length=4000)
    cv_text: str = Field(default="", max_length=3000)
    notes: list[Note] = Field(default_factory=list, max_length=3)
    locale: Literal["en", "pt", "sv"] = "en"
    competencies: list[Short] = Field(min_length=1, max_length=20)
    node_types: list[Short] = Field(min_length=1, max_length=60)


class ModelStory(BaseModel):
    title: str = Field(max_length=200)
    competency: str = Field(max_length=40)
    situation: Star
    task: Star
    action: Star
    result: Star
    technical: bool


class ModelCheck(BaseModel):
    # ponytail: node and edge only; "prop" checks wait until a generated scenario needs them.
    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["node", "edge"]
    type: list[Short] = Field(max_length=6)
    from_: list[Short] = Field(alias="from", max_length=6)
    to: list[Short] = Field(max_length=6)
    label: str = Field(max_length=120)
    points: int = Field(ge=1, le=30)


class ModelScenario(BaseModel):
    name: str = Field(max_length=80)
    brief: str = Field(max_length=1500)
    # Components placed on the user's first board: a start, not the solution.
    starter: list[Short] = Field(min_length=2, max_length=4)
    checks: list[ModelCheck] = Field(min_length=3, max_length=12)


class ModelPlaybook(BaseModel):
    stories: list[ModelStory] = Field(min_length=5, max_length=8)
    scenario: ModelScenario


def _invalid(detail: str) -> AppError:
    return AppError(
        502,
        "invalid_model_response",
        f"The model returned a playbook Grip cannot use ({detail}). Try again.",
    )


def validate_playbook(
    playbook: ModelPlaybook, competencies: list[str], node_types: list[str]
) -> None:
    """Reject ids outside the catalogs the client sent; nothing partial is kept."""
    allowed = set(node_types)
    canonical = {c.lower(): c for c in competencies}
    for story in playbook.stories:
        # Case slips are repaired; anything else (a translated label) is rejected.
        match = canonical.get(story.competency.strip().lower())
        if match is None:
            raise _invalid("unknown competency")
        story.competency = match
    if any(i not in allowed for i in playbook.scenario.starter):
        raise _invalid("unknown starter component")
    for check in playbook.scenario.checks:
        ids = check.type + check.from_ + check.to
        if any(i not in allowed for i in ids):
            raise _invalid("unknown component")
        if check.kind == "node" and not check.type:
            raise _invalid("component check without a type")
        if check.kind == "edge" and not (check.from_ and check.to):
            raise _invalid("connection check without both ends")
