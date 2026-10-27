"""
Test del flujo multi-coautor para BOOKS.

Cubre:
  - Coautor 1 firma (no avanza si arbitraje pendiente)
  - Coautor 2 firma + arbitraje confirmado → auto-avance
  - Idempotencia: recargar no duplica
  - Token inválido → error
"""
import re
import pytest
from playwright.sync_api import Page, expect

from tests.conftest import (
    COAUTHORS_URL, get_articulo, get_confirmaciones, tokens_book_id,
)
from seeds._common import generate_token


def _go_to_token(page: Page, token: str):
    page.goto(f"{COAUTHORS_URL}/?token={token}")
    page.wait_for_selector("text=Manuscript Approval", timeout=20000)


def _sign(page: Page, comentario: str):
    cb = page.get_by_text(
        "I have read the manuscript in full and I agree with its content for publication."
    )
    expect(cb).to_be_visible(timeout=15000)
    cb.click()
    page.get_by_label("Optional comments for the editor").fill(comentario)
    btn = page.get_by_role("button",
                           name=re.compile("Confirm approval", re.IGNORECASE))
    expect(btn).to_be_visible(timeout=10000)
    btn.click()
    expect(page.get_by_text("Thank you! Your approval has been recorded.")
           ).to_be_visible(timeout=15000)


def test_firma_coautor_1(page: Page, sftp_session, tokens_book_id):
    """Coautor 1 firma; el book NO debe avanzar (arbitraje pendiente)."""
    bid, tokens = tokens_book_id
    if len(tokens) < 2:
        pytest.skip("El seed no creó 2 coautores")

    # Tomar el primer coautor (Carlos Polanco)
    email1 = "polanco@unam.mx"
    assert email1 in tokens, f"Token de {email1} no encontrado"

    _go_to_token(page, tokens[email1])
    _sign(page, "Coautor 1 firma")

    # Verificar en CSV
    confs = get_confirmaciones(sftp_session, bid)
    by_email = {c["email"]: c for c in confs}
    assert by_email[email1]["aprobado"] == "1"
    # El estado NO debe ser todos_aprobados (falta arbitraje y coautor 2)
    row = get_articulo(sftp_session, bid)
    assert row["estado"] == "en_revision", f"Esperaba en_revision, obtuve {row['estado']}"
    print(f"✅ Coautor 1 firmó; estado sigue en_revision")


def test_firma_coautor_2(page: Page, sftp_session, tokens_book_id):
    """Coautor 2 firma; estado sigue en_revision (arbitraje pendiente)."""
    bid, tokens = tokens_book_id
    email2 = "cpg7000cpg@gmail.com"
    assert email2 in tokens

    _go_to_token(page, tokens[email2])
    _sign(page, "Coautor 2 firma")

    confs = get_confirmaciones(sftp_session, bid)
    assert all(c["aprobado"] == "1" for c in confs), confs
    # Aún en en_revision porque arbitraje no está confirmado
    row = get_articulo(sftp_session, bid)
    assert row["estado"] == "en_revision"
    print(f"✅ Coautor 2 firmó; estado sigue en_revision (falta arbitraje)")


def test_idempotente(page: Page, sftp_session, tokens_book_id):
    """Recargar el token del coautor 1 no duplica confirmaciones."""
    bid, tokens = tokens_book_id
    email1 = "polanco@unam.mx"

    before = len(get_confirmaciones(sftp_session, bid))
    _go_to_token(page, tokens[email1])
    # Debe mostrar "already approved"
    expect(page.get_by_text(re.compile("already approved", re.IGNORECASE))
           ).to_be_visible(timeout=15000)
    after = len(get_confirmaciones(sftp_session, bid))
    assert before == after
    print(f"✅ Idempotente: {before} confirmaciones antes y después")


def test_token_invalido(page: Page):
    """Token basura → error."""
    page.goto(f"{COAUTHORS_URL}/?token=esto.no.es.valido")
    expect(page.get_by_text(re.compile("invalid|tampered|expired", re.IGNORECASE))
           ).to_be_visible(timeout=15000)
    print("✅ Token inválido rechazado")
