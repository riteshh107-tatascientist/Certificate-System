"""Certificate Vault — Ritesh Kumar Singh. Streamlit portfolio with a public gallery and admin studio."""

from datetime import date, datetime
from html import escape
from pathlib import Path
import base64
import hashlib

import pandas as pd
import streamlit as st

import auth
import certificate_ai
import cloud_backup
import database as db
import exporter
import storage

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"

CATEGORIES = [
    "AI & Machine Learning", "Data Science & Analytics", "Programming & Software Development",
    "Hackathon", "Quiz Competition", "Coding Competition", "Workshop", "Webinar & Seminar",
    "Bootcamp & Training", "Internship", "Job Simulation", "Course Completion",
    "Academic Achievement", "Award & Recognition", "Volunteering & Leadership", "Other",
]
MIN_DATE, MAX_DATE = date(1990, 1, 1), date(2100, 12, 31)
PER_PAGE_PUBLIC, PER_PAGE_ADMIN = 6, 8
UPLOAD_TYPES = ["pdf", "png", "jpg", "jpeg", "webp"]


def find_photo():
    for name in ("ritesh_profile.jpeg", "ritesh_profile.jpg", "ritesh_profile.png", "ritesh_profile.webp"):
        if (ASSETS / name).exists():
            return ASSETS / name
    return None


PHOTO_PATH = find_photo()

st.set_page_config(page_title="Certificate Vault | Ritesh Kumar Singh", page_icon="🏆",
                   layout="wide", initial_sidebar_state="expanded")

st.session_state.setdefault("page", "Home")
st.session_state.setdefault("flash", None)
st.session_state.setdefault("focus_cert", None)
st.session_state.setdefault("seen_views", set())
st.session_state.setdefault("add_nonce", 0)
st.session_state.setdefault("bulk_nonce", 0)



@st.cache_resource(show_spinner="Checking cloud backup...")
def _startup_restore():
    """Runs once per server start: if the vault is empty and a GitHub backup exists, restore it."""
    return cloud_backup.restore_latest_if_empty()


_startup_msg = _startup_restore()
if _startup_msg and not st.session_state.get("_startup_msg_shown"):
    st.session_state["_startup_msg_shown"] = True
    st.toast(_startup_msg, icon="☁️")

# Deep link support: ?cert=12 opens that certificate directly (published ones only).
if not st.session_state.get("_params_read"):
    st.session_state["_params_read"] = True
    try:
        raw = st.query_params.get("cert")
        if raw and str(raw).isdigit():
            st.session_state.focus_cert = int(raw)
            st.session_state.page = "Certificates"
    except Exception:
        pass

try:
    st.markdown(f"<style>{(ROOT / 'styles.css').read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)
except OSError:
    pass


# ============================ HELPERS ============================

def html(markup):
    """Render HTML via st.markdown; compacting avoids markdown code-block quirks."""
    st.markdown("".join(line.strip() for line in markup.splitlines()), unsafe_allow_html=True)


def navigate(page, focus=None):
    st.session_state.page = page
    st.session_state.focus_cert = focus
    st.rerun()


def flash(message, kind="success"):
    st.session_state.flash = (kind, message)


def show_flash():
    item = st.session_state.get("flash")
    if item:
        kind, message = item
        getattr(st, kind, st.info)(message)
        st.session_state.flash = None


def valid_url(url):
    url = (url or "").strip()
    return url == "" or url.lower().startswith(("http://", "https://"))


def to_date(value):
    try:
        d = datetime.strptime(str(value), "%Y-%m-%d").date()
        return d if MIN_DATE <= d <= MAX_DATE else None
    except (ValueError, TypeError):
        return None


def clamp_date(d):
    return d if d and MIN_DATE <= d <= MAX_DATE else None


def iso(d):
    return d.isoformat() if d else ""


def profile_photo_html():
    if PHOTO_PATH is None:
        return ('<div class="hero-photo" style="height:200px;display:flex;align-items:center;'
                'justify-content:center;font-size:70px">👨‍💻</div>')
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[PHOTO_PATH.suffix.lower()]
    data = base64.b64encode(PHOTO_PATH.read_bytes()).decode("utf-8")
    return f'<div class="hero-photo"><img src="data:{mime};base64,{data}" alt="Ritesh Kumar Singh profile photo"></div>'


def expiry_badge(cert):
    exp = to_date(cert.get("expiry_date"))
    if not exp:
        return ""
    left = (exp - date.today()).days
    if left < 0:
        return f'<span class="badge bad">Expired {escape(exp.isoformat())}</span>'
    if left <= 60:
        return f'<span class="badge warn">Expires in {left} days</span>'
    return f'<span class="badge good">Valid until {escape(exp.isoformat())}</span>'


def load_file(cert):
    """(bytes, filename, mime) for a certificate or None. Supports legacy on-disk files."""
    blob = db.get_certificate_blob(cert["id"])
    if blob:
        return blob
    legacy = storage.get_certificate_file(cert.get("file_path", ""))
    if legacy is not None:
        mime = storage.MIME_TYPES.get(legacy.suffix.lower(), "application/octet-stream")
        return legacy.read_bytes(), legacy.name, mime
    return None


@st.cache_data(show_spinner=False, max_entries=64)
def pdf_to_images(pdf_bytes, max_pages=3):
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            return []
    pages = []
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            for i in range(min(len(doc), max_pages)):
                pages.append(doc[i].get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6)).tobytes("png"))
    except Exception:
        return []
    return pages


def has_file(cert):
    return bool(cert.get("has_blob") or cert.get("file_path"))


def count_view(cert_id):
    seen = st.session_state["seen_views"]
    if cert_id not in seen:
        seen.add(cert_id)
        try:
            db.increment_counter(cert_id, "views")
        except Exception:
            pass


def download_button(cert, key, count=True):
    """Download button; loads the file lazily when Streamlit supports it."""
    if not has_file(cert):
        return
    name = cert.get("file_name") or "certificate"
    mime = cert.get("file_mime") or "application/octet-stream"
    kwargs = dict(file_name=name, mime=mime, key=key)
    if count:
        kwargs.update(on_click=db.increment_counter, args=(cert["id"], "downloads"))

    def _data():
        loaded = load_file(cert)
        return loaded[0] if loaded else b""

    try:
        st.download_button("⬇️ Download certificate", data=_data, **kwargs)
    except Exception:  # older Streamlit without lazy data
        st.download_button("⬇️ Download certificate", data=_data(), **kwargs)


def render_preview(cert, key):
    if not has_file(cert):
        st.caption("📎 No certificate file attached.")
        return
    loaded = load_file(cert)
    if loaded is None:
        st.warning("Certificate file is missing from storage. Open Admin Studio → Manage to upload it again.")
        return
    data, _, mime = loaded
    if mime == "application/pdf":
        pages = pdf_to_images(data)
        if pages:
            for png in pages:
                st.image(png, width="stretch")
        else:
            st.info("PDF preview unavailable — use the download button.")
    else:
        st.image(data, width="stretch")


def paginate(items, per_page, key):
    pages = max(1, -(-len(items) // per_page))
    current = min(max(1, int(st.session_state.get(key, 1))), pages)
    st.session_state[key] = current
    if pages > 1:
        _, mid, _ = st.columns([2, 1, 2])
        current = mid.number_input(f"Page (1–{pages})", min_value=1, max_value=pages, step=1, key=key)
    start = (current - 1) * per_page
    return items[start:start + per_page]


def badges_html(cert):
    parts = []
    if cert.get("featured"):
        parts.append('<span class="badge star">⭐ Featured</span>')
    exp = expiry_badge(cert)
    if exp:
        parts.append(exp)
    for s in (cert.get("skills") or "").split(","):
        if s.strip():
            parts.append(f'<span class="badge">{escape(s.strip())}</span>')
    return f'<div class="cert-badges">{"".join(parts)}</div>' if parts else ""


def render_public_card(cert, prefix):
    cid = cert["id"]
    with st.container(border=True):
        left, right = st.columns([2, 3], gap="large")
        with left:
            st.subheader(cert["title"])
            lines = [f"**Issuer:** {cert['issuer']}", f"**Category:** {cert['category']}"]
            if cert["issue_date"]:
                lines.append(f"**Issued:** {cert['issue_date']}")
            if cert["credential_id"]:
                lines.append(f"**Credential ID:** `{cert['credential_id']}`")
            st.markdown("  \n".join(lines))
            if cert["description"]:
                st.write(cert["description"])
            html(badges_html(cert))
            b1, b2 = st.columns(2)
            if cert["verification_url"] and valid_url(cert["verification_url"]):
                b1.link_button("🔗 Verify credential", cert["verification_url"], width="stretch")
            with st.popover("🔗 Share", width="stretch"):
                st.caption("Add this to your app address to open this certificate directly:")
                st.code(f"?cert={cid}", language=None)
        with right:
            if has_file(cert):
                if st.toggle("👁️ Show certificate", key=f"{prefix}_show_{cid}"):
                    count_view(cid)
                    render_preview(cert, f"{prefix}_{cid}")
                download_button(cert, f"{prefix}_dl_{cid}")
            else:
                st.caption("📎 No certificate file attached.")


def do_restore(zip_bytes, mode):
    """Restore a backup (keeps a safety copy of the current vault first). Returns a message."""
    if mode == "replace" and db.count_certificates() > 0:
        st.session_state["pre_restore_snapshot"] = exporter.full_backup()
    result = exporter.restore_backup(zip_bytes, mode=mode)
    msg = (f"Restore complete ({'exact copy' if mode == 'replace' else 'merged'}): "
           f"{result['imported']} restored, {result['skipped']} duplicate(s) skipped")
    if result["corrupt"]:
        msg += f", {result['corrupt']} corrupt file(s) skipped"
    return msg + "."


# ============================ SIDEBAR ============================

with st.sidebar:
    st.markdown("## 🏆 Certificate Vault")
    st.caption("PERSONAL ACHIEVEMENT PORTFOLIO")
    st.divider()
    st.markdown("### Ritesh Kumar Singh")
    st.caption("Founder & CEO")
    st.caption("Data Science · AI · Technology")
    st.divider()

    navigation = [("🏠  Home", "Home"), ("🏆  Certificates", "Certificates"), ("✅  Verify Credential", "Verify"),
                  ("👤  About Owner", "About"), ("⚙️  Admin Studio", "Admin")]
    for label, page_name in navigation:
        if st.button(label, key=f"nav_{page_name}", width="stretch",
                     type="primary" if st.session_state.page == page_name else "secondary"):
            navigate(page_name)

    if auth.is_authenticated():
        st.divider()
        st.success("Admin signed in")
        if st.button("🚪  Sign out", key="nav_logout", width="stretch"):
            auth.logout()
            flash("You have been signed out.", "info")
            navigate("Home")

    st.divider()
    st.caption("Built with Python & Streamlit")
    st.caption("© Ritesh Kumar Singh")

page = st.session_state.page

# ============================ HOME ============================

if page == "Home":
    show_flash()
    stats = db.get_dashboard_stats()
    html(f"""
        <section class="hero">
            <div class="hero-copy">
                <div class="eyebrow">MY LEARNING JOURNEY · MY ACHIEVEMENTS</div>
                <h1>Knowledge Earned.<br>Potential Unlocked.</h1>
                <p>Welcome to my personal Certificate Vault — a central portfolio for my certifications,
                technical skills, learning milestones, and professional growth.</p>
                <div class="owner-pill">✦ Ritesh Kumar Singh &nbsp; | &nbsp; Founder &amp; CEO</div>
            </div>
            {profile_photo_html()}
        </section>""")
    html('<div class="section-title">Portfolio at a Glance</div>'
         '<div class="section-subtitle">A professional home for my learning achievements.</div>')

    c1, c2, c3, c4 = st.columns(4)
    for col, label, value in [(c1, "PUBLISHED CERTIFICATES", stats["published"]),
                              (c2, "SKILL CATEGORIES", stats["categories"]),
                              (c3, "FEATURED", stats["featured"]),
                              (c4, "PORTFOLIO FOCUS", "AI + DATA")]:
        with col:
            html(f'<div class="metric-card"><div class="metric-label">{label}</div>'
                 f'<div class="metric-value">{escape(str(value))}</div></div>')

    featured = db.get_certificates(published_only=True, featured_only=True, sort="Issue date (newest)")[:3]
    if featured:
        html('<div class="section-title">⭐ Featured Certificates</div>'
             '<div class="section-subtitle">Highlights from my portfolio.</div>')
        cols = st.columns(len(featured))
        for col, cert in zip(cols, featured):
            with col, st.container(border=True):
                st.markdown(f"**{cert['title']}**")
                st.caption(f"{cert['issuer']} · {cert['category']}")
                if st.button("Open →", key=f"feat_{cert['id']}", width="stretch"):
                    navigate("Certificates", focus=cert["id"])

    cat_counts = db.get_category_counts(published_only=True)
    top_skills = db.get_all_skills(True)[:14]
    if cat_counts or top_skills:
        gl, gr = st.columns(2)
        if cat_counts:
            with gl:
                html('<div class="section-title">Certificates by Category</div>')
                df = pd.DataFrame(cat_counts, columns=["Category", "Certificates"]).set_index("Category")
                st.bar_chart(df, horizontal=True, height=max(200, 38 * len(df)))
        if top_skills:
            with gr:
                html('<div class="section-title">Top Skills</div>')
                html('<div class="cert-badges">' + "".join(
                    f'<span class="badge">{escape(s)} · {n}</span>' for s, n in top_skills) + "</div>")

    html('<div class="section-title">Explore Your Portfolio</div>'
         '<div class="section-subtitle">Choose a section to continue.</div>')
    a, b, c = st.columns(3)
    for col, icon, title, desc in [
        (a, "🏆", "Certificate Library", "Browse published certificates, skills, and credentials."),
        (b, "✅", "Verify Credential", "Check any credential ID against the vault instantly."),
        (c, "🔐", "Owner Control Center", "Manage certificate records through the private admin area."),
    ]:
        with col:
            html(f'<div class="feature-card"><div class="feature-icon">{icon}</div><h3>{title}</h3><p>{desc}</p></div>')
    st.write("")
    x, y, z = st.columns(3)
    if x.button("Explore Certificates  →", type="primary", width="stretch"):
        navigate("Certificates")
    if y.button("Verify a Credential  →", width="stretch"):
        navigate("Verify")
    if z.button("Meet the Owner  →", width="stretch"):
        navigate("About")

# ============================ CERTIFICATES ============================

elif page == "Certificates":
    st.title("🏆 Certificate Library")
    focus = st.session_state.get("focus_cert")

    if focus:
        cert = db.get_certificate(focus)
        if st.button("← Back to all certificates"):
            try:
                st.query_params.clear()
            except Exception:
                pass
            navigate("Certificates")
        if cert and cert["published"]:
            render_public_card(cert, "focus")
        else:
            st.warning("That certificate is not available.")
    else:
        st.caption("Search and explore certificates published by the owner.")
        skills = [s for s, _ in db.get_all_skills(True)]
        years = sorted({y for y, _ in db.get_year_counts(True)}, reverse=True)

        r1a, r1b, r1c = st.columns([2, 1, 1])
        search = r1a.text_input("🔎 Search certificates", placeholder="Title, issuer, skill or credential ID...")
        category = r1b.selectbox("Category", ["All Categories"] + CATEGORIES)
        sort = r1c.selectbox("Sort by", list(db.SORTS))
        r2a, r2b, r2c = st.columns([1, 1, 1])
        skill = r2a.selectbox("Skill", ["Any skill"] + skills)
        year = r2b.selectbox("Year", ["Any year"] + years)
        only_featured = r2c.toggle("⭐ Featured only")

        certificates = db.get_certificates(
            published_only=True, category=category, search=search, sort=sort,
            featured_only=only_featured, skill=None if skill == "Any skill" else skill,
            year=None if year == "Any year" else year,
        )
        if not certificates:
            st.info("No published certificates match. Try clearing a filter, or add certificates in Admin Studio.")
        else:
            st.caption(f"Showing {len(certificates)} certificate(s)")
            for cert in paginate(certificates, PER_PAGE_PUBLIC, "pub_page"):
                render_public_card(cert, "pub")

# ============================ VERIFY ============================

elif page == "Verify":
    st.title("✅ Verify a Credential")
    st.caption("Enter a credential / certificate ID to confirm it is listed in this vault.")
    with st.form("verify_form"):
        cid_in = st.text_input("Credential ID", placeholder="e.g. BCT/2026/505181")
        go = st.form_submit_button("Verify", type="primary")
    if go:
        needle = cid_in.strip().lower()
        if not needle:
            st.error("Please enter a credential ID.")
        else:
            matches = [c for c in db.get_certificates(published_only=True, search=needle)
                       if c["credential_id"].strip().lower() == needle]
            if matches:
                for c in matches:
                    html(f'<div class="verify-ok"><b>✔ Verified — listed in the vault</b><br>'
                         f'{escape(c["title"])} · {escape(c["issuer"])}<br>'
                         f'Issued: {escape(c["issue_date"] or "n/a")} {expiry_badge(c)}</div>')
                    if c["verification_url"] and valid_url(c["verification_url"]):
                        st.link_button("Open issuer verification page", c["verification_url"])
                    if st.button("View certificate →", key=f"vopen_{c['id']}"):
                        navigate("Certificates", focus=c["id"])
            else:
                html('<div class="verify-bad"><b>✖ No published certificate has that credential ID.</b></div>')

# ============================ ABOUT ============================

elif page == "About":
    st.title("👤 About the Owner")
    left, right = st.columns([1, 2], gap="large")
    with left:
        if PHOTO_PATH is not None:
            st.image(str(PHOTO_PATH), caption="Ritesh Kumar Singh", width="stretch")
        else:
            st.info("Add your photo at assets/ritesh_profile.jpeg")
    with right:
        html('<div class="eyebrow">OWNER · FOUNDER · CEO</div>')
        st.header("Ritesh Kumar Singh")
        st.write("Data Science Student | Python | Machine Learning | Artificial Intelligence")
        st.markdown("### My Mission")
        st.write("To continuously learn, build practical projects, develop industry-relevant skills, "
                 "and document my professional growth.")
        st.markdown("### Areas of Interest")
        st.markdown("- Data Science and Data Analytics\n- Python Programming and SQL\n"
                    "- Machine Learning and Artificial Intelligence\n- Building practical technology projects")
        html('<div class="owner-pill">✦ Founder &amp; CEO — Certificate Vault</div>')

# ============================ ADMIN ============================

elif page in ("Admin", "Login"):
    st.title("⚙️ Admin Studio")
    st.caption("Private workspace for the portfolio owner.")

    if not auth.is_authenticated():
        if not auth.credentials_configured():
            st.error("Admin credentials are not configured. Create `.streamlit/secrets.toml` (locally) or add the "
                     "same text under **Settings → Secrets** (Streamlit Cloud):")
            st.code('[admin]\nemail = "your-email@example.com"\npassword = "choose-a-strong-password"', language="toml")
            st.stop()
        show_flash()
        if st.session_state.pop("session_expired", False):
            st.warning("Your session expired after 30 minutes of inactivity. Please sign in again.")
        st.subheader("🔐 Owner Sign In")
        _, mid, _ = st.columns([1, 2, 1])
        with mid:
            with st.form("login_form"):
                email = st.text_input("Email", autocomplete="username")
                password = st.text_input("Password", type="password", autocomplete="current-password")
                submitted = st.form_submit_button("Sign In", type="primary", width="stretch")
            if submitted:
                ok, message = auth.login(email, password)
                if ok:
                    db.log_action("login", "admin signed in")
                    flash(message, "success")
                    navigate("Admin")
                else:
                    st.error(message)
        st.stop()

    show_flash()
    if auth.using_default_password():
        st.warning("You are using the temporary default password. Change it in `.streamlit/secrets.toml` "
                   "(or Streamlit Cloud → Secrets) before sharing this app.")

    pending = cloud_backup.run_pending_backup()
    if pending:
        if pending[0] == "success":
            st.toast(pending[1], icon="☁️")
        else:
            st.warning(pending[1])
    if not cloud_backup.is_configured():
        st.info("☁️ **Cloud backup is not set up.** On Streamlit Cloud your certificates can be lost when the app "
                "restarts. Open **Backup & Restore** and follow the 2-minute setup.")

    stats = db.get_dashboard_stats()
    m = st.columns(6)
    for col, label, val in zip(m, ["Total", "Published", "Drafts", "Featured", "Views", "Downloads"],
                               [stats["total"], stats["published"], stats["drafts"], stats["featured"],
                                stats["views"], stats["downloads"]]):
        col.metric(label, val)

    tab_dash, tab_add, tab_bulk, tab_manage, tab_backup = st.tabs(
        ["📊 Dashboard", "➕ Add", "📦 Bulk Upload", "🗂️ Manage", "💾 Backup & Restore"])

    # ---------- Dashboard ----------
    with tab_dash:
        expiring = db.get_expiring(60)
        if expiring:
            st.subheader("⏰ Expiry alerts")
            for e in expiring:
                msg = (f"**{e['title']}** ({e['issuer']}) — expired {-e['days_left']} day(s) ago"
                       if e["days_left"] < 0 else f"**{e['title']}** ({e['issuer']}) — expires in {e['days_left']} day(s)")
                (st.error if e["days_left"] < 0 else st.warning)(msg)
        cats, yrs = db.get_category_counts(), db.get_year_counts()
        if not cats:
            st.info("Add certificates to see analytics.")
        else:
            d1, d2 = st.columns(2)
            with d1:
                st.markdown("**By category**")
                st.bar_chart(pd.DataFrame(cats, columns=["Category", "Certificates"]).set_index("Category"), horizontal=True)
            with d2:
                st.markdown("**By year issued**")
                if yrs:
                    st.bar_chart(pd.DataFrame(yrs, columns=["Year", "Certificates"]).set_index("Year"))
                else:
                    st.caption("No issue dates recorded yet.")
            allc = db.get_certificates(sort="Most viewed")[:10]
            st.markdown("**Most viewed**")
            st.dataframe(pd.DataFrame([{"Title": c["title"], "Views": c["views"], "Downloads": c["downloads"],
                                        "Status": "Published" if c["published"] else "Draft"} for c in allc]),
                         width="stretch", hide_index=True)

    # ---------- Add (single) ----------
    with tab_add:
        n = st.session_state.add_nonce
        K = lambda name: f"add_{name}_{n}"  # noqa: E731
        for name, default in [("title", ""), ("issuer", ""), ("category", "Other"), ("issue_date", None),
                              ("expiry_date", None), ("credential_id", ""), ("verification_url", ""),
                              ("skills", ""), ("description", "")]:
            st.session_state.setdefault(K(name), default)

        upload = st.file_uploader("Certificate file (PDF, PNG, JPG, JPEG, WEBP — max 10 MB)",
                                  type=UPLOAD_TYPES, key=K("upload"))
        upload_hash, dup = "", None
        if upload is not None:
            ok, err = storage.validate_file(upload)
            if not ok:
                st.error(err)
                upload = None
            else:
                raw = upload.getvalue()
                upload_hash = hashlib.sha256(raw).hexdigest()
                dup = db.find_by_hash(upload_hash)
                if st.session_state.get(K("sig")) != upload_hash:
                    st.session_state[K("sig")] = upload_hash
                    try:
                        with st.spinner("Reading certificate and detecting details..."):
                            det = certificate_ai.analyze_certificate(raw, upload.name, CATEGORIES)
                        st.session_state[K("title")] = det["title"]
                        st.session_state[K("issuer")] = det["issuer"]
                        st.session_state[K("category")] = det["category"]
                        st.session_state[K("issue_date")] = clamp_date(det["issue_date"])
                        st.session_state[K("expiry_date")] = clamp_date(det["expiry_date"])
                        st.session_state[K("credential_id")] = det["credential_id"]
                        st.session_state[K("verification_url")] = det["verification_url"]
                        st.session_state[K("skills")] = det["skills"]
                        st.session_state[K("description")] = det["description"]
                        st.session_state[K("recipient")] = det["recipient"]
                        st.session_state[K("scan_msg")] = ("success", "Certificate scanned. Review the suggested details below.")
                    except Exception as exc:
                        st.session_state[K("scan_msg")] = (
                            "warning", f"Automatic reading was not possible ({exc}). Please enter the details manually.")
        if upload is not None:
            kind, msg = st.session_state.get(K("scan_msg"), (None, None))
            if kind:
                getattr(st, kind)(msg)
            if st.session_state.get(K("recipient")):
                st.info(f"Detected recipient name (please verify): {st.session_state[K('recipient')]}")
            if dup:
                st.warning(f"This exact file is already in the vault as #{dup['id']} — “{dup['title']}”.")

        with st.form(f"add_form_{n}"):
            c1, c2 = st.columns(2)
            title = c1.text_input("Title *", key=K("title"))
            issuer = c2.text_input("Issuer *", key=K("issuer"))
            category = c1.selectbox("Category", CATEGORIES, key=K("category"))
            issue_date = c2.date_input("Issue date", min_value=MIN_DATE, max_value=MAX_DATE, key=K("issue_date"))
            expiry_date = c1.date_input("Expiry date (optional)", min_value=MIN_DATE, max_value=MAX_DATE, key=K("expiry_date"))
            credential_id = c2.text_input("Credential ID", key=K("credential_id"))
            verification_url = c1.text_input("Verification URL (https://...)", key=K("verification_url"))
            skills = c2.text_input("Skills (comma separated)", key=K("skills"))
            description = st.text_area("Description", key=K("description"))
            p1, p2, p3 = st.columns(3)
            publish = p1.checkbox("Publish immediately", value=True, key=K("publish"))
            featured = p2.checkbox("⭐ Featured", value=False, key=K("featured"))
            allow_dup = p3.checkbox("Save even if duplicate", value=False, key=K("allow_dup")) if dup else False
            add_clicked = st.form_submit_button("Save Certificate", type="primary")

        if add_clicked:
            if not title.strip() or not issuer.strip():
                st.error("Title and issuer are required.")
            elif not valid_url(verification_url):
                st.error("Verification URL must start with http:// or https://")
            elif upload is None:
                st.error("Please upload a valid certificate file.")
            elif expiry_date and issue_date and expiry_date < issue_date:
                st.error("Expiry date cannot be before the issue date.")
            elif dup and not allow_dup:
                st.error("Duplicate file. Tick “Save even if duplicate” to save anyway.")
            else:
                try:
                    data, fname, fmime = storage.prepare_upload(upload)
                    new_id = db.add_certificate(
                        title=title, issuer=issuer, category=category, issue_date=iso(issue_date),
                        expiry_date=iso(expiry_date), credential_id=credential_id.strip(),
                        verification_url=verification_url.strip(), description=description.strip(),
                        skills=", ".join(s.strip() for s in skills.split(",") if s.strip()),
                        published=publish, featured=featured, file_data=data, file_name=fname, file_mime=fmime)
                    st.session_state.add_nonce += 1
                    flash(f"Certificate #{new_id} saved.", "success")
                    st.rerun()
                except storage.StorageError as exc:
                    st.error(str(exc))
                except Exception as exc:
                    st.error(f"Could not save certificate: {exc}")

    # ---------- Bulk upload ----------
    with tab_bulk:
        bn = st.session_state.bulk_nonce
        st.caption("Upload many certificates at once. Each is scanned automatically and saved as a **draft** "
                   "so you can review and publish it in the Manage tab. Duplicates are skipped.")
        files = st.file_uploader("Certificate files", type=UPLOAD_TYPES, accept_multiple_files=True, key=f"bulk_up_{bn}")
        if st.button("🚀 Scan & save as drafts", type="primary", disabled=not files, key=f"bulk_go_{bn}"):
            results, bar = [], st.progress(0.0)
            for i, f in enumerate(files, start=1):
                row = {"File": f.name, "Result": "", "Title": ""}
                try:
                    data, fname, fmime = storage.prepare_upload(f)
                    sha = hashlib.sha256(data).hexdigest()
                    existing = db.find_by_hash(sha)
                    if existing:
                        row["Result"] = f"Skipped (duplicate of #{existing['id']})"
                    else:
                        try:
                            det = certificate_ai.analyze_certificate(data, fname, CATEGORIES)
                        except Exception:
                            det = {"title": Path(fname).stem.replace("_", " ")[:100], "issuer": "", "category": "Other",
                                   "issue_date": None, "expiry_date": None, "credential_id": "",
                                   "verification_url": "", "skills": "", "description": ""}
                        new_id = db.add_certificate(
                            title=det["title"] or Path(fname).stem, issuer=det["issuer"] or "Unknown — please edit",
                            category=det["category"], issue_date=iso(clamp_date(det["issue_date"])),
                            expiry_date=iso(clamp_date(det["expiry_date"])), credential_id=det["credential_id"],
                            verification_url=det["verification_url"], description=det["description"],
                            skills=det["skills"], published=False, file_data=data, file_name=fname, file_mime=fmime)
                        row["Result"], row["Title"] = f"Saved as draft #{new_id}", det["title"]
                except storage.StorageError as exc:
                    row["Result"] = f"Rejected: {exc}"
                except Exception as exc:
                    row["Result"] = f"Error: {exc}"
                results.append(row)
                bar.progress(i / len(files))
            st.session_state["bulk_results"] = results
            st.session_state.bulk_nonce += 1
            st.rerun()
        if st.session_state.get("bulk_results"):
            st.success("Bulk upload finished.")
            st.dataframe(pd.DataFrame(st.session_state["bulk_results"]), width="stretch", hide_index=True)

    # ---------- Manage ----------
    with tab_manage:
        f1, f2, f3 = st.columns([2, 1, 1])
        q = f1.text_input("🔎 Filter", key="mg_q", placeholder="Title, issuer, skill, credential ID...")
        status = f2.selectbox("Status", ["All", "Published", "Drafts", "Featured"], key="mg_status")
        cat_f = f3.selectbox("Category", ["All Categories"] + CATEGORIES, key="mg_cat")
        certs = db.get_certificates(category=cat_f, search=q, featured_only=(status == "Featured"))
        if status == "Published":
            certs = [c for c in certs if c["published"]]
        elif status == "Drafts":
            certs = [c for c in certs if not c["published"]]

        if not certs:
            st.info("No certificates found. Add one in the Add or Bulk Upload tab.")
        else:
            labels = {f"#{c['id']} · {c['title']}": c["id"] for c in certs}
            with st.expander("☑️ Bulk actions"):
                picked = st.multiselect("Select certificates", list(labels), key="mg_pick")
                ids = [labels[p] for p in picked if p in labels]
                b1, b2, b3 = st.columns(3)
                if b1.button("Publish selected", disabled=not ids, width="stretch", key="bulk_pub"):
                    flash(f"{db.bulk_set_published(ids, True)} certificate(s) published.")
                    st.rerun()
                if b2.button("Unpublish selected", disabled=not ids, width="stretch", key="bulk_unpub"):
                    flash(f"{db.bulk_set_published(ids, False)} certificate(s) moved to drafts.")
                    st.rerun()
                ok_del = b3.checkbox("Confirm bulk delete", key="bulk_del_ok")
                if st.button("🗑️ Delete selected", disabled=not ids, key="bulk_del"):
                    if ok_del:
                        flash(f"{db.bulk_delete(ids)} certificate(s) deleted.")
                        st.rerun()
                    else:
                        st.warning("Tick “Confirm bulk delete” first.")

            st.caption(f"{len(certs)} certificate(s)")
            for cert in paginate(certs, PER_PAGE_ADMIN, "mg_page"):
                cid = cert["id"]
                tag = ("✅ Published" if cert["published"] else "📝 Draft") + ("  ⭐" if cert["featured"] else "")
                with st.expander(f"{tag}  ·  {cert['title']}  —  {cert['issuer']}"):
                    st.caption(f"👁️ {cert['views']} views · ⬇️ {cert['downloads']} downloads · updated {cert['updated_at']}")
                    with st.form(f"edit_{cid}"):
                        c1, c2 = st.columns(2)
                        e_title = c1.text_input("Title *", cert["title"])
                        e_issuer = c2.text_input("Issuer *", cert["issuer"])
                        cat_opts = CATEGORIES if cert["category"] in CATEGORIES else CATEGORIES + [cert["category"]]
                        e_cat = c1.selectbox("Category", cat_opts, index=cat_opts.index(cert["category"]))
                        e_date = c2.date_input("Issue date", value=to_date(cert["issue_date"]), min_value=MIN_DATE, max_value=MAX_DATE)
                        e_exp = c1.date_input("Expiry date (optional)", value=to_date(cert["expiry_date"]), min_value=MIN_DATE, max_value=MAX_DATE)
                        e_cred = c2.text_input("Credential ID", cert["credential_id"])
                        e_url = c1.text_input("Verification URL", cert["verification_url"])
                        e_skills = c2.text_input("Skills (comma separated)", cert["skills"])
                        e_desc = st.text_area("Description", cert["description"])
                        e_file = st.file_uploader("Replace certificate file (optional)", type=UPLOAD_TYPES, key=f"file_{cid}")
                        e1, e2 = st.columns(2)
                        e_pub = e1.checkbox("Published", value=bool(cert["published"]))
                        e_feat = e2.checkbox("⭐ Featured", value=bool(cert["featured"]))
                        save_clicked = st.form_submit_button("Save Changes", type="primary")

                    if save_clicked:
                        if not e_title.strip() or not e_issuer.strip():
                            st.error("Title and issuer are required.")
                        elif not valid_url(e_url):
                            st.error("Verification URL must start with http:// or https://")
                        elif e_exp and e_date and e_exp < e_date:
                            st.error("Expiry date cannot be before the issue date.")
                        else:
                            try:
                                fields = dict(
                                    title=e_title, issuer=e_issuer, category=e_cat, issue_date=iso(e_date),
                                    expiry_date=iso(e_exp), credential_id=e_cred.strip(), verification_url=e_url.strip(),
                                    description=e_desc.strip(),
                                    skills=", ".join(s.strip() for s in e_skills.split(",") if s.strip()),
                                    published=e_pub, featured=e_feat)
                                if e_file is not None:
                                    data, fname, fmime = storage.prepare_upload(e_file)
                                    fields.update(file_data=data, file_name=fname, file_mime=fmime)
                                db.update_certificate(cid, **fields)
                                flash("Certificate updated.")
                                st.rerun()
                            except storage.StorageError as exc:
                                st.error(str(exc))
                            except Exception as exc:
                                st.error(f"Could not update certificate: {exc}")

                    if has_file(cert):
                        st.caption(f"Current file: {cert['file_name'] or Path(cert['file_path']).name}")
                        if st.toggle("Preview file", key=f"adm_prev_{cid}"):
                            render_preview(cert, f"adm_{cid}")
                        download_button(cert, f"adm_dl_{cid}", count=False)
                    else:
                        st.caption("No file attached.")

                    d1, d2, d3, d4 = st.columns([1, 1, 1, 1.4])
                    if d1.button("Unpublish" if cert["published"] else "Publish", key=f"toggle_{cid}", width="stretch"):
                        db.set_published(cid, not cert["published"])
                        flash("Certificate unpublished." if cert["published"] else "Certificate published.")
                        st.rerun()
                    if d2.button("Unfeature" if cert["featured"] else "⭐ Feature", key=f"feat_{cid}", width="stretch"):
                        db.set_featured(cid, not cert["featured"])
                        flash("Featured status updated.")
                        st.rerun()
                    confirm = d4.checkbox("Confirm delete", key=f"confirm_{cid}")
                    if d3.button("🗑️ Delete", key=f"delete_{cid}", width="stretch"):
                        if confirm:
                            db.delete_certificate(cid)
                            if cert["file_path"]:
                                storage.delete_certificate_file(cert["file_path"])
                            flash("Certificate deleted.")
                            st.rerun()
                        else:
                            st.warning("Tick “Confirm delete” first.")

    # ---------- Backup & restore ----------
    with tab_backup:
        st.subheader("☁️ Automatic cloud backup (GitHub)")
        cfg = cloud_backup.config()
        if not cfg:
            st.warning("Not configured yet — your data is only safe while the app's disk survives.")
            with st.expander("🛠️ 2-minute setup guide", expanded=True):
                st.markdown(
                    "1. On GitHub create a **new PRIVATE repository**, e.g. `certificate-vault-backup` "
                    "(tick *Add a README* so the branch exists).\n"
                    "2. GitHub → Settings → Developer settings → **Fine-grained tokens** → *Generate new token*. "
                    "Repository access: **only** that repo. Permissions: **Contents → Read and write**.\n"
                    "3. Streamlit Cloud → your app → **Settings → Secrets** → paste:")
                st.code('[github_backup]\ntoken = "github_pat_xxxxxxxx"\nrepo  = "your-username/certificate-vault-backup"\n'
                        '# branch = "main"            # optional\n# folder = "vault-backups"    # optional',
                        language="toml")
                st.markdown("4. Save, reboot the app, then press **Test connection** here. After that every change is "
                            "backed up automatically, and an empty vault is **restored automatically** on startup.")
        else:
            last, err = db.get_meta("last_cloud_backup"), db.get_meta("last_cloud_error")
            k1, k2, k3 = st.columns(3)
            k1.metric("Last cloud backup", last or "never")
            k2.metric("Backup repository", cfg["repo"])
            k3.metric("Status", "⚠️ Error" if err else "✅ Active")
            if err:
                st.error(err)
            b1, b2 = st.columns(2)
            if b1.button("☁️ Backup now", width="stretch", key="cloud_now"):
                try:
                    with st.spinner("Uploading backup to GitHub..."):
                        st.session_state["cloud_msg"] = ("success", cloud_backup.backup_now())
                except cloud_backup.CloudError as exc:
                    db.set_meta("last_cloud_error", str(exc))
                    st.session_state["cloud_msg"] = ("error", str(exc))
                st.rerun()
            if b2.button("🔌 Test connection", width="stretch", key="cloud_test"):
                ok, msg = cloud_backup.test_connection()
                st.session_state["cloud_msg"] = ("success" if ok else "error", msg)
                st.rerun()
            note = st.session_state.pop("cloud_msg", None)
            if note:
                getattr(st, note[0])(note[1])
            st.caption("Every change is uploaded automatically (latest + one snapshot per day, 14 days kept). "
                       "If the app restarts with an empty vault, it is restored automatically.")

            with st.expander("📜 Cloud backup history & restore"):
                if st.button("Load backup list", key="cloud_list_btn"):
                    try:
                        st.session_state["cloud_list"] = cloud_backup.list_backups()
                    except cloud_backup.CloudError as exc:
                        st.error(str(exc))
                backups = st.session_state.get("cloud_list")
                if backups is not None and not backups:
                    st.info("No backups in the repository yet. Press “Backup now”.")
                elif backups:
                    names = [f"{b['name']}  ·  {b['size']/1024/1024:.2f} MB" for b in backups]
                    pick = st.selectbox("Backup", names, key="cloud_pick")
                    cmode = st.radio("Restore mode", ["Merge — add what is missing", "Replace — exact copy of that backup"],
                                     key="cloud_mode")
                    replace = cmode.startswith("Replace")
                    cok = st.checkbox("I understand Replace overwrites the current vault", key="cloud_ok") if replace else True
                    if st.button("♻️ Restore selected backup", disabled=not cok, key="cloud_restore"):
                        try:
                            with st.spinner("Downloading and verifying backup..."):
                                blob = cloud_backup.download(backups[names.index(pick)]["name"])
                                exporter.inspect_backup(blob)
                                flash(do_restore(blob, "replace" if replace else "merge"))
                            st.rerun()
                        except (cloud_backup.CloudError, ValueError) as exc:
                            st.error(str(exc))
                        except Exception as exc:
                            st.error(f"Restore failed: {exc}")

        if st.session_state.get("pre_restore_snapshot"):
            st.warning("A safety copy of your previous vault was kept for this session.")
            st.download_button("⬇️ Download safety copy of previous data", st.session_state["pre_restore_snapshot"],
                               "vault_before_restore.zip", "application/zip", key="dl_safety")

        st.divider()
        st.subheader("💾 Manual backup & export")
        b1, b2, b3 = st.columns(3)
        if b1.button("Prepare full backup (ZIP)", width="stretch", key="prep_zip"):
            st.session_state["backup_zip"] = exporter.full_backup()
        if b2.button("Prepare CSV export", width="stretch", key="prep_csv"):
            st.session_state["backup_csv"] = exporter.certificates_csv(db.get_certificates())
        if b3.button("Prepare all files (ZIP)", width="stretch", key="prep_files"):
            st.session_state["backup_files"] = exporter.files_zip(db.get_certificates())
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        if st.session_state.get("backup_zip"):
            st.download_button("⬇️ Download backup", st.session_state["backup_zip"],
                               f"certificate_vault_backup_{stamp}.zip", "application/zip", key="dl_backup")
        if st.session_state.get("backup_csv"):
            st.download_button("⬇️ Download CSV", st.session_state["backup_csv"],
                               f"certificates_{stamp}.csv", "text/csv", key="dl_csv")
        if st.session_state.get("backup_files"):
            st.download_button("⬇️ Download certificate files", st.session_state["backup_files"],
                               f"certificate_files_{stamp}.zip", "application/zip", key="dl_files")

        st.divider()
        st.subheader("♻️ Restore from a backup file")
        restore = st.file_uploader("Backup ZIP created by this app", type=["zip"], key="restore_up")
        info = None
        if restore is not None:
            try:
                info = exporter.inspect_backup(restore.getvalue())
                r1, r2, r3, r4 = st.columns(4)
                r1.metric("Certificates", info["count"])
                r2.metric("Published", info["published"])
                r3.metric("Files verified", info["files_ok"])
                r4.metric("Corrupt / missing", info["files_corrupt"] + info["files_missing"])
                st.caption(f"Backup created: {info['created']}")
                if info["files_corrupt"] or info["files_missing"]:
                    st.warning("Some files are damaged or missing. They will be skipped; everything else is restored.")
            except ValueError as exc:
                st.error(str(exc))
        fmode = st.radio("Restore mode", ["Merge — add what is missing, keep current data",
                                          "Replace — make the vault an exact copy of the backup"], key="file_mode")
        freplace = fmode.startswith("Replace")
        fok = st.checkbox("I understand Replace overwrites the current vault", key="file_ok") if freplace else True
        if st.button("Restore backup", disabled=info is None or not fok, key="restore_go"):
            try:
                flash(do_restore(restore.getvalue(), "replace" if freplace else "merge"))
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Restore failed: {exc}")

        st.divider()
        st.subheader("🧾 Activity log")
        log = db.get_audit_log(100)
        if log:
            st.dataframe(pd.DataFrame(log), width="stretch", hide_index=True)
        else:
            st.caption("No activity yet.")

        st.divider()
        with st.expander("🔑 Change admin password"):
            st.write("Run `python make_password_hash.py`, then put the printed `password_hash = ...` line in "
                     "`.streamlit/secrets.toml` (or Streamlit Cloud → Settings → Secrets) and remove the plain `password` line.")
        st.caption(f"OCR engine: {'available ✅' if certificate_ai.ocr_available() else 'not installed — PDFs with text still work, images need Tesseract ⚠️'}")

# ============================ FOOTER ============================

html("""
    <div class="footer">
        © 2026 Ritesh Kumar Singh · Founder &amp; CEO<br>
        Certificate Vault · Built with Python &amp; Streamlit<br>
        Learn. Build. Verify. Grow.
    </div>""")
