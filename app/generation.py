"""Validated output and usage shared by the two model transports."""

from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel


T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class GenerationResult(Generic[T]):
    value: T
    prompt_tokens: int | None
    completion_tokens: int | None
    retries: int
