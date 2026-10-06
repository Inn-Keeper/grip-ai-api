"""Contracts for ledger import.

The model's output is deliberately loose (status and dates are plain strings)
so one bad row cannot fail the whole schema; each row is checked in
`import_service.py` and moved to `unplaced` if it does not hold up. The model
points at its source by line number, which keeps its output small and lets the
response show the user's own words rather than the redacted ones.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Must stay in step with STATUSES in packages/core/src/contacts.js.
Stage = Literal["Contacted", "Applied", "Interviewing", "Offer", "Rejected"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportRequest(StrictModel):
    """Either pasted text, or a file sent as base64 (no multipart dependency)."""

    text: str | None = None
    filename: str | None = Field(default=None, max_length=255)
    content_base64: str | None = None

    @model_validator(mode="after")
    def one_source(self):
        has_file = self.filename is not None and self.content_base64 is not None
        if (self.text is not None) == has_file:
            raise ValueError("send either text, or filename with content_base64")
        return self


class ModelRow(StrictModel):
    lines: list[int]
    name: str
    role: str | None
    status: str
    date: str | None
    stage_date: str | None
    next_action: str | None
    next_action_date: str | None
    note: str | None
    must_have_techs: list[str]


class ModelLedger(StrictModel):
    rows: list[ModelRow]
    unplaced: list[int]


class ImportRow(BaseModel):
    name: str
    role: str | None
    status: Stage
    date: str | None
    stage_date: str | None
    next_action: str | None
    next_action_date: str | None
    link: str | None
    note: str | None
    must_have_techs: list[str]
    source: str
    warnings: list[str]


class ImportResponse(BaseModel):
    rows: list[ImportRow]
    unplaced: list[str]


PostingStatus = Literal[
    "ok", "blocked", "unreachable", "not_html", "too_large", "empty"
]


class PostingsRequest(StrictModel):
    urls: list[Annotated[str, Field(max_length=2048)]] = Field(
        min_length=1, max_length=10
    )


class PostingResult(BaseModel):
    url: str
    status: PostingStatus
    text: str | None


class PostingsResponse(BaseModel):
    postings: list[PostingResult]
