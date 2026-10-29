"""
RandPunkt — Public coauthor approval view.

Única función: mostrar el PDF de la última versión y registrar la aprobación
del coautor. NO gestiona manuscritos, NO publica, NO envía correos.

Fuente de verdad:
  - PDF: JSON de metadata (versions[-1].pdf), leído por SFTP con fallback de rutas.
  - Registro del coautor: confirmaciones.csv, buscado por EMAIL y ARTÍCULO (no por token).
"""
from __future__ import annotations

import io
import json
import re
import time
from datetime import datetime
from io import StringIO

import pandas as pd
import paramiko
import streamlit as st
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature

import workflow as wf

st.set_page_config(
    page_title="RandPunkt — Approval",
    page_icon="randpunkt-favicon.png",
    layout="wide",
)

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
COLS_VERSIONES = [
    "articulo_id", "fecha", "ruta_pdf", "nombre_archivo", "notas",
]

TOKEN_MAX_AGE_SEC = 30 * 86400
LOCK_TIMEOUT_SEC = 30
LOCK_RETRY_SEC = 0.5

# ============================================================
# UTILIDADES
# ============================================================
def _log_error(context: str, exc: Exception):
    print(f"[ERROR] {context}: {type(exc).__name__}: {exc}")

def _normalize_email(e: str) -> str:
    return (e or "").strip().lower().replace("<", "").replace(">", "")

def _normalize_id(s: str) -> str:
    return (s or "").strip()

# ============================================================
# SFTP
# ============================================================
def _sftp_connect():
    rs = st.secrets["remote_server"]
    transport = paramiko.Transport((rs["host"], int(rs["port"])))
    key = paramiko.Ed25519Key.from_private_key(StringIO(rs["private_key"]))
    transport.connect(username=rs["user"], pkey=key)
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp

def _remote_path(filename):
    rs = st.secrets["remote_server"]
    return f"{rs['dir'].rstrip('/')}/{filename}"

def _revista_base() -> str:
    rs = st.secrets["remote_server"]
    return rs.get("revista_dir", rs["dir"].rsplit("/editorial", 1)[0]).rstrip("/")

def _ensure_remote_dir(sftp, path: str):
    parts = path.rstrip("/").split("/")
    current = ""
    for p in parts:
        if not p:
            continue
        current += "/" + p
        try:
            sftp.stat(current)
        except IOError:
            try:
                sftp.mkdir(current)
            except OSError as e:
                _log_error(f"mkdir {current}", e)

# ============================================================
# LOCKS (Solo para el auto-avance, no para confirmaciones)
# ============================================================
def _lock_path(name: str) -> str:
    rs = st.secrets["remote_server"]
    return f"{rs['dir'].rstrip('/')}/.locks/{name}.lock"

def _acquire_lock(name: str, timeout: int = LOCK_TIMEOUT_SEC) -> bool:
    try:
        transport, sftp = _sftp_connect()
        lock_dir = _lock_path(name).rsplit("/", 1)[0]
        _ensure_remote_dir(sftp, lock_dir)
        path = _lock_path(name)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                f = sftp.open(path, "x")
                f.write(
                    json.dumps({
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "src": "coauthors",
                    }).encode("utf-8")
                )
                f.close()
                sftp.close(); transport.close()
                return True
            except IOError:
                time.sleep(LOCK_RETRY_SEC)
        sftp.close(); transport.close()
        return False
    except Exception as e:
        _log_error(f"acquire lock {name}", e)
        return False

def _release_lock(name: str):
    try:
        transport, sftp = _sftp_connect()
        path = _lock_path(name)
        try:
            sftp.remove(path)
        except IOError:
            pass
        sftp.close(); transport.close()
    except Exception as e:
        _log_error(f"release lock {name}", e)

def _break_stale_lock(name: str, max_age_sec: int = 600):
    try:
        transport, sftp = _sftp_connect()
        path = _lock_path(name)
        try:
            attr = sftp.stat(path)
            if time.time() - attr.st_mtime > max_age_sec:
                sftp.remove(path)
        except IOError:
            pass
        sftp.close(); transport.close()
    except Exception as e:
        _log_error(f"break stale lock {name}", e)

# ============================================================
# RESOLUCIÓN DE RUTAS CON FALLBACK
# ============================================================
def _candidate_paths(filename: str, kind: str) -> list[str]:
    rs = st.secrets["remote_server"]
    base = _revista_base()
    editorial = rs["dir"].rstrip("/")
    candidates = [f"{editorial}/versiones/{filename}"]
    if kind == "pdf":
        candidates += [
            f"{base}/PDF/{filename}",
            f"{base}/BOOKS/pdf/{filename}",
            f"{base}/BOOKS/PDF/{filename}",
        ]
    else:
        candidates += [
            f"{base}/ZIP/{filename}",
            f"{base}/BOOKS/zip/{filename}",
            f"{base}/BOOKS/ZIP/{filename}",
        ]
    return candidates

def _resolve_remote_file(sftp, filename: str, kind: str) -> str | None:
    for path in _candidate_paths(filename, kind):
        try:
            sftp.stat(path)
            return path
        except IOError:
            continue
    return None

def _download_remote_bytes(filename: str, kind: str) -> bytes | None:
    try:
        transport, sftp = _sftp_connect()
        path = _resolve_remote_file(sftp, filename, kind)
        if not path:
            sftp.close(); transport.close()
            return None
        with sftp.open(path, "rb") as f:
            data = f.read()
        sftp.close(); transport.close()
        return data
    except Exception as e:
        _log_error(f"download {filename}", e)
        return None

# ============================================================
# CSV
# ============================================================
def read_remote_csv(filename, columns):
    try:
        transport, sftp = _sftp_connect()
        path = _remote_path(filename)
        try:
            with sftp.open(path, "r") as f:
                content = f.read().decode("utf-8")
            df = pd.read_csv(io.StringIO(content), dtype=str).fillna("")
            for c in columns:
                if c not in df.columns:
                    df[c] = ""
            df = df[columns]
        except FileNotFoundError:
            df = pd.DataFrame(columns=columns)
        sftp.close(); transport.close()
        return df
    except Exception as e:
        _log_error(f"read {filename}", e)
        st.error(f"Error reading {filename}: {e}")
        return pd.DataFrame(columns=columns)

def save_remote_csv(filename, df, use_global_lock: bool = False):
    """
    Guarda un CSV de forma atómica.
    use_global_lock ya no se usa (siempre False).
    """
    try:
        transport, sftp = _sftp_connect()
        path = _remote_path(filename)
        tmp = f"{path}.tmp"
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = f"{path}.{ts}.bak"
            with sftp.open(path, "r") as src:
                with sftp.open(backup, "w") as dst:
                    dst.write(src.read())
        except IOError:
            pass
        with sftp.open(tmp, "w") as f:
            f.write(df.to_csv(index=False).encode("utf-8"))
        try:
            sftp.remove(path)
        except IOError:
            pass
        sftp.rename(tmp, path)
        sftp.close(); transport.close()
        return True
    except Exception as e:
        _log_error(f"save {filename}", e)
        st.error(f"Error saving {filename}: {e}")
        return False

def read_articles():
    return read_remote_csv(st.secrets["csv"]["articulos_file"], COLS_ARTICULOS)

def save_articles(df):
    return save_remote_csv(st.secrets["csv"]["articulos_file"], df)

def read_confirmations():
    return read_remote_csv(st.secrets["csv"]["confirmaciones_file"], COLS_CONFIRMACIONES)

def save_confirmations(df):
    """Escritura de confirmaciones.csv sin lock."""
    return save_remote_csv(
        st.secrets["csv"]["confirmaciones_file"],
        df,
        use_global_lock=False,
    )

# ============================================================
# JSON
# ============================================================
def get_submission(tracking_id):
    try:
        transport, sftp = _sftp_connect()
        base = _revista_base()
        for sub in [f"{base}/METADATA", f"{base}/BOOKS/metadata"]:
            try:
                with sftp.open(f"{sub}/{tracking_id}.json", "r") as f:
                    data = json.loads(f.read().decode("utf-8"))
                sftp.close(); transport.close()
                return data
            except FileNotFoundError:
                continue
        sftp.close(); transport.close()
        return None
    except Exception as e:
        _log_error(f"get_submission {tracking_id}", e)
        return None

# ============================================================
# TOKENS
# ============================================================
def get_serializer():
    return URLSafeTimedSerializer(st.secrets["app"]["secret_key"])

def validate_token(token):
    try:
        data = get_serializer().loads(
            token, salt="aprobacion-autor", max_age=TOKEN_MAX_AGE_SEC
        )
        return data, None
    except SignatureExpired:
        return None, "The link has expired (more than 30 days)."
    except BadTimeSignature:
        return None, "The link is invalid or has been tampered with."
    except Exception as e:
        _log_error("validate_token", e)
        return None, "The link could not be validated."

# ============================================================
# AUTO-AVANCE
# ============================================================
def _advance_if_all_approved(article_id: str) -> tuple[bool, str]:
    _break_stale_lock(article_id)
    lock_ok = _acquire_lock(article_id)
    if not lock_ok:
        return False, "Lock ocupado; reintenta en unos segundos."
    try:
        df_conf = read_confirmations()
        conf = df_conf[df_conf["articulo_id"] == article_id]
        if conf.empty:
            return False, "Sin coautores."
        if not (conf["aprobado"] == "1").all():
            pend = conf[conf["aprobado"] != "1"]
            return False, f"Pendientes: {len(pend)}."
        df_art = read_articles()
        mask = df_art["articulo_id"] == article_id
        if not mask.any():
            return False, "Artículo no encontrado."
        estado = df_art.loc[mask, "estado"].iloc[0]
        if estado == wf.STATE_TODOS_APROBADOS:
            return False, "Ya estaba en todos_aprobados."
        if estado != wf.STATE_EN_REVISION:
            return False, f"Estado '{estado}' no permite avance automático."
        df_art.loc[mask, "estado"] = wf.STATE_TODOS_APROBADOS
        df_art.loc[mask, "arbitraje_confirmado"] = "1"
        if save_articles(df_art):
            return True, "Todos firmaron. Estado → todos_aprobados."
        return False, "No se pudo guardar el estado."
    finally:
        _release_lock(article_id)

# ============================================================
# AUTHOR VIEW
# ============================================================
def author_view(token: str):
    st.title("✍️ Manuscript Approval")

    # 1) Validar token
    data, error = validate_token(token)
    if error:
        st.error(error)
        return

    payload_email = _normalize_email(data.get("email", ""))
    payload_aid = _normalize_id(data.get("articulo_id", ""))
    payload_name = data.get("nombre", "")

    if not payload_email or not payload_aid:
        st.error("The link is missing required data.")
        return

    # 2) Verificar que el registro existe (Búsqueda por email y artículo)
    df_conf = read_confirmations()
    
    # Normalizar las columnas del DataFrame para hacer una búsqueda robusta
    df_conf["email_norm"] = df_conf["email"].apply(_normalize_email)
    df_conf["aid_norm"] = df_conf["articulo_id"].apply(_normalize_id)
    
    row = df_conf[(df_conf["email_norm"] == payload_email) & (df_conf["aid_norm"] == payload_aid)]
    
    if row.empty:
        st.error(
            "Your record was not found. Please check that you are using the correct link "
            "or contact the editor."
        )
        return

    conf_row = row.iloc[0]
    record_aid = _normalize_id(conf_row["articulo_id"])
    record_name = conf_row["nombre"] or payload_name

    # 3) Artículo
    df_art = read_articles()
    art_row = df_art[df_art["articulo_id"] == record_aid]
    if art_row.empty:
        st.error("The associated manuscript no longer exists.")
        return
    art = art_row.iloc[0]

    st.write(f"Dear **{record_name}**, you have been invited to approve:")
    st.markdown(f"### {art['titulo']}")
    st.caption(f"Reference: {record_aid}")

    # 4) PDF de la última versión (con fallback)
    st.markdown("### Manuscript File")
    submission_data = get_submission(record_aid)
    if not submission_data:
        st.warning("⚠️ Metadata JSON not found. Please contact the editor.")
    else:
        versions_list = submission_data.get("versions", [])
        if not versions_list:
            st.warning("⚠️ No file uploaded yet. Please contact the editor.")
        else:
            latest = versions_list[-1]
            pdf_name = latest.get("pdf", "")
            if not pdf_name:
                st.warning("⚠️ No PDF registered in the latest version.")
            else:
                pdf_bytes = _download_remote_bytes(pdf_name, "pdf")
                if pdf_bytes:
                    st.download_button(
                        "⬇️ Download Manuscript (PDF)",
                        data=pdf_bytes,
                        file_name=pdf_name,
                        mime="application/pdf",
                        use_container_width=True,
                    )
                    st.caption(
                        f"Version: v{latest.get('version', 1)} — "
                        f"{latest.get('notes', '')}"
                    )
                else:
                    st.error(
                        "Could not load the manuscript file. "
                        "Please contact the editor to re-upload it."
                    )

    # 5) Si ya aprobó, no permitir re-aprobar
    if conf_row["aprobado"] == "1":
        st.success(
            f"✅ You already approved this on {conf_row['fecha_aprobacion']}."
        )
        return

    # 6) Formulario
    with st.form("approval_form"):
        st.write("Please confirm:")
        accept = st.checkbox(
            "I have read the manuscript in full and I agree with its content for publication."
        )
        comments = st.text_area("Optional comments for the editor", height=120)
        submit = st.form_submit_button("✅ Confirm approval", use_container_width=True)

    if not submit:
        return

    if not accept:
        st.warning("You must check the box to approve.")
        return

    # 7) Guardar aprobación
    df_conf2 = read_confirmations()
    df_conf2["email_norm"] = df_conf2["email"].apply(_normalize_email)
    df_conf2["aid_norm"] = df_conf2["articulo_id"].apply(_normalize_id)
    
    mask2 = (df_conf2["email_norm"] == payload_email) & (df_conf2["aid_norm"] == payload_aid)
    
    if not mask2.any():
        st.error("Your record disappeared while approving. Please contact the editor.")
        return

    df_conf2.loc[mask2, "aprobado"] = "1"
    df_conf2.loc[mask2, "fecha_aprobacion"] = datetime.now().isoformat(timespec="seconds")
    df_conf2.loc[mask2, "comentarios"] = comments or ""

    # Limpiamos las columnas temporales antes de guardar
    df_conf2 = df_conf2.drop(columns=["email_norm", "aid_norm"])

    if not save_confirmations(df_conf2):
        st.error("Could not save your approval. Please try again.")
        return

    # 8) Revocar token (dejar vacío)
    df_conf3 = read_confirmations()
    df_conf3["email_norm"] = df_conf3["email"].apply(_normalize_email)
    df_conf3["aid_norm"] = df_conf3["articulo_id"].apply(_normalize_id)
    
    mask3 = (df_conf3["email_norm"] == payload_email) & (df_conf3["aid_norm"] == payload_aid)
    if mask3.any():
        df_conf3.loc[mask3, "token"] = ""
        df_conf3 = df_conf3.drop(columns=["email_norm", "aid_norm"])
        save_confirmations(df_conf3)

    st.success("Thank you! Your approval has been recorded.")
    st.balloons()

    # 9) Auto-avance
    advanced, msg = _advance_if_all_approved(record_aid)
    if advanced:
        st.info("✅ All coauthors have signed. The item has advanced to 'todos_aprobados'.")
    else:
        df_conf_final = read_confirmations()
        pend = df_conf_final[
            (df_conf_final["articulo_id"] == record_aid)
            & (df_conf_final["aprobado"] != "1")
        ]
        if not pend.empty:
            st.info(f"⏳ {len(pend)} coauthor(s) still pending.")
        else:
            st.info(f"ℹ️ {msg}")

# ============================================================
# MAIN
# ============================================================
def main():
    token = st.query_params.get("token")
    if token:
        author_view(token)
    else:
        st.info("This page is only accessible via a personal approval link.")

if __name__ == "__main__":
    main()
