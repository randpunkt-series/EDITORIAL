"""
workflow.py — Lógica única de transiciones de estado para productos
(artículos y libros) del sistema RandPunkt.

Modelo minimalista de 3 estados:
    en_revision  →  todos_aprobados  →  published
                          ↘
                        retirado
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

STATE_EN_REVISION     = "en_revision"
STATE_TODOS_APROBADOS = "todos_aprobados"
STATE_PUBLISHED       = "published"
STATE_RETIRADO        = "retirado"

VALID_STATES = {
    STATE_EN_REVISION, STATE_TODOS_APROBADOS,
    STATE_PUBLISHED, STATE_RETIRADO,
}

ARBITRAJE_COL = "arbitraje_confirmado"


@dataclass
class TransitionResult:
    ok: bool
    message: str
    new_state: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None


def _find_row(df, art_id):
    if df.empty:
        return None
    m = df[df["articulo_id"] == art_id]
    return m.iloc[0] if not m.empty else None


def _simulated_doi(art_id):
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"10.5281/zenodo.test-{art_id.lower()}-{stamp}"


def _simulated_url(art_id):
    return f"https://zenodo.org/records/test-{art_id.lower()}"


def pending_coauthors(confirmations, art_id):
    if confirmations.empty:
        return confirmations
    sub = confirmations[confirmations["articulo_id"] == art_id]
    return sub[sub["aprobado"] != "1"]


def has_coauthors(confirmations, art_id):
    if confirmations.empty:
        return False
    return not confirmations[confirmations["articulo_id"] == art_id].empty


def is_arbitraje_confirmado(art_row):
    val = str(art_row.get(ARBITRAJE_COL, "")).strip().lower()
    return val in {"1", "true", "yes", "si", "sí"}


def can_advance_to_todos_aprobados(art_row, confirmations, *, force=False):
    art_id = art_row["articulo_id"]
    current = art_row.get("estado", "")
    if current == STATE_TODOS_APROBADOS:
        return TransitionResult(False, f"{art_id} ya está en 'todos_aprobados'.")
    if current == STATE_PUBLISHED:
        return TransitionResult(False, f"{art_id} ya está publicado.")
    if current == STATE_RETIRADO:
        return TransitionResult(False, f"{art_id} está retirado.")
    if current != STATE_EN_REVISION:
        return TransitionResult(False, f"Estado actual '{current}' no permite avanzar.")
    if force:
        return TransitionResult(True, "Forzado por el Chief.")
    if not is_arbitraje_confirmado(art_row):
        return TransitionResult(False, "El arbitraje aún no ha sido confirmado por un editor.")
    pend = pending_coauthors(confirmations, art_id)
    if not pend.empty:
        emails = ", ".join(pend["email"].tolist())
        return TransitionResult(False, f"{len(pend)} coautor(es) pendientes: {emails}")
    return TransitionResult(True, "Listo para 'todos_aprobados'.")


def confirm_arbitraje(df_art, art_id):
    row = _find_row(df_art, art_id)
    if row is None:
        return TransitionResult(False, f"Producto '{art_id}' no encontrado.")
    if row.get("estado") != STATE_EN_REVISION:
        return TransitionResult(False, f"No se puede confirmar arbitraje en estado '{row.get('estado')}'.")
    if is_arbitraje_confirmado(row):
        return TransitionResult(False, "El arbitraje ya estaba confirmado.")
    idx = df_art.index[df_art["articulo_id"] == art_id]
    df_art.loc[idx, ARBITRAJE_COL] = "1"
    return TransitionResult(True, "Arbitraje confirmado.")


def force_advance_to_todos_aprobados(df_art, art_id):
    row = _find_row(df_art, art_id)
    if row is None:
        return TransitionResult(False, f"Producto '{art_id}' no encontrado.")
    if row.get("estado") == STATE_TODOS_APROBADOS:
        return TransitionResult(False, "Ya estaba en 'todos_aprobados'.")
    if row.get("estado") != STATE_EN_REVISION:
        return TransitionResult(False, f"No se puede avanzar desde '{row.get('estado')}'.")
    idx = df_art.index[df_art["articulo_id"] == art_id]
    df_art.loc[idx, "estado"] = STATE_TODOS_APROBADOS
    df_art.loc[idx, ARBITRAJE_COL] = "1"
    return TransitionResult(True, f"{art_id} avanzado a 'todos_aprobados' (forzado).",
                            new_state=STATE_TODOS_APROBADOS)


def try_auto_advance_after_signature(df_art, df_conf, art_id):
    row = _find_row(df_art, art_id)
    if row is None:
        return TransitionResult(False, f"Producto '{art_id}' no encontrado.")
    if row.get("estado") != STATE_EN_REVISION:
        return TransitionResult(False, f"Estado actual '{row.get('estado')}'; no se auto-avanza.")
    check = can_advance_to_todos_aprobados(row, df_conf)
    if not check.ok:
        return check
    idx = df_art.index[df_art["articulo_id"] == art_id]
    df_art.loc[idx, "estado"] = STATE_TODOS_APROBADOS
    return TransitionResult(True, f"{art_id} avanzado a 'todos_aprobados'.",
                            new_state=STATE_TODOS_APROBADOS)


def publish(df_art, art_id, *, simulate=True, real_publisher=None):
    row = _find_row(df_art, art_id)
    if row is None:
        return TransitionResult(False, f"Producto '{art_id}' no encontrado.")
    if row.get("estado") == STATE_PUBLISHED:
        return TransitionResult(False, "Ya está publicado.")
    if row.get("estado") != STATE_TODOS_APROBADOS:
        return TransitionResult(False, f"Solo se publica desde 'todos_aprobados' (actual: '{row.get('estado')}').")
    if simulate:
        doi = _simulated_doi(art_id)
        url = _simulated_url(art_id)
        ok = True
    else:
        if real_publisher is None:
            return TransitionResult(False, "real_publisher no configurado.")
        ok, doi, url = real_publisher(art_id)
        if not ok:
            return TransitionResult(False, f"Zenodo falló: {doi}")
    idx = df_art.index[df_art["articulo_id"] == art_id]
    df_art.loc[idx, "estado"] = STATE_PUBLISHED
    return TransitionResult(True, f"{art_id} publicado con DOI {doi}.",
                            new_state=STATE_PUBLISHED, doi=doi, url=url)


def retire(df_art, art_id, *, motivo):
    row = _find_row(df_art, art_id)
    if row is None:
        return TransitionResult(False, f"Producto '{art_id}' no encontrado.")
    if row.get("estado") == STATE_RETIRADO:
        return TransitionResult(False, "Ya está retirado.")
    if not re.match(r"^[a-z_]+$", motivo or ""):
        return TransitionResult(False, "Motivo inválido.")
    idx = df_art.index[df_art["articulo_id"] == art_id]
    df_art.loc[idx, "estado"] = STATE_RETIRADO
    df_art.loc[idx, "motivo_retiro"] = motivo
    df_art.loc[idx, "fecha_retiro"] = datetime.now().isoformat(timespec="seconds")
    return TransitionResult(True, f"{art_id} retirado ({motivo}).", new_state=STATE_RETIRADO)


def restore(df_art, art_id):
    row = _find_row(df_art, art_id)
    if row is None:
        return TransitionResult(False, f"Producto '{art_id}' no encontrado.")
    if row.get("estado") != STATE_RETIRADO:
        return TransitionResult(False, "Solo se restaura un retirado.")
    idx = df_art.index[df_art["articulo_id"] == art_id]
    df_art.loc[idx, "estado"] = STATE_EN_REVISION
    df_art.loc[idx, "motivo_retiro"] = ""
    df_art.loc[idx, "fecha_retiro"] = ""
    df_art.loc[idx, ARBITRAJE_COL] = "0"
    return TransitionResult(True, f"{art_id} restaurado.", new_state=STATE_EN_REVISION)
