"""Cloud backup / restore tests using an in-memory fake of the GitHub contents API."""
import base64
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp()
import database as db  # noqa: E402
db.DATA_DIR = Path(_tmp)
db.DB_PATH = Path(_tmp) / "cloud.db"
db.initialize_database()

import cloud_backup as cb  # noqa: E402
import exporter  # noqa: E402

cb.LOCAL_DIR = Path(_tmp) / "backups"
STORE = {}  # repo path -> bytes


def fake_http(method, url, token, body=None, accept=""):
    assert token == "tok"
    p = urlparse(url)
    path = unquote(p.path)
    base = "/repos/me/vault-backup"
    if path == base:
        return 200, json.dumps({"private": True, "permissions": {"push": True}}).encode()
    assert path.startswith(base + "/contents/")
    rel = path[len(base + "/contents/"):]
    if method == "GET":
        if rel in STORE:
            if "raw" in accept:
                return 200, STORE[rel]
            return 200, json.dumps({"sha": hashlib.sha1(STORE[rel]).hexdigest()}).encode()
        listing = [{"name": k.split("/")[-1], "type": "file", "size": len(v), "sha": "x"}
                   for k, v in STORE.items() if k.startswith(rel + "/")]
        return (200, json.dumps(listing).encode()) if listing else (404, b"{}")
    if method == "PUT":
        STORE[rel] = base64.b64decode(body["content"])
        return 201, b"{}"
    if method == "DELETE":
        STORE.pop(rel, None)
        return 200, b"{}"
    raise AssertionError(method)



def with_cloud(fn):
    """Run a test with the fake GitHub configured, then restore the real environment."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **k):
        old_http, old_env = cb._http, {k_: os.environ.get(k_) for k_ in ("GITHUB_BACKUP_TOKEN", "GITHUB_BACKUP_REPO")}
        cb._http = fake_http
        os.environ.update(GITHUB_BACKUP_TOKEN="tok", GITHUB_BACKUP_REPO="me/vault-backup")
        try:
            return fn(*a, **k)
        finally:
            cb._http = old_http
            for k_, v in old_env.items():
                os.environ.pop(k_, None) if v is None else os.environ.__setitem__(k_, v)
    return wrapper


def _wipe():
    for c in db.get_certificates():
        db.delete_certificate(c["id"])


@with_cloud
def test_config_and_connection():
    assert cb.is_configured()
    ok, msg = cb.test_connection()
    assert ok and "Connected" in msg


@with_cloud
def test_backup_then_auto_restore_keeps_ids():
    _wipe(); STORE.clear()
    a = db.add_certificate("Cert A", "Issuer", published=True, featured=True, file_data=b"%PDF-1.4 AAA",
                           file_name="a.pdf", file_mime="application/pdf", skills="Python")
    b = db.add_certificate("Cert B", "Issuer2", file_data=b"%PDF-1.4 BBB", file_name="b.pdf", file_mime="application/pdf")
    db.increment_counter(a, "views")
    assert db.get_meta("dirty") == "1"
    kind, msg = cb.run_pending_backup()
    assert kind == "success", msg
    assert db.get_meta("dirty") == "0"
    assert "vault-backups/latest.zip" in STORE
    assert any(k.startswith("vault-backups/daily_") for k in STORE)
    # nothing changed -> no second upload
    assert cb.run_pending_backup() is None
    # simulate Streamlit Cloud wiping the disk
    db.wipe_certificates()
    assert db.count_certificates() == 0
    msg = cb.restore_latest_if_empty()
    assert msg and "2 certificate" in msg, msg
    ids = sorted(c["id"] for c in db.get_certificates())
    assert ids == sorted([a, b])                       # ids preserved => share links still work
    assert db.get_certificate_blob(a)[0] == b"%PDF-1.4 AAA"
    assert db.get_certificate(a)["featured"] == 1 and db.get_certificate(a)["views"] == 1
    # non-empty vault is never overwritten by auto-restore
    assert cb.restore_latest_if_empty() is None


@with_cloud
def test_empty_vault_never_overwrites_cloud_backup():
    before = STORE["vault-backups/latest.zip"]
    _wipe()
    db.log_action("delete", "all")
    assert cb.run_pending_backup() is None
    assert STORE["vault-backups/latest.zip"] == before


@with_cloud
def test_listing_download_prune():
    STORE.clear(); _wipe()
    db.add_certificate("X", "Y", file_data=b"%PDF-1.4 x", file_name="x.pdf", file_mime="application/pdf")
    for i in range(20):
        STORE[f"vault-backups/daily_202601{i:02d}.zip"] = b"zip"
    cb.backup_now()
    names = [b["name"] for b in cb.list_backups()]
    assert names[0] == "latest.zip"
    assert len([n for n in names if n.startswith("daily_")]) <= cb.KEEP_DAILY
    assert cb.download("latest.zip")[:2] == b"PK"


@with_cloud
def test_failure_is_reported_not_raised():
    db.add_certificate("Z", "Y")
    orig = cb._http
    cb._http = lambda *a, **k: (401, b'{"message":"Bad credentials"}')
    try:
        kind, msg = cb.run_pending_backup()
        assert kind == "warning" and "token" in msg
        assert db.get_meta("dirty") == "1"      # stays dirty => retried later
    finally:
        cb._http = orig


def test_inspect_and_corruption_detection():
    _wipe()
    db.add_certificate("C1", "I", file_data=b"%PDF-1.4 one", file_name="1.pdf", file_mime="application/pdf")
    blob = exporter.full_backup()
    info = exporter.inspect_backup(blob)
    assert info["count"] == 1 and info["files_ok"] == 1 and info["files_corrupt"] == 0
    # tamper with the stored file
    import io, zipfile
    src = zipfile.ZipFile(io.BytesIO(blob)); out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for n in src.namelist():
            z.writestr(n, b"%PDF-TAMPERED" if n.startswith("files/") else src.read(n))
    bad = out.getvalue()
    assert exporter.inspect_backup(bad)["files_corrupt"] == 1
    r = exporter.restore_backup(bad, mode="merge")
    assert r["corrupt"] == 1 and r["imported"] == 0


def test_restore_modes():
    _wipe()
    keep = db.add_certificate("Keep", "I", file_data=b"%PDF-1.4 k", file_name="k.pdf", file_mime="application/pdf")
    blob = exporter.full_backup()
    r = exporter.restore_backup(blob, mode="merge")
    assert r["skipped"] == 1 and r["imported"] == 0 and db.count_certificates() == 1
    db.add_certificate("Extra", "I")
    r = exporter.restore_backup(blob, mode="replace")
    assert r["imported"] == 1 and [c["title"] for c in db.get_certificates()] == ["Keep"]
    assert db.get_certificates()[0]["id"] == keep


@with_cloud
def test_admin_ui_backup_and_startup_restore():
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    app = str(ROOT / "app.py")

    def fresh(page="Home"):
        st.cache_resource.clear()
        at = AppTest.from_file(app, default_timeout=90)
        at.session_state["_params_read"] = True
        at.session_state.page = page
        return at

    def login(at):
        at.secrets["admin"] = {"email": "a@b.c", "password": "pw12345678"}
        at.run()
        at.text_input[0].set_value("a@b.c"); at.text_input[1].set_value("pw12345678")
        at.get("form_submit_button")[0].click(); at.run()
        assert at.session_state.authenticated
        return at

    _wipe(); STORE.clear()
    cid = db.add_certificate("UI Cert", "Org", published=True, file_data=b"%PDF-1.4 ui",
                             file_name="u.pdf", file_mime="application/pdf")
    at = login(fresh("Admin"))
    at.run()                                           # admin load => pending backup runs
    assert not at.exception, [e.value for e in at.exception]
    assert "vault-backups/latest.zip" in STORE and db.get_meta("dirty") == "0"

    # server restart with a wiped disk => automatic restore on first page load
    db.wipe_certificates()
    home = fresh("Home").run()
    assert not home.exception, [e.value for e in home.exception]
    assert db.count_certificates() == 1 and db.get_certificate(cid)["title"] == "UI Cert"

    # Backup & Restore tab buttons
    at = login(fresh("Admin"))
    [b for b in at.button if b.key == "cloud_test"][0].click(); at.run()
    assert not at.exception and any("Connected" in s.value for s in at.success)
    [b for b in at.button if b.key == "cloud_now"][0].click(); at.run()
    assert not at.exception and any("Backed up" in s.value for s in at.success)
    [b for b in at.button if b.key == "cloud_list_btn"][0].click(); at.run()
    assert not at.exception
    [r for r in at.radio if r.key == "cloud_mode"][0].set_value("Replace — exact copy of that backup"); at.run()
    [c for c in at.checkbox if c.key == "cloud_ok"][0].check(); at.run()
    [b for b in at.button if b.key == "cloud_restore"][0].click(); at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert db.count_certificates() == 1 and db.get_certificate(cid) is not None

    # restore from an uploaded file (replace)
    blob = exporter.full_backup()
    db.add_certificate("Temp", "X")
    up = [u for u in at.file_uploader if u.key == "restore_up"][0]
    up.upload("backup.zip", blob); at.run()
    assert not at.exception, [e.value for e in at.exception]
    [r for r in at.radio if r.key == "file_mode"][0].set_value("Replace — make the vault an exact copy of the backup"); at.run()
    [c for c in at.checkbox if c.key == "file_ok"][0].check(); at.run()
    [b for b in at.button if b.key == "restore_go"][0].click(); at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert [c["title"] for c in db.get_certificates()] == ["UI Cert"]
    _wipe()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("PASS", name)
