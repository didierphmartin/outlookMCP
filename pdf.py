"""An email as one PDF: header block, then the body (HTML rendered, or plain text as a fallback)."""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF

from extract import body_text

FONTS = Path(__file__).parent / "fonts"
FAMILY = "DejaVu"
CID_IMG = re.compile(r"<img\b[^>]*>", re.I)
SRC = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def _addresses(recipients) -> str:
    return ", ".join(a for r in recipients or [] if (a := (r.get("emailAddress") or {}).get("address")))


def _when(iso: str | None) -> str:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso or ""


def _attachment_line(a: dict) -> str:
    return f"{a.get('name', '')} ({round(int(a.get('size') or 0) / 1024)} KB)"


def _with_inline_images(html: str, inline_images: dict[str, bytes]) -> str:
    """cid: sources become data URIs; an <img> whose source cannot be resolved is dropped."""
    def swap(m: re.Match) -> str:
        src = SRC.search(m.group(0))
        value = (src.group(1) or src.group(2) or "").strip() if src else ""
        if value.lower().startswith("cid:"):
            data = inline_images.get(value[4:])
            if data is None:
                return ""
            return f'<img src="data:image/png;base64,{base64.b64encode(data).decode()}">'
        return m.group(0) if value.lower().startswith("data:") else ""    # remote images are never fetched
    return CID_IMG.sub(swap, html)


def _page(mail: dict) -> FPDF:
    pdf = FPDF(format="A4")
    pdf.set_margins(15, 15, 15)
    pdf.add_font(FAMILY, "", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font(FAMILY, "B", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.add_page()
    attachments = [_attachment_line(a) for a in mail.get("attachments") or [] if not a.get("isInline")]
    rows = [("From", _addresses([mail.get("from")] if mail.get("from") else [])),
            ("To", _addresses(mail.get("toRecipients"))), ("Cc", _addresses(mail.get("ccRecipients"))),
            ("Date", _when(mail.get("receivedDateTime"))), ("Subject", mail.get("subject") or ""),
            ("Attachments", "; ".join(attachments))]
    for label, value in rows:
        if not value and label in ("Cc", "Attachments"):
            continue
        pdf.set_font(FAMILY, "B", 10)
        pdf.cell(28, 6, label + ":")
        pdf.set_font(FAMILY, "", 10)
        pdf.multi_cell(0, 6, value, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(4)
    pdf.set_font(FAMILY, "", 10)
    return pdf


def render_mail(mail: dict, inline_images: dict[str, bytes]) -> bytes:
    body = mail.get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "text").lower() == "html" and content.strip():
        pdf = _page(mail)
        try:
            pdf.write_html(_with_inline_images(content, inline_images), font_family=FAMILY)
            return bytes(pdf.output())
        except Exception:  # noqa: BLE001 -- any HTML the renderer cannot take falls back to its plain text
            pass
    pdf = _page(mail)
    text = body_text(body)
    if text:
        pdf.multi_cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
