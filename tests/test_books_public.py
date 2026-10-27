"""
Test de la parte pública de books (FastAPI, puerto 8000).
"""
import pytest
import requests
from playwright.sync_api import Page, expect

from tests.conftest import PUBLIC_URL, _fastapi_disponible


@pytest.mark.skipif(not _fastapi_disponible(), reason="FastAPI no responde en :8000")
def test_books_list(page: Page, book_id_published, sftp_session):
    """La página /books muestra el libro publicado (por título)."""
    # Leer el título del JSON
    import json
    from seeds._common import books_dir
    sftp = sftp_session["sftp"]
    rs = sftp_session["rs"]
    bk = books_dir(rs)
    with sftp.open(f"{bk}/metadata/{book_id_published}.json", "r") as f:
        meta = json.loads(f.read().decode("utf-8"))
    title = meta.get("title") or meta.get("titulo")

    page.goto(f"{PUBLIC_URL}/books")
    expect(page.get_by_role("heading", name="Books")).to_be_visible(timeout=15000)
    # El título del libro aparece como <h3><a>...</a></h3>
    expect(page.get_by_role("heading", name=title, level=3)).to_be_visible(timeout=10000)
    print(f"   ✅ Book '{title}' visible en /books")


@pytest.mark.skipif(not _fastapi_disponible(), reason="FastAPI no responde en :8000")
def test_book_detail(page: Page, book_id_published):
    """La página /books/{id} muestra el detalle."""
    page.goto(f"{PUBLIC_URL}/books/{book_id_published}")
    expect(page.get_by_role("heading", level=1)).to_be_visible(timeout=10000)
    expect(page.get_by_text("⬇ Download PDF")).to_be_visible(timeout=5000)
    print(f"   ✅ Detalle de {book_id_published} OK")


@pytest.mark.skipif(not _fastapi_disponible(), reason="FastAPI no responde en :8000")
def test_book_pdf(book_id_published):
    """GET /books/{id}/pdf devuelve application/pdf (usando requests)."""
    url = f"{PUBLIC_URL}/books/{book_id_published}/pdf"
    r = requests.get(url, timeout=10)
    assert r.status_code == 200, f"Esperaba 200, obtuve {r.status_code}"
    ct = r.headers.get("content-type", "").lower()
    assert ct.startswith("application/pdf"), f"content-type inesperado: {ct}"
    assert len(r.content) > 0, "El PDF está vacío"
    print(f"   ✅ PDF: {ct}, {len(r.content)} bytes")


@pytest.mark.skipif(not _fastapi_disponible(), reason="FastAPI no responde en :8000")
def test_book_cover(book_id_published):
    """GET /books/{id}/cover devuelve una imagen."""
    url = f"{PUBLIC_URL}/books/{book_id_published}/cover"
    r = requests.get(url, timeout=10)
    assert r.status_code == 200, f"Esperaba 200, obtuve {r.status_code}"
    ct = r.headers.get("content-type", "").lower()
    assert ct.startswith("image/"), f"content-type inesperado: {ct}"
    print(f"   ✅ Cover: {ct}")


@pytest.mark.skipif(not _fastapi_disponible(), reason="FastAPI no responde en :8000")
def test_book_not_found(page: Page):
    """ID inexistente → 404."""
    response = page.goto(f"{PUBLIC_URL}/books/BK-9999-9999")
    assert response is not None
    assert response.status == 404
    print("   ✅ 404 correcto")


@pytest.mark.skipif(not _fastapi_disponible(), reason="FastAPI no responde en :8000")
def test_book_no_publicado_404(page: Page, book_id_en_revision):
    """Book en en_revision → 404."""
    response = page.goto(f"{PUBLIC_URL}/books/{book_id_en_revision}")
    assert response is not None
    assert response.status == 404
    print(f"   ✅ Book no publicado → 404")
