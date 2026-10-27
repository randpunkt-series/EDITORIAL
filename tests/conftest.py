"""
Fixtures compartidas para los tests de Playwright.
"""
import os
import sys
import paramiko
import pandas as pd
import toml
import pytest
from io import StringIO
from pathlib import Path

# Añadir el directorio padre al path para importar seeds
sys.path.insert(0, str(Path(__file__).parent.parent))

from seeds._common import (
    sftp_connect, revista_dir, editorial_dir, books_dir, read_csv,
)


SECRETS_PATH = os.path.expanduser("~/POLANCO/EDITORIAL/.streamlit/secrets.toml")


# ============================================================
# URLs
# ============================================================
REVIEWERS_URL = "http://localhost:8501"
COAUTHORS_URL = "http://localhost:8502"
PUBLIC_URL = "http://localhost:8000"  # FastAPI

CHIEF_USER = "polanco"
CHIEF_PASSWORD = "polanco"
LUIS_USER = "luis"
LUIS_PASSWORD = "luis"


# ============================================================
# Fixtures de conexión
# ============================================================
@pytest.fixture(scope="session")
def sftp_session():
    transport, sftp, rs = sftp_connect()
    yield {"transport": transport, "sftp": sftp, "rs": rs}
    sftp.close()
    transport.close()


# ============================================================
# Helpers de consulta
# ============================================================
COLS_ARTICULOS = [
    "articulo_id", "tipo", "titulo", "ruta_pdf", "ruta_cover",
    "fecha_creacion", "estado", "isbn", "motivo_retiro", "fecha_retiro",
    "idioma_original", "formato_original", "editor_asignado",
    "arbitraje_confirmado",
]
COLS_CONFIRMACIONES = [
    "articulo_id", "nombre", "email", "token",
    "aprobado", "fecha_aprobacion", "comentarios",
]


def get_articulo(sftp_session, art_id: str):
    rs = sftp_session["rs"]
    sftp = sftp_session["sftp"]
    path = f"{editorial_dir(rs)}/registro_articulos.csv"
    df = read_csv(sftp, path, COLS_ARTICULOS)
    row = df[df["articulo_id"] == art_id]
    return row.iloc[0].to_dict() if not row.empty else None


def get_confirmaciones(sftp_session, art_id: str):
    rs = sftp_session["rs"]
    sftp = sftp_session["sftp"]
    path = f"{editorial_dir(rs)}/registro_confirmaciones.csv"
    df = read_csv(sftp, path, COLS_CONFIRMACIONES)
    return df[df["articulo_id"] == art_id].to_dict("records")


def find_by_prefix(sftp_session, prefix: str, estado: str | None = None):
    """Busca un producto por prefijo de ID (BK, RP, BK-C, BK-P)."""
    rs = sftp_session["rs"]
    sftp = sftp_session["sftp"]
    path = f"{editorial_dir(rs)}/registro_articulos.csv"
    df = read_csv(sftp, path, COLS_ARTICULOS)
    mask = df["articulo_id"].str.startswith(prefix)
    if estado:
        mask &= (df["estado"] == estado)
    rows = df[mask]
    return rows.iloc[0].to_dict() if not rows.empty else None


# ============================================================
# Fixtures de IDs (buscan en el CSV del servidor)
# ============================================================
@pytest.fixture(scope="session")
def book_id_en_revision(sftp_session):
    """Book subido por luis, en en_revision."""
    row = find_by_prefix(sftp_session, "BK-2026-", estado="en_revision")
    if not row or row.get("editor_asignado") != "luis":
        pytest.skip("No hay book de 'luis' en en_revision. Corre seed_test_book.py")
    return row["articulo_id"]


@pytest.fixture(scope="session")
def book_id_en_revision_chief(sftp_session):
    """Book subido por polanco, en en_revision."""
    row = find_by_prefix(sftp_session, "BK-C-2026-", estado="en_revision")
    if not row:
        pytest.skip("No hay book de Chief en en_revision. Corre seed_test_book_chief.py")
    return row["articulo_id"]


@pytest.fixture(scope="session")
def book_id_published(sftp_session):
    """Book ya publicado (seed)."""
    row = find_by_prefix(sftp_session, "BK-P-2026-", estado="published")
    if not row:
        pytest.skip("No hay book publicado. Corre seed_test_book_published.py")
    return row["articulo_id"]


@pytest.fixture(scope="session")
def tokens_book_id(sftp_session):
    """Devuelve (book_id, {email: token}) del book de luis en en_revision."""
    row = find_by_prefix(sftp_session, "BK-2026-", estado="en_revision")
    if not row or row.get("editor_asignado") != "luis":
        pytest.skip("No hay book de 'luis' en en_revision.")
    confs = get_confirmaciones(sftp_session, row["articulo_id"])
    tokens = {c["email"]: c["token"] for c in confs}
    return row["articulo_id"], tokens


# ============================================================
# Helper de login para Playwright
# ============================================================
def login(page, user: str, password: str, url: str = REVIEWERS_URL):
    """Hace login en Streamlit. Espera a que aparezca el dashboard."""
    page.goto(url)
    page.wait_for_selector("text=Editorial Panel", timeout=20000)
    page.get_by_label("Login").fill(user)
    page.get_by_role("textbox", name="Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_selector("text=Manuscripts and books", timeout=30000)


# ============================================================
# Helper: detección de FastAPI
# ============================================================
def _fastapi_disponible() -> bool:
    """Devuelve True si FastAPI responde en PUBLIC_URL."""
    import requests
    try:
        r = requests.get(f"{PUBLIC_URL}/books", timeout=3)
        return r.status_code == 200
    except Exception:
        return False
