import base64
import io
import struct
import zlib

from pypdf import PdfReader

from pdf import render_mail


def text_of(data: bytes) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)


def png() -> bytes:
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 4 for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def mail(**kw) -> dict:
    base = {"from": {"emailAddress": {"address": "alice@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "me@example.com"}}], "ccRecipients": [],
            "receivedDateTime": "2026-10-07T10:00:00Z", "subject": "Quarterly report",
            "body": {"contentType": "html", "content": "<p>Hello <b>world</b></p>"}, "attachments": []}
    return {**base, **kw}


def test_header_and_html_body_text_present():
    atts = [{"name": "report.xlsx", "size": 2048, "isInline": False}]
    t = text_of(render_mail(mail(attachments=atts), {}))
    for needle in ("Quarterly report", "alice@example.com", "world", "report.xlsx", "2026-10-07 10:00 UTC"):
        assert needle in t


def test_unicode_renders():
    t = text_of(render_mail(mail(body={"contentType": "html", "content": "<p>Café – 50 € — Ærø</p>"}), {}))
    assert "Café – 50 € — Ærø" in t


def test_inline_cid_image_embedded():
    atts = [{"name": "logo.png", "size": 99, "isInline": True, "contentId": "logo1"}]
    body = {"contentType": "html", "content": '<p>see</p><img src="cid:logo1">'}
    data = render_mail(mail(attachments=atts, body=body), {"logo1": png()})
    page = PdfReader(io.BytesIO(data)).pages[0]
    assert len(page.images) == 1
    assert "logo.png" not in text_of(data)


def test_unresolved_cid_image_dropped():
    body = {"contentType": "html", "content": '<p>x</p><img src="cid:nope">'}
    data = render_mail(mail(body=body), {})
    assert len(PdfReader(io.BytesIO(data)).pages[0].images) == 0


def test_broken_html_falls_back_to_text():
    body = {"contentType": "html", "content": "<table><tr><td>unclosed <b>x"}
    assert "x" in text_of(render_mail(mail(body=body), {}))


def test_empty_body_still_renders():
    data = render_mail(mail(body=None), {})
    r = PdfReader(io.BytesIO(data))
    assert len(r.pages) == 1 and "Quarterly report" in text_of(data)


def test_tool_returns_base64_pdf(svc, fake):
    mid = fake.add_message(subject="Hi", attachments=[("logo.png", "image/png", png(), True, False, "c1"),
                                                      ("a.txt", "text/plain", b"abc")],
                           body_html='<p>x</p><img src="cid:c1">')
    out = svc.mail_to_pdf("key-1", {"mail_id": mid})
    assert out["name"] == "Email.pdf"
    raw = base64.b64decode(out["base64"])
    assert raw.startswith(b"%PDF") and out["size"] == len(raw)
    t = text_of(raw)
    assert "a.txt" in t and "logo.png" not in t


def test_tool_unknown_mail_is_not_found(svc):
    assert svc.mail_to_pdf("key-1", {"mail_id": "nope"})["error"] == "not_found"


def test_plain_text_body_renders():
    body = {"contentType": "text", "content": "Line one\nLine two <not a tag>"}
    t = text_of(render_mail(mail(body=body), {}))
    assert "Line one" in t and "Line two <not a tag>" in t and "Quarterly report" in t


def test_tool_lists_cloud_link_attachments_as_not_stored(svc, fake):
    mid = fake.add_message(subject="Hi", attachments=[("Budget.xlsx", "", b"", False, False, None, True),
                                                      ("a.txt", "text/plain", b"abc")])
    t = text_of(base64.b64decode(svc.mail_to_pdf("key-1", {"mail_id": mid})["base64"]))
    assert "Budget.xlsx (cloud link, not stored)" in t and "a.txt" in t


def test_tool_embeds_inline_image_with_graph_valid_requests(svc, fake):
    """Real Graph rejects contentId in a base-type $select (400): contentId and bytes come from the attachment GET."""
    mid = fake.add_message(subject="Hi", attachments=[("logo.png", "image/png", png(), True, False, "c1"),
                                                      ("a.txt", "text/plain", b"abc")],
                           body_html='<p>x</p><img src="cid:c1">')
    out = svc.mail_to_pdf("key-1", {"mail_id": mid})
    assert "error" not in out, out
    assert len(PdfReader(io.BytesIO(base64.b64decode(out["base64"]))).pages[0].images) == 1
    assert not any(f"{mid}-a1" in p for _, p in fake.requests)      # a non-inline attachment is never fetched


def test_tool_skips_oversized_inline_image(svc, fake):
    mid = fake.add_message(subject="Hi", attachments=[("logo.png", "image/png", png(), True, False, "c1")],
                           body_html='<img src="cid:c1">')
    object.__setattr__(svc.cfg, "attachment_max_bytes", 10)      # AppConfig is frozen
    out = svc.mail_to_pdf("key-1", {"mail_id": mid})
    assert len(PdfReader(io.BytesIO(base64.b64decode(out["base64"]))).pages[0].images) == 0


def test_fake_rejects_subtype_property_in_select(fake):
    mid = fake.add_message(subject="Hi")
    for q in ({"$expand": ["attachments($select=id,contentId)"]}, {"$select": ["id,contentBytes"]}):
        resp = fake._graph(f"/me/messages/{mid}", q)
        assert resp.status_code == 400 and "on type 'microsoft.graph.attachment'" in resp.json()["error"]["message"]


OUTLOOK_HTML = ('<html><head>\r\n<meta http-equiv="Content-Type" content="text/html; charset=utf-8">\r\n'
                '<style type="text/css" style="display:none;"><!-- p {margin-top:0;margin-bottom:0;} --></style>\r\n'
                '</head>\r\n<body dir="ltr">\r\n<div>Test 90</div>\r\n</body></html>')


def test_outlook_head_style_and_comments_are_not_rendered_as_text():
    t = text_of(render_mail(mail(body={"contentType": "html", "content": OUTLOOK_HTML}), {}))
    assert "Test 90" in t and "margin" not in t and "<!--" not in t


def test_style_script_and_comments_dropped_case_insensitively():
    html = ('<STYLE>\np { margin:0 }\n</STYLE><Script type="x">var margin = 1;</sCRIPT>'
            '<!--\n margin note\n--><p>kept</p>')
    t = text_of(render_mail(mail(body={"contentType": "html", "content": html}), {}))
    assert "kept" in t and "margin" not in t


def test_text_fallback_also_drops_head_style_and_comments(monkeypatch):
    from fpdf import FPDF

    def refuse(self, *a, **k):
        raise ValueError("renderer refused")
    monkeypatch.setattr(FPDF, "write_html", refuse)          # force the plain-text path
    t = text_of(render_mail(mail(body={"contentType": "html", "content": OUTLOOK_HTML}), {}))
    assert "Test 90" in t and "margin" not in t
