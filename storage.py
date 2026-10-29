
from pathlib import Path
from uuid import uuid4
import hashlib

# ==========================================
# CERTIFICATE VAULT — FILE STORAGE
# Owner: Ritesh Kumar Singh
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"

# Only these file types are accepted.
ALLOWED_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}

# Maximum upload size: 10 MB per file.
MAX_FILE_SIZE = 10 * 1024 * 1024


class StorageError(Exception):
    """Raised when certificate file storage fails."""


def initialize_storage():
    """Create the legacy upload directory if possible (read-only hosts are fine)."""
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def validate_file(uploaded_file):
    """
    Validate a Streamlit UploadedFile.

    Returns:
        (is_valid, error_message)
    """
    if uploaded_file is None:
        return False, "Please select a file."

    filename = getattr(uploaded_file, "name", "")
    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        return (
            False,
            "Unsupported file type. Upload PDF, PNG, JPG, JPEG, or WEBP.",
        )

    try:
        content = uploaded_file.getvalue()
    except Exception:
        return False, "The uploaded file could not be read."

    if not content:
        return False, "The selected file is empty."

    if len(content) > MAX_FILE_SIZE:
        return False, "File is too large. Maximum allowed size is 10 MB."

    # Check file signatures instead of trusting only the filename.
    signatures = {
        ".pdf": lambda data: data.startswith(b"%PDF-"),
        ".png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
        ".jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
        ".webp": lambda data: (
            len(data) >= 12
            and data[:4] == b"RIFF"
            and data[8:12] == b"WEBP"
        ),
    }

    if not signatures[extension](content):
        return (
            False,
            "The file content does not match its extension or is invalid.",
        )

    return True, ""


MIME_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def prepare_upload(uploaded_file):
    """
    Validate an uploaded file and return (bytes, filename, mime_type)
    so it can be stored directly in the database.
    """
    is_valid, error = validate_file(uploaded_file)
    if not is_valid:
        raise StorageError(error)
    extension = Path(uploaded_file.name).suffix.lower()
    return uploaded_file.getvalue(), Path(uploaded_file.name).name, MIME_TYPES[extension]


def save_certificate_file(uploaded_file):
    """
    Validate and save a certificate file.

    Returns:
        Relative path such as uploads/abc123.pdf

    The returned path can be stored in the SQLite database.
    """
    initialize_storage()

    is_valid, error = validate_file(uploaded_file)

    if not is_valid:
        raise StorageError(error)

    content = uploaded_file.getvalue()
    extension = Path(uploaded_file.name).suffix.lower()

    # Use a generated filename to avoid collisions and path traversal.
    filename = f"{uuid4().hex}{extension}"
    destination = UPLOAD_DIR / filename

    try:
        destination.write_bytes(content)
    except OSError as exc:
        raise StorageError("Unable to save the uploaded file.") from exc

    return str(Path("uploads") / filename)


def get_certificate_file(relative_path):
    """
    Resolve a database-stored path safely.

    Returns:
        Path if the file exists, otherwise None.
    """
    if not relative_path:
        return None

    try:
        candidate = (BASE_DIR / relative_path).resolve()
        upload_root = UPLOAD_DIR.resolve()

        # Only serve files inside the uploads directory.
        if not candidate.is_relative_to(upload_root):
            return None

        if not candidate.is_file():
            return None

        if candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
            return None

        return candidate

    except (OSError, ValueError):
        return None


def delete_certificate_file(relative_path):
    """
    Delete a stored certificate file.

    Call this only after confirming the file is no longer referenced
    by any certificate record.
    """
    path = get_certificate_file(relative_path)

    if path is None:
        return False

    try:
        path.unlink()
        return True
    except OSError:
        return False


def get_file_sha256(relative_path):
    """Return a SHA-256 checksum for an existing stored file."""
    path = get_certificate_file(relative_path)

    if path is None:
        return None

    digest = hashlib.sha256()

    try:
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)

        return digest.hexdigest()

    except OSError:
        return None


# Prepare the local storage directory.
initialize_storage()
