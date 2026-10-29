"""Backup / restore / export helpers."""
import csv
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime

import database as db

CSV_FIELDS = ["id", "title", "issuer", "category", "issue_date", "expiry_date", "credential_id",
              "verification_url", "skills", "description", "published", "featured", "views", "downloads",
              "file_name", "file_sha256", "created_at", "updated_at"]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"


def certificates_csv(certs) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    w.writeheader()
    for c in certs:
        w.writerow(c)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM so Excel opens UTF-8 correctly


def files_zip(certs) -> bytes:
    """ZIP of the actual certificate files for the given certificates."""
    buf = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for c in certs:
            blob = db.get_certificate_blob(c["id"])
            if not blob:
                continue
            data, name, _ = blob
            name = f"{c['id']:04d}_{_safe(name)}"
            while name in used:
                name = "dup_" + name
            used.add(name)
            z.writestr(name, data)
    return buf.getvalue()


def full_backup() -> bytes:
    """Complete backup: manifest.json (all metadata + checksums) and files/<id>_<name>."""
    certs = db.get_certificates()
    manifest = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for c in certs:
            entry = {k: v for k, v in c.items() if k != "has_blob"}
            blob = db.get_certificate_blob(c["id"])
            if blob:
                data, name, mime = blob
                arc = f"files/{c['id']:04d}_{_safe(name)}"
                z.writestr(arc, data)
                entry.update(archive_file=arc, file_name=name, file_mime=mime,
                             file_sha256=hashlib.sha256(data).hexdigest(), file_size=len(data))
            manifest.append(entry)
        z.writestr("manifest.json", json.dumps(
            {"app": "certificate-vault", "version": 3,
             "created": datetime.now().isoformat(timespec="seconds"), "count": len(manifest),
             "certificates": manifest}, indent=2, ensure_ascii=False))
    return buf.getvalue()


def _open_backup(zip_bytes: bytes):
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
        manifest = json.loads(z.read("manifest.json").decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ValueError("This is not a valid Certificate Vault backup file.") from exc
    if manifest.get("app") != "certificate-vault" or not isinstance(manifest.get("certificates"), list):
        raise ValueError("This is not a valid Certificate Vault backup file.")
    return z, manifest


def inspect_backup(zip_bytes: bytes) -> dict:
    """Summarise a backup and verify every file checksum, without importing anything."""
    z, manifest = _open_backup(zip_bytes)
    ok = bad = missing = 0
    with z:
        names = set(z.namelist())
        for e in manifest["certificates"]:
            arc = e.get("archive_file")
            if not arc:
                continue
            if arc not in names:
                missing += 1
                continue
            want = e.get("file_sha256")
            if want and hashlib.sha256(z.read(arc)).hexdigest() != want:
                bad += 1
            else:
                ok += 1
    return {"created": manifest.get("created", "unknown"), "count": len(manifest["certificates"]),
            "files_ok": ok, "files_corrupt": bad, "files_missing": missing,
            "published": sum(1 for e in manifest["certificates"] if e.get("published")),
            "size": len(zip_bytes), "version": manifest.get("version", 1)}


def restore_backup(zip_bytes: bytes, mode: str = "merge") -> dict:
    """Restore a backup.

    mode="merge"   : add certificates that are not already present (duplicates by file hash skipped).
    mode="replace" : wipe the vault and restore everything exactly (ids, views, downloads kept).
    Corrupt/missing files are skipped and reported, never imported silently.
    """
    if mode not in ("merge", "replace"):
        raise ValueError("Unknown restore mode.")
    z, manifest = _open_backup(zip_bytes)
    imported = skipped = corrupt = 0
    with z:
        names = set(z.namelist())
        prepared = []
        for e in manifest["certificates"]:
            data, arc = None, e.get("archive_file")
            if arc:
                if arc not in names:
                    corrupt += 1
                    continue
                data = z.read(arc)
                if e.get("file_sha256") and hashlib.sha256(data).hexdigest() != e["file_sha256"]:
                    corrupt += 1
                    continue
            prepared.append((e, data))
        if mode == "replace":
            db.wipe_certificates()
        for e, data in prepared:
            if mode == "merge":
                if data and db.find_by_hash(hashlib.sha256(data).hexdigest()):
                    skipped += 1
                    continue
                db.add_certificate(
                    title=e.get("title"), issuer=e.get("issuer"), category=e.get("category") or "Other",
                    issue_date=e.get("issue_date") or "", expiry_date=e.get("expiry_date") or "",
                    credential_id=e.get("credential_id") or "", verification_url=e.get("verification_url") or "",
                    description=e.get("description") or "", skills=e.get("skills") or "",
                    published=bool(e.get("published")), featured=bool(e.get("featured")),
                    file_data=data, file_name=e.get("file_name") or "", file_mime=e.get("file_mime") or "")
            else:
                db.import_certificate(e, data)
            imported += 1
    db.log_action("restore", f"mode={mode} imported={imported} skipped={skipped} corrupt={corrupt}")
    return {"imported": imported, "skipped": skipped, "corrupt": corrupt, "mode": mode}
