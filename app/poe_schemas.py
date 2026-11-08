"""Contracts for the Poe guide chat. The client sends the visible history each
time; nothing is stored. Caps mirror POE_LIMITS in packages/core/src/poeChat.js."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.import_schemas import StrictModel

USER_CHARS = 500
POE_CHARS = 1200
MAX_TURNS = 20


class PoeTurn(StrictModel):
    role: Literal["user", "poe"]
    text: str = Field(min_length=1, max_length=POE_CHARS)

    @model_validator(mode="after")
    def user_turns_are_short(self):
        if self.role == "user" and len(self.text) > USER_CHARS:
            raise ValueError(f"user messages are at most {USER_CHARS} characters")
        return self


class PoeChatRequest(StrictModel):
    locale: Literal["en", "pt", "sv"] = "en"
    messages: list[PoeTurn] = Field(min_length=1, max_length=MAX_TURNS)

    @model_validator(mode="after")
    def ends_with_user(self):
        if self.messages[-1].role != "user":
            raise ValueError("the last message must be from the user")
        return self


class PoeReply(BaseModel):
    reply: str = Field(min_length=1, max_length=POE_CHARS)
