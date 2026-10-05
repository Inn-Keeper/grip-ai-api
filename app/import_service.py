"""Turns an uploaded ledger into reviewable rows. Nothing is stored or logged
except counts; the web app saves the rows the user confirms."""

import base64
import binascii
import logging
import re
from datetime import date

from app.errors import AppError
from app.import_prompt import IMPORT_PROMPT
from app.import_schemas import (
    ImportRequest,
    ImportResponse,
    ImportRow,
    ModelLedger,
    ModelRow,
)
from app.ledger import extract_text, redact, restore, split_chunks
from app.service import GradeService

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 1_000_000
MAX_TEXT_CHARS = 40_000
# Leaves room for the prompt and JSON escaping under AI_CONTEXT_MAX_CHARS (12,000).
CHUNK_CHARS = 8_000
STAGES = {"Contacted", "Applied", "Interviewing", "Offer", "Rejected"}
# Reached on the application or first-contact date itself; later stages need their own date.
DATED_BY_APPLICATION = {"Contacted", "Applied"}
LINK = re.compile(r"\[LINK_\d+\]")


def iso_or_none(value: str | None) -> str | None:
    try:
        return date.fromisoformat(value).isoformat() if value else None
    except ValueError:
        return None


class ImportService:
    """Shares the grading service's provider, auth and quota memory, so a spent
    daily quota blocks both features with the same answer."""

    def __init__(self, grading: GradeService) -> None:
        self.grading = grading

    async def parse(
        self, payload: ImportRequest, token: str | None, request_id: str
    ) -> ImportResponse:
        if not token:
            raise AppError(401, "authentication_required", "Authentication required.")
        await self.grading.supabase.validate_session(token)

        lines = [line for line in self._text(payload).split("\n") if line.strip()]
        if not lines:
            raise AppError(
                422, "nothing_to_import", "We didn't find any text to import."
            )

        clean, mapping = redact("\n".join(lines))
        numbered = "\n".join(
            f"{n}: {line}" for n, line in enumerate(clean.split("\n"), 1)
        )
        redacted_lines = clean.split("\n")
        chunks = split_chunks(numbered, CHUNK_CHARS)

        rows: list[ImportRow] = []
        unplaced: list[int] = []
        today = self.grading.clock().date().isoformat()
        for chunk in chunks:
            ledger = await self._generate({"today": today, "ledger": chunk})
            for model_row in ledger.rows:
                row = self._row(model_row, lines, redacted_lines, mapping)
                if row is None:
                    unplaced.extend(model_row.lines)
                else:
                    rows.append(row)
            unplaced.extend(ledger.unplaced)

        log.info(
            "import %s: chunks=%d rows=%d unplaced=%d",
            request_id,
            len(chunks),
            len(rows),
            len(unplaced),
        )
        return ImportResponse(
            rows=rows,
            unplaced=[
                lines[n - 1] for n in dict.fromkeys(unplaced) if 1 <= n <= len(lines)
            ],
        )

    def _text(self, payload: ImportRequest) -> str:
        if payload.text is not None:
            text = payload.text
        else:
            try:
                data = base64.b64decode(payload.content_base64 or "", validate=True)
            except binascii.Error as exc:
                raise AppError(
                    422, "unreadable_file", "The file could not be read."
                ) from exc
            if len(data) > MAX_FILE_BYTES:
                raise AppError(413, "file_too_large", "Files can be at most 1 MB.")
            text = extract_text(payload.filename or "", data)
        if len(text) > MAX_TEXT_CHARS:
            raise AppError(
                413,
                "ledger_too_long",
                "This list is too long to import at once. Split it into smaller files.",
            )
        return text

    async def _generate(self, context: dict) -> ModelLedger:
        limit = self.grading.current_limit()
        if limit is not None:
            raise limit
        try:
            result = await self.grading.provider.generate(
                self.grading.settings.ai_model_grade,
                IMPORT_PROMPT,
                context,
                ModelLedger,
            )
        except AppError as error:
            self.grading.remember(error)
            raise
        return result.value

    @staticmethod
    def _row(
        row: ModelRow, lines: list[str], redacted: list[str], mapping: dict[str, str]
    ) -> ImportRow | None:
        """A validated row, or None when it cannot be trusted (it goes to unplaced)."""
        numbers = [n for n in dict.fromkeys(row.lines) if 1 <= n <= len(lines)]
        if row.status not in STAGES or not row.name.strip() or not numbers:
            return None
        links = [m for n in numbers for m in LINK.findall(redacted[n - 1])]
        next_action_date = iso_or_none(row.next_action_date)
        stage_date = iso_or_none(row.stage_date)
        if stage_date is None and row.status in DATED_BY_APPLICATION:
            stage_date = iso_or_none(row.date)
        warnings = [
            name
            for name, missing in [
                ("missing_role", not row.role),
                ("missing_next_action_date", next_action_date is None),
                ("stage_date_unknown", stage_date is None),
            ]
            if missing
        ]
        return ImportRow(
            name=restore(row.name, mapping).strip(),
            role=restore(row.role, mapping) if row.role else None,
            status=row.status,
            date=iso_or_none(row.date),
            stage_date=stage_date,
            next_action=restore(row.next_action, mapping) if row.next_action else None,
            next_action_date=next_action_date,
            link=mapping.get(links[0]) if links else None,
            note=restore(row.note, mapping) if row.note else None,
            source="\n".join(lines[n - 1] for n in numbers),
            warnings=warnings,
        )
