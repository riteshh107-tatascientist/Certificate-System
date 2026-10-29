"""Generate a secure password hash for .streamlit/secrets.toml.

Usage:  python make_password_hash.py
"""
import getpass
from auth import hash_password

pw = getpass.getpass("New admin password: ")
if len(pw) < 8:
    raise SystemExit("Use at least 8 characters.")
if pw != getpass.getpass("Repeat password: "):
    raise SystemExit("Passwords do not match.")
print("\nPaste this into .streamlit/secrets.toml (remove the plain 'password' line):\n")
print(f'password_hash = "{hash_password(pw)}"')
