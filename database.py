"""SQLite data layer for Certificate Vault (certificates, audit log, backups)."""

from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
import hashlib
import sqlite3

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "certificates.db"


@contextmanager
def get_connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


MIGRATIONS = {
    "file_data": "BLOB",
    "file_name": "TEXT DEFAULT ''",
    "file_mime": "TEXT DEFAULT ''",
    "file_sha256": "TEXT DEFAULT ''",
    "expiry_date": "TEXT DEFAULT ''",
    "featured": "INTEGER NOT NULL DEFAULT 0",
    "downloads": "INTEGER NOT NULL DEFAULT 0",
    "views": "INTEGER NOT NULL DEFAULT 0",
}


def initialize_database():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS certificates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                issuer TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Other',
                issue_date TEXT DEFAULT '',
                credential_id TEXT DEFAULT '',
                verification_url TEXT DEFAULT '',
                description TEXT DEFAULT '',
                skills TEXT DEFAULT '',
                file_path TEXT DEFAULT '',
                published INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(certificates)")}
        for col, definition in MIGRATIONS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE certificates ADD COLUMN {col} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cert_published ON certificates(published)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cert_category ON certificates(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cert_sha ON certificates(file_sha256)")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                action TEXT NOT NULL,
                detail TEXT DEFAULT ''
            )""")


LIST_COLUMNS = (
    "id, title, issuer, category, issue_date, expiry_date, credential_id, "
    "verification_url, description, skills, file_path, published, featured, "
    "downloads, views, created_at, updated_at, file_name, file_mime, file_sha256, "
    "(file_data IS NOT NULL AND LENGTH(file_data) > 0) AS has_blob"
)

SORTS = {
    "Newest added": "created_at DESC, id DESC",
    "Oldest added": "created_at ASC, id ASC",
    "Issue date (newest)": "CASE WHEN issue_date='' THEN 1 ELSE 0 END, issue_date DESC, id DESC",
    "Issue date (oldest)": "CASE WHEN issue_date='' THEN 1 ELSE 0 END, issue_date ASC, id ASC",
    "Title A–Z": "title COLLATE NOCASE ASC",
    "Most viewed": "views DESC, id DESC",
}


def _like(term):
    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def get_meta(key, default=""):
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key, value):
    with get_connection() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


DATA_CHANGING_ACTIONS = {"add", "update", "delete", "restore"}


def log_action(action, detail=""):
    try:
        with get_connection() as conn:
            conn.execute("INSERT INTO audit_log (action, detail) VALUES (?, ?)", (action, detail[:500]))
            if action in DATA_CHANGING_ACTIONS:  # marks vault as needing a (cloud) backup
                conn.execute("INSERT INTO meta (key, value) VALUES ('dirty', '1') "
                             "ON CONFLICT(key) DO UPDATE SET value = '1'")
    except Exception:
        pass


def count_certificates():
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM certificates").fetchone()[0]


def wipe_certificates():
    with get_connection() as conn:
        conn.execute("DELETE FROM certificates")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def import_certificate(entry, data=None):
    """Insert a certificate keeping its original id, counters and timestamps (used by restore)."""
    sha = hashlib.sha256(data).hexdigest() if data else ""
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO certificates (id, title, issuer, category, issue_date, expiry_date,
               credential_id, verification_url, description, skills, file_path, published, featured,
               downloads, views, created_at, updated_at, file_data, file_name, file_mime, file_sha256)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entry.get("id"), entry.get("title") or "Untitled", entry.get("issuer") or "Unknown",
             entry.get("category") or "Other", entry.get("issue_date") or "", entry.get("expiry_date") or "",
             entry.get("credential_id") or "", entry.get("verification_url") or "",
             entry.get("description") or "", entry.get("skills") or "", "",
             int(bool(entry.get("published"))), int(bool(entry.get("featured"))),
             int(entry.get("downloads") or 0), int(entry.get("views") or 0),
             entry.get("created_at") or _now(), entry.get("updated_at") or entry.get("created_at") or _now(),
             data, entry.get("file_name") or "", entry.get("file_mime") or "", sha),
        )


def get_audit_log(limit=100):
    with get_connection() as conn:
        rows = conn.execute("SELECT at, action, detail FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_certificate_blob(certificate_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT file_data, file_name, file_mime FROM certificates WHERE id = ?", (certificate_id,)
        ).fetchone()
    if row and row["file_data"]:
        return bytes(row["file_data"]), row["file_name"] or "certificate", row["file_mime"] or ""
    return None


def find_by_hash(sha256, exclude_id=None):
    if not sha256:
        return None
    with get_connection() as conn:
        if exclude_id is None:
            row = conn.execute("SELECT id, title FROM certificates WHERE file_sha256 = ? LIMIT 1", (sha256,)).fetchone()
        else:
            row = conn.execute(
                "SELECT id, title FROM certificates WHERE file_sha256 = ? AND id != ? LIMIT 1", (sha256, exclude_id)
            ).fetchone()
    return dict(row) if row else None


def add_certificate(title, issuer, category="Other", issue_date="", expiry_date="", credential_id="",
                    verification_url="", description="", skills="", file_path="", published=False,
                    featured=False, file_data=None, file_name="", file_mime=""):
    title, issuer = str(title or "").strip(), str(issuer or "").strip()
    if not title:
        raise ValueError("Certificate title is required.")
    if not issuer:
        raise ValueError("Certificate issuer is required.")
    sha = hashlib.sha256(file_data).hexdigest() if file_data else ""
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO certificates (title, issuer, category, issue_date, expiry_date, credential_id,
               verification_url, description, skills, file_path, published, featured,
               file_data, file_name, file_mime, file_sha256)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title, issuer, category, issue_date, expiry_date, credential_id, verification_url,
             description, skills, file_path, int(bool(published)), int(bool(featured)),
             file_data, file_name, file_mime, sha),
        )
        new_id = cur.lastrowid
    log_action("add", f"#{new_id} {title}")
    return new_id


def get_certificate(certificate_id):
    with get_connection() as conn:
        row = conn.execute(f"SELECT {LIST_COLUMNS} FROM certificates WHERE id = ?", (certificate_id,)).fetchone()
    return dict(row) if row else None


def get_certificates(published_only=False, category=None, search=None, sort="Newest added",
                     featured_only=False, skill=None, year=None):
    query = f"SELECT {LIST_COLUMNS} FROM certificates WHERE 1=1"
    params = []
    if published_only:
        query += " AND published = 1"
    if featured_only:
        query += " AND featured = 1"
    if category and category != "All Categories":
        query += " AND category = ?"
        params.append(category)
    if skill:
        query += " AND (',' || REPLACE(LOWER(skills), ', ', ',') || ',') LIKE ? ESCAPE '\\'"
        params.append(_like(skill.strip().lower()))
    if year:
        query += " AND substr(issue_date, 1, 4) = ?"
        params.append(str(year))
    if search and search.strip():
        query += """ AND (title LIKE ? ESCAPE '\\' OR issuer LIKE ? ESCAPE '\\' OR skills LIKE ? ESCAPE '\\'
                     OR description LIKE ? ESCAPE '\\' OR credential_id LIKE ? ESCAPE '\\')"""
        params.extend([_like(search.strip())] * 5)
    query += " ORDER BY " + SORTS.get(sort, SORTS["Newest added"])
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def update_certificate(certificate_id, **fields):
    allowed = {"title", "issuer", "category", "issue_date", "expiry_date", "credential_id", "verification_url",
               "description", "skills", "file_path", "published", "featured", "file_data", "file_name", "file_mime"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        raise ValueError("No valid fields supplied for update.")
    for f in ("title", "issuer"):
        if f in updates:
            updates[f] = str(updates[f] or "").strip()
            if not updates[f]:
                raise ValueError(f"{f.capitalize()} cannot be empty.")
    for f in ("published", "featured"):
        if f in updates:
            updates[f] = int(bool(updates[f]))
    if updates.get("file_data"):
        updates["file_sha256"] = hashlib.sha256(updates["file_data"]).hexdigest()
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE certificates SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            list(updates.values()) + [certificate_id],
        )
    ok = cur.rowcount > 0
    if ok:
        log_action("update", f"#{certificate_id} {', '.join(k for k in updates if k not in ('file_data','file_sha256'))}")
    return ok


def delete_certificate(certificate_id):
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM certificates WHERE id = ?", (certificate_id,))
    ok = cur.rowcount > 0
    if ok:
        log_action("delete", f"#{certificate_id}")
    return ok


def set_published(certificate_id, published):
    return update_certificate(certificate_id, published=published)


def set_featured(certificate_id, featured):
    return update_certificate(certificate_id, featured=featured)


def bulk_set_published(ids, published):
    n = 0
    for i in ids:
        n += 1 if update_certificate(i, published=published) else 0
    return n


def bulk_delete(ids):
    return sum(1 for i in ids if delete_certificate(i))


def increment_counter(certificate_id, column):
    if column not in ("views", "downloads"):
        raise ValueError("Invalid counter")
    with get_connection() as conn:
        conn.execute(f"UPDATE certificates SET {column} = {column} + 1 WHERE id = ?", (certificate_id,))


def get_dashboard_stats():
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM certificates").fetchone()[0]
        published = conn.execute("SELECT COUNT(*) FROM certificates WHERE published = 1").fetchone()[0]
        categories = conn.execute("SELECT COUNT(DISTINCT category) FROM certificates WHERE published = 1").fetchone()[0]
        featured = conn.execute("SELECT COUNT(*) FROM certificates WHERE featured = 1 AND published = 1").fetchone()[0]
        views = conn.execute("SELECT COALESCE(SUM(views),0) FROM certificates").fetchone()[0]
        downloads = conn.execute("SELECT COALESCE(SUM(downloads),0) FROM certificates").fetchone()[0]
    return {"total": total, "published": published, "categories": categories, "drafts": total - published,
            "featured": featured, "views": views, "downloads": downloads}


def get_category_counts(published_only=False):
    q = "SELECT category, COUNT(*) AS n FROM certificates"
    if published_only:
        q += " WHERE published = 1"
    q += " GROUP BY category ORDER BY n DESC, category"
    with get_connection() as conn:
        return [(r["category"], r["n"]) for r in conn.execute(q)]


def get_year_counts(published_only=False):
    q = "SELECT substr(issue_date,1,4) AS y, COUNT(*) AS n FROM certificates WHERE issue_date != ''"
    if published_only:
        q += " AND published = 1"
    q += " GROUP BY y ORDER BY y"
    with get_connection() as conn:
        return [(r["y"], r["n"]) for r in conn.execute(q) if r["y"] and r["y"].isdigit()]


def get_all_skills(published_only=True):
    q = "SELECT skills FROM certificates WHERE skills != ''"
    if published_only:
        q += " AND published = 1"
    counts = {}
    with get_connection() as conn:
        for r in conn.execute(q):
            for s in r["skills"].split(","):
                s = s.strip()
                if s:
                    counts[s] = counts.get(s, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def get_expiring(days=60):
    """Certificates whose expiry_date falls in the past or within `days`."""
    today = date.today()
    out = []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, title, issuer, expiry_date FROM certificates WHERE expiry_date != '' ORDER BY expiry_date"
        ).fetchall()
    for r in rows:
        try:
            exp = datetime.strptime(r["expiry_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        left = (exp - today).days
        if left <= days:
            out.append({**dict(r), "days_left": left})
    return out


initialize_database()
