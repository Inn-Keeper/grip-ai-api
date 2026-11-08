"""Playbook contracts and the checks that keep model output inside Grip's catalogs."""

import pytest
from pydantic import ValidationError

from app.errors import AppError
from app.playbook_schemas import (
    ModelCheck,
    ModelPlaybook,
    PlaybookRequest,
    validate_playbook,
)

COMPETENCIES = ["Conflict", "Failure", "Impact"]
NODE_TYPES = ["client", "service", "sql", "cache"]


def story(**over) -> dict:
    base = {
        "title": "Tell me about a time you owned an outage.",
        "competency": "Failure",
        "situation": "s",
        "task": "t",
        "action": "a",
        "result": "r",
        "technical": False,
    }
    return {**base, **over}


def check(**over) -> dict:
    base = {
        "kind": "node",
        "type": ["cache"],
        "from": [],
        "to": [],
        "label": "A cache",
        "points": 10,
    }
    return {**base, **over}


def playbook(stories=None, checks=None) -> ModelPlaybook:
    return ModelPlaybook.model_validate(
        {
            "stories": stories or [story() for _ in range(5)],
            "scenario": {
                "name": "Payments",
                "brief": "Design a payments service.",
                "checks": checks
                or [
                    check(),
                    check(kind="edge", type=[], **{"from": ["client"]}, to=["service"]),
                    check(type=["sql"]),
                ],
            },
        }
    )


def test_valid_playbook_passes():
    validate_playbook(playbook(), COMPETENCIES, NODE_TYPES)


def test_rejects_unknown_competency():
    bad = playbook(stories=[story(competency="Teamwork")] + [story()] * 4)
    with pytest.raises(AppError) as error:
        validate_playbook(bad, COMPETENCIES, NODE_TYPES)
    assert error.value.status_code == 502
    assert error.value.code == "invalid_model_response"


def test_rejects_unknown_node_type():
    edge = check(kind="edge", type=[], **{"from": ["kafka"]}, to=["service"])
    with pytest.raises(AppError) as error:
        validate_playbook(
            playbook(checks=[edge, check(), check()]), COMPETENCIES, NODE_TYPES
        )
    assert error.value.status_code == 502


def test_edge_needs_from_and_to():
    edge = check(kind="edge", type=[], **{"from": ["client"]}, to=[])
    with pytest.raises(AppError):
        validate_playbook(
            playbook(checks=[edge, check(), check()]), COMPETENCIES, NODE_TYPES
        )


def test_node_needs_type():
    with pytest.raises(AppError):
        validate_playbook(
            playbook(checks=[check(type=[]), check(), check()]),
            COMPETENCIES,
            NODE_TYPES,
        )


def test_request_caps():
    with pytest.raises(ValidationError):
        PlaybookRequest(
            stack=["React"],
            goal="technical",
            job_text="x" * 4001,
            competencies=COMPETENCIES,
            node_types=NODE_TYPES,
        )


def test_check_serializes_from_alias():
    dumped = ModelCheck.model_validate(check(**{"from": ["client"]})).model_dump(
        by_alias=True
    )
    assert dumped["from"] == ["client"]


def test_competency_case_is_normalized():
    book = playbook(stories=[story(competency="failure")] + [story()] * 4)
    validate_playbook(book, COMPETENCIES, NODE_TYPES)
    assert book.stories[0].competency == "Failure"


def test_prompt_keeps_ids_untranslated():
    from app.playbook_prompt import PLAYBOOK_PROMPT

    assert "never translate" in PLAYBOOK_PROMPT.lower()
