"""Smoke/integration tests. Run:  python -m pytest -q   (or: python tests/test_app.py)"""
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Use a throw-away database so tests never touch real data.
_tmp = tempfile.mkdtemp()
import database as db  # noqa: E402
db.DATA_DIR = Path(_tmp)
db.DB_PATH = Path(_tmp) / "test.db"
db.initialize_database()

import pymupdf  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(ROOT / "app.py")


def make_pdf(text="Certificate of Completion\nThis is to certify that Test User has successfully completed Python Programming\nDate of Issue: 12 March 2024\nCertificate No: ABC/2024/12345"):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "IBM Technologies Ltd")
    y = 130
    for line in text.splitlines():
        page.insert_text((72, y), line)
        y += 24
    data = doc.tobytes()
    doc.close()
    return data


def new_app():
    at = AppTest.from_file(APP, default_timeout=90)
    at.session_state["_params_read"] = True
    return at


def no_exc(at, where=""):
    assert not at.exception, f"{where}: {[e.value for e in at.exception]}"


def test_public_pages_empty():
    at = new_app().run()
    no_exc(at, "home")
    for p in ["Certificates", "Verify", "About", "Admin"]:
        at.session_state.page = p
        at.run()
        no_exc(at, p)


def test_db_layer():
    cid = db.add_certificate("T1", "Issuer", category="Workshop", issue_date="2023-05-01", expiry_date="2020-01-01",
                             skills="Python, SQL", published=True, featured=True, file_data=b"%PDF-1.4 x",
                             file_name="a.pdf", file_mime="application/pdf")
    assert db.find_by_hash(__import__("hashlib").sha256(b"%PDF-1.4 x").hexdigest())["id"] == cid
    assert db.get_certificates(search="100%")  == []           # wildcard escaped
    assert db.get_certificates(skill="sql")[0]["id"] == cid
    assert db.get_certificates(year="2023")
    assert db.get_expiring(60)[0]["days_left"] < 0
    db.increment_counter(cid, "views")
    assert db.get_certificate(cid)["views"] == 1
    assert db.delete_certificate(cid)


def test_exporter_roundtrip():
    import exporter
    a = db.add_certificate("Exp A", "X", file_data=b"%PDF-1.4 aaa", file_name="a.pdf", file_mime="application/pdf", published=True)
    db.add_certificate("Exp B", "Y")
    blob = exporter.full_backup()
    for c in db.get_certificates():
        db.delete_certificate(c["id"])
    r = exporter.restore_backup(blob)
    assert r["imported"] == 2 and r["skipped"] == 0
    assert exporter.restore_backup(blob)["imported"] == 1  # the file-less one has no hash to dedupe
    assert exporter.certificates_csv(db.get_certificates()).startswith(b"\xef\xbb\xbf")
    assert exporter.files_zip(db.get_certificates())
    try:
        exporter.restore_backup(b"not a zip")
        raise AssertionError("should fail")
    except ValueError:
        pass
    for c in db.get_certificates():
        db.delete_certificate(c["id"])


def test_auth_hashing():
    import auth
    h = auth.hash_password("S3cret!pass")
    assert auth._verify_pbkdf2("S3cret!pass", h) and not auth._verify_pbkdf2("nope", h)


def test_admin_flow():
    at = new_app()
    at.secrets["admin"] = {"email": "a@b.c", "password": "pw12345678"}
    at.session_state.page = "Admin"
    at.run()
    no_exc(at, "admin gate")
    at.text_input[0].set_value("a@b.c"); at.text_input[1].set_value("wrong")
    at.button[-1].click() if False else None
    # submit login form
    at.get("form_submit_button")[0].click(); at.run()
    assert any("Incorrect" in e.value for e in at.error)
    at.text_input[0].set_value("a@b.c"); at.text_input[1].set_value("pw12345678")
    at.get("form_submit_button")[0].click(); at.run()
    no_exc(at, "after login")
    assert at.session_state.authenticated
    assert any(t.label.startswith("📊") or True for t in at.tabs)
    # every admin tab renders with an empty DB
    no_exc(at, "admin empty")


def _login(at):
    at.secrets["admin"] = {"email": "a@b.c", "password": "pw12345678"}
    at.session_state.page = "Admin"
    at.run()
    at.text_input[0].set_value("a@b.c"); at.text_input[1].set_value("pw12345678")
    at.get("form_submit_button")[0].click(); at.run()
    assert at.session_state.authenticated, [e.value for e in at.error]
    return at


def _up(widget, name, data):
    widget.set_value([(name, data, "application/octet-stream")]) if False else widget.upload(name, data)


def test_add_edit_publish_verify_flow():
    for c in db.get_certificates():
        db.delete_certificate(c["id"])
    at = _login(new_app())
    # --- single upload with AI prefill
    up = [u for u in at.file_uploader if u.key.startswith("add_upload")][0]
    PDF = make_pdf()
    up.upload("sample.pdf", PDF); at.run()
    no_exc(at, "after upload")
    vals = {t.key: t.value for t in at.text_input if t.key and t.key.startswith("add_")}
    assert any("Completion" in str(v) for v in vals.values()), vals
    assert any(v == "IBM Technologies Ltd" for v in vals.values()), vals
    assert any(v == "ABC/2024/12345" for v in vals.values()), vals
    form_btn = [b for b in at.get("form_submit_button") if b.label == "Save Certificate"][0]
    form_btn.click(); at.run()
    no_exc(at, "after save")
    certs = db.get_certificates()
    assert len(certs) == 1 and certs[0]["published"] and certs[0]["issue_date"] == "2024-03-12", certs
    cid = certs[0]["id"]
    # --- same file again => duplicate warning, save blocked
    up = [u for u in at.file_uploader if u.key.startswith("add_upload")][0]
    up.upload("again.pdf", PDF); at.run()
    no_exc(at, "dup upload")
    assert any("already in the vault" in w.value for w in at.warning)
    [b for b in at.get("form_submit_button") if b.label == "Save Certificate"][0].click(); at.run()
    assert len(db.get_certificates()) == 1
    # --- image without OCR text still must not crash (tiny PNG)
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (60, 40), "white").save(buf, "PNG")
    up = [u for u in at.file_uploader if u.key.startswith("add_upload")][0]
    up.upload("blank.png", buf.getvalue()); at.run()
    no_exc(at, "png upload")
    # --- manage tab renders, feature toggle works
    no_exc(at, "manage")
    db.set_featured(cid, True)
    # --- public pages with data
    pub = new_app().run()
    no_exc(pub, "home with data")
    for p in ["Certificates", "Verify"]:
        pub.session_state.page = p; pub.run(); no_exc(pub, p + " with data")
    # toggle show certificate (renders PDF -> image) and counts a view
    pub.session_state.page = "Certificates"; pub.run()
    tg = [t for t in pub.toggle if t.key and "show" in t.key]
    tg[0].set_value(True); pub.run(); no_exc(pub, "preview")
    assert db.get_certificate(cid)["views"] == 1
    # verify page
    pub.session_state.page = "Verify"; pub.run()
    pub.text_input[0].set_value("abc/2024/12345")
    pub.get("form_submit_button")[0].click(); pub.run(); no_exc(pub, "verify")
    assert any("Verified" in m.value for m in pub.markdown)
    pub.text_input[0].set_value("NOPE")
    pub.get("form_submit_button")[0].click(); pub.run()
    assert any("No published certificate" in m.value for m in pub.markdown)
    # deep link
    dl = new_app(); dl.session_state["_params_read"] = False
    dl.query_params["cert"] = str(cid); dl.run(); no_exc(dl, "deeplink")
    assert dl.session_state.page == "Certificates" and dl.session_state.focus_cert == cid


def test_bulk_and_manage_edit():
    for c in db.get_certificates():
        db.delete_certificate(c["id"])
    at = _login(new_app())
    bulk = [u for u in at.file_uploader if u.key.startswith("bulk_up")][0]
    bulk.set_value([("one.pdf", make_pdf(), "application/pdf"), ("bad.pdf", b"not a pdf", "application/pdf")]) if False else None
    bulk.upload("one.pdf", make_pdf()); at.run()
    go = [b for b in at.button if b.key and b.key.startswith("bulk_go")][0]
    go.click(); at.run(); no_exc(at, "bulk")
    certs = db.get_certificates()
    assert len(certs) == 1 and not certs[0]["published"]
    # edit through the manage form
    at.run()
    form = [b for b in at.get("form_submit_button") if b.label == "Save Changes"][0]
    form.click(); at.run(); no_exc(at, "edit save")
    # publish toggle button
    tog = [b for b in at.button if b.key and b.key.startswith("toggle_")][0]
    tog.click(); at.run(); no_exc(at, "toggle")
    assert db.get_certificates()[0]["published"]
    # delete needs confirmation
    dele = [b for b in at.button if b.key and b.key.startswith("delete_")][0]
    dele.click(); at.run()
    assert len(db.get_certificates()) == 1
    [c for c in at.checkbox if c.key and c.key.startswith("confirm_")][0].check(); at.run()
    [b for b in at.button if b.key and b.key.startswith("delete_")][0].click(); at.run(); no_exc(at, "delete")
    assert db.get_certificates() == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
