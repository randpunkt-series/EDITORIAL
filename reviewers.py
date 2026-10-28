"""
RandPunkt — Editorial panel (reviewers.py) — Versión integrada.

Modelo de permisos:
  - Associate (Level 1): gestiona coautores y ve el estado de las firmas.
  - Chief (Level 0): publica en Zenodo cuando el estado es 'todos_aprobados'.

Modelo de estados:
  en_revision  →  todos_aprobados  →  published
       ↘               ↘
              retirado
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import smtplib
from email.mime.text import MIMEText
from datetime import date, datetime
from io import StringIO

import pandas as pd
import paramiko
import streamlit as st
from itsdangerous import URLSafeTimedSerializer

from auth_utils import authenticate_user, logout_button
import workflow as wf


st.set_page_config(
    page_title="RandPunkt — Reviewers Management",
    page_icon="randpunkt-favicon.png",
    layout="wide",
)

COLS_ARTICULOS = [
    "articulo_id", "tipo", "titulo", "ruta_pdf", "ruta_cover",
    "fecha_creacion", "estado", "isbn", "motivo_retiro", "fecha_retiro",
    "idioma_original", "formato_original", "editor_asignado",
    "arbitraje_confirmado",
]
COLS_VERSIONES = [
    "articulo_id", "fecha", "ruta_pdf", "nombre_archivo", "notas",
]
COLS_CONFIRMACIONES = [
    "articulo_id", "nombre", "email", "token",
    "aprobado", "fecha_aprobacion", "comentarios",
]
COLS_COAUTORES = ["Nombre", "Email"]

MOTIVOS_RETIRO = [
    "rejected_by_reviewers", "withdrawn_by_author", "author_unresponsive",
    "duplicate", "out_of_scope", "other",
]


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
            except OSError:
                pass


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
                except Exception:
                    pass
    except Exception:
        pass


# ============================================================
# CSV
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
        st.error(f"Error reading {filename}: {e}")
        return pd.DataFrame(columns=columns)


def _save_csv(filename, df):
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
        st.error(f"Error saving {filename}: {e}")
        return False


def read_articles():
    return _read_csv(st.secrets["csv"]["articulos_file"], COLS_ARTICULOS)

def save_articles(df):
    return _save_csv(st.secrets["csv"]["articulos_file"], df)

def read_versions():
    return _read_csv(st.secrets["csv"]["versiones_file"], COLS_VERSIONES)

def save_versions(df):
    return _save_csv(st.secrets["csv"]["versiones_file"], df)

def read_confirmations():
    return _read_csv(st.secrets["csv"]["confirmaciones_file"], COLS_CONFIRMACIONES)

def save_confirmations(df):
    return _save_csv(st.secrets["csv"]["confirmaciones_file"], df)

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
        st.error(f"Error reading coauthors: {e}")
        return pd.DataFrame(columns=COLS_COAUTORES)

def save_coauthors(df):
    return _save_csv(st.secrets["csv"]["coautores_file"], df)

def read_editores():
    return _read_csv(st.secrets["csv"]["editores_file"],
                     ["login", "password", "level", "email"])


def get_submission(tracking_id):
    try:
        transport, sftp = _sftp_connect()
        rs = st.secrets["remote_server"]
        revista_base = rs.get("revista_dir", rs["dir"].rsplit("/editorial", 1)[0]).rstrip("/")
        for base in [f"{revista_base}/METADATA", f"{revista_base}/BOOKS/metadata"]:
            try:
                with sftp.open(f"{base}/{tracking_id}.json", "r") as f:
                    data = json.loads(f.read().decode("utf-8"))
                sftp.close(); transport.close()
                return data
            except FileNotFoundError:
                continue
        sftp.close(); transport.close()
        return None
    except Exception as e:
        print(f"[ERROR] get_submission({tracking_id}): {e}")
        return None


def save_submission_json(tracking_id, data):
    try:
        transport, sftp = _sftp_connect()
        rs = st.secrets["remote_server"]
        revista_base = rs.get("revista_dir", rs["dir"].rsplit("/editorial", 1)[0]).rstrip("/")
        if tracking_id.startswith("BK-"):
            json_path = f"{revista_base}/BOOKS/metadata/{tracking_id}.json"
        else:
            json_path = f"{revista_base}/METADATA/{tracking_id}.json"
        with sftp.open(json_path, "w") as f:
            f.write(json.dumps(data, indent=2).encode("utf-8"))
        sftp.close(); transport.close()
        return True
    except Exception as e:
        st.error(f"Error saving {tracking_id}.json: {e}")
        return False


# ============================================================
# LOG
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


# ============================================================
# TOKENS
# ============================================================
def get_serializer():
    return URLSafeTimedSerializer(st.secrets["app"]["secret_key"])

def generate_token(email, article_id, name):
    return get_serializer().dumps(
        {"email": email, "articulo_id": article_id, "nombre": name},
        salt="aprobacion-autor",
    )


# ============================================================
# EMAIL
# ============================================================
def enviar_correo_asignacion(email_revisor, nombre_revisor, articulo_id, titulo_articulo):
    """Envía un correo al revisor cuando se le asigna un manuscrito."""
    try:
        email_conf = st.secrets.get("email", {})
        smtp_server = email_conf.get("smtp_server", "smtp.gmail.com")
        smtp_port = int(email_conf.get("smtp_port", 587))
        sender_email = email_conf.get("sender_email", "randpunkt@gmail.com")
        sender_password = email_conf.get("sender_password", "")

        if not sender_password:
            print("[EMAIL ERROR] No hay contraseña SMTP configurada en los secrets.")
            return False

        msg = MIMEText(
            f"Hola {nombre_revisor},\n\n"
            f"Se te ha asignado el manuscrito con ID: {articulo_id}\n"
            f"Título: {titulo_articulo}\n\n"
            f"Por favor, ingresa al panel de revisores para aceptar o rechazar esta asignación.\n\n"
            f"Saludos,\nRandPunkt Journal"
        )
        msg['Subject'] = f"Nueva asignación de revisión: {articulo_id}"
        msg['From'] = sender_email
        msg['To'] = email_revisor

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        print(f"[EMAIL OK] Correo enviado a {email_revisor} para {articulo_id}")
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] Falló el envío a {email_revisor}: {e}")
        return False


def enviar_correo_coautor(email_coautor, nombre_coautor, articulo_id, titulo, link, is_reminder=False):
    """Envía un correo de invitación o recordatorio a un coautor."""
    try:
        email_conf = st.secrets.get("email", {})
        smtp_server = email_conf.get("smtp_server", "smtp.gmail.com")
        smtp_port = int(email_conf.get("smtp_port", 587))
        sender_email = email_conf.get("sender_email", "randpunkt@gmail.com")
        sender_password = email_conf.get("sender_password", "")
        sender_name = email_conf.get("sender_name", "RandPunkt Journal")

        if not sender_password:
            print("[EMAIL ERROR] No hay contraseña SMTP configurada.")
            return False

        prefix = "Recordatorio — " if is_reminder else ""
        intro = ("Este es un recordatorio. Aún no hemos recibido tu aprobación."
                 if is_reminder else
                 "Como parte del proceso editorial, te pedimos revisar y aprobar el manuscrito antes de su publicación.")

        msg = MIMEText(
            f"Estimado/a {nombre_coautor},\n\n"
            f"{intro}\n\n"
            f"Manuscrito: \"{titulo}\"\n"
            f"Referencia: {articulo_id}\n\n"
            f"Por favor, haz clic en el siguiente enlace para confirmar tu aprobación:\n\n"
            f"{link}\n\n"
            f"El enlace es personal e intransferible, y expira en 30 días.\n\n"
            f"Saludos cordiales,\n{sender_name}"
        )
        msg['Subject'] = f"{prefix}[RandPunkt] Aprobación requerida: {titulo}"
        msg['From'] = f"{sender_name} <{sender_email}>"
        msg['To'] = email_coautor

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        print(f"[EMAIL OK] Correo enviado a {email_coautor} para {articulo_id}")
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] Falló el envío a {email_coautor}: {e}")
        return False


# ============================================================
# IMPORT
# ============================================================
def import_from_revista():
    rs = st.secrets["remote_server"]
    errors = []
    imported = 0
    try:
        transport, sftp = _sftp_connect()
        revista_base = rs.get("revista_dir", rs["dir"].rsplit("/editorial", 1)[0]).rstrip("/")
        metadata_dir = f"{revista_base}/METADATA"
        pdf_dir = f"{revista_base}/PDF"
        zip_dir = f"{revista_base}/ZIP"
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
                except Exception:
                    pass
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
            raw_pdf = versions[0].get("pdf", "") if versions else ""
            raw_zip = versions[0].get("supplementary_zip", "") if versions else ""
            pdf_name = os.path.basename(raw_pdf) if raw_pdf else ""
            zip_name = os.path.basename(raw_zip) if raw_zip else ""
            if pdf_name:
                try:
                    sftp.get(f"{pdf_dir}/{pdf_name}", f"{versions_dir}/{pdf_name}")
                except Exception:
                    pass
            if zip_name:
                try:
                    sftp.get(f"{zip_dir}/{zip_name}", f"{versions_dir}/{zip_name}")
                except Exception:
                    pass
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
        return 0, [f"Connection error: {e}"]


# ============================================================
# ZENODO
# ============================================================
def _get_zenodo_config():
    zen = st.secrets.get("zenodo", {})
    if zen.get("sandbox", False):
        return zen.get("sandbox_api", "https://sandbox.zenodo.org/api"), zen.get("sandbox_token", "")
    return zen.get("api", "https://zenodo.org/api"), zen.get("token", "")


def _zenodo_request(method, url, *, headers=None, json_data=None, data=None,
                    timeout=30, max_retries=3):
    import requests
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = requests.request(method, url, headers=headers,
                                 json=json_data, data=data, timeout=timeout)
            if r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code}: {r.text}")
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last_exc = e
            time.sleep(2 ** attempt)
    raise last_exc


def build_zenodo_metadata(submission):
    ca = submission.get("corresponding_author", {})
    authors = [{
        "name": ca.get("name", "Unknown"),
        "affiliation": ca.get("affiliation", ""),
        "orcid": ca.get("orcid", "").replace("https://orcid.org/", ""),
    }]
    for c in submission.get("coauthors", []):
        authors.append({"name": c} if isinstance(c, str) else c)
    keywords = [k.strip() for k in submission.get("keywords", "").split(",") if k.strip()]
    msc = submission.get("msc", {})
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
    submitted = submission.get("submitted_at", "")
    if not submitted:
        vs = submission.get("versions", [])
        if vs:
            submitted = vs[0].get("submitted_at", "")
    pub_date = submitted[:10] if len(submitted) >= 10 else "2026-01-01"
    community = st.secrets.get("zenodo", {}).get("community", "randpunkt")
    return {
        "title": submission.get("titulo", "Untitled"),
        "upload_type": "publication",
        "publication_type": tmap.get(submission.get("article_type"), "article"),
        "publication_date": pub_date,
        "description": submission.get("abstract", ""),
        "creators": authors, "keywords": keywords,
        "access_right": "open", "license": "cc-by-4.0",
        "communities": [{"identifier": community}],
    }


def publish_to_zenodo_real(articulo_id, submission_data, pdf_bytes):
    try:
        api_url, token = _get_zenodo_config()
        if not token:
            return False, "Zenodo token not configured", ""
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        metadata = build_zenodo_metadata(submission_data)
        r = _zenodo_request("POST", f"{api_url}/deposit/depositions",
                            headers=headers, json_data={"metadata": metadata})
        dep = r.json()
        dep_id = dep["id"]
        bucket_url = dep["links"]["bucket"]
        filename = f"{articulo_id}.pdf"
        _zenodo_request("PUT", f"{bucket_url}/{filename}",
                        headers={"Authorization": f"Bearer {token}"},
                        data=pdf_bytes, timeout=300)
        r = _zenodo_request("POST",
                            f"{api_url}/deposit/depositions/{dep_id}/actions/publish",
                            headers=headers)
        published = r.json()
        return True, published.get("doi", ""), published.get("links", {}).get("record_html", "")
    except Exception as e:
        return False, str(e), ""


# ============================================================
# TAB 1: MANUSCRIPTS AND BOOKS (CON GESTIÓN DE COAUTORES)
# ============================================================
def tab_articles():
    st.subheader("📚 Manuscripts and books")

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
            df_art[[
                "articulo_id", "tipo", "titulo", "estado",
                "arbitraje_confirmado", "editor_asignado",
            ]],
            use_container_width=True,
        )

    df_conf_all = read_confirmations()

    st.markdown("### Detail")
    for idx, row in df_art.iterrows():
        aid = row["articulo_id"]
        icon = "📚" if row["tipo"] == "libro" else "📄"
        header = (
            f"{icon} {aid} — {row['titulo']}  |  "
            f"status: {row['estado']}  |  "
            f"assigned: {row['editor_asignado'] or 'None'}"
        )
        with st.expander(header):
            c1, c2 = st.columns([4, 2])

            # --- INFO ---
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
                    a = sub.get("corresponding_author", {})
                    if a.get("name"):
                        st.markdown("---")
                        st.markdown("**Author Contact (Editor Only)**")
                        st.write(f"**Name:** {a.get('name', '—')}")
                        st.write(f"**Email:** `{a.get('email', '—')}`")

                # --- GESTIÓN DE COAUTORES (Integrada) ---
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

                # Formulario para añadir/modificar coautor
                with st.expander("➕ Add or modify a coauthor"):
                    with st.form(f"add_coauthor_{aid}"):
                        new_name = st.text_input("Name", key=f"new_name_{aid}")
                        new_email = st.text_input("Email", key=f"new_email_{aid}")
                        if st.form_submit_button("Add coauthor", use_container_width=True):
                            if new_name and new_email:
                                df_coauthors = read_coauthors()
                                new_row = pd.DataFrame([{"Nombre": new_name, "Email": new_email}])
                                df_coauthors = pd.concat([df_coauthors, new_row], ignore_index=True)
                                df_coauthors = df_coauthors.drop_duplicates(subset=["Email"], keep="last").reset_index(drop=True)
                                if save_coauthors(df_coauthors):
                                    st.success(f"Coauthor {new_name} added.")
                                    st.rerun()
                            else:
                                st.error("Please fill both fields.")

                # Botón para invitar a todos los coautores no invitados
                if st.button("📨 Request Coauthors", key=f"req_coauth_{aid}", use_container_width=True, type="primary"):
                    sub = get_submission(aid)
                    if not sub:
                        st.error("Metadata JSON not found.")
                    else:
                        df_coauthors = read_coauthors()
                        author = sub.get("corresponding_author", {})
                        a_name = author.get("name", "").strip()
                        a_email = author.get("email", "").strip()
                        if a_name and a_email and a_email not in df_coauthors["Email"].values:
                            new_row = pd.DataFrame([{"Nombre": a_name, "Email": a_email}])
                            df_coauthors = pd.concat([df_coauthors, new_row], ignore_index=True)

                        if df_coauthors.empty:
                            st.warning("No coauthors available.")
                        else:
                            existing_emails = set(existing["email"].tolist()) if not existing.empty else set()
                            base_url = st.secrets["app"]["base_url"].rstrip("/")
                            df_conf = read_confirmations()
                            ok = 0
                            for _, r in df_coauthors.iterrows():
                                name = r["Nombre"]
                                email = r["Email"]
                                if email.strip().lower() == editor_email_actual:
                                    continue
                                if email in existing_emails:
                                    continue
                                token = generate_token(email, aid, name)
                                link = f"{base_url}/?token={token}"
                                if enviar_correo_coautor(email, name, aid, row["titulo"], link):
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
                            save_confirmations(df_conf)
                            st.success(f"✅ Sent {ok} invitation(s).")
                            st.rerun()

                # Botón para enviar recordatorios
                pending = existing[existing["aprobado"] == "0"] if not existing.empty else pd.DataFrame()
                if not pending.empty:
                    if st.button(f"🔔 Remind {len(pending)} pending coauthor(s)", key=f"remind_{aid}", use_container_width=True):
                        base_url = st.secrets["app"]["base_url"].rstrip("/")
                        sent = 0
                        for _, p in pending.iterrows():
                            link = f"{base_url}/?token={p['token']}"
                            if enviar_correo_coautor(p["email"], p["nombre"], aid, row["titulo"], link, is_reminder=True):
                                sent += 1
                        st.success(f"Reminders sent: {sent}")

                if is_chief:
                    st.markdown("---")
                    st.markdown("**Assign editor**")
                    df_ed = read_editores()
                    l1 = df_ed[df_ed["level"] == "1"]["login"].tolist()
                    current = row["editor_asignado"]
                    opts = ["— Unassigned —"] + l1
                    idx0 = opts.index(current) if current in opts else 0
                    selected = st.selectbox("Associate Editor", opts, index=idx0,
                                            key=f"assign_{aid}")
                    if st.button("💾 Save assignment", key=f"save_assign_{aid}"):
                        new_val = "" if selected == "— Unassigned —" else selected
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

            # --- ACTIONS ---
            with c2:
                st.write("**Actions**")
                estado = row["estado"]

                if estado == wf.STATE_EN_REVISION:
                    st.info(f"Status: **en_revision**")

                    if is_chief:
                        st.markdown("---")
                        st.caption("**Chief options**")
                        if st.button("🚀 Advance to 'todos_aprobados' (force)",
                                     key=f"force_adv_{aid}",
                                     use_container_width=True, type="primary"):
                            df_full = read_articles()
                            res = wf.force_advance_to_todos_aprobados(df_full, aid)
                            if res.ok and save_articles(df_full):
                                append_log("FORCE-ADV", aid, res.message)
                                st.success(res.message)
                                st.rerun()
                            else:
                                st.error(res.message)

                elif estado == wf.STATE_TODOS_APROBADOS:
                    st.success("🎉 Ready for publication.")
                    if is_chief:
                        st.markdown("---")
                        st.caption("**Chief only**")
                        has_creds = bool(st.secrets.get("zenodo", {}).get("token")) or \
                                    bool(st.secrets.get("zenodo", {}).get("sandbox_token"))
                        simulate = st.checkbox(
                            "Simulate Zenodo (test mode)", value=True,
                            key=f"sim_{aid}",
                            help="If unchecked, uses real Zenodo (requires [zenodo] in secrets.toml).",
                        )
                        if not simulate and not has_creds:
                            st.warning("No Zenodo credentials. Keep simulation ON.")
                        if st.button("🚀 PUBLISH", key=f"pub_{aid}",
                                     use_container_width=True, type="primary"):
                            real_pub = None
                            if not simulate:
                                sub = get_submission(aid)
                                vs = (sub or {}).get("versions", [])
                                pdf_name = vs[-1]["pdf"] if vs else ""
                                pdf_bytes = None
                                if pdf_name:
                                    try:
                                        transport, sftp = _sftp_connect()
                                        rs = st.secrets["remote_server"]
                                        revista_base = rs.get("revista_dir",
                                            rs["dir"].rsplit("/editorial", 1)[0]).rstrip("/")
                                        versions_dir = f"{rs['dir'].rstrip('/')}/versiones"
                                        for p in [f"{versions_dir}/{pdf_name}",
                                                  f"{revista_base}/PDF/{pdf_name}",
                                                  f"{revista_base}/BOOKS/pdf/{aid}.pdf"]:
                                            try:
                                                with sftp.open(p, "rb") as f:
                                                    pdf_bytes = f.read()
                                                break
                                            except Exception:
                                                continue
                                        sftp.close(); transport.close()
                                    except Exception as e:
                                        st.error(f"Could not read PDF: {e}")
                                if pdf_bytes and sub:
                                    real_pub = lambda a, s=sub, b=pdf_bytes: publish_to_zenodo_real(a, s, b)
                                else:
                                    st.error("Cannot publish: PDF not found.")
                            if simulate or real_pub is not None:
                                df_full = read_articles()
                                res = wf.publish(df_full, aid, simulate=simulate,
                                                 real_publisher=real_pub)
                                if res.ok:
                                    save_articles(df_full)
                                    sub = get_submission(aid) or {}
                                    sub["status"] = "published"
                                    sub["zenodo"] = {
                                        "doi": res.doi, "url": res.url,
                                        "environment": "simulated" if simulate else "real",
                                        "published_at": datetime.now().isoformat(timespec="seconds"),
                                    }
                                    save_submission_json(aid, sub)
                                    append_log("PUBLISH", aid,
                                               f"DOI {res.doi} (sim={simulate})")
                                    st.balloons()
                                    st.success(res.message)
                                    st.rerun()
                                else:
                                    st.error(res.message)
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

                elif estado == wf.STATE_RETIRADO:
                    st.warning(f"Retired: {row['motivo_retiro']}")
                    if is_chief:
                        if st.button("♻️ Restore", key=f"rest_{aid}", use_container_width=True):
                            df_full = read_articles()
                            res = wf.restore(df_full, aid)
                            if res.ok and save_articles(df_full):
                                append_log("RESTORE", aid, "Restored")
                                st.success(res.message)
                                st.rerun()
                            else:
                                st.error(res.message)

                if estado in {wf.STATE_EN_REVISION, wf.STATE_TODOS_APROBADOS, wf.STATE_PUBLISHED}:
                    if is_chief or (row["editor_asignado"] == user_login):
                        st.markdown("---")
                        if st.button("🚫 Retire", key=f"ret_{aid}_{idx}",
                                     use_container_width=True):
                            st.session_state[f"retirando_{aid}"] = True
                        if st.session_state.get(f"retirando_{aid}", False):
                            with st.form(f"ret_form_{aid}_{idx}"):
                                motivo = st.selectbox("Reason", MOTIVOS_RETIRO)
                                comentario = st.text_input("Comment")
                                cc1, cc2 = st.columns(2)
                                with cc1:
                                    if st.form_submit_button("✅ Confirm",
                                                             use_container_width=True):
                                        df_full = read_articles()
                                        res = wf.retire(df_full, aid, motivo=motivo)
                                        if res.ok and save_articles(df_full):
                                            append_log("RETIRE", aid, f"{motivo} — {comentario}")
                                            st.session_state[f"retirando_{aid}"] = False
                                            st.success(res.message)
                                            st.rerun()
                                        else:
                                            st.error(res.message)
                                with cc2:
                                    if st.form_submit_button("❌ Cancel",
                                                             use_container_width=True):
                                        st.session_state[f"retirando_{aid}"] = False
                                        st.rerun()


# ============================================================
# MAIN
# ============================================================
def main():
    if not authenticate_user():
        return
    logout_button()

    st.title("🧑‍⚖️ Reviewers Management")
    tab_articles()


if __name__ == "__main__":
    main()
