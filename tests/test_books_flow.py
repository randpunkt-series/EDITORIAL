"""
Test del flujo editorial completo de BOOKS.

Escenario A (book subido por luis):
  en_revision → (luis confirma arbitraje) → auto-avance a todos_aprobados (si ya no hay coautores pendientes)
              → (polanco publica) → published

Escenario B (book subido por polanco):
  en_revision → (polanco force-advance) → todos_aprobados
              → (polanco publica) → published
"""
import re
import time
import pytest
from playwright.sync_api import Page, expect

from tests.conftest import (
    REVIEWERS_URL, CHIEF_USER, CHIEF_PASSWORD, LUIS_USER, LUIS_PASSWORD,
    login, get_articulo, get_confirmaciones,
)


def _expander_for(page: Page, art_id: str):
    """Devuelve el locator del expander del producto por su ID."""
    return page.locator('[data-testid="stExpander"]').filter(
        has_text=re.compile(re.escape(art_id))
    ).first


def _open_expander(page: Page, art_id: str, timeout_ms: int = 15000):
    """
    Abre el expander del producto. Garantiza que el contenido interno
    (más allá del chevron del summary) sea visible.

    Estrategia:
      1. Localizar el expander.
      2. Ver si el CONTENIDO del expander está visible (más allá del summary).
         En Streamlit, el contenido está en un <div> hermano del <summary>.
      3. Si no lo está, clickear el summary.
      4. Esperar a que aparezca al menos un botón dentro del contenido.
    """
    exp = _expander_for(page, art_id)
    expect(exp).to_be_visible(timeout=15000)

    summary = exp.locator("summary")
    if summary.count() == 0:
        summary = exp.locator("> div").first

    # Detectar si el expander está abierto mirando el aria-expanded del summary
    def _is_expanded() -> bool:
        try:
            attr = summary.first.get_attribute("aria-expanded", timeout=1000)
            return attr == "true"
        except Exception:
            return False

    # Si no está expandido, clickear
    if not _is_expanded():
        summary.first.click(timeout=5000)
        page.wait_for_timeout(600)
        # Si aún no se expandió, reintentar vía JS
        if not _is_expanded():
            try:
                page.evaluate("(el) => el.click()", exp.element_handle())
            except Exception:
                pass
            page.wait_for_timeout(600)

    # Esperar a que aparezca el contenido (al menos un botón DENTRO
    # del expander, excluyendo el summary)
    exp.locator('[data-testid="stExpanderDetails"], div').first.wait_for(
        timeout=timeout_ms
    )
    return exp


def _click_action(page: Page, art_id: str, button_name_re: str):
    """Abre el expander y clickea el botón cuyo texto coincida con el regex."""
    exp = _open_expander(page, art_id)
    btn = exp.get_by_role("button",
                          name=re.compile(button_name_re, re.IGNORECASE))
    if btn.count() == 0:
        # Fallback: buscar por texto plano (Streamlit a veces envuelve el texto)
        btn = exp.locator(f"button:has-text('{button_name_re}')")
    expect(btn.first).to_be_visible(timeout=10000)
    btn.first.click()


def _wait_until_csv(sftp_session, art_id: str, field: str, expected: str,
                    timeout: float = 30.0):
    """Polling: espera a que el CSV remoto refleje el cambio."""
    start = time.time()
    last = None
    while time.time() - start < timeout:
        row = get_articulo(sftp_session, art_id)
        if row is None:
            time.sleep(0.5)
            continue
        last = row.get(field)
        if last == expected:
            return row
        time.sleep(0.8)
    raise AssertionError(
        f"Timeout esperando {field}={expected} en {art_id}. Último: {last}"
    )


def _wait_until_state_in(sftp_session, art_id: str, states: list,
                          timeout: float = 30.0):
    """Espera hasta que el estado esté en la lista dada."""
    start = time.time()
    last = None
    while time.time() - start < timeout:
        row = get_articulo(sftp_session, art_id)
        if row is None:
            time.sleep(0.5)
            continue
        last = row.get("estado")
        if last in states:
            return row
        time.sleep(0.8)
    raise AssertionError(
        f"Timeout esperando estado in {states} en {art_id}. Último: {last}"
    )


# ============================================================
# ESCENARIO A: luis confirma arbitraje
# ============================================================
def test_luis_confirma_arbitraje(page: Page, sftp_session, book_id_en_revision):
    """Luis confirma arbitraje → arbitraje_confirmado=1."""
    bid = book_id_en_revision
    assert bid
    print(f"\n🧪 Probando confirm arbitration en {bid}")

    row0 = get_articulo(sftp_session, bid)
    assert row0["arbitraje_confirmado"] == "0", f"Precondición: {row0}"
    print(f"   Estado inicial: {row0['estado']}, arb={row0['arbitraje_confirmado']}")

    login(page, LUIS_USER, LUIS_PASSWORD, REVIEWERS_URL)

    exp = _open_expander(page, bid)
    btn = exp.get_by_role("button",
                          name=re.compile("Confirm arbitration", re.IGNORECASE))
    if btn.count() == 0:
        pytest.fail("No aparece el botón 'Confirm arbitration'. "
                    "¿El book está asignado a luis? ¿Ya estaba confirmado?")
    expect(btn.first).to_be_visible(timeout=10000)
    btn.first.click()
    print("   Click en 'Confirm arbitration'")

    # Verificar SOLO por CSV (más fiable que el UI)
    row = _wait_until_csv(sftp_session, bid, "arbitraje_confirmado", "1")
    print(f"   ✅ CSV: arb={row['arbitraje_confirmado']}, estado={row['estado']}")


# ============================================================
# ESCENARIO A: polanco publica el book de luis
# ============================================================
def test_chief_publica(page: Page, sftp_session, book_id_en_revision):
    """Polanco fuerza avance (si hace falta) y publica el book de luis."""
    bid = book_id_en_revision

    login(page, CHIEF_USER, CHIEF_PASSWORD, REVIEWERS_URL)

    # Intentar force-advance si aún está en en_revision
    row = get_articulo(sftp_session, bid)
    if row["estado"] == "en_revision":
        exp = _open_expander(page, bid)
        force_btn = exp.get_by_role(
            "button",
            name=re.compile("Advance to 'todos_aprobados' \\(force\\)",
                            re.IGNORECASE)
        )
        if force_btn.count() > 0:
            expect(force_btn.first).to_be_visible(timeout=10000)
            force_btn.first.click()
            print(f"   Click en 'force advance' para {bid}")
            _wait_until_csv(sftp_session, bid, "estado", "todos_aprobados")
            # Dar tiempo a que Streamlit re-renderice tras el rerun
            page.wait_for_timeout(1500)
        else:
            pytest.fail(f"No aparece botón 'force advance' y estado es "
                        f"'{row['estado']}'")

    row = get_articulo(sftp_session, bid)
    assert row["estado"] == "todos_aprobados", f"Estado: {row['estado']}"
    print(f"   ✅ Estado: todos_aprobados")

    # Publicar
    exp = _open_expander(page, bid)
    page.wait_for_timeout(800)

    # Checkbox "Simulate Zenodo" (asegurar marcado)
    sim_cb = exp.get_by_role("checkbox",
                             name=re.compile("Simulate Zenodo", re.IGNORECASE))
    if sim_cb.count() > 0:
        if not sim_cb.first.is_checked():
            sim_cb.first.check()
        print("   ✅ Simulate Zenodo ON")

    pub_btn = exp.get_by_role("button", name=re.compile("PUBLISH", re.IGNORECASE))
    expect(pub_btn.first).to_be_visible(timeout=15000)
    pub_btn.first.click()
    print(f"   Click en 'PUBLISH'")

    row = _wait_until_csv(sftp_session, bid, "estado", "published")
    print(f"   ✅ Estado: published")


# ============================================================
# ESCENARIO B: book subido por polanco
# ============================================================
def test_chief_force_advance_y_publica(page: Page, sftp_session, book_id_en_revision_chief):
    """Chief force-avanza un book suyo y lo publica."""
    bid = book_id_en_revision_chief
    print(f"\n🧪 Probando force-advance + publish para {bid}")

    login(page, CHIEF_USER, CHIEF_PASSWORD, REVIEWERS_URL)

    row = get_articulo(sftp_session, bid)
    print(f"   Estado inicial: {row['estado']}")

    # PASO 1: force-advance (si está en en_revision)
    if row["estado"] == "en_revision":
        exp = _open_expander(page, bid)
        force_btn = exp.get_by_role(
            "button",
            name=re.compile("Advance to 'todos_aprobados' \\(force\\)",
                            re.IGNORECASE)
        )
        expect(force_btn.first).to_be_visible(timeout=15000)
        force_btn.first.click()
        print(f"   Click en 'force advance'")
        _wait_until_csv(sftp_session, bid, "estado", "todos_aprobados")

    row = get_articulo(sftp_session, bid)
    print(f"   Estado tras force-advance: {row['estado']}")
    assert row["estado"] == "todos_aprobados", f"Estado inesperado: {row['estado']}"

    # PASO 2: reload + re-login para DOM limpio
    print(f"   Recargando página + re-login...")
    page.reload()
    page.wait_for_timeout(3000)

    # Verificar si necesitamos re-loguearnos (Streamlit pierde sesión en reload)
    try:
        # ¿Estamos en la pantalla de login?
        login_visible = page.get_by_label("Login").is_visible(timeout=2000)
    except Exception:
        login_visible = False

    if login_visible:
        print(f"   Re-logueando...")
        login(page, CHIEF_USER, CHIEF_PASSWORD, REVIEWERS_URL)
        page.wait_for_timeout(2000)
    else:
        page.wait_for_selector("text=Manuscripts and books", timeout=20000)

    # PASO 3: abrir expander y publicar
    exp = _open_expander(page, bid)
    page.wait_for_timeout(1500)

    # Asegurar que el checkbox "Simulate Zenodo" está marcado
    sim_cb = exp.get_by_role("checkbox",
                             name=re.compile("Simulate Zenodo", re.IGNORECASE))
    if sim_cb.count() > 0:
        if not sim_cb.first.is_checked():
            sim_cb.first.check()
        print(f"   Simulate Zenodo ON")

    # Click PUBLISH
    pub_btn = exp.get_by_role("button", name=re.compile("PUBLISH", re.IGNORECASE))
    expect(pub_btn.first).to_be_visible(timeout=20000)
    pub_btn.first.click()
    print(f"   Click en 'PUBLISH'")

    # Verificar
    row = _wait_until_csv(sftp_session, bid, "estado", "published")
    print(f"   ✅ Estado final: {row['estado']}")



# ============================================================
# VERIFICAR que luis NO puede publicar
# ============================================================
def test_luis_no_ve_boton_publicar(page: Page, sftp_session, book_id_en_revision):
    """Luis (Associate) NO debe ver el botón PUBLISH."""
    bid = book_id_en_revision

    login(page, LUIS_USER, LUIS_PASSWORD, REVIEWERS_URL)

    exp = _open_expander(page, bid)
    page.wait_for_timeout(500)

    pub_btn = exp.get_by_role("button", name=re.compile("PUBLISH", re.IGNORECASE))
    assert pub_btn.count() == 0, "Luis NO debe ver el botón PUBLISH"
    print(f"✅ Luis no ve PUBLISH en {bid}")
