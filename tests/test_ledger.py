"""Text extraction, redaction and chunking for ledger import."""

import io
import zipfile

import pytest

from app.errors import AppError
from app.ledger import extract_text, redact, restore, split_chunks


def docx_bytes(paragraphs: list[str]) -> bytes:
    """A minimal .docx: only word/document.xml, which is all extract_text reads."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


class TestExtract:
    def test_docx_paragraphs_become_lines(self):
        data = docx_bytes(["Acme, applied", "Globex, interviewing"])
        assert (
            extract_text("ledger.docx", data) == "Acme, applied\nGlobex, interviewing"
        )

    def test_markdown_passes_through(self):
        assert (
            extract_text("notes.MD", "- Acme\n- Globex".encode()) == "- Acme\n- Globex"
        )

    def test_unknown_type_is_rejected(self):
        with pytest.raises(AppError) as error:
            extract_text("ledger.pdf", b"%PDF")
        assert error.value.status_code == 415

    def test_oversized_docx_xml_is_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", "<a>" + "x" * 10_000_001 + "</a>")
        with pytest.raises(AppError) as error:
            extract_text("ledger.docx", buffer.getvalue())
        assert error.value.status_code == 413

    def test_broken_docx_is_rejected(self):
        with pytest.raises(AppError) as error:
            extract_text("ledger.docx", b"not a zip")
        assert error.value.status_code == 422


SENSITIVE = (
    "Acme FE role https://jobs.acme.com/123?ref=a@b.com applied\n"
    "recruiter jane.doe@acme.com, call +46 70 123 45 67\n"
    "offer was $140k, other one 120.000 SEK"
)


class TestRedact:
    def test_every_pattern_is_replaced(self):
        clean, mapping = redact(SENSITIVE)
        for secret in [
            "https://jobs.acme.com/123?ref=a@b.com",
            "jane.doe@acme.com",
            "+46 70 123 45 67",
            "$140k",
            "120.000 SEK",
        ]:
            assert secret not in clean
            assert secret in mapping.values()
        assert "[LINK_1]" in clean and "[EMAIL_1]" in clean and "[PHONE_1]" in clean
        assert "[SALARY_1]" in clean and "[SALARY_2]" in clean

    def test_a_url_with_an_email_inside_is_one_link(self):
        clean, mapping = redact("see https://x.io/a@b.com/555-123-4567 now")
        assert clean == "see [LINK_1] now"
        assert len(mapping) == 1

    def test_restore_round_trips(self):
        clean, mapping = redact(SENSITIVE)
        assert restore(clean, mapping) == SENSITIVE

    def test_invented_placeholders_are_dropped(self):
        assert restore("call [PHONE_9] today", {}) == "call  today"

    def test_dates_reach_the_model(self):
        text = "applied 05-10-2026, interview 2026-10-20, follow up 12/10/2026"
        assert redact(text) == (text, {})

    def test_plain_text_is_untouched(self):
        assert redact("Acme, applied in May") == ("Acme, applied in May", {})


class TestChunks:
    def test_lines_are_kept_whole_and_once(self):
        lines = [f"entry {i} " + "x" * 30 for i in range(100)]
        chunks = split_chunks("\n".join(lines), limit=200)
        assert all(len(chunk) <= 200 for chunk in chunks)
        assert "\n".join(chunks).split("\n") == lines

    def test_short_text_is_one_chunk(self):
        assert split_chunks("a\nb", limit=200) == ["a\nb"]

    def test_a_line_longer_than_the_limit_is_cut(self):
        chunks = split_chunks("y" * 450, limit=200)
        assert [len(chunk) for chunk in chunks] == [200, 200, 50]
