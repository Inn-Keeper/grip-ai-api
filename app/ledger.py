"""Ledger import helpers: file to text, redaction before the model, chunking.

Redaction swaps details the model does not need (links, emails, phones,
salaries) for placeholders, and restore puts them back afterwards. People's
names in free text are not detected; the upload screen says so.
"""

import re
import zipfile
from io import BytesIO
from xml.etree import ElementTree

from app.errors import AppError

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Order matters: a URL can contain an email or digits, so it is taken first.
PATTERNS = [
    ("LINK", re.compile(r"(?:https?://|www\.)[^\s]+?(?=[.,;:!?)\]]*(?:\s|$))")),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    (
        "SALARY",
        re.compile(
            r"(?:R\$|US\$|[$€£])\s?\d[\d.,]*\s?[kKmM]?\b"
            r"|\b\d[\d.,]*\s?[kK]?\s?(?:SEK|USD|EUR|GBP|BRL|kr)\b"
        ),
    ),
    ("PHONE", re.compile(r"\+?\(?\d[\d\s().-]{5,}\d")),
]
PLACEHOLDER = re.compile(r"\[(?:LINK|EMAIL|SALARY|PHONE)_\d+\]")
DATE_SHAPE = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")
MIN_PHONE_DIGITS = 7
# A 1 MB upload can be a zip bomb; real ledgers unpack to well under this.
MAX_XML_BYTES = 10_000_000


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".md"):
        return data.decode("utf-8", errors="replace")
    if not name.endswith(".docx"):
        raise AppError(
            415, "unsupported_file", "Only .docx and .md files can be imported."
        )
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            if archive.getinfo("word/document.xml").file_size > MAX_XML_BYTES:
                raise AppError(413, "file_too_large", "This .docx file is too large.")
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise AppError(
            422, "unreadable_file", "This .docx file could not be read."
        ) from exc
    # One line per Word paragraph; list items are paragraphs too.
    lines = (
        "".join(t.text or "" for t in p.iter(f"{WORD_NS}t"))
        for p in root.iter(f"{WORD_NS}p")
    )
    return "\n".join(line for line in lines if line.strip())


def _is_phone(candidate: str) -> bool:
    digits = sum(ch.isdigit() for ch in candidate)
    return digits >= MIN_PHONE_DIGITS and not DATE_SHAPE.fullmatch(candidate.strip())


def redact(text: str) -> tuple[str, dict[str, str]]:
    """Text with sensitive values replaced, and the placeholder -> value map."""
    mapping: dict[str, str] = {}
    by_value: dict[str, str] = {}
    counts: dict[str, int] = {}

    for kind, pattern in PATTERNS:

        def swap(match: re.Match, kind: str = kind) -> str:
            value = match.group(0)
            if kind == "PHONE" and not _is_phone(value):
                return value
            if value not in by_value:
                counts[kind] = counts.get(kind, 0) + 1
                placeholder = f"[{kind}_{counts[kind]}]"
                by_value[value] = placeholder
                mapping[placeholder] = value
            return by_value[value]

        text = pattern.sub(swap, text)
    return text, mapping


def restore(value: str, mapping: dict[str, str]) -> str:
    """Put real values back; placeholders the model invented are dropped."""
    return PLACEHOLDER.sub(lambda match: mapping.get(match.group(0), ""), value)


def split_chunks(text: str, limit: int) -> list[str]:
    """Split on line boundaries into chunks of at most `limit` characters."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        pieces = [line[i : i + limit] for i in range(0, len(line), limit)] or [""]
        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks
