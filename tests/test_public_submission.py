"""
Test end-to-end del flujo de autor externo:
  /submission → Import → arbitraje → publicar.

Opcionalmente, si el operador humano firma el token, se cubre también
el flujo de coautores.
"""
import re
import time
import pytest
from pathlib import Path
from playwright.sync_api import Page, expect

from tests.conftest import (
    REVIEWERS_URL, COAUTHORS_URL, PUBLIC_URL,
    CHIEF_USER, CHIEF_PASSWORD, LUIS_USER, LUIS_PASSWORD,
    login, get_articulo,
    _fastapi_disponible,
)
from seeds._common import sftp_connect, revista_dir, editorial_dir
import json


def _find_expander(page: Page, art_id: str):
    return page.locator('[data-testid="stExpander"]').filter(
        has_text=re.compile(re.escape(art_id))
    ).first


def _open_expander(page: Page, art_id: str, timeout_ms: int = 15000):
    """
    Abre el expander del producto de forma robusta.
    Tras st.rerun() de Streamlit, el expander puede cerrarse solo;
    reintentamos hasta 3 veces.
    """
    exp = _find_expander(page, art_id)
    expect(exp).to_be_visible(timeout=20000)

    summary = exp.locator("summary")
    if summary.count() == 0:
        summary = exp.locator("> div").first

    def _is_open():
        try:
            return summary.first.get_attribute("aria-expanded", timeout=1000) == "true"
        except Exception:
            return False

    for attempt in range(3):
        if _is_open() and exp.get_by_role("button").first.is_visible():
            return exp
        summary.first.click()
        page.wait_for_timeout(800)

    try:
        page.evaluate("(el) => el.click()", exp.element_handle())
        page.wait_for_timeout(800)
    except Exception:
        pass

    exp.get_by_role("button").first.wait_for(timeout=timeout_ms)
    return exp


def _wait_until(sftp_session, art_id: str, field: str, expected: str,
                timeout: float = 30.0):
    from tests.conftest import get_articulo
    start = time.time()
    while time.time() - start < timeout:
        row = get_articulo(sftp_session, art_id)
        if row and row.get(field) == expected:
            return row
        time.sleep(0.8)
    row = get_articulo(sftp_session, art_id)
    raise AssertionError(f"Timeout esperando {field}={expected}. Último: {row}")


def _find_latest_external(sftp_session) -> dict:
    """Devuelve el JSON del artículo externo más reciente en METADATA/."""
    rs = sftp_session["rs"]
    sftp = sftp_session["sftp"]
    rev = revista_dir(rs)
    files = sorted(
        [f for f in sftp.listdir(f"{rev}/METADATA") if f.endswith(".json")],
        reverse=True,
    )
    if not files:
        pytest.fail("No hay JSONs en METADATA/. Corre seed_test_external_author primero.")
    with sftp.open(f"{rev}/METADATA/{files[0]}", "r") as f:
        return json.loads(f.read().decode("utf-8"))


# ============================================================
# TEST 1: artículo externo → visible para Chief (import)
# ============================================================
@pytest.mark.skipif(not _fastapi_disponible(),
                    reason="FastAPI no responde en :8000")
def test_external_article_imported(page: Page, sftp_session):
    """
    Simula que un autor externo envía un artículo.
    Luego el Chief lo importa con "Import from journal".
    """
    # 1. Verificar que existe el JSON (precondición)
    meta = _find_latest_external(sftp_session)
    art_id = meta["tracking_id"]
    print(f"\n🧪 Artículo externo a importar: {art_id}")

    # 2. Chief hace login e importa
    login(page, CHIEF_USER, CHIEF_PASSWORD, REVIEWERS_URL)
    import_btn = page.get_by_role(
        "button", name=re.compile("Import from journal", re.IGNORECASE)
    )
    if import_btn.count() > 0:
        expect(import_btn.first).to_be_visible(timeout=15000)
        import_btn.first.click()
        page.wait_for_timeout(4000)

    # 3. Verificar en CSV
    row = _wait_until(sftp_session, art_id, "estado", "en_revision")
    print(f"   ✅ Importado: {row['articulo_id']} — estado={row['estado']}")


# ============================================================
# TEST 2: flujo completo del externo (import → arbitraje → publicar)
# ============================================================
@pytest.mark.skipif(not _fastapi_disponible(),
                    reason="FastAPI no responde en :8000")
def test_external_full_flow(page: Page, sftp_session):
    """Import → asignar a luis → arbitraje → publicar."""
    meta = _find_latest_external(sftp_session)
    art_id = meta["tracking_id"]
    print(f"\n🧪 Flujo completo para {art_id}")

    # (Si aún no está en registro, se importa)
    from tests.conftest import get_articulo
    row = get_articulo(sftp_session, art_id)
    if not row:
        # Import
        login(page, CHIEF_USER, CHIEF_PASSWORD, REVIEWERS_URL)
        imp = page.get_by_role("button",
                                name=re.compile("Import from journal", re.IGNORECASE))
        if imp.count() > 0:
            imp.first.click()
            page.wait_for_timeout(4000)
        row = _wait_until(sftp_session, art_id, "estado", "en_revision")
        print(f"   Importado: {row['articulo_id']}")

    # Asignar a luis directamente en el CSV (el Chief lo haría en UI)
    if row.get("editor_asignado") != "luis":
        from seeds._common import (
            sftp_connect, editorial_dir, read_csv, write_csv,
        )
        print(f"   Asignando {art_id} a luis vía SFTP...")
        transport, sftp, rs = sftp_connect()
        ed = editorial_dir(rs)
        cols_full = [
            "articulo_id", "tipo", "titulo", "ruta_pdf", "ruta_cover",
            "fecha_creacion", "estado", "isbn", "motivo_retiro", "fecha_retiro",
            "idioma_original", "formato_original", "editor_asignado",
            "arbitraje_confirmado",
        ]
        df_full = read_csv(sftp, f"{ed}/registro_articulos.csv", cols_full)
        df_full.loc[df_full["articulo_id"] == art_id, "editor_asignado"] = "luis"
        write_csv(sftp, f"{ed}/registro_articulos.csv", df_full)
        sftp.close(); transport.close()
        print(f"   ✅ Asignado a luis")
        row["editor_asignado"] = "luis"

    # Luis confirma arbitraje
    login(page, LUIS_USER, LUIS_PASSWORD, REVIEWERS_URL)
    exp = _open_expander(page, art_id)
    btn = exp.get_by_role("button",
                          name=re.compile("Confirm arbitration", re.IGNORECASE))
    expect(btn.first).to_be_visible(timeout=10000)
    btn.first.click()
    _wait_until(sftp_session, art_id, "arbitraje_confirmado", "1")
    print(f"   ✅ Arbitraje confirmado")

    # Polanco: reload limpio + force-advance + publish
    print(f"   Recargando página y re-logueando (st.rerun es inestable)...")
    login(page, CHIEF_USER, CHIEF_PASSWORD, REVIEWERS_URL)
    page.reload()
    page.wait_for_timeout(2500)
    if page.get_by_label("Login").is_visible(timeout=3000):
        login(page, CHIEF_USER, CHIEF_PASSWORD, REVIEWERS_URL)
        page.wait_for_timeout(1500)

    # Force-advance (SOLO si aún no está en todos_aprobados)
    from tests.conftest import get_articulo
    row_now = get_articulo(sftp_session, art_id)
    if row_now["estado"] != "todos_aprobados":
        exp = _open_expander(page, art_id)
        force = exp.get_by_role(
            "button",
            name=re.compile("Advance to 'todos_aprobados' \\(force\\)",
                            re.IGNORECASE)
        )
        expect(force.first).to_be_visible(timeout=15000)
        force.first.click()
        _wait_until(sftp_session, art_id, "estado", "todos_aprobados")
        print(f"   ✅ Force-advance OK")
    else:
        print(f"   ℹ️  Ya estaba en todos_aprobados (auto-avance tras arbitraje)")

    # Reload otra vez para DOM limpio antes del publish
    page.reload()
    page.wait_for_timeout(2500)
    if page.get_by_label("Login").is_visible(timeout=3000):
        login(page, CHIEF_USER, CHIEF_PASSWORD, REVIEWERS_URL)
        page.wait_for_timeout(1500)

    # Publish
    exp = _open_expander(page, art_id)
    pub = exp.get_by_role("button", name=re.compile("PUBLISH", re.IGNORECASE))
    expect(pub.first).to_be_visible(timeout=20000)
    pub.first.click()
    _wait_until(sftp_session, art_id, "estado", "published")
    print(f"   ✅ Publicado")
