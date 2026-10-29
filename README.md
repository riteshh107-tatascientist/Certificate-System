# Certificate Vault — Ritesh Kumar Singh

A Streamlit certificate portfolio: public gallery + password-protected Admin Studio.

## Features
**Public**
- Branded home page with live stats, featured certificates, category chart and top-skills cloud
- Gallery with search (title / issuer / skill / credential ID), category, skill, year, featured filter, sorting and pagination
- Real PDF/image preview (loaded on demand), download button, expiry badges (Valid / Expiring / Expired)
- **Verify Credential** page (check a credential ID) and shareable deep links (`?cert=ID`)

**Admin Studio**
- Secure login: salted PBKDF2 passwords, 5-attempt lockout, 30-minute session timeout
- Auto-scan of uploads (PDF text + OCR) that pre-fills title, issuer, dates, credential ID, URL, skills, category
- Duplicate-file detection, expiry-date tracking with alerts
- Bulk upload (auto-scan, saved as drafts), bulk publish / unpublish / delete
- Featured toggle, edit, replace file, delete with confirmation
- Analytics dashboard (by category / year, most viewed, view & download counters)
- **Automatic GitHub backup after every change + automatic restore on startup** (keeps data safe on Streamlit Cloud)
- Backup history (latest + 14 daily snapshots), restore any version, Merge or Replace modes, checksum verification, safety copy before Replace
- Manual full backup (ZIP) + restore from file, CSV export, all-files ZIP, activity log

## Run locally
```
python -m venv .venv
# Windows: .venv\Scripts\activate     Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```
Image OCR needs the Tesseract program (`sudo apt install tesseract-ocr`, or the Windows installer).
Without it the app still works: PDFs with text are read, images are filled in manually.

## Admin login
Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`. A temporary one is included
(`admin@certvault.local` / `ChangeMe@2026`) — **change it**. For a hashed password run
`python make_password_hash.py` and paste the `password_hash = ...` line.

### Streamlit Community Cloud
App → Settings → **Secrets** → paste the `[admin]` block. `packages.txt` installs Tesseract automatically.

## Deploy on Streamlit Cloud with permanent data
Streamlit Cloud wipes the app disk on restart, so enable the built-in GitHub backup (one-time, 2 minutes):
1. Create a **private** GitHub repo, e.g. `certificate-vault-backup` (tick *Add a README*).
2. GitHub → Settings → Developer settings → Fine-grained tokens → new token for **only that repo**, permission **Contents: Read and write**.
3. Streamlit Cloud → App → Settings → **Secrets**, paste:
```
[admin]
email = "your-email@example.com"
password = "choose-a-strong-password"

[github_backup]
token = "github_pat_xxxxxxxx"
repo  = "your-username/certificate-vault-backup"
```
4. Reboot the app → Admin Studio → **Backup & Restore** → *Test connection*.

From then on every add/edit/delete is uploaded automatically, and if the app restarts with an empty vault it is
**restored automatically** (same IDs, so share links keep working). Keep the backup repo private; backups
are limited to ~95 MB total by GitHub.

## Tests
```
pip install pytest && python -m pytest -q tests
```

## Files
`app.py` UI · `auth.py` login · `database.py` SQLite · `storage.py` upload validation ·
`certificate_ai.py` scan/OCR · `exporter.py` backup/export · `styles.css` theme
