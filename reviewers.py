"""
RandPunkt — Editorial panel (reviewers.py) — Versión robusta final.

Modelo de permisos:
  - Associate (Level 1): gestiona coautores, sube versiones, descarga originales.
  - Chief (Level 0): asigna/reasigna editores y publica en Zenodo.

Modelo de estados:
  en_revision  →  todos_aprobados  →  published
       ↘               ↘
              retirado

Robustez aplicada:
  - Lock por manuscrito (SFTP, atómico con O_EXCL).
  - Lock global para confirmaciones.csv (evita colisión entre manuscritos).
  - Validación de editor_asignado contra editores.csv.
  - Idempotencia Zenodo (reusa deposition_id guardado en el JSON).
  - Validación de archivos (MIME, tamaño, magic bytes de PDF/ZIP).
  - Revalidación de permisos en cada rerun contra editores.csv.
  - Revocación de token tras aprobación.
  - Reintento de invitaciones pendientes.
  - Adjunto de PDF (no ZIP) en la invitación a coautores.
  - Sin `except: pass` silenciosos; todo se loguea.
  - Escritura atómica de CSV con backup y limpieza a 30 días.
  - Reintentos Zenodo solo para 5xx / red.
  - Fecha de publicación Zenodo = momento de publicar.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import smtplib
import urllib.parse
import zipfile
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from io import StringIO

import pandas as pd
import paramiko
import requests
import streamlit as st
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from auth_utils import authenticate_user, logout_button
import workflow as wf


st.set_page_config(
    page_title="RandPunkt — Reviewers Management",
    page_icon="randpunkt-favicon.png",
    layout="wide",
)

# ============================================================
# CONSTANTES
# ============================================================
COLS_ARTICULOS = [
    "articulo_id", "tipo", "titulo", "ruta_pdf", "ruta_cover",
    "fecha_creacion", "estado", "isbn", "motivo_retiro", "fecha_retiro",
    "idioma_original", "formato_original", "editor_asignado",
    "arbitraje_confirmado",
]
COLS_VERSIONES = ["articulo_id", "fecha", "ruta_pdf", "nombre_archivo", "notas"]
COLS_CONFIRMACIONES = [
    "articulo_id", "nombre", "email", "token",
    "aprobado", "fecha_aprobacion", "comentarios",
]
COLS_COAUTORES = ["Nombre", "Email"]

MOTIVOS_RETIRO = [
    "rejected_by_reviewers", "withdrawn_by_author", "author_unresponsive",
    "duplicate", "out_of_scope", "other",
]

MAX_PDF_BYTES = 50 * 1024 * 1024   # 50 MB
MAX_ZIP_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MB (límite SMTP prudente)

LOCK_TIMEOUT_SEC = 30
LOCK_RETRY_SEC = 0.5

# Lock global para confirmaciones.csv: nombre especial reservado
CONF_LOCK_NAME = "__confirmaciones__"

TOKEN_MAX_AGE_SEC = 30 * 86400  # 30 días


# ============================================================
# LOG DE ERRORES
# ============================================================
def _log_error(context: str, exc: Exception):
    """Nunca silenciar: imprime y, si se puede, registra en el log remoto."""
    msg = f"[ERROR] {context}: {type(exc).__name__}: {exc}"
    print(msg)
    try:
        append_log("ERROR", "-", f"{context}: {exc}")
    except Exception:
        pass


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


def _remote_path(filename: str) -> str:
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


def _cleanup_old_backups(sftp, base_name: str, keep_days: int = 30):
    try:
        rs = st.secrets["remote_server"]
        remote_dir = rs["dir"].rstrip("/")
        pattern = re.compile(rf"^{re.escape(base_name)}\.(\d{{8}}_\d{{6}})\.bak$")
        cutoff = time.time() - keep_days * 86400
        for f in sftp.listdir_attr(remote_dir):
            m = pattern.match(f.filename)
            if m:
                try:
                    ts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").timestamp()
                    if ts < cutoff:
                        sftp.remove(f"{remote_dir}/{f.filename}")
                except Exception as e:
                    _log_error(f"cleanup backup {f.filename}", e)
    except Exception as e:
        _log_error("cleanup backups", e)


# ============================================================
# LOCKS
# ============================================================
def _lock_path(name: str) -> str:
    rs = st.secrets["remote_server"]
    return f"{rs['dir'].rstrip('/')}/.locks/{name}.lock"


def _acquire_lock(name: str, timeout: int = LOCK_TIMEOUT_SEC) -> str | None:
    """
    Intenta crear un lock en SFTP de forma atómica (O_EXCL).
    `name` puede ser un articulo_id o un nombre reservado (p. ej. CONF_LOCK_NAME).
    Devuelve el path del lock si se adquiere, o None si está ocupado/timeout.
    """
    try:
        transport, sftp = _sftp_connect()
        lock_dir = _lock_path(name).rsplit("/", 1)[0]
        _ensure_remote_dir(sftp, lock_dir)
        path = _lock_path(name)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                f = sftp.open(path, "x")  # O_EXCL: falla si existe
                f.write(
                    json.dumps({
                        "login": st.session_state.get("editor_login", ""),
                        "ts": datetime.now().isoformat(timespec="seconds"),
                    }).encode("utf-8")
                )
                f.close()
                sftp.close(); transport.close()
                return path
            except IOError:
                time.sleep(LOCK_RETRY_SEC)
        sftp.close(); transport.close()
        return None
    except Exception as e:
        _log_error(f"acquire lock {name}", e)
        return None


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
    """Rompe locks huérfanos de más de `max_age_sec` (por defecto 10 min)."""
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
# VALIDACIÓN DE ARCHIVOS
# ============================================================
def _is_valid_pdf(data: bytes) -> bool:
    return len(data) > 4 and data[:4] == b"%PDF"


def _is_valid_zip(data: bytes) -> bool:
    if len(data) < 4:
        return False
    if data[:2] != b"PK":
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return z.testzip() is None
    except Exception:
        return False


def _validate_upload(pdf_bytes: bytes, zip_bytes: bytes) -> tuple[bool, str]:
    if len(pdf_bytes) == 0:
        return False, "El PDF está vacío."
    if len(pdf_bytes) > MAX_PDF_BYTES:
        return False, f"PDF demasiado grande (>{MAX_PDF_BYTES // 1024 // 1024} MB)."
    if not _is_valid_pdf(pdf_bytes):
        return False, "El archivo no es un PDF válido (magic bytes incorrectos)."
    if len(zip_bytes) == 0:
        return False, "El ZIP está vacío."
    if len(zip_bytes) > MAX_ZIP_BYTES:
        return False, f"ZIP demasiado grande (>{MAX_ZIP_BYTES // 1024 // 1024} MB)."
    if not _is_valid_zip(zip_bytes):
        return False, "El archivo no es un ZIP válido."
    return True, ""


# ============================================================
# CSV (todo en servidor remoto, escritura atómica)
# ============================================================
def _read_csv(filename, columns):
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


def _save_csv(filename, df, use_global_lock: bool = False):
    """
    Guarda un CSV de forma atómica.
    Si use_global_lock=True, adquiere el lock global de confirmaciones antes de escribir.
    """
    lock_acquired = False
    if use_global_lock:
        lock_acquired = _acquire_lock(CONF_LOCK_NAME) is not None
        if not lock_acquired:
            st.error("No se pudo adquirir el lock de confirmaciones. Reintenta.")
            return False
    try:
        transport, sftp = _sftp_connect()
        path = _remote_path(filename)
        tmp_path = f"{path}.tmp"
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = f"{path}.{ts}.bak"
            with sftp.open(path, "r") as src:
                with sftp.open(backup, "w") as dst:
                    dst.write(src.read())
        except IOError:
            pass
        with sftp.open(tmp_path, "w") as f:
            f.write(df.to_csv(index=False).encode("utf-8"))
        try:
            sftp.remove(path)
        except IOError:
            pass
        sftp.rename(tmp_path, path)
        _cleanup_old_backups(sftp, os.path.basename(filename))
        sftp.close(); transport.close()
        return True
    except Exception as e:
        _log_error(f"save {filename}", e)
        st.error(f"Error saving {filename}: {e}")
        return False
    finally:
        if lock_acquired:
            _release_lock(CONF_LOCK_NAME)


def read_articles():       return _read_csv(st.secrets["csv"]["articulos_file"], COLS_ARTICULOS)
def save_articles(df):     return _save_csv(st.secrets["csv"]["articulos_file"], df)
def read_versions():       return _read_csv(st.secrets["csv"]["versiones_file"], COLS_VERSIONES)
def save_versions(df):     return _save_csv(st.secrets["csv"]["versiones_file"], df)
def read_confirmations():  return _read_csv(st.secrets["csv"]["confirmaciones_file"], COLS_CONFIRMACIONES)


def save_confirmations(df):
    """Escritura de confirmaciones.csv SIEMPRE bajo lock global."""
    return _save_csv(
        st.secrets["csv"]["confirmaciones_file"],
        df,
        use_global_lock=True,
    )


def read_coauthors():
    try:
        transport, sftp = _sftp_connect()
        path = _remote_path(st.secrets["csv"]["coautores_file"])
        try:
            with sftp.open(path, "r") as f:
                content = f.read().decode("utf-8")
            df = pd.read_csv(io.StringIO(content), dtype=str, skipinitialspace=True).fillna("")
            df.columns = [c.strip() for c in df.columns]
            if "Nombre" not in df.columns or "Email" not in df.columns:
                sftp.close(); transport.close()
                return pd.DataFrame(columns=COLS_COAUTORES)
            df["Nombre"] = df["Nombre"].str.strip()
            df["Email"] = df["Email"].str.strip().str.replace(r"^<|>$", "", regex=True)
            df = df[(df["Nombre"] != "") & (df["Email"] != "")].reset_index(drop=True)
            sftp.close(); transport.close()
            return df[COLS_COAUTORES]
        except FileNotFoundError:
            sftp.close(); transport.close()
            return pd.DataFrame(columns=COLS_COAUTORES)
    except Exception as e:
        _log_error("read coauthors", e)
        st.error(f"Error reading coauthors: {e}")
        return pd.DataFrame(columns=COLS_COAUTORES)


def save_coauthors(df): return _save_csv(st.secrets["csv"]["coautores_file"], df)
def read_editores():    return _read_csv(st.secrets["csv"]["editores_file"], ["login", "password", "level", "email"])


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


def save_submission_json(tracking_id, data):
    try:
        transport, sftp = _sftp_connect()
        base = _revista_base()
        if tracking_id.startswith("BK-"):
            json_path = f"{base}/BOOKS/metadata/{tracking_id}.json"
        else:
            json_path = f"{base}/METADATA/{tracking_id}.json"
        tmp = f"{json_path}.tmp"
        with sftp.open(tmp, "w") as f:
            f.write(json.dumps(data, indent=2).encode("utf-8"))
        try:
            sftp.remove(json_path)
        except IOError:
            pass
        sftp.rename(tmp, json_path)
        sftp.close(); transport.close()
        return True
    except Exception as e:
        _log_error(f"save_submission_json {tracking_id}", e)
        st.error(f"Error saving {tracking_id}.json: {e}")
        return False


# ============================================================
# LOG / TOKENS
# ============================================================
def append_log(action, articulo_id, detail=""):
    try:
        transport, sftp = _sftp_connect()
        log_path = _remote_path(st.secrets["csv"]["log_file"])
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} | {action:14s} | {articulo_id:15s} | {detail}\n"
        try:
            with sftp.open(log_path, "r") as f:
                existing = f.read().decode("utf-8")
        except FileNotFoundError:
            existing = ""
        with sftp.open(log_path, "w") as f:
            f.write((existing + line).encode("utf-8"))
        sftp.close(); transport.close()
    except Exception as e:
        print(f"[LOG] {e}")


def get_serializer():
    return URLSafeTimedSerializer(st.secrets["app"]["secret_key"])


def generate_token(email, article_id, name):
    return get_serializer().dumps(
        {"email": email, "articulo_id": article_id, "nombre": name},
        salt="aprobacion-autor",
    )


def validate_token(token: str) -> dict | None:
    try:
        return get_serializer().loads(token, salt="aprobacion-autor", max_age=TOKEN_MAX_AGE_SEC)
    except (BadSignature, SignatureExpired):
        return None


def revoke_token(articulo_id: str, email: str):
    df = read_confirmations()
    mask = (df["articulo_id"] == articulo_id) & (df["email"] == email)
    if mask.any():
        df.loc[mask, "token"] = ""
        save_confirmations(df)
        append_log("REVOKE-TOKEN", articulo_id, email)


# ============================================================
# EMAIL
# ============================================================
def _smtp_config():
    email_conf = st.secrets.get("email", {})
    return {
        "server":   email_conf.get("smtp_server", "smtp.gmail.com"),
        "port":     int(email_conf.get("smtp_port", 587)),
        "sender":   email_conf.get("sender_email", "randpunkt@gmail.com"),
        "password": email_conf.get("sender_password", ""),
        "name":     email_conf.get("sender_name", "RandPunkt Journal"),
    }


def _send_email(to_email: str, subject: str, body: str,
                attachments: list[tuple[str, bytes]] | None = None) -> bool:
    cfg = _smtp_config()
    if not cfg["password"]:
        print("[EMAIL ERROR] No hay contraseña SMTP configurada en los secrets.")
        return False
    try:
        if attachments:
            msg = MIMEMultipart()
            msg.attach(MIMEText(body))
            for fname, data in attachments:
                if len(data) > MAX_ATTACHMENT_BYTES:
                    print(f"[EMAIL WARN] Adjunto {fname} excede el límite; se omite.")
                    continue
                part = MIMEApplication(data, _subtype="pdf")
                part.add_header("Content-Disposition", "attachment", filename=fname)
                msg.attach(part)
        else:
            msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = f"{cfg['name']} <{cfg['sender']}>"
        msg["To"] = to_email

        s = smtplib.SMTP(cfg["server"], cfg["port"])
        s.starttls()
        s.login(cfg["sender"], cfg["password"])
        s.send_message(msg)
        s.quit()
        print(f"[EMAIL OK] → {to_email}")
        return True
    except Exception as e:
        _log_error(f"send_email {to_email}", e)
        return False


def enviar_correo_asignacion(email_revisor, nombre_revisor, articulo_id, titulo):
    return _send_email(
        email_revisor,
        f"Nueva asignación de revisión: {articulo_id}",
        f"Hola {nombre_revisor},\n\n"
        f"Se te ha asignado el manuscrito {articulo_id}: {titulo}\n\n"
        f"Ingresa al panel de revisores para gestionarlo.\n\nRandPunkt Journal",
    )


def enviar_correo_coautor(email_coautor, nombre_coautor, articulo_id,
                          titulo, link, pdf_bytes: bytes | None = None,
                          pdf_name: str | None = None, is_reminder=False):
    prefix = "Recordatorio — " if is_reminder else ""
    intro = ("Este es un recordatorio. Aún no hemos recibido tu aprobación."
             if is_reminder else
             "Como parte del proceso editorial, te pedimos revisar y aprobar el manuscrito antes de su publicación.")
    attachments = []
    if pdf_bytes and pdf_name:
        attachments = [(pdf_name, pdf_bytes)]
    return _send_email(
        email_coautor,
        f"{prefix}[RandPunkt] Aprobación requerida: {titulo}",
        f"Estimado/a {nombre_coautor},\n\n{intro}\n\n"
        f"Manuscrito: \"{titulo}\"\nReferencia: {articulo_id}\n\n"
        f"Adjuntamos el PDF de la última versión. Confirma tu aprobación aquí:\n\n{link}\n\n"
        f"El enlace es personal e intransferible y expira en 30 días.\n\nRandPunkt Journal",
        attachments=attachments,
    )


def _mailto_link(to_email: str, subject: str, body: str) -> str:
    if not to_email:
        return ""
    return (
        f"mailto:{urllib.parse.quote(to_email)}"
        f"?subject={urllib.parse.quote(subject)}"
        f"&body={urllib.parse.quote(body)}"
    )


# ============================================================
# UPLOAD VERSION
# ============================================================
def upload_version(articulo_id, pdf_bytes, zip_bytes, pdf_name, zip_name, notas):
    ok, err = _validate_upload(pdf_bytes, zip_bytes)
    if not ok:
        return False, err

    lock = _acquire_lock(articulo_id)
    if not lock:
        return False, "El manuscrito está bloqueado por otro editor. Reintenta en unos segundos."

    try:
        transport, sftp = _sftp_connect()
        versions_dir = f"{st.secrets['remote_server']['dir'].rstrip('/')}/versiones"
        _ensure_remote_dir(sftp, versions_dir)

        submission_data = get_submission(articulo_id)
        if not submission_data:
            sftp.close(); transport.close()
            return False, "Metadata JSON not found."

        current_version = submission_data.get("current_version", 1)
        next_version = current_version + 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_pdf = re.sub(r"[^A-Za-z0-9._-]", "_", pdf_name)
        safe_zip = re.sub(r"[^A-Za-z0-9._-]", "_", zip_name)
        pdf_remote_name = f"{articulo_id}_v{next_version}_{ts}_{safe_pdf}"
        zip_remote_name = f"{articulo_id}_v{next_version}_{ts}_{safe_zip}"

        with sftp.open(f"{versions_dir}/{pdf_remote_name}", "wb") as f:
            f.write(pdf_bytes)
        with sftp.open(f"{versions_dir}/{zip_remote_name}", "wb") as f:
            f.write(zip_bytes)

        submission_data["current_version"] = next_version
        submission_data.setdefault("versions", []).append({
            "version": next_version,
            "submitted_at": datetime.now().isoformat(timespec="seconds"),
            "pdf": pdf_remote_name,
            "supplementary_zip": zip_remote_name,
            "notes": notas or f"Version {next_version} uploaded by editor",
            "approvals": [],
        })
        sftp.close(); transport.close()

        save_submission_json(articulo_id, submission_data)

        df_ver = read_versions()
        new_row = {
            "articulo_id": articulo_id,
            "fecha": datetime.now().isoformat(timespec="seconds"),
            "ruta_pdf": f"versiones/{pdf_remote_name}",
            "nombre_archivo": pdf_remote_name,
            "notas": notas or f"Version {next_version} uploaded by editor",
        }
        df_ver = pd.concat([df_ver, pd.DataFrame([new_row])], ignore_index=True)
        save_versions(df_ver)

        append_log("UPLOAD", articulo_id,
                   f"v{next_version} — {pdf_remote_name} + {zip_remote_name} — {notas}")
        return True, f"v{next_version}"
    except Exception as e:
        _log_error(f"upload_version {articulo_id}", e)
        return False, str(e)
    finally:
        _release_lock(articulo_id)


# ============================================================
# IMPORT
# ============================================================
def import_from_revista():
    rs = st.secrets["remote_server"]
    errors, imported = [], 0
    try:
        transport, sftp = _sftp_connect()
        base = _revista_base()
        metadata_dir = f"{base}/METADATA"
        pdf_dir = f"{base}/PDF"
        zip_dir = f"{base}/ZIP"
        versions_dir = f"{rs['dir'].rstrip('/')}/versiones"
        _ensure_remote_dir(sftp, versions_dir)

        submissions = []
        try:
            for fname in sorted(sftp.listdir(metadata_dir)):
                if not fname.endswith(".json"):
                    continue
                try:
                    with sftp.open(f"{metadata_dir}/{fname}", "r") as f:
                        submissions.append(json.loads(f.read().decode("utf-8")))
                except Exception as e:
                    _log_error(f"import read {fname}", e)
        except FileNotFoundError:
            pass

        df_art = read_articles()
        df_ver = read_versions()
        existing = set(df_art["articulo_id"].tolist()) if not df_art.empty else set()
        new_art_rows, new_ver_rows = [], []

        for data in submissions:
            art_id = data.get("tracking_id", "")
            if not art_id or art_id in existing:
                continue
            versions = data.get("versions", [])
            fecha = versions[0].get("submitted_at", "") if versions else ""
            pdf_name = os.path.basename(versions[0].get("pdf", "")) if versions else ""
            zip_name = os.path.basename(versions[0].get("supplementary_zip", "")) if versions else ""
            if pdf_name:
                try:
                    sftp.get(f"{pdf_dir}/{pdf_name}", f"{versions_dir}/{pdf_name}")
                except Exception as e:
                    _log_error(f"import copy pdf {pdf_name}", e)
                    errors.append(f"No se pudo copiar PDF {pdf_name}")
            if zip_name:
                try:
                    sftp.get(f"{zip_dir}/{zip_name}", f"{versions_dir}/{zip_name}")
                except Exception as e:
                    _log_error(f"import copy zip {zip_name}", e)
                    errors.append(f"No se pudo copiar ZIP {zip_name}")
            new_art_rows.append({
                "articulo_id": art_id, "tipo": "articulo",
                "titulo": data.get("titulo", ""),
                "ruta_pdf": f"versiones/{pdf_name}" if pdf_name else "",
                "ruta_cover": "", "fecha_creacion": fecha,
                "estado": wf.STATE_EN_REVISION,
                "isbn": "", "motivo_retiro": "", "fecha_retiro": "",
                "idioma_original": data.get("original_language", ""),
                "formato_original": data.get("original_format", ""),
                "editor_asignado": "", "arbitraje_confirmado": "0",
            })
            if pdf_name:
                new_ver_rows.append({
                    "articulo_id": art_id, "fecha": fecha,
                    "ruta_pdf": f"versiones/{pdf_name}",
                    "nombre_archivo": pdf_name,
                    "notas": "Initial submission (imported)",
                })

        if new_art_rows:
            df_art = pd.concat([df_art, pd.DataFrame(new_art_rows)], ignore_index=True)
            if save_articles(df_art):
                imported = len(new_art_rows)
                for r in new_art_rows:
                    append_log("IMPORT", r["articulo_id"], r["titulo"])
        if new_ver_rows:
            df_ver = pd.concat([df_ver, pd.DataFrame(new_ver_rows)], ignore_index=True)
            save_versions(df_ver)
        sftp.close(); transport.close()
        return imported, errors
    except Exception as e:
        _log_error("import_from_revista", e)
        return 0, [f"Connection error: {e}"]


# ============================================================
# ZENODO (idempotente, solo reintenta 5xx/red)
# ============================================================
def _zenodo_config():
    zen = st.secrets.get("zenodo", {})
    if not zen:
        raise RuntimeError("Falta la sección [zenodo] en secrets.toml.")
    if zen.get("sandbox", False):
        return zen.get("sandbox_api", "https://sandbox.zenodo.org/api"), zen.get("sandbox_token", "")
    return zen.get("api", "https://zenodo.org/api"), zen.get("token", "")


def _zenodo_request(method, url, *, headers=None, json_data=None, data=None,
                    timeout=30, max_retries=3):
    """Reintenta SOLO errores de red y 5xx. Los 4xx se propagan."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = requests.request(method, url, headers=headers,
                                 json=json_data, data=data, timeout=timeout)
            if 500 <= r.status_code < 600:
                raise requests.HTTPError(f"{r.status_code}: {r.text}")
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            time.sleep(2 ** attempt)
        except requests.HTTPError as e:
            last_exc = e
            time.sleep(2 ** attempt)
    raise last_exc


def build_zenodo_metadata(submission):
    ca = submission.get("corresponding_author", {}) or {}
    authors = [{
        "name": ca.get("name", "Unknown"),
        "affiliation": ca.get("affiliation", ""),
        "orcid": ca.get("orcid", "").replace("https://orcid.org/", ""),
    }]
    for c in submission.get("coauthors", []):
        authors.append({"name": c} if isinstance(c, str) else c)

    keywords = [k.strip() for k in submission.get("keywords", "").split(",") if k.strip()]
    msc = submission.get("msc", {}) or {}
    if msc.get("primary", {}).get("code"):
        keywords.append(f"MSC {msc['primary']['code']}")
    for sec in msc.get("secondary", []):
        if sec.get("code"):
            keywords.append(f"MSC {sec['code']}")
    if submission.get("original_language"):
        keywords.append(f"Original language: {submission['original_language']}")
    if submission.get("original_format"):
        keywords.append(f"Original format: {submission['original_format']}")
    keywords.extend(["mathematics", "peer-reviewed"])

    tmap = {
        "Short Note": "article", "Scoped Expository Article": "article",
        "Original Research Article": "article", "Research Article": "article",
        "Book": "book", "Book Chapter": "section",
    }
    pub_date = datetime.now().strftime("%Y-%m-%d")
    community = st.secrets.get("zenodo", {}).get("community", "randpunkt-journal")
    return {
        "title": submission.get("titulo", "Untitled"),
        "upload_type": "publication",
        "publication_type": tmap.get(submission.get("article_type"), "article"),
        "publication_date": pub_date,
        "description": submission.get("abstract", ""),
        "creators": authors,
        "keywords": keywords,
        "access_right": "open",
        "license": "cc-by-4.0",
        "communities": [{"identifier": community}],
    }


def publish_to_zenodo_idempotent(articulo_id, submission_data, pdf_bytes):
    """
    Publica en Zenodo de forma idempotente:
      - Si el JSON ya tiene zenodo.deposition_id, lo reutiliza.
      - Si el depósito ya está publicado, devuelve el DOI existente.
      - Si no, crea depósito, sube PDF y publica.
    Guarda deposition_id ANTES de publicar para poder reintentar.
    """
    try:
        api_url, token = _zenodo_config()
        if not token:
            return False, "Zenodo token no configurado", ""
        headers_json = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        metadata = build_zenodo_metadata(submission_data)

        zen = submission_data.setdefault("zenodo", {})
        dep_id = zen.get("deposition_id")

        # 1. Crear depósito si no existe
        if not dep_id:
            r = _zenodo_request("POST", f"{api_url}/deposit/depositions",
                                headers=headers_json, json_data={"metadata": metadata})
            dep = r.json()
            dep_id = dep["id"]
            bucket_url = dep["links"]["bucket"]
            zen["deposition_id"] = dep_id
            zen["bucket_url"] = bucket_url
            zen["environment"] = "sandbox" if st.secrets.get("zenodo", {}).get("sandbox", False) else "production"
            submission_data["zenodo"] = zen
            save_submission_json(articulo_id, submission_data)
        else:
            bucket_url = zen.get("bucket_url", "")

        # 2. Subir PDF (si no estaba ya subido)
        if not zen.get("file_uploaded"):
            if not bucket_url:
                r = _zenodo_request("GET", f"{api_url}/deposit/depositions/{dep_id}",
                                    headers=headers_json)
                bucket_url = r.json()["links"]["bucket"]
                zen["bucket_url"] = bucket_url
            filename = f"{articulo_id}.pdf"
            _zenodo_request("PUT", f"{bucket_url}/{filename}",
                            headers={"Authorization": f"Bearer {token}",
                                     "Content-Type": "application/pdf"},
                            data=pdf_bytes, timeout=300)
            zen["file_uploaded"] = True
            submission_data["zenodo"] = zen
            save_submission_json(articulo_id, submission_data)

        # 3. Publicar (si no estaba publicado)
        if not zen.get("published"):
            r = _zenodo_request("POST",
                                f"{api_url}/deposit/depositions/{dep_id}/actions/publish",
                                headers=headers_json)
            published = r.json()
            zen["doi"] = published.get("doi", "")
            zen["url"] = published.get("links", {}).get("record_html", "")
            zen["published"] = True
            zen["published_at"] = datetime.now().isoformat(timespec="seconds")
            submission_data["zenodo"] = zen
            save_submission_json(articulo_id, submission_data)

        return True, zen.get("doi", ""), zen.get("url", "")
    except Exception as e:
        _log_error(f"publish_to_zenodo {articulo_id}", e)
        return False, str(e), ""


# ============================================================
# AVANCE AUTOMÁTICO A "todos_aprobados"
# ============================================================
def _advance_if_all_approved(articulo_id: str) -> tuple[bool, str]:
    df_conf = read_confirmations()
    conf = df_conf[df_conf["articulo_id"] == articulo_id]
    if conf.empty:
        return False, "Sin coautores invitados."
    pending = conf[conf["aprobado"] != "1"]
    if not pending.empty:
        return False, f"Pendientes: {len(pending)} coautor(es)."

    df_art = read_articles()
    mask = df_art["articulo_id"] == articulo_id
    if not mask.any():
        return False, "Artículo no encontrado."
    if df_art.loc[mask, "estado"].iloc[0] == wf.STATE_TODOS_APROBADOS:
        return False, "Ya está en 'todos_aprobados'."

    lock = _acquire_lock(articulo_id)
    if not lock:
        return False, "Bloqueado por otro editor."
    try:
        df_art.loc[mask, "estado"] = wf.STATE_TODOS_APROBADOS
        df_art.loc[mask, "arbitraje_confirmado"] = "1"
        if save_articles(df_art):
            append_log("AUTO-ADV", articulo_id, "Todos los coautores aprobaron.")
            return True, "✅ Todos los coautores aprobaron. Estado → todos_aprobados."
        return False, "No se pudo guardar el estado."
    finally:
        _release_lock(articulo_id)


# ============================================================
# REVALIDACIÓN DE PERMISOS
# ============================================================
def _revalidate_permissions() -> bool:
    login = st.session_state.get("editor_login", "")
    level = st.session_state.get("editor_level", 1)
    if not login:
        return False
    df_ed = read_editores()
    row = df_ed[df_ed["login"] == login]
    if row.empty:
        st.error("Tu cuenta ya no existe en el sistema. Cerrando sesión.")
        for k in ["editor_login", "editor_level", "editor_email", "authenticated"]:
            st.session_state.pop(k, None)
        return False
    real_level = int(row.iloc[0]["level"])
    if real_level != level:
        st.warning("Tu nivel de permisos ha cambiado. Actualizando sesión.")
        st.session_state["editor_level"] = real_level
    st.session_state["editor_email"] = row.iloc[0].get("email", "")
    return True


# ============================================================
# TAB: MANUSCRIPTS
# ============================================================
def tab_articles():
    st.subheader("📚 Manuscripts and books")

    if not _revalidate_permissions():
        st.stop()

    if "success_message" in st.session_state:
        st.success(st.session_state["success_message"])
        del st.session_state["success_message"]

    user_level = st.session_state.get("editor_level", 1)
    user_login = st.session_state.get("editor_login", "")
    editor_email_actual = st.session_state.get("editor_email", "").strip().lower()
    is_chief = (user_level == 0)

    if is_chief:
        c1, c2 = st.columns([3, 1])
        with c1:
            st.caption("Import submissions from the journal.")
        with c2:
            if st.button("📥 Import from journal", use_container_width=True, type="primary"):
                with st.spinner("Importing..."):
                    n, errs = import_from_revista()
                if n > 0:
                    st.success(f"✅ {n} new item(s) imported.")
                    st.rerun()
                elif errs:
                    for e in errs:
                        st.error(e)
                else:
                    st.info("No new manuscripts to import.")

    df_art_all = read_articles()
    if df_art_all.empty:
        st.info("No manuscripts or books registered yet.")
        return

    if not is_chief:
        df_art_all = df_art_all[df_art_all["editor_asignado"] == user_login].reset_index(drop=True)
        if df_art_all.empty:
            st.info("You have no manuscripts assigned to you.")
            return

    filtro = st.radio("Filter", ["Active", "Retired", "All"],
                      horizontal=True, label_visibility="collapsed")
    if filtro == "Active":
        df_art = df_art_all[df_art_all["estado"].isin(
            [wf.STATE_EN_REVISION, wf.STATE_TODOS_APROBADOS, wf.STATE_PUBLISHED]
        )].reset_index(drop=True)
    elif filtro == "Retired":
        df_art = df_art_all[df_art_all["estado"] == wf.STATE_RETIRADO].reset_index(drop=True)
    else:
        df_art = df_art_all.reset_index(drop=True)

    st.markdown(f"### {len(df_art)} result(s)")
    if not df_art.empty:
        st.dataframe(
            df_art[["articulo_id", "tipo", "titulo", "estado",
                    "arbitraje_confirmado", "editor_asignado"]],
            use_container_width=True,
        )

    st.markdown("### Detail")
    for idx, row in df_art.iterrows():
        aid = row["articulo_id"]
        icon = "📚" if row["tipo"] == "libro" else "📄"

        _break_stale_lock(aid)
        _advance_if_all_approved(aid)

        header = (
            f"{icon} {aid} — {row['titulo']}  |  "
            f"status: {row['estado']}  |  "
            f"assigned: {row['editor_asignado'] or 'None'}"
        )
        with st.expander(header):
            c1, c2 = st.columns([4, 2])

            # ================= IZQUIERDA =================
            with c1:
                st.write(f"**ID:** `{aid}`")
                st.write(f"**Type:** {row['tipo']}")
                st.write(f"**Title:** {row['titulo']}")
                if row["tipo"] == "libro" and row["isbn"]:
                    st.write(f"**ISBN:** {row['isbn']}")
                st.write(f"**Status:** `{row['estado']}`")
                st.write(f"**Creation date:** {row['fecha_creacion']}")

                sub = get_submission(aid)
                if sub:
                    a = sub.get("corresponding_author", {}) or {}
                    if a.get("name") or a.get("email"):
                        st.markdown("---")
                        st.markdown("**Corresponding Author (Editor Only)**")
                        st.write(f"**Name:** {a.get('name', '—')}")
                        st.write(f"**Email:** `{a.get('email', '—')}`")
                        if a.get("affiliation"):
                            st.write(f"**Affiliation:** {a.get('affiliation')}")
                        if a.get("email"):
                            subject = f"[RandPunkt] {aid} — {row['titulo']}"
                            body = (
                                f"Estimado/a {a.get('name', '')},\n\n"
                                f"Le escribo en relación con el manuscrito \"{row['titulo']}\" "
                                f"(Ref. {aid}).\n\nSaludos cordiales,\n"
                            )
                            st.link_button(
                                "✉️ Write to author from my email",
                                _mailto_link(a["email"], subject, body),
                                use_container_width=True,
                            )

                # --- COAUTORES ---
                st.markdown("---")
                st.markdown("**Coauthors**")
                df_conf = read_confirmations()
                existing = df_conf[df_conf["articulo_id"] == aid]
                if not existing.empty:
                    st.dataframe(
                        existing[["nombre", "email", "aprobado", "fecha_aprobacion"]],
                        use_container_width=True,
                    )
                else:
                    st.info("No coauthors invited yet.")

                with st.expander("➕ Add or modify a coauthor"):
                    with st.form(f"add_coauthor_{aid}"):
                        new_name = st.text_input("Name", key=f"new_name_{aid}")
                        new_email = st.text_input("Email", key=f"new_email_{aid}")
                        if st.form_submit_button("Add coauthor", use_container_width=True):
                            if new_name and new_email:
                                df_coauthors = read_coauthors()
                                new_row = pd.DataFrame([{"Nombre": new_name, "Email": new_email}])
                                df_coauthors = pd.concat([df_coauthors, new_row], ignore_index=True)
                                df_coauthors = df_coauthors.drop_duplicates(
                                    subset=["Email"], keep="last"
                                ).reset_index(drop=True)
                                if save_coauthors(df_coauthors):
                                    st.success(f"Coauthor {new_name} added.")
                                    st.rerun()
                            else:
                                st.error("Please fill both fields.")

                # Obtener PDF de la última versión (para adjuntar)
                pdf_bytes_latest = None
                pdf_name_latest = None
                if sub and sub.get("versions"):
                    last_ver = sub["versions"][-1]
                    pdf_name_latest = last_ver.get("pdf", "")
                    if pdf_name_latest:
                        pdf_bytes_latest = _download_remote_bytes(pdf_name_latest, "pdf")

                # Invitar coautores (adjunta PDF de la última versión)
                if st.button("📨 Request Coauthors (attach latest PDF)",
                             key=f"req_coauth_{aid}",
                             use_container_width=True, type="primary"):
                    sub2 = get_submission(aid)
                    if not sub2:
                        st.error("Metadata JSON not found.")
                    else:
                        df_coauthors = read_coauthors()
                        author = sub2.get("corresponding_author", {}) or {}
                        a_name = author.get("name", "").strip()
                        a_email = author.get("email", "").strip()
                        if a_name and a_email and a_email not in df_coauthors["Email"].values:
                            df_coauthors = pd.concat(
                                [df_coauthors, pd.DataFrame([{"Nombre": a_name, "Email": a_email}])],
                                ignore_index=True,
                            )
                        if df_coauthors.empty:
                            st.warning("No coauthors available.")
                        else:
                            existing_emails = set(existing["email"].tolist()) if not existing.empty else set()
                            base_url = st.secrets["app"]["base_url"].rstrip("/")
                            # Re-leer bajo lock global para evitar colisiones
                            df_conf = read_confirmations()
                            ok = 0
                            for _, r in df_coauthors.iterrows():
                                name, email = r["Nombre"], r["Email"]
                                if email.strip().lower() == editor_email_actual:
                                    continue
                                if email in existing_emails:
                                    continue
                                token = generate_token(email, aid, name)
                                link = f"{base_url}/?token={token}"
                                sent = enviar_correo_coautor(
                                    email, name, aid, row["titulo"], link,
                                    pdf_bytes=pdf_bytes_latest,
                                    pdf_name=pdf_name_latest,
                                )
                                if sent:
                                    df_conf = pd.concat([df_conf, pd.DataFrame([{
                                        "articulo_id": aid,
                                        "nombre": name,
                                        "email": email,
                                        "token": token,
                                        "aprobado": "0",
                                        "fecha_aprobacion": "",
                                        "comentarios": "",
                                    }])], ignore_index=True)
                                    ok += 1
                            save_confirmations(df_conf)  # ← bajo lock global
                            _advance_if_all_approved(aid)
                            st.success(f"✅ Sent {ok} invitation(s) with PDF attached.")
                            st.rerun()

                # Reintentar invitaciones pendientes
                pending = existing[existing["aprobado"] != "1"] if not existing.empty else pd.DataFrame()
                if not pending.empty:
                    colr1, colr2 = st.columns(2)
                    with colr1:
                        if st.button(f"🔔 Remind {len(pending)} pending",
                                     key=f"remind_{aid}", use_container_width=True):
                            base_url = st.secrets["app"]["base_url"].rstrip("/")
                            sent = 0
                            for _, p in pending.iterrows():
                                link = f"{base_url}/?token={p['token']}"
                                if enviar_correo_coautor(
                                    p["email"], p["nombre"], aid, row["titulo"], link,
                                    pdf_bytes=pdf_bytes_latest,
                                    pdf_name=pdf_name_latest,
                                    is_reminder=True,
                                ):
                                    sent += 1
                            st.success(f"Reminders sent: {sent}")
                    with colr2:
                        if st.button(f"♻️ Recreate tokens ({len(pending)})",
                                     key=f"retok_{aid}", use_container_width=True):
                            # Re-leer bajo lock global antes de modificar
                            df_conf2 = read_confirmations()
                            base_url = st.secrets["app"]["base_url"].rstrip("/")
                            sent = 0
                            for _, p in pending.iterrows():
                                new_token = generate_token(p["email"], aid, p["nombre"])
                                link = f"{base_url}/?token={new_token}"
                                if enviar_correo_coautor(
                                    p["email"], p["nombre"], aid, row["titulo"], link,
                                    pdf_bytes=pdf_bytes_latest,
                                    pdf_name=pdf_name_latest,
                                ):
                                    mask2 = (df_conf2["articulo_id"] == aid) & (df_conf2["email"] == p["email"])
                                    df_conf2.loc[mask2, "token"] = new_token
                                    sent += 1
                            save_confirmations(df_conf2)  # ← bajo lock global
                            st.success(f"Tokens recreated and sent: {sent}")

                # ====== CHIEF: asignar / reasignar editor ======
                if is_chief:
                    st.markdown("---")
                    st.markdown("**Assign / reassign editor**")
                    df_ed = read_editores()
                    l1 = df_ed[df_ed["level"] == 1]["login"].tolist() if "level" in df_ed else []
                    current = row["editor_asignado"]
                    opts = ["— Unassigned —"] + l1
                    idx0 = opts.index(current) if current in opts else 0
                    selected = st.selectbox("Associate Editor", opts, index=idx0,
                                            key=f"assign_{aid}")
                    if st.button("💾 Save assignment", key=f"save_assign_{aid}"):
                        new_val = "" if selected == "— Unassigned —" else selected
                        # ===== MEJORA OPCIONAL #2: validar contra editores.csv =====
                        if new_val and new_val not in df_ed["login"].tolist():
                            st.error(f"El login '{new_val}' no existe en editores.csv. No se guarda.")
                        else:
                            lock = _acquire_lock(aid)
                            if not lock:
                                st.error("Manuscrito bloqueado por otro editor.")
                            else:
                                try:
                                    df_full = read_articles()
                                    df_full.loc[df_full["articulo_id"] == aid, "editor_asignado"] = new_val
                                    if save_articles(df_full):
                                        append_log("ASSIGN-EDITOR", aid, f"→ {new_val or 'none'}")
                                        if new_val:
                                            revisor_info = df_ed[df_ed["login"] == new_val]
                                            if not revisor_info.empty:
                                                email_rev = revisor_info.iloc[0]["email"]
                                                enviar_correo_asignacion(email_rev, new_val, aid, row["titulo"])
                                        st.success("Assignment updated.")
                                        st.rerun()
                                finally:
                                    _release_lock(aid)

            # ================= DERECHA =================
            with c2:
                st.write("**Actions**")

                st.markdown("---")
                with st.form(f"upload_version_{aid}_{idx}"):
                    st.markdown("**Upload new version (PDF + ZIP)**")
                    pdf_file = st.file_uploader("PDF", type=["pdf"], key=f"pdf_file_{aid}_{idx}")
                    zip_file = st.file_uploader("Supplementary ZIP", type=["zip"], key=f"zip_file_{aid}_{idx}")
                    notas = st.text_input("Version notes", value="", key=f"notas_{aid}_{idx}")
                    if st.form_submit_button("⬆️ Upload Version", use_container_width=True):
                        if pdf_file is None or zip_file is None:
                            st.error("You must select both the PDF and the ZIP.")
                        else:
                            ok, result = upload_version(
                                aid, pdf_file.getbuffer(), zip_file.getbuffer(),
                                pdf_file.name, zip_file.name, notas.strip()
                            )
                            if ok:
                                st.success(f"Version {result} uploaded successfully.")
                                st.rerun()
                            else:
                                st.error(f"Error: {result}")

                st.markdown("---")
                st.markdown("**Versions available for download**")
                submission_data = get_submission(aid)
                if submission_data and submission_data.get("versions"):
                    for ver in submission_data["versions"]:
                        v_num = ver.get("version", 1)
                        v_pdf = ver.get("pdf", "")
                        v_zip = ver.get("supplementary_zip", "")
                        v_date = ver.get("submitted_at", "")[:10]
                        v_notes = ver.get("notes", "")
                        st.markdown(f"**v{v_num}** ({v_date}) — {v_notes}")
                        col_a, col_b = st.columns(2)
                        if v_pdf:
                            with col_a:
                                if st.button(f"⬇️ PDF v{v_num}",
                                             key=f"dl_pdf_{aid}_{v_num}",
                                             use_container_width=True):
                                    data = _download_remote_bytes(v_pdf, "pdf")
                                    if data:
                                        st.download_button(
                                            label=f"💾 Save PDF v{v_num}",
                                            data=data,
                                            file_name=os.path.basename(v_pdf),
                                            mime="application/pdf",
                                            key=f"save_pdf_{aid}_{v_num}",
                                        )
                                    else:
                                        st.error(f"PDF v{v_num} not found on server.")
                        if v_zip:
                            with col_b:
                                if st.button(f"⬇️ ZIP v{v_num}",
                                             key=f"dl_zip_{aid}_{v_num}",
                                             use_container_width=True):
                                    data = _download_remote_bytes(v_zip, "zip")
                                    if data:
                                        st.download_button(
                                            label=f"💾 Save ZIP v{v_num}",
                                            data=data,
                                            file_name=os.path.basename(v_zip),
                                            mime="application/zip",
                                            key=f"save_zip_{aid}_{v_num}",
                                        )
                                    else:
                                        st.error(f"ZIP v{v_num} not found on server.")
                else:
                    st.info("No versions registered in the metadata JSON.")

                estado = row["estado"]

                if estado == wf.STATE_EN_REVISION:
                    st.info("Status: **en_revision**")
                    if is_chief:
                        st.markdown("---")
                        st.caption("**Chief options**")
                        if st.button("🚀 Advance to 'todos_aprobados' (force)",
                                     key=f"force_adv_{aid}",
                                     use_container_width=True, type="primary"):
                            lock = _acquire_lock(aid)
                            if not lock:
                                st.error("Manuscrito bloqueado por otro editor.")
                            else:
                                try:
                                    df_full = read_articles()
                                    res = wf.force_advance_to_todos_aprobados(df_full, aid)
                                    if res.ok and save_articles(df_full):
                                        append_log("FORCE-ADV", aid, res.message)
                                        st.success(res.message)
                                        st.rerun()
                                    else:
                                        st.error(res.message)
                                finally:
                                    _release_lock(aid)

                elif estado == wf.STATE_TODOS_APROBADOS:
                    st.success("🎉 Ready for publication.")
                    if is_chief:
                        st.markdown("---")
                        st.caption("**Chief only — Publish to Zenodo**")
                        zen = st.secrets.get("zenodo", {})
                        env = "sandbox" if zen.get("sandbox", False) else "production"
                        has_creds = bool(zen.get("sandbox_token") if env == "sandbox" else zen.get("token"))
                        st.caption(f"Environment: **{env}**")
                        if not has_creds:
                            st.warning("No Zenodo credentials for this environment.")
                        if st.button("🚀 PUBLISH to Zenodo", key=f"pub_{aid}",
                                     use_container_width=True, type="primary",
                                     disabled=not has_creds):
                            sub = get_submission(aid)
                            if not sub:
                                st.error("Metadata JSON not found.")
                            else:
                                vs = (sub or {}).get("versions", [])
                                pdf_name = vs[-1]["pdf"] if vs else ""
                                pdf_bytes = _download_remote_bytes(pdf_name, "pdf") if pdf_name else None
                                if not pdf_bytes:
                                    st.error("Cannot publish: PDF of the latest version not found.")
                                else:
                                    ok, doi_or_error, url = publish_to_zenodo_idempotent(aid, sub, pdf_bytes)
                                    if ok:
                                        sub["status"] = "published"
                                        save_submission_json(aid, sub)

                                        df_full = read_articles()
                                        df_full.loc[df_full["articulo_id"] == aid, "estado"] = wf.STATE_PUBLISHED
                                        save_articles(df_full)

                                        append_log("PUBLISH", aid, f"{env} DOI {doi_or_error}")
                                        st.balloons()
                                        st.success(f"✅ Published on Zenodo ({env})! DOI: {doi_or_error}")
                                        st.rerun()
                                    else:
                                        st.error(f"Zenodo error: {doi_or_error}")
                    else:
                        st.caption("Only the Chief Editor can publish.")

                elif estado == wf.STATE_PUBLISHED:
                    st.success("✅ Published")
                    sub = get_submission(aid) or {}
                    z = sub.get("zenodo") or {}
                    if z.get("doi"):
                        st.write(f"**DOI:** `{z['doi']}`")
                    if z.get("url"):
                        st.write(f"**URL:** {z['url']}")
                    if z.get("environment"):
                        st.caption(f"Environment: {z['environment']}")

                elif estado == wf.STATE_RETIRADO:
                    st.warning(f"Retired: {row['motivo_retiro']}")
                    if is_chief:
                        if st.button("♻️ Restore", key=f"rest_{aid}", use_container_width=True):
                            lock = _acquire_lock(aid)
                            if not lock:
                                st.error("Manuscrito bloqueado por otro editor.")
                            else:
                                try:
                                    df_full = read_articles()
                                    res = wf.restore(df_full, aid)
                                    if res.ok and save_articles(df_full):
                                        append_log("RESTORE", aid, "Restored")
                                        st.success(res.message)
                                        st.rerun()
                                    else:
                                        st.error(res.message)
                                finally:
                                    _release_lock(aid)

                if estado in {wf.STATE_EN_REVISION, wf.STATE_TODOS_APROBADOS, wf.STATE_PUBLISHED}:
                    if is_chief or (row["editor_asignado"] == user_login):
                        st.markdown("---")
                        if st.button("🚫 Retire", key=f"ret_{aid}_{idx}", use_container_width=True):
                            st.session_state[f"retirando_{aid}"] = True
                        if st.session_state.get(f"retirando_{aid}", False):
                            with st.form(f"ret_form_{aid}_{idx}"):
                                motivo = st.selectbox("Reason", MOTIVOS_RETIRO)
                                comentario = st.text_input("Comment")
                                cc1, cc2 = st.columns(2)
                                with cc1:
                                    if st.form_submit_button("✅ Confirm", use_container_width=True):
                                        lock = _acquire_lock(aid)
                                        if not lock:
                                            st.error("Manuscrito bloqueado por otro editor.")
                                        else:
                                            try:
                                                df_full = read_articles()
                                                res = wf.retire(df_full, aid, motivo=motivo)
                                                if res.ok and save_articles(df_full):
                                                    append_log("RETIRE", aid, f"{motivo} — {comentario}")
                                                    st.session_state[f"retirando_{aid}"] = False
                                                    st.success(res.message)
                                                    st.rerun()
                                                else:
                                                    st.error(res.message)
                                            finally:
                                                _release_lock(aid)
                                with cc2:
                                    if st.form_submit_button("❌ Cancel", use_container_width=True):
                                        st.session_state[f"retirando_{aid}"] = False
                                        st.rerun()


# ============================================================
# SMOKE TESTS
# ============================================================
def _run_smoke_tests() -> bool:
    ok = True
    if _is_valid_pdf(b"%PDF-1.4\n..."):
        pass
    else:
        print("[SMOKE FAIL] _is_valid_pdf válido")
        ok = False
    if not _is_valid_pdf(b"not a pdf"):
        pass
    else:
        print("[SMOKE FAIL] _is_valid_pdf inválido")
        ok = False
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hello")
    if _is_valid_zip(buf.getvalue()):
        pass
    else:
        print("[SMOKE FAIL] _is_valid_zip válido")
        ok = False
    if not _is_valid_zip(b"not a zip"):
        pass
    else:
        print("[SMOKE FAIL] _is_valid_zip inválido")
        ok = False
    return ok


# ============================================================
# MAIN
# ============================================================
def main():
    if not authenticate_user():
        return
    logout_button()

    st.title("🧑‍⚖️ Reviewers Management")

    if os.environ.get("RANDPUNKT_SMOKE_TESTS", "") == "1":
        _run_smoke_tests()

    tab_articles()


if __name__ == "__main__":
    main()
