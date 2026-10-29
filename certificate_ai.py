"""Certificate text extraction (PDF text / OCR) and metadata suggestions."""

from datetime import date, datetime
import io
import re

try:
    import pymupdf as fitz  # modern name
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from PIL import Image

try:
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None


def ocr_available() -> bool:
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


CATEGORY_RULES = [
    ("Hackathon", ["hackathon", "hack fest", "innovation challenge"]),
    ("Quiz Competition", ["quiz competition", "tech quiz", "quiz contest"]),
    ("Coding Competition", ["coding competition", "coding contest", "programming contest"]),
    ("Workshop", ["workshop"]),
    ("Webinar & Seminar", ["webinar", "seminar", "guest lecture"]),
    ("Bootcamp & Training", ["bootcamp", "training program", "training programme"]),
    ("Internship", ["internship", "intern "]),
    ("Job Simulation", ["job simulation", "virtual experience program"]),
    ("AI & Machine Learning", ["artificial intelligence", "machine learning", "deep learning", "generative ai"]),
    ("Data Science & Analytics", ["data science", "data analytics", "data analysis"]),
    ("Programming & Software Development", ["python programming", "java programming", "software development"]),
    ("Course Completion", ["course completion", "successfully completed", "completion certificate"]),
    ("Academic Achievement", ["academic achievement", "academic excellence"]),
    ("Award & Recognition", ["winner", "first prize", "second prize", "award of excellence"]),
    ("Volunteering & Leadership", ["volunteer", "leadership", "community service"]),
]

SKILL_KEYWORDS = [
    "Python", "Java", "C++", "SQL", "Machine Learning", "Deep Learning", "Artificial Intelligence",
    "Data Science", "Data Analytics", "Power BI", "Tableau", "Excel", "Statistics", "Generative AI",
    "NLP", "Computer Vision", "Git", "GitHub", "Cloud Computing", "Cybersecurity", "Web Development",
    "HTML", "CSS", "JavaScript", "Communication", "Leadership", "Problem Solving", "Critical Thinking",
    "Teamwork", "Research",
]

BAD_TITLE_PHRASES = [
    "certificate no", "certificate number", "certificate id", "credential id", "serial number",
    "registration number", "this is to certify", "has successfully", "successfully completed",
    "awarded to", "presented to", "date of issue", "father name", "father's name", "son of", "daughter of",
]
BAD_ISSUER_PHRASES = [
    "successfully", "this is to certify", "certificate no", "certificate number", "certificate id",
    "awarded to", "presented to", "has completed", "father name", "son of", "daughter of",
]


def clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t:|-•")


def get_lines(text: str) -> list[str]:
    return [c for c in (clean_line(l) for l in text.splitlines()) if c]


def extract_text(file_bytes: bytes, filename: str) -> str:
    """Embedded PDF text, falling back to OCR. Raises RuntimeError only if nothing can be read."""
    ocr_ok = ocr_available()
    if filename.lower().endswith(".pdf"):
        with fitz.open(stream=file_bytes, filetype="pdf") as document:
            pages = list(document)
            embedded = "\n".join(p.get_text("text") for p in pages[:5])
            if len(embedded.strip()) >= 40 or not ocr_ok:
                return embedded[:30000]
            parts = []
            for page in pages[:3]:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                parts.append(pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png")))))
            return "\n".join(parts)[:30000]
    if not ocr_ok:
        raise RuntimeError("OCR engine (Tesseract) is not installed, so images cannot be read automatically.")
    image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    return pytesseract.image_to_string(image)[:30000]


def detect_credential_id(text: str) -> str:
    patterns = [
        r"(?:certificate\s*(?:no\.?|number|id)|credential\s*id|certification\s*id|serial\s*(?:no\.?|number))"
        r"\s*[:#\-]?\s*([A-Z0-9][A-Z0-9/_\-]{2,})",
        r"\b([A-Z]{2,}[/-]\d{4}[/-]\d{3,})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            value = m.group(1).strip(" .,:;")
            if value.lower() not in {"successfully", "completed", "participation"}:
                return value[:100]
    return ""


def detect_url(text: str) -> str:
    m = re.search(r"https?://[^\s<>\"')]+", text)
    return m.group(0).rstrip(".,;") if m else ""


def detect_title(text: str, filename: str) -> str:
    lines = get_lines(text)
    title_patterns = [
        r"certificate of (?:participation|completion|achievement|appreciation|excellence|attendance)",
        r"(?:hackathon|quiz competition|coding competition|workshop|internship|bootcamp)",
        r"(?:award of|certificate in|course on|training in)",
    ]
    for line in lines[:40]:
        lower = line.lower()
        if any(b in lower for b in BAD_TITLE_PHRASES):
            continue
        if re.search(r"\b(?:no\.?|number|id)\s*[:#]?\s*[A-Z0-9/_-]{4,}", line, re.I):
            continue
        if re.fullmatch(r"[\W\d_]+", line):
            continue
        if any(re.search(p, lower) for p in title_patterns):
            return line[:100]
    for line in lines[:15]:
        lower = line.lower()
        if any(b in lower for b in BAD_TITLE_PHRASES) or not (5 <= len(line) <= 75):
            continue
        if re.search(r"\b(?:20\d{2}|BCT/)\b", line, re.I) or lower in {"certificate", "congratulations", "successfully", "has"}:
            continue
        if re.search(r"\b(?:father|mother|son of|daughter of)\b", lower):
            continue
        if len(line.split()) <= 9 and not line.endswith("."):
            return line
    name = re.sub(r"[_-]+", " ", re.sub(r"\.[^.]+$", "", filename)).strip()
    if name and name.lower() not in {"certificate", "certificate pdf", "document", "scan"}:
        return name[:100]
    return "Certificate of Achievement"


def detect_recipient(text: str) -> str:
    pattern = (r"(?:this is to certify that|awarded to|presented to|certify that|participant name|"
               r"student name|recipient)\s*[:\-]?\s*(.{3,100})")
    stop = re.compile(r"\b(?:has|have|had|father|mother|son of|daughter of|for successfully|successfully|who has|date|certificate no)\b", re.I)
    m = re.search(pattern, text, flags=re.IGNORECASE)
    if not m:
        return ""
    cand = clean_line(m.group(1))
    cand = re.split(r"[\r\n]", cand)[0]
    cand = clean_line(stop.split(cand)[0])
    return cand if 3 <= len(cand) <= 60 and len(cand.split()) <= 6 else ""


def detect_issuer(text: str) -> str:
    words = ("university", "institute", "college", "technologies", "technology", "foundation", "academy",
             "corporation", "limited", "ltd", "private", "pvt", "company", "department", "council", "association",
             "society", "school", "laboratory", "labs", "ibm", "google", "microsoft", "tata", "forage",
             "coursera", "udemy", "nptel", "iit ", "iit-", "government")
    for line in get_lines(text)[:30]:
        lower = line.lower()
        if any(b in lower for b in BAD_ISSUER_PHRASES) or not (4 <= len(line) <= 100):
            continue
        if re.search(r"\b(?:20\d{2}|BCT/)\b", line, re.I) or re.search(r"\b(?:father|mother|son of|daughter of)\b", lower):
            continue
        if re.search(r"\b(?:certificate|participation|successfully|completed)\b", lower):
            continue
        if any(w in lower for w in words):
            return line[:100]
    return ""


_DATE_PATTERNS = [
    (r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b", ["%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y"]),
    (r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b", ["%Y-%m-%d", "%Y/%m/%d"]),
    (r"\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+\d{4}\b", ["%d %B %Y", "%d %b %Y"]),
    (r"\b[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b", ["%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"]),
]


def _parse_date(raw: str):
    raw = re.sub(r"(?<=\d)(st|nd|rd|th)", "", raw)
    raw = re.sub(r"\bSept\b", "Sep", raw, flags=re.I)
    raw = re.sub(r"[.,]", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    formats = ["%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%Y-%m-%d", "%Y/%m/%d",
               "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def find_dates(text: str) -> list[date]:
    found = []
    for pattern, _ in _DATE_PATTERNS:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            d = _parse_date(m.group(0))
            if d and 1990 <= d.year <= 2100:
                found.append((m.start(), d))
    return [d for _, d in sorted(found)]


def detect_date(text: str):
    """Issue date, or None when nothing reliable is found."""
    m = re.search(r"(?:date of issue|issued on|issue date|issued|dated?|completed on)\s*[:\-]?\s*(.{6,30})", text, re.I)
    if m:
        ds = find_dates(m.group(1))
        if ds:
            return ds[0]
    ds = find_dates(text)
    return ds[0] if ds else None


def detect_expiry(text: str):
    m = re.search(r"(?:valid (?:until|till|through|upto)|expir(?:es|y|ation)(?: date)?|valid up to)\s*[:\-]?\s*(.{6,30})", text, re.I)
    if m:
        ds = find_dates(m.group(1))
        if ds:
            return ds[0]
    return None


def detect_category(text: str, categories: list[str]) -> str:
    lower = text.lower()
    for category, keywords in CATEGORY_RULES:
        if category in categories and any(w in lower for w in keywords):
            return category
    return "Other" if "Other" in categories else categories[0]


def detect_skills(text: str) -> str:
    found = []
    for skill in SKILL_KEYWORDS:
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(skill) + r"(?![A-Za-z0-9])", text, re.I) and skill not in found:
            found.append(skill)
    return ", ".join(found[:12])


def detect_description(text: str, title: str, category: str) -> str:
    pattern = (r"[^.\n]{0,100}(?:successfully completed|completed the|participated in|participation in|attended|"
               r"qualified for|awarded for|recognized for)[^.\n]{0,180}")
    m = re.search(pattern, text, flags=re.IGNORECASE)
    if m:
        s = clean_line(m.group(0))
        if len(s) >= 20:
            return s[:300].rstrip(" .") + "."
    for line in get_lines(text):
        lower = line.lower()
        if 25 <= len(line) <= 220 and not any(b in lower for b in BAD_TITLE_PHRASES) \
                and re.search(r"\b(?:participat|complet|attend|achiev|award|training|workshop|hackathon|quiz|course|internship|skill)\w*\b", lower):
            return line[:300]
    return f"Certificate categorized as {category}. Please review the original and add any missing details."


def analyze_certificate(file_bytes: bytes, filename: str, categories: list[str]) -> dict:
    """Suggested fields for admin review. Never invents an issue date."""
    text = extract_text(file_bytes, filename)
    title = detect_title(text, filename)
    category = detect_category(text, categories)
    return {
        "title": title,
        "issuer": detect_issuer(text),
        "recipient": detect_recipient(text),
        "category": category,
        "issue_date": detect_date(text),
        "expiry_date": detect_expiry(text),
        "credential_id": detect_credential_id(text),
        "verification_url": detect_url(text),
        "skills": detect_skills(text),
        "description": detect_description(text, title, category),
        "extracted_text": text,
    }
