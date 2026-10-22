import base64
import io


def test_whoami(svc):
    w = svc.whoami("key-1")
    assert w["account"] == "me@contoso.com" and w["tenant"] == "tid-1"
    assert svc.whoami("nope")["error"] == "reconnect_needed"


def test_list_folders_with_paths(svc):
    assert svc.list_folders("key-1")["folders"] == [{"id": "inbox-id", "name": "Inbox", "path": "Inbox"},
                                                    {"id": "invoices-id", "name": "Invoices", "path": "Inbox/Invoices"},
                                                    {"id": "projects-id", "name": "Projects", "path": "Projects"}]


def test_list_folders_errors_are_returned(svc):
    assert svc.list_folders("nope")["error"] == "reconnect_needed"


def test_poll_by_folder_path(fake, svc):
    svc.poll_new_mail("key-1", {"folder": "Inbox/Invoices"})
    fake.add_message(folder="invoices-id", subject="inv", received="2026-10-07T10:01:00Z")
    assert svc.poll_new_mail("key-1", {"folder": "Inbox/Invoices"})["mail"]["subject"] == "inv"


def test_get_attachment_text_and_base64(fake, svc):
    mid = fake.add_message(attachments=[("note.txt", "text/plain", "héllo".encode()),
                                        ("pic.png", "image/png", b"\x89PNG..")])
    t = svc.get_attachment("key-1", {"mail_id": mid, "attachment_id": f"{mid}-a0"})
    assert t == {"name": "note.txt", "content_type": "text/plain", "size": 6, "text": "héllo"}
    b = svc.get_attachment("key-1", {"mail_id": mid, "attachment_id": f"{mid}-a1"})
    assert base64.b64decode(b["base64"]) == b"\x89PNG.." and "text" not in b


def test_get_attachment_docx(fake, svc):
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   '<w:body><w:p><w:r><w:t>Invoice total 42</w:t></w:r></w:p></w:body></w:document>')
    mid = fake.add_message(attachments=[("inv.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", buf.getvalue())])
    assert "Invoice total 42" in svc.get_attachment("key-1", {"mail_id": mid, "attachment_id": f"{mid}-a0"})["text"]


def test_get_attachment_size_cap(fake, svc):
    from dataclasses import replace
    svc.cfg = replace(svc.cfg, attachment_max_bytes=3)
    mid = fake.add_message(attachments=[("big.txt", "text/plain", b"12345")])
    r = svc.get_attachment("key-1", {"mail_id": mid, "attachment_id": f"{mid}-a0"})
    assert r["error"] == "too_large"
    assert not [p for m, p in fake.requests if p.endswith("/$value")], "over the cap: content never downloaded"


def test_get_attachment_unknown(svc):
    assert svc.get_attachment("key-1", {"mail_id": "zz", "attachment_id": "zz-a0"})["error"] == "not_found"


def test_get_attachment_corrupt_files_are_reported(fake, svc):
    mid = fake.add_message(attachments=[("bad.pdf", "application/pdf", b"not a pdf"),
                                        ("bad.docx", "application/octet-stream", b"not a zip")])
    for i in (0, 1):
        r = svc.get_attachment("key-1", {"mail_id": mid, "attachment_id": f"{mid}-a{i}"})
        assert r["error"] == "unreadable" and "text" not in r


def test_get_attachment_graph_failures(fake, svc):
    mid = fake.add_message(attachments=[("a.txt", "text/plain", b"x")])
    args = {"mail_id": mid, "attachment_id": f"{mid}-a0"}
    assert svc.get_attachment("nope", args)["error"] == "reconnect_needed"
    fake.throttle_next = 2
    svc._graphs.clear()
    assert svc.get_attachment("key-1", args)["error"] == "throttled"


def test_get_attachment_size_cap_applies_to_downloaded_bytes(fake, svc):
    from dataclasses import replace
    svc.cfg = replace(svc.cfg, attachment_max_bytes=3)
    mid = fake.add_message(attachments=[("big.txt", "text/plain", b"12345")])
    fake.messages[mid]["attachments"][0]["size"] = 0          # metadata says 0 (or nothing)
    r = svc.get_attachment("key-1", {"mail_id": mid, "attachment_id": f"{mid}-a0"})
    assert r["error"] == "too_large" and "text" not in r and "base64" not in r


def test_whoami_asks_microsoft_so_a_refused_sign_in_is_reconnect_needed(fake, svc):
    fake.revoke_refresh()                                   # e.g. AADSTS530035: the tenant now blocks the grant
    w = svc.whoami("key-1")
    assert w["error"] == "reconnect_needed" and "invalid_grant" in w["message"]


def test_raw_returns_base64_for_pdf(fake, svc):
    pdf = b"%PDF-1.4 raw bytes \x00\xff"
    mid = fake.add_message(attachments=[("a.pdf", "application/pdf", pdf)])
    args = {"mail_id": mid, "attachment_id": f"{mid}-a0"}
    r = svc.get_attachment("key-1", {**args, "raw": True})
    assert r == {"name": "a.pdf", "content_type": "application/pdf", "size": len(pdf),
                 "base64": base64.b64encode(pdf).decode()}
    assert "text" not in svc.get_attachment("key-1", {**args, "raw": True})
    txt = fake.add_message(attachments=[("n.txt", "text/plain", b"hello")])
    r = svc.get_attachment("key-1", {"mail_id": txt, "attachment_id": f"{txt}-a0", "raw": True})
    assert base64.b64decode(r["base64"]) == b"hello" and "text" not in r


def test_raw_too_large_is_error(fake, svc):
    from dataclasses import replace
    svc.cfg = replace(svc.cfg, attachment_max_bytes=3)
    mid = fake.add_message(attachments=[("big.bin", "application/octet-stream", b"12345")])
    r = svc.get_attachment("key-1", {"mail_id": mid, "attachment_id": f"{mid}-a0", "raw": True})
    assert r["error"] == "too_large"
    assert not [p for m, p in fake.requests if p.endswith("/$value")]


def test_raw_item_attachment_is_eml(fake, svc):
    mime = b"From: a@b.c\r\nSubject: Hi\r\n\r\nbody"
    mid = fake.add_message(attachments=[("Quarterly report", "", mime, False, True),
                                        ("fwd.eml", "", mime, False, True)])
    r = svc.get_attachment("key-1", {"mail_id": mid, "attachment_id": f"{mid}-a0", "raw": True})
    assert r == {"name": "Quarterly report.eml", "content_type": "message/rfc822", "size": len(mime),
                 "base64": base64.b64encode(mime).decode()}
    r = svc.get_attachment("key-1", {"mail_id": mid, "attachment_id": f"{mid}-a1", "raw": True})
    assert r["name"] == "fwd.eml"
    nameless = fake.add_message(attachments=[("", "", mime, False, True)])
    r = svc.get_attachment("key-1", {"mail_id": nameless, "attachment_id": f"{nameless}-a0", "raw": True})
    assert r["name"] == "Attached email.eml"


def test_attachment_list_has_is_inline(fake, svc):
    svc.poll_new_mail("key-1", {"folder": "Inbox"})
    fake.add_message(received="2026-10-07T10:01:00Z",
                     attachments=[("logo.png", "image/png", b"x", True), ("a.txt", "text/plain", b"y")])
    atts = svc.poll_new_mail("key-1", {"folder": "Inbox"})["mail"]["attachments"]
    assert [(a["name"], a["is_inline"]) for a in atts] == [("logo.png", True), ("a.txt", False)]


def test_attachment_list_has_kind(fake, svc):
    svc.poll_new_mail("key-1", {"folder": "Inbox"})
    fake.add_message(received="2026-10-07T10:01:00Z",
                     attachments=[("a.pdf", "application/pdf", b"x"), ("Fwd", "", b"y", False, True),
                                  ("Budget.xlsx", "", b"", False, False, None, True)])
    atts = svc.poll_new_mail("key-1", {"folder": "Inbox"})["mail"]["attachments"]
    assert [(a["name"], a["kind"]) for a in atts] == [("a.pdf", "file"), ("Fwd", "item"), ("Budget.xlsx", "reference")]


def test_raw_reference_attachment_is_cloud_attachment_error(fake, svc):
    mid = fake.add_message(attachments=[("Budget.xlsx", "", b"", False, False, None, True)])
    r = svc.get_attachment("key-1", {"mail_id": mid, "attachment_id": f"{mid}-a0", "raw": True})
    assert r["error"] == "cloud_attachment" and "Budget.xlsx" in r["message"]
    assert not [p for m, p in fake.requests if p.endswith("/$value")]
