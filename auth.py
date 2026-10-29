"""Owner authentication: salted PBKDF2 hashes, lockout, session timeout."""
import hashlib
import hmac
import os
import secrets as pysecrets
import time

import streamlit as st

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 60
SESSION_TIMEOUT_SECONDS = 30 * 60
PBKDF2_ITERATIONS = 240_000
DEFAULT_PASSWORD = "ChangeMe@2026"


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = pysecrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def _verify_pbkdf2(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(test, digest)
    except (ValueError, TypeError):
        return False


def _read_credentials():
    email = password = password_hash = ""
    try:
        section = st.secrets.get("admin", {})
        email = str(section.get("email", ""))
        password = str(section.get("password", ""))
        password_hash = str(section.get("password_hash", "")) or str(section.get("password_sha256", ""))
    except Exception:
        pass  # no secrets file present
    email = email or os.environ.get("ADMIN_EMAIL", "")
    password = password or os.environ.get("ADMIN_PASSWORD", "")
    password_hash = password_hash or os.environ.get("ADMIN_PASSWORD_HASH", "") or os.environ.get("ADMIN_PASSWORD_SHA256", "")
    return email.strip(), password, password_hash.strip()


def credentials_configured() -> bool:
    email, password, password_hash = _read_credentials()
    return bool(email and (password or password_hash))


def using_default_password() -> bool:
    _, password, password_hash = _read_credentials()
    return not password_hash and password == DEFAULT_PASSWORD


def _check(email_in: str, password_in: str) -> bool:
    email, password, password_hash = _read_credentials()
    if not email:
        return False
    email_ok = hmac.compare_digest(email_in.strip().lower().encode(), email.lower().encode())
    if password_hash.startswith("pbkdf2_sha256$"):
        pass_ok = _verify_pbkdf2(password_in, password_hash)
    elif password_hash:  # legacy plain SHA-256 hex
        supplied = hashlib.sha256(password_in.encode()).hexdigest()
        pass_ok = hmac.compare_digest(supplied.encode(), password_hash.lower().encode())
    else:
        pass_ok = bool(password) and hmac.compare_digest(password_in.encode(), password.encode())
    return email_ok and pass_ok


def is_authenticated() -> bool:
    if not st.session_state.get("authenticated", False):
        return False
    last = st.session_state.get("last_activity", 0)
    if time.time() - last > SESSION_TIMEOUT_SECONDS:
        st.session_state.authenticated = False
        st.session_state.session_expired = True
        return False
    st.session_state.last_activity = time.time()
    return True


def login(email_in: str, password_in: str):
    now = time.time()
    locked_until = st.session_state.get("locked_until", 0)
    if now < locked_until:
        return False, f"Too many failed attempts. Try again in {int(locked_until - now) + 1} seconds."
    if _check(email_in or "", password_in or ""):
        st.session_state.authenticated = True
        st.session_state.last_activity = now
        st.session_state.failed_attempts = 0
        st.session_state.session_expired = False
        return True, "Signed in successfully."
    attempts = st.session_state.get("failed_attempts", 0) + 1
    st.session_state.failed_attempts = attempts
    if attempts >= MAX_ATTEMPTS:
        st.session_state.locked_until = now + LOCKOUT_SECONDS
        st.session_state.failed_attempts = 0
        return False, f"Too many failed attempts. Locked for {LOCKOUT_SECONDS} seconds."
    return False, f"Incorrect email or password. ({MAX_ATTEMPTS - attempts} attempts left)"


def logout():
    st.session_state.authenticated = False
    st.session_state.last_activity = 0
