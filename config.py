import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

STORAGE_MODE = os.environ.get("STORAGE_MODE", "local")
UPLOADS_DIR = Path(os.environ.get("UPLOADS_DIR", BASE_DIR / "uploads"))

PDF_DIR      = UPLOADS_DIR / "PDF"
ZIP_DIR      = UPLOADS_DIR / "ZIP"
TEX_DIR      = UPLOADS_DIR / "TEX"
DOCX_DIR     = UPLOADS_DIR / "DOCX"
METADATA_DIR = UPLOADS_DIR / "METADATA"

BOOKS_DIR          = UPLOADS_DIR / "BOOKS"
BOOKS_METADATA_DIR = BOOKS_DIR / "metadata"
BOOKS_PDF_DIR      = BOOKS_DIR / "pdf"
BOOKS_COVERS_DIR   = BOOKS_DIR / "covers"

MAX_PDF_MB        = 20
MAX_ZIP_MB        = 50
MAX_BOOK_PDF_MB   = 100
MAX_BOOK_COVER_MB = 5

JOURNAL_NAME = "RandPunkt Journal"
TRACKING_PREFIX = "RP"
BOOK_PREFIX = "BK"

ZENODO_TOKEN = os.environ.get("ZENODO_TOKEN", "")
ZENODO_API = os.environ.get("ZENODO_API", "https://zenodo.org/api")
ZENODO_COMMUNITY = os.environ.get("ZENODO_COMMUNITY", "randpunkt-journal")
ZENODO_SANDBOX_TOKEN = os.environ.get("ZENODO_SANDBOX_TOKEN", "")
ZENODO_SANDBOX_API = os.environ.get("ZENODO_SANDBOX_API", "https://sandbox.zenodo.org/api")
ZENODO_SIMULATE = os.environ.get("ZENODO_SIMULATE", "true").lower() == "true"

REMOTE_HOST    = os.environ.get("REMOTE_HOST", "")
REMOTE_PORT    = int(os.environ.get("REMOTE_PORT", "3792"))
REMOTE_USER    = os.environ.get("REMOTE_USER", "")
REMOTE_PATH    = os.environ.get("REMOTE_PATH", "")
REMOTE_SSH_KEY = os.environ.get("REMOTE_SSH_KEY", "")

STATE_EN_REVISION     = "en_revision"
STATE_TODOS_APROBADOS = "todos_aprobados"
STATE_PUBLISHED       = "published"
STATE_RETIRADO        = "retirado"

VALID_STATES = {
    STATE_EN_REVISION, STATE_TODOS_APROBADOS,
    STATE_PUBLISHED, STATE_RETIRADO,
}

ARBITRAJE_CONFIRMADO_COL = "arbitraje_confirmado"

def ensure_directories():
    for folder in [
        PDF_DIR, ZIP_DIR, TEX_DIR, DOCX_DIR, METADATA_DIR,
        BOOKS_DIR, BOOKS_METADATA_DIR, BOOKS_PDF_DIR, BOOKS_COVERS_DIR,
    ]:
        folder.mkdir(parents=True, exist_ok=True)
