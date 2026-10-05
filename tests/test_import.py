"""Ledger import endpoint, with the provider and Supabase stubbed."""

import base64
from datetime import datetime, timezone

from app.errors import AppError
from app.generation import GenerationResult
from app.import_schemas import ModelLedger, ModelRow
from tests.conftest import make_client
from tests.test_ledger import docx_bytes

URL = "/api/v1/ai/import/parse"
NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def row(lines, name="Acme", status="Applied", **fields) -> ModelRow:
    base = {
        "lines": lines,
        "name": name,
        "role": "Frontend Dev",
        "status": status,
        "date": None,
        "stage_date": None,
        "next_action": None,
        "next_action_date": None,
        "note": None,
    }
    return ModelRow(**{**base, **fields})


class SequenceProvider:
    """Answers each call with the next canned ledger and records what it was sent."""

    def __init__(self, *ledgers: ModelLedger, fail: Exception | None = None):
        self.ledgers = list(ledgers)
        self.fail = fail
        self.contexts: list[dict] = []

    async def generate(self, model, system_prompt, context, output_type):
        self.contexts.append(context)
        if self.fail:
            raise self.fail
        return GenerationResult(
            value=self.ledgers.pop(0), prompt_tokens=1, completion_tokens=1, retries=0
        )


def post(client, body, token="token"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(URL, headers=headers, json=body)


TEXT = (
    "Acme FE role https://jobs.acme.com/1 applied, recruiter jane@acme.com\n"
    "random thought about lunch\n"
    "Globex backend, interview on 2026-09-20"
)


class TestParse:
    def test_rows_are_restored_with_link_source_and_warnings(self):
        provider = SequenceProvider(
            ModelLedger(
                rows=[
                    row(
                        [1],
                        note="Recruiter: [EMAIL_1]",
                        next_action="Follow up",
                        next_action_date="2026-10-12",
                    ),
                    row(
                        [3],
                        name="Globex",
                        status="Interviewing",
                        role=None,
                        stage_date="2026-09-20",
                    ),
                ],
                unplaced=[2],
            )
        )
        response = post(make_client(gemini=provider), {"text": TEXT})
        assert response.status_code == 200
        body = response.json()
        acme, globex = body["rows"]
        assert acme["link"] == "https://jobs.acme.com/1"
        assert acme["note"] == "Recruiter: jane@acme.com"
        assert acme["source"] == TEXT.split("\n")[0]
        assert acme["next_action_date"] == "2026-10-12"
        assert acme["warnings"] == ["stage_date_unknown"]
        assert globex["stage_date"] == "2026-09-20"
        assert globex["warnings"] == ["missing_role", "missing_next_action_date"]
        assert body["unplaced"] == ["random thought about lunch"]

    def test_provider_sees_only_redacted_numbered_text(self):
        provider = SequenceProvider(ModelLedger(rows=[], unplaced=[]))
        post(make_client(gemini=provider), {"text": TEXT})
        sent = provider.contexts[0]["ledger"]
        assert "jane@acme.com" not in sent and "https://" not in sent
        assert sent.startswith("1: Acme FE role [LINK_1]")
        assert provider.contexts[0]["today"]

    def test_invalid_stage_and_bad_dates(self):
        provider = SequenceProvider(
            ModelLedger(
                rows=[
                    row([1], status="Ghosted"),
                    row(
                        [3],
                        name="Globex",
                        next_action_date="next tuesday",
                        date="2026-02-30",
                    ),
                ],
                unplaced=[],
            )
        )
        body = post(make_client(gemini=provider), {"text": TEXT}).json()
        assert [r["name"] for r in body["rows"]] == ["Globex"]
        assert body["rows"][0]["next_action_date"] is None
        assert body["rows"][0]["date"] is None
        assert body["unplaced"] == [TEXT.split("\n")[0]]

    def test_early_stages_are_dated_by_the_application_date(self):
        # Applying is reaching Applied; later stages need their own date.
        provider = SequenceProvider(
            ModelLedger(
                rows=[
                    row([1], status="Applied", date="2026-06-15"),
                    row([3], name="Globex", status="Interviewing", date="2026-06-15"),
                ],
                unplaced=[],
            )
        )
        acme, globex = post(make_client(gemini=provider), {"text": TEXT}).json()["rows"]
        assert acme["stage_date"] == "2026-06-15"
        assert "stage_date_unknown" not in acme["warnings"]
        assert globex["stage_date"] is None
        assert "stage_date_unknown" in globex["warnings"]

    def test_long_text_is_parsed_in_chunks_and_merged(self):
        lines = [f"Company{i} applied " + "x" * 80 for i in range(200)]
        # About 21,000 numbered characters: three chunks of at most 8,000.
        first = ModelLedger(rows=[row([1], name="Company0")], unplaced=[])
        middle = ModelLedger(rows=[], unplaced=[])
        tail = ModelLedger(rows=[row([len(lines)], name="Company199")], unplaced=[])
        provider = SequenceProvider(first, middle, tail)
        body = post(make_client(gemini=provider), {"text": "\n".join(lines)}).json()
        assert len(provider.contexts) == 3
        assert [r["name"] for r in body["rows"]] == ["Company0", "Company199"]

    def test_docx_upload(self):
        provider = SequenceProvider(ModelLedger(rows=[row([1])], unplaced=[]))
        data = base64.b64encode(docx_bytes(["Acme applied"])).decode()
        body = post(
            make_client(gemini=provider),
            {"filename": "ledger.docx", "content_base64": data},
        ).json()
        assert body["rows"][0]["source"] == "Acme applied"


class TestRejects:
    def test_missing_token(self):
        assert post(make_client(), {"text": "a"}, token=None).status_code == 401

    def test_text_and_file_together_or_neither(self):
        client = make_client()
        assert post(client, {}).status_code == 422
        both = {"text": "a", "filename": "a.md", "content_base64": "YQ=="}
        assert post(client, both).status_code == 422

    def test_too_long(self):
        response = post(make_client(), {"text": "x\n" * 30_000})
        assert response.status_code == 413

    def test_file_too_big(self):
        data = base64.b64encode(b"x" * 1_100_000).decode()
        response = post(make_client(), {"filename": "a.md", "content_base64": data})
        assert response.status_code == 413

    def test_empty_text(self):
        assert post(make_client(), {"text": "  \n "}).status_code == 422

    def test_spent_quota_blocks_import_like_grading(self):
        limit = AppError(
            429, "provider_quota_exhausted", "Out of quota.", retry_after=600
        )
        provider = SequenceProvider(fail=limit)
        client = make_client(gemini=provider, clock=lambda: NOW)
        assert post(client, {"text": TEXT}).status_code == 429
        assert post(client, {"text": TEXT}).status_code == 429
        assert len(provider.contexts) == 1
        assert client.get("/api/v1/ai/status").json()["grading"] == "unavailable"
