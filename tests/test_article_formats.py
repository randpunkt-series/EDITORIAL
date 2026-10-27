"""
Test parametrizado para verificar los 4 formatos de publicación:
- Short Note
- Scoped Expository Article
- Original Research Article
- Book

Flujo por cada formato:
1. Luis (o Polanco) confirma arbitraje.
2. Polanco fuerza el avance a 'todos_aprobados'.
3. Polanco publica (Zenodo simulado).
4. Verifica que el JSON tiene el 'article_type' correcto.
"""
import re
import json
import pytest
from playwright.sync_api import Page, expect

from tests.conftest import (
    REVIEWERS_URL, CHIEF_USER, CHIEF_PASSWORD, LUIS_USER, LUIS_PASSWORD,
    login, get_articulo,
)
from seeds._common import sftp_connect, revista_dir, books_dir

# ============================================================
# Fixtures
# ============================================================
def _find_product(sftp_session, article_type: str) -> str:
    """Busca un producto en en_revision con el article_type dado."""
    from tests.conftest import find_by_prefix, editorial_dir, read_csv
    
    rs = sftp_session["rs"]
    sftp = sftp_session["sftp"]
    
    # Para libros buscamos en BK-2026-, para artículos en RP-2026-
    prefix = "BK-2026-" if article_type == "Book" else "RP-2026-"
    df = read_csv(sftp, f"{editorial_dir(rs)}/registro_articulos.csv",
                  ["articulo_id", "estado", "titulo"])
    
    # Filtrar por prefijo y por el título que empieza con "Prueba de formato:"
    mask = df["articulo_id"].str.startswith(prefix) & df["titulo"].str.contains(article_type)
    row = df[mask & (df["estado"] == "en_revision")]
    
    if row.empty:
        pytest.skip(f"No hay producto '{article_type}' en en_revision. Corre seed_test_article_formats.")
    
    return row.iloc[0]["articulo_id"]


def _expander_for(page: Page, art_id: str):
    return page.locator('[data-testid="stExpander"]').filter(
        has_text=re.compile(re.escape(art_id))
    ).first


def _open_expander(page: Page, art_id: str, timeout_ms: int = 15000):
    exp = _expander_for(page, art_id)
    expect(exp).to_be_visible(timeout=20000)
    summary = exp.locator("summary")
    if summary.count() == 0:
        summary = exp.locator("> div").first

    for _ in range(3):
        if summary.first.get_attribute("aria-expanded") == "true" and exp.get_by_role("button").first.is_visible():
            return exp
        summary.first.click()
        page.wait_for_timeout(800)
    
    exp.get_by_role("button").first.wait_for(timeout=timeout_ms)
    return exp


def _wait_until(sftp_session, art_id: str, field: str, expected: str, timeout: float = 30.0):
    import time
    start = time.time()
    while time.time() - start < timeout:
        row = get_articulo(sftp_session, art_id)
        if row and row.get(field) == expected:
            return row
        time.sleep(0.8)
    raise AssertionError(f"Timeout esperando {field}={expected}")


# ============================================================
# Test
# ============================================================
@pytest.mark.parametrize("article_type,zenodo_type", [
    ("Short Note", "article"),
    ("Scoped Expository Article", "article"),
    ("Original Research Article", "article"),
    ("Book", "book"),
])
def test_article_format_full_flow(page: Page, sftp_session, article_type, zenodo_type):
    """Prueba el flujo completo para un formato específico."""
    art_id = _find_product(sftp_session, article_type)
    print(f"\n🧪 Probando formato: {article_type} (ID: {art_id})")

    # 1. Arbitraje (Luis)
    login(page, LUIS_USER, LUIS_PASSWORD, REVIEWERS_URL)
    exp = _open_expander(page, art_id)
    btn = exp.get_by_role("button", name=re.compile("Confirm arbitration", re.IGNORECASE))
    if btn.count() > 0:
        btn.first.click()
        _wait_until(sftp_session, art_id, "arbitraje_confirmado", "1")
        print(f"   ✅ Arbitraje confirmado")
    else:
        print(f"   ℹ️  Arbitraje ya estaba confirmado")

    # 2. Force-advance + Publicar (Polanco)
    login(page, CHIEF_USER, CHIEF_PASSWORD, REVIEWERS_URL)
    
    # Force advance si es necesario
    row = get_articulo(sftp_session, art_id)
    if row["estado"] == "en_revision":
        exp = _open_expander(page, art_id)
        force = exp.get_by_role("button", name=re.compile("Advance to 'todos_aprobados' \\(force\\)", re.IGNORECASE))
        expect(force.first).to_be_visible(timeout=15000)
        force.first.click()
        _wait_until(sftp_session, art_id, "estado", "todos_aprobados")
        print(f"   ✅ Force-advance completado")
    
    # Publicar
    page.reload()
    page.wait_for_timeout(2500)
    if page.get_by_label("Login").is_visible(timeout=3000):
        login(page, CHIEF_USER, CHIEF_PASSWORD, REVIEWERS_URL)
        page.wait_for_timeout(1500)

    exp = _open_expander(page, art_id)
    pub_btn = exp.get_by_role("button", name=re.compile("PUBLISH", re.IGNORECASE))
    expect(pub_btn.first).to_be_visible(timeout=20000)
    pub_btn.first.click()
    _wait_until(sftp_session, art_id, "estado", "published")
    print(f"   ✅ Publicado")

    # 3. Verificar JSON (con polling porque el CSV se actualiza antes que el JSON)
    import time
    rs = sftp_session["rs"]
    sftp = sftp_session["sftp"]
    rev = revista_dir(rs)
    bk = books_dir(rs)
    
    json_path = f"{bk}/metadata/{art_id}.json" if article_type == "Book" else f"{rev}/METADATA/{art_id}.json"
    
    # Polling: esperar hasta 30s a que el JSON tenga zenodo.doi
    start = time.time()
    meta = None
    while time.time() - start < 30:
        try:
            with sftp.open(json_path, "r") as f:
                meta = json.loads(f.read().decode("utf-8"))
            if meta.get("zenodo") and meta["zenodo"].get("doi"):
                break
        except Exception as e:
            print(f"   ⚠️  Error leyendo JSON: {e}")
        time.sleep(1)
    else:
        pytest.fail(f"Timeout esperando zenodo.doi en {json_path}")
    
    assert meta.get("article_type") == article_type, f"Esperaba article_type={article_type}, obtuve {meta.get('article_type')}"
    assert meta["zenodo"]["doi"], "Falta el DOI en Zenodo"
    
    print(f"   ✅ JSON verificado: article_type='{meta['article_type']}', DOI='{meta['zenodo']['doi']}'")
