"""Document adapters -- the Xtract ingest lane.

Six formats a traveller's itinerary actually arrives in. Each flattens to one
searchable text body plus a landmark map, so a character offset found later
can be reported as "Subject line" or "page 2" or "row 4, column Departure"
instead of "offset 812".
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
from email import policy
from email.parser import BytesParser

from ..schema import Document
from .base import register_document


@register_document("text")
class TextSource:
    extensions = (".txt", ".md", ".log")

    def parse(self, blob: bytes, source_id: str, retrieved_at: int) -> Document:
        text = blob.decode("utf-8", errors="replace")
        # Landmark every blank-line-separated block so offsets resolve to
        # something a person can scan for.
        landmarks, pos, block = [], 0, 1
        for para in text.split("\n\n"):
            landmarks.append((pos, f"paragraph {block}"))
            pos += len(para) + 2
            block += 1
        return Document(
            source_id=source_id,
            source_type="text",
            title=source_id,
            text=text,
            retrieved_at=retrieved_at,
            locator_map=landmarks,
        )


@register_document("email")
class EmailSource:
    """RFC822. Headers are landmarks; we walk to the text/plain part, or
    strip tags out of text/html if that is all the airline sent."""

    extensions = (".eml", ".email")

    def parse(self, blob: bytes, source_id: str, retrieved_at: int) -> Document:
        msg = BytesParser(policy=policy.default).parsebytes(blob)
        chunks, landmarks = [], []
        pos = 0
        for header in ("From", "To", "Date", "Subject"):
            value = msg.get(header)
            if not value:
                continue
            line = f"{header}: {value}\n"
            landmarks.append((pos, f"{header} header"))
            chunks.append(line)
            pos += len(line)

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    body = part.get_content()
                    break
            if not body:
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        body = _strip_html(part.get_content())
                        break
        else:
            raw = msg.get_content()
            body = raw if msg.get_content_type() == "text/plain" else _strip_html(raw)

        landmarks.append((pos, "message body"))
        chunks.append("\n" + body)
        return Document(
            source_id=source_id,
            source_type="email",
            title=msg.get("Subject", source_id) or source_id,
            text="".join(chunks),
            retrieved_at=retrieved_at,
            locator_map=landmarks,
        )


@register_document("ics")
class IcsSource:
    """iCalendar. Unfolds continuation lines (RFC 5545 folds at 75 octets,
    which otherwise splits a location mid-word and defeats extraction)."""

    extensions = (".ics", ".ical")

    def parse(self, blob: bytes, source_id: str, retrieved_at: int) -> Document:
        raw = blob.decode("utf-8", errors="replace").replace("\r\n", "\n")
        unfolded = re.sub(r"\n[ \t]", "", raw)
        landmarks, pos, n = [], 0, 1
        lines = []
        for line in unfolded.split("\n"):
            if line.startswith("BEGIN:VEVENT"):
                landmarks.append((pos, f"event {n}"))
                n += 1
            lines.append(line)
            pos += len(line) + 1
        title = ""
        m = re.search(r"^SUMMARY:(.*)$", unfolded, re.M)
        if m:
            title = m.group(1).strip()
        return Document(
            source_id=source_id,
            source_type="ics",
            title=title or source_id,
            text="\n".join(lines),
            retrieved_at=retrieved_at,
            locator_map=landmarks or [(0, "calendar")],
        )


@register_document("pdf")
class PdfSource:
    """PDF via pdftotext when present, with a raw-stream fallback.

    Page breaks become landmarks so a finding cites "page 2" -- the thing a
    judge can verify by looking at the document.
    """

    extensions = (".pdf",)

    def parse(self, blob: bytes, source_id: str, retrieved_at: int) -> Document:
        text = ""
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", "-", "-"],
                input=blob,
                capture_output=True,
                timeout=25,
            )
            if proc.returncode == 0:
                text = proc.stdout.decode("utf-8", errors="replace")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            text = ""
        if not text.strip():
            # No pdftotext on this machine: pull any literal strings out of
            # the content streams. Crude, but beats returning nothing.
            text = "\n".join(
                s.decode("latin-1", errors="replace")
                for s in re.findall(rb"\(([^)\\]{3,})\)", blob)
            )

        landmarks, pos = [], 0
        for i, page in enumerate(text.split("\f"), start=1):
            landmarks.append((pos, f"page {i}"))
            pos += len(page) + 1
        return Document(
            source_id=source_id,
            source_type="pdf",
            title=source_id,
            text=text.replace("\f", "\n"),
            retrieved_at=retrieved_at,
            locator_map=landmarks,
        )


@register_document("csv")
class CsvSource:
    """CSV/TSV. Landmarks are rows, so provenance reads "row 4"."""

    extensions = (".csv", ".tsv")

    def parse(self, blob: bytes, source_id: str, retrieved_at: int) -> Document:
        raw = blob.decode("utf-8", errors="replace")
        delim = "\t" if source_id.lower().endswith(".tsv") else ","
        try:
            rows = list(csv.reader(io.StringIO(raw), delimiter=delim))
        except csv.Error:
            rows = [line.split(delim) for line in raw.splitlines()]
        header = rows[0] if rows else []
        landmarks, out, pos = [], [], 0
        for i, row in enumerate(rows):
            if i == 0:
                landmarks.append((pos, "header row"))
                line = " | ".join(row)
            else:
                landmarks.append((pos, f"row {i}"))
                pairs = [
                    f"{header[j] if j < len(header) else f'col{j}'}: {cell}"
                    for j, cell in enumerate(row)
                ]
                line = "; ".join(pairs)
            out.append(line)
            pos += len(line) + 1
        return Document(
            source_id=source_id,
            source_type="csv",
            title=source_id,
            text="\n".join(out),
            retrieved_at=retrieved_at,
            locator_map=landmarks,
        )


@register_document("html")
class HtmlSource:
    extensions = (".html", ".htm", ".xhtml")

    def parse(self, blob: bytes, source_id: str, retrieved_at: int) -> Document:
        raw = blob.decode("utf-8", errors="replace")
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
        if m:
            title = _collapse(m.group(1))
        text = _strip_html(raw)
        landmarks, pos, n = [], 0, 1
        for block in text.split("\n\n"):
            landmarks.append((pos, f"section {n}"))
            pos += len(block) + 2
            n += 1
        return Document(
            source_id=source_id,
            source_type="html",
            title=title or source_id,
            text=text,
            retrieved_at=retrieved_at,
            locator_map=landmarks,
        )


# --------------------------------------------------------------------------


def _strip_html(raw: str) -> str:
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", raw, flags=re.I)
    raw = re.sub(r"</t[dh]>", " | ", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", "", raw)
    for ent, ch in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
    ):
        raw = raw.replace(ent, ch)
    raw = re.sub(r"[ \t]+", " ", raw)
    return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()
