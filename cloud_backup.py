"""Automatic backup & restore to a (private) GitHub repository.

Streamlit Community Cloud wipes the local disk on restart. This module keeps the vault safe:
  * after every change the vault is uploaded to GitHub  (latest.zip + one daily snapshot, 14 kept)
  * on startup, if the vault is empty, it is restored automatically from latest.zip

Configure in Streamlit secrets (or environment variables GITHUB_BACKUP_TOKEN / _REPO / _BRANCH / _FOLDER):

    [github_backup]
    token  = "github_pat_xxx"        # fine-grained token, Contents: Read & write, on ONE private repo
    repo   = "your-username/certificate-vault-backup"
    branch = "main"                  # optional
    folder = "vault-backups"         # optional
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

import database as db
import exporter

API = "https://api.github.com"
KEEP_DAILY = 14
MAX_BYTES = 95 * 1024 * 1024  # GitHub contents API limit is 100 MB
LATEST = "latest.zip"
LOCAL_DIR = db.DATA_DIR / "backups"
LOCAL_KEEP = 5


class CloudError(Exception):
    """Friendly, user-displayable backup error."""


def normalize_repo(value):
    """Accept 'user/repo', 'https://github.com/user/repo', '...repo.git' or '.../tree/main' -> 'user/repo'."""
    v = str(value or "").strip()
    for prefix in ("https://", "http://", "git@"):
        if v.lower().startswith(prefix):
            v = v[len(prefix):]
    v = v.replace("github.com:", "github.com/")
    if v.lower().startswith("www."):
        v = v[4:]
    if v.lower().startswith("github.com/"):
        v = v[len("github.com/"):]
    parts = [p for p in v.split("/") if p]
    if len(parts) >= 2:
        v = f"{parts[0]}/{parts[1]}"
    if v.endswith(".git"):
        v = v[:-4]
    return v


def config():
    """Return the GitHub backup config dict, or None when not configured."""
    section = {}
    try:
        import streamlit as st
        section = dict(st.secrets.get("github_backup", {}))
    except Exception:
        pass
    token = str(section.get("token") or os.environ.get("GITHUB_BACKUP_TOKEN", "")).strip()
    repo = normalize_repo(section.get("repo") or os.environ.get("GITHUB_BACKUP_REPO", ""))
    if not token or repo.count("/") != 1:
        return None
    return {
        "token": token,
        "repo": repo,
        "branch": str(section.get("branch") or os.environ.get("GITHUB_BACKUP_BRANCH", "main")).strip(),
        "folder": str(section.get("folder") or os.environ.get("GITHUB_BACKUP_FOLDER", "vault-backups")).strip().strip("/"),
    }


def is_configured():
    return config() is not None


# --------------------------------------------------------------------------- HTTP layer
def _http(method, url, token, body=None, accept="application/vnd.github+json"):
    """Single place that talks to GitHub (monkeypatched in tests). Returns (status, bytes)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "certificate-vault",
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudError(f"Could not reach GitHub ({exc}). Check your internet connection.") from exc


def _call(method, path, body=None, accept="application/vnd.github+json", ok=(200, 201)):
    cfg = config()
    if not cfg:
        raise CloudError("GitHub backup is not configured.")
    status, payload = _http(method, f"{API}{path}", cfg["token"], body, accept)
    if status in ok:
        return status, payload
    if status == 404:
        return 404, payload
    messages = {
        401: "GitHub rejected the token (401). Create a new token and update the secret.",
        403: "GitHub denied access (403). The token needs 'Contents: Read and write' permission on the backup repo.",
        422: "GitHub could not process the request (422). Check the branch name.",
    }
    detail = ""
    try:
        detail = json.loads(payload).get("message", "")
    except Exception:
        pass
    raise CloudError(messages.get(status, f"GitHub error {status}. {detail}").strip())


def _contents_path(name):
    cfg = config()
    return f"/repos/{cfg['repo']}/contents/{urllib.parse.quote(cfg['folder'] + '/' + name)}"


# --------------------------------------------------------------------------- repo operations
def test_connection():
    """(ok, message) — verifies token, repo, write permission and privacy."""
    cfg = config()
    if not cfg:
        return False, "Not configured. Add the [github_backup] block to your Streamlit secrets."
    try:
        status, payload = _call("GET", f"/repos/{cfg['repo']}")
        if status == 404:
            return False, f"Repository '{cfg['repo']}' was not found, or the token cannot see it."
        info = json.loads(payload)
        if not info.get("permissions", {}).get("push", True):
            return False, "The token can read the repo but cannot write. Give it 'Contents: Read and write'."
        note = ""
        if not info.get("private", True):
            note = " ⚠️ This repository is PUBLIC — anyone could download your certificates. Make it private."
        return True, f"Connected to {cfg['repo']} (branch {cfg['branch']}).{note}"
    except CloudError as exc:
        return False, str(exc)


def _get_sha(name):
    cfg = config()
    status, payload = _call("GET", f"{_contents_path(name)}?ref={urllib.parse.quote(cfg['branch'])}")
    return None if status == 404 else json.loads(payload).get("sha")


def upload(name, data, message):
    cfg = config()
    if len(data) > MAX_BYTES:
        raise CloudError(f"Backup is {len(data)//1024//1024} MB — larger than GitHub's ~100 MB limit. "
                         "Delete unneeded certificates or use the manual download.")
    body = {"message": message, "branch": cfg["branch"], "content": base64.b64encode(data).decode()}
    sha = _get_sha(name)
    if sha:
        body["sha"] = sha
    _call("PUT", _contents_path(name), body)


def list_backups():
    """Backups in the repo folder, newest first: [{'name','size','sha'}]."""
    cfg = config()
    status, payload = _call("GET", f"/repos/{cfg['repo']}/contents/{urllib.parse.quote(cfg['folder'])}"
                                   f"?ref={urllib.parse.quote(cfg['branch'])}")
    if status == 404:
        return []
    items = [{"name": i["name"], "size": i.get("size", 0), "sha": i.get("sha")}
             for i in json.loads(payload) if i.get("type") == "file" and i["name"].endswith(".zip")]
    latest = [i for i in items if i["name"] == LATEST]
    rest = sorted((i for i in items if i["name"] != LATEST), key=lambda i: i["name"], reverse=True)
    return latest + rest


def download(name):
    cfg = config()
    status, payload = _call("GET", f"{_contents_path(name)}?ref={urllib.parse.quote(cfg['branch'])}",
                            accept="application/vnd.github.raw+json")
    if status == 404:
        raise CloudError(f"Backup '{name}' was not found.")
    return payload


def delete(name):
    sha = _get_sha(name)
    if not sha:
        return
    cfg = config()
    _call("DELETE", _contents_path(name), {"message": f"Delete {name}", "sha": sha, "branch": cfg["branch"]})


def _prune_dailies():
    dailies = [b for b in list_backups() if b["name"].startswith("daily_")]
    for old in dailies[KEEP_DAILY:]:
        try:
            delete(old["name"])
        except CloudError:
            pass


# --------------------------------------------------------------------------- high-level API
def backup_now():
    """Upload the vault to GitHub. Returns a human message; raises CloudError on failure."""
    data = exporter.full_backup()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    upload(LATEST, data, f"Vault backup {stamp}")
    daily = f"daily_{date.today():%Y%m%d}.zip"
    names = {b["name"] for b in list_backups()}
    if daily not in names:
        upload(daily, data, f"Daily snapshot {date.today()}")
        _prune_dailies()
    db.set_meta("last_cloud_backup", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    db.set_meta("last_cloud_error", "")
    db.set_meta("dirty", "0")
    return f"Backed up {db.count_certificates()} certificate(s) to GitHub ({len(data)/1024/1024:.1f} MB)."


def local_snapshot(min_interval=600):
    """Rotating local copy in data/backups (useful when running on your own PC/server)."""
    try:
        last = float(db.get_meta("last_local_snapshot", "0") or 0)
        if time.time() - last < min_interval:
            return
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        (LOCAL_DIR / f"vault_{datetime.now():%Y%m%d_%H%M%S}.zip").write_bytes(exporter.full_backup())
        for old in sorted(LOCAL_DIR.glob("vault_*.zip"), reverse=True)[LOCAL_KEEP:]:
            old.unlink(missing_ok=True)
        db.set_meta("last_local_snapshot", str(time.time()))
    except OSError:
        pass


def run_pending_backup():
    """Called on admin page load. Backs up if something changed. Returns (kind, message) or None."""
    if db.get_meta("dirty", "0") != "1":
        return None
    if db.count_certificates() == 0:
        return None  # never overwrite a good cloud backup with an empty vault automatically
    local_snapshot()
    if not is_configured():
        return None
    try:
        return "success", backup_now()
    except CloudError as exc:
        db.set_meta("last_cloud_error", str(exc))
        return "warning", f"Cloud backup failed (will retry on the next change): {exc}"
    except Exception as exc:  # never break the app because of a backup problem
        db.set_meta("last_cloud_error", str(exc))
        return "warning", f"Cloud backup failed: {exc}"


def restore_latest_if_empty():
    """Startup hook: if the vault is empty and a cloud backup exists, restore it. Returns message or None."""
    if not is_configured() or db.count_certificates() > 0:
        return None
    try:
        if LATEST not in {b["name"] for b in list_backups()}:
            return None
        result = exporter.restore_backup(download(LATEST), mode="replace")
        db.set_meta("dirty", "0")
        msg = f"Vault restored automatically from GitHub backup: {result['imported']} certificate(s)."
        if result["corrupt"]:
            msg += f" {result['corrupt']} file(s) were corrupt and skipped."
        return msg
    except Exception as exc:
        db.set_meta("last_cloud_error", f"Auto-restore failed: {exc}")
        return f"Automatic restore from GitHub failed: {exc}"
