"""
Test del envío de emails + flujo de firma manual.

NOTA: desde la introducción de `auto_send_all` en coauthors.py, el test
NO necesita marcar los checkboxes manualmente (Streamlit los renderiza
como <label> con <input> oculto, difícil de clickear de forma fiable
desde Playwright). Solo hace click en "Send approval requests".
"""
import re
import time
import pytest
from pathlib import Path
from playwright.sync_api import Page, expect

from tests.conftest import (
    REVIEWERS_URL, COAUTHORS_URL,
    CHIEF_USER, CHIEF_PASSWORD,
    login, get_articulo, get_confirmaciones,
)
from utils.tokens_manager import register_token, RUN_ID
from utils.wait_for_human_signature import wait_for_signature


def _find_book_asignado_a_luis(sftp_session) -> str:
    from tests.conftest import find_by_prefix
    row = find_by_prefix(sftp_session, "BK-2026-", estado="en_revision")
    if not row:
        pytest.skip("No hay book en en_revision asignado a luis.")
    return row["articulo_id"]


def _goto_coauthors_and_login(page: Page, user: str, password: str):
    """Navega a :8502 y garantiza sesión activa. Reintenta hasta 2 veces."""
    page.goto(COAUTHORS_URL)
    page.wait_for_timeout(3000)

    for attempt in range(1, 3):
        try:
            login_input = page.get_by_label("Login")
            is_login = login_input.is_visible(timeout=2000)
        except Exception:
            is_login = False

        if not is_login:
            break

        print(f"   Intento {attempt}/2: re-login en coauthors.py...")
        try:
            login_input.fill(user)
            page.get_by_role("textbox", name="Password").fill(password)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_timeout(4000)
        except Exception as e:
            print(f"   Error en re-login: {e}")
            page.wait_for_timeout(2000)

    try:
        page.wait_for_selector("h1", timeout=15000)
    except Exception:
        pass

    page.wait_for_timeout(1000)
    h1_text = page.locator("h1").first.text_content() or ""
    print(f"   H1: {h1_text!r}")

    if "Editorial Panel" in h1_text or "Sign in" in h1_text:
        page.screenshot(path="/tmp/coauthors_still_login.png", full_page=True)
        pytest.fail("Sigue en login.")


def test_send_approval_email(page: Page, sftp_session):
    """Envía invitación de aprobación al coautor (auto_send_all activado)."""
    bid = _find_book_asignado_a_luis(sftp_session)
    print(f"\nBook a aprobar: {bid}")

    login(page, CHIEF_USER, CHIEF_PASSWORD, REVIEWERS_URL)
    _goto_coauthors_and_login(page, CHIEF_USER, CHIEF_PASSWORD)

    # Esperar a que el combobox esté listo
    page.wait_for_timeout(2000)
    select = page.get_by_role("combobox")
    if select.count() > 0:
        try:
            select.first.click(timeout=5000)
            page.wait_for_timeout(800)
        except Exception:
            pass
        option = page.get_by_role("option", name=re.compile(re.escape(bid)))
        if option.count() > 0:
            option.first.click()
            page.wait_for_timeout(1500)
            print(f"   Book seleccionado: {bid}")
        else:
            print(f"   Opcion {bid} no encontrada; usando la primera")
            page.keyboard.press("Escape")

    # Esperar a que aparezca el botón "Send approval requests"
    print("   Esperando a que aparezca 'Send approval requests'...")
    page.wait_for_selector(
        'button:has-text("Send approval requests")',
        timeout=20000,
    )
    print("   ✅ Botón visible")

    # Click en Send
    send_btn = page.get_by_role(
        "button", name=re.compile("Send approval requests", re.IGNORECASE)
    )
    expect(send_btn.first).to_be_visible(timeout=10000)
    send_btn.first.click()
    print(f"   Click en 'Send approval requests'")

    # Polling: esperar hasta 60s a que aparezcan confirmaciones
    print("   Esperando a que se creen las confirmaciones (SMTP tarda)...")
    confs = []
    start = time.time()
    while time.time() - start < 60:
        confs = get_confirmaciones(sftp_session, bid)
        if confs:
            elapsed = int(time.time() - start)
            print(f"   ✅ {len(confs)} confirmaciones creadas ({elapsed}s)")
            break
        print(f"      … esperando ({(int(time.time()-start))}s)", end="\r")
        time.sleep(2)

    if not confs:
        page.screenshot(path="/tmp/email_send_fail.png", full_page=True)
        pytest.fail(
            f"Timeout (60s): no se crearon confirmaciones. "
            f"Screenshot: /tmp/email_send_fail.png"
        )

    n_pending = 0
    for c in confs:
        if c["aprobado"] == "0":
            n_pending += 1
            register_token(
                art_id=bid,
                email=c["email"],
                name=c["nombre"],
                token=c["token"],
                base_url=COAUTHORS_URL,
                run_id=RUN_ID,
            )
    print(f"   ✅ {len(confs)} confirmaciones ({n_pending} pendientes)")
    print(f"   Tokens en /tmp/tokens_test_{RUN_ID}.json")
    print(f"\n   Revisa la bandeja de los coautores:")
    for c in confs:
        print(f"      - {c['nombre']} <{c['email']}>")


def test_wait_for_manual_signature(sftp_session):
    """Espera a que el operador humano firme el link."""
    from utils.tokens_manager import load_all
    tokens = load_all(RUN_ID)
    if not tokens:
        pytest.skip("No hay tokens registrados.")

    target = None
    for t in tokens:
        row = get_articulo(sftp_session, t["art_id"])
        if not row:
            continue
        if row.get("estado") == "en_revision" and row.get("arbitraje_confirmado") == "1":
            target = t
            break

    if not target:
        pytest.skip("No hay books con arbitraje confirmado y pendientes de firma.")

    print(f"\nEsperando firma para {target['art_id']}")
    print(f"   Email: {target['email']}")
    print(f"   URL:   {target['url']}")

    ok = wait_for_signature(
        art_id=target["art_id"],
        expected_email=target["email"],
        timeout_sec=300,
    )
    if not ok:
        pytest.fail("Timeout esperando firma.")

    row = get_articulo(sftp_session, target["art_id"])
    assert row["estado"] == "todos_aprobados", (
        f"Esperaba todos_aprobados, obtuve {row['estado']}"
    )
    print(f"   Auto-avance a todos_aprobados OK")
