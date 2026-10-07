"""Text out of mail bodies and (in get_attachment only) attachments."""
from __future__ import annotations

import io
import re
from html.parser import HTMLParser
from pathlib import PurePath

TEXT_SUFFIXES = {".txt", ".csv", ".md", ".json", ".xml", ".log"}


class _Text(HTMLParser):
    SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        match tag:
            case _ if tag in self.SKIP:
                self._skip += 1
            case "body":
                self._skip = 0                  # an unclosed <head>/<style> must not swallow the body
            case "br" | "p" | "div" | "li" | "tr":
                self.parts.append("\n")
            case "td" | "th":
                self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html or "")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(p.parts).split("\n"))
    return "\n".join(line for line in lines if line)


def body_text(body: dict | None) -> str:
    body = body or {}
    match (body.get("contentType") or "text").lower():
        case "html":
            return html_to_text(body.get("content", ""))
        case _:
            return (body.get("content") or "").strip()


def attachment_text(name: str, content_type: str, data: bytes) -> str | None:
    """Text of an attachment, or None when it is not a text-convertible type."""
    suffix, ctype = PurePath(name or "").suffix.lower(), (content_type or "").lower()
    match suffix:
        case ".pdf":
            from pypdf import PdfReader
            return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages).strip()
        case ".docx":
            import docx2txt
            return (docx2txt.process(io.BytesIO(data)) or "").strip()
        case ".html" | ".htm":
            return html_to_text(data.decode("utf-8", "replace"))
        case s if s in TEXT_SUFFIXES or ctype.startswith("text/"):
            return data.decode("utf-8", "replace")
        case _:
            return None
