import base64
import io


def test_whoami(svc):
    w = svc.whoami("key-1")
    assert w["account"] == "me@contoso.com" and w["tenant"] == "tid-1"
    assert svc.whoami("nope")["error"] == "reconnect_needed"


def test_list_folders_with_paths(svc):
    assert svc.list_folders("key-1") == [{"id": "inbox-id", "name": "Inbox", "path": "Inbox"},
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
