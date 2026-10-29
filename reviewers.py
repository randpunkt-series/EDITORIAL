"""
RandPunkt Journal — Editorial panel (Single View).
Gestión de manuscritos, envío a coautores, descargas y publicación en Zenodo Sandbox.
"""

import io
import json
import os
import re
import time
import zipfile
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import date, datetime
from io import StringIO, BytesIO
from pathlib import Path

import pandas as pd
import paramiko
import streamlit as st
import requests

from auth_utils import authenticate_user, logout_button, read_remote_csv_generic
# Importamos funciones de Zenodo (que leen de secrets.toml)
from zenodo_service import (
    create_deposition,
    upload_file,
    publish_deposition,
    build_manuscript_zenodo_metadata,
)

# ============================================================
# PAGE CONFIGURATION
# ============================================================
st.set_page_config(
    page_title="RandPunkt — Editorial Panel",
    page_icon="randpunkt-favicon.png",
    layout="wide",
)

# ============================================================
# CONSTANTS
# ============================================================
COLS_ARTICULOS = [
    "articulo_id", "tipo", "titulo", "ruta_pdf", "ruta_cover",
    "fecha_creacion", "estado", "isbn", "motivo_retiro", "fecha_retiro",
    "idioma_original", "formato_original", "editor_asignado",
]
COLS_REVISORES = [
    "articulo_id", "titulo", "nombre_revisor", "email_revisor",
    "fecha_envio_revision", "version", "notas",
]
COLS_VERSIONES = [
    "articulo_id", "fecha", "ruta_pdf", "nombre_archivo", "notas",
]

ESTADOS_ACTIVOS = [
    "en_revision", "aprobado_por_asociado", "listo_para_coautores",
    "aprobado_final", "aceptado", "todos_aprobados", "published"
]
ESTADOS_RETIRADOS = ["retirado"]
MOTIVOS_RETIRO = [
    "rejected_by_reviewers", "withdrawn_by_author", "author_unresponsive",
    "duplicate", "out_of_scope", "other",
]

# ============================================================
# SFTP LAYER
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
# EMAIL & HELPERS
# ============================================================
def _send_email(recipient: str, subject: str, body: str, attachment_bytes: bytes = None, attachment_name: str = None) -> bool:
    try:
        cfg = st.secrets.get("email", {})
        if not cfg.get("smtp_server"):
            print("[WARN] SMTP configuration not found in secrets.toml")
            return False
        msg = MIMEMultipart()
        msg["From"] = f"{cfg.get('sender_name', 'RandPunkt')} <{cfg['sender_email']}>"
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        if attachment_bytes and attachment_name:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment_bytes)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={attachment_name}")
            msg.attach(part)
        server = smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]))
        server.starttls()
        server.login(cfg["sender_email"], cfg["sender_password"])
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"[ERROR] Could not send email to {recipient}: {e}")
        return False

def notify_generic(recipient_email, recipient_name, subject, mensaje, attachment_bytes=None, attachment_name=None):
    body = f"Dear {recipient_name},\n\n{mensaje}\n\nBest regards,\nEditorial Committee\nRandPunkt Journal"
    return _send_email(recipient_email, subject, body, attachment_bytes, attachment_name)

# ============================================================
# CSV READ/WRITE
# ============================================================
def _read_csv(filename: str, columns: list) -> pd.DataFrame:
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
        sftp.close()
        transport.close()
        return df
    except Exception as e:
        st.error(f"Error reading {filename}: {e}")
        return pd.DataFrame(columns=columns)

def _save_csv(filename: str, df: pd.DataFrame) -> bool:
    try:
        transport, sftp = _sftp_connect()
        path = _remote_path(filename)
        tmp_path = f"{path}.tmp"
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{path}.{ts}.bak"
            with sftp.open(path, "r") as src:
                with sftp.open(backup_path, "w") as dst:
                    dst.write(src.read())
        except IOError:
            pass
        new_content = df.to_csv(index=False).encode("utf-8")
        with sftp.open(tmp_path, "w") as f:
            f.write(new_content)
        try:
            sftp.remove(path)
        except IOError:
            pass
        sftp.rename(tmp_path, path)
        base_name = os.path.basename(filename)
        _cleanup_old_backups(sftp, base_name, keep_days=30)
        sftp.close()
        transport.close()
        return True
    except Exception as e:
        st.error(f"Error saving {filename}: {e}")
        return False

def read_articles() -> pd.DataFrame:
    return _read_csv(st.secrets["csv"]["articulos_file"], COLS_ARTICULOS)

def save_articles(df: pd.DataFrame) -> bool:
    return _save_csv(st.secrets["csv"]["articulos_file"], df)

def read_reviewers() -> pd.DataFrame:
    return _read_csv(st.secrets["csv"]["revisiones_file"], COLS_REVISORES)

def save_reviewers(df: pd.DataFrame) -> bool:
    return _save_csv(st.secrets["csv"]["revisiones_file"], df)

def read_versions() -> pd.DataFrame:
    return _read_csv(st.secrets["csv"]["versiones_file"], COLS_VERSIONES)

def save_versions(df: pd.DataFrame) -> bool:
    return _save_csv(st.secrets["csv"]["versiones_file"], df)

def read_editores() -> pd.DataFrame:
    return _read_csv(st.secrets["csv"]["editores_file"], ["login", "password", "level", "email"])

def get_submission(tracking_id: str) -> dict | None:
    try:
        transport, sftp = _sftp_connect()
        rs = st.secrets["remote_server"]
        revista_base = rs['revista_dir'].rstrip('/')
        json_path = f"{revista_base}/METADATA/{tracking_id}.json"
        with sftp.open(json_path, "r") as f:
            data = json.loads(f.read().decode("utf-8"))
        sftp.close()
        transport.close()
        return data
    except Exception as e:
        print(f"[ERROR] Could not read {tracking_id}.json: {e}")
        return None

def append_log(action: str, articulo_id: str, detail: str = ""):
    try:
        transport, sftp = _sftp_connect()
        log_path = _remote_path(st.secrets["csv"]["log_file"])
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} | {action:10s} | {articulo_id:15s} | {detail}\n"
        try:
            with sftp.open(log_path, "r") as f:
                existing = f.read().decode("utf-8")
        except FileNotFoundError:
            existing = ""
        with sftp.open(log_path, "w") as f:
            f.write((existing + line).encode("utf-8"))
        sftp.close()
        transport.close()
    except Exception as e:
        print(f"[LOG] Could not write to log: {e}")

# ============================================================
# IMPORT FROM JOURNAL
# ============================================================
def import_from_revista() -> tuple[int, list]:
    rs = st.secrets["remote_server"]
    errors = []
    imported = 0
    try:
        transport, sftp = _sftp_connect()
        revista_base = rs['revista_dir'].rstrip('/')
        metadata_dir = f"{revista_base}/METADATA"
        pdf_dir = f"{revista_base}/PDF" 
        zip_dir = f"{revista_base}/ZIP"
        versions_dir = f"{rs['dir'].rstrip('/')}/versiones"
        _ensure_remote_dir(sftp, versions_dir)

        submissions = []
        try:
            for fname in sorted(sftp.listdir(metadata_dir)):
                if not fname.endswith(".json"): continue
                with sftp.open(f"{metadata_dir}/{fname}", "r") as f:
                    submissions.append(json.loads(f.read().decode("utf-8")))
        except FileNotFoundError:
            pass

        df_art = read_articles()
        df_ver = read_versions()
        existing_ids = set(df_art["articulo_id"].tolist()) if not df_art.empty else set()
        new_art_rows, new_ver_rows = [], []

        for data in submissions:
            art_id = data.get("tracking_id", "")
            if not art_id or (art_id in existing_ids and not df_ver[df_ver["articulo_id"] == art_id].empty):
                continue
            
            versions = data.get("versions", [])
            fecha = versions[0].get("submitted_at", "") if versions else ""
            pdf_filename = os.path.basename(versions[0].get("pdf", "")) if versions else ""
            zip_filename = os.path.basename(versions[0].get("supplementary_zip", "")) if versions else ""

            if pdf_filename:
                try:
                    sftp.stat(f"{pdf_dir}/{pdf_filename}")
                    sftp.get(f"{pdf_dir}/{pdf_filename}", f"{versions_dir}/{pdf_filename}")
                except: errors.append(f"PDF not copied for {art_id}")

            if zip_filename:
                try:
                    sftp.stat(f"{zip_dir}/{zip_filename}")
                    sftp.get(f"{zip_dir}/{zip_filename}", f"{versions_dir}/{zip_filename}")
                except: errors.append(f"ZIP not copied for {art_id}")

            if art_id not in existing_ids:
                new_art_rows.append({
                    "articulo_id": art_id, "tipo": "articulo", "titulo": data.get("titulo", ""),
                    "ruta_pdf": f"versiones/{pdf_filename}", "ruta_cover": "", "fecha_creacion": fecha,
                    "estado": "en_revision", "isbn": "", "motivo_retiro": "", "fecha_retiro": "",
                    "idioma_original": data.get("original_language", ""),
                    "formato_original": data.get("original_format", ""), "editor_asignado": "",
                })
            if pdf_filename:
                new_ver_rows.append({
                    "articulo_id": art_id, "fecha": fecha, "ruta_pdf": f"versiones/{pdf_filename}",
                    "nombre_archivo": pdf_filename, "notas": "Initial submission (imported)",
                })

        if new_art_rows:
            df_art = pd.concat([df_art, pd.DataFrame(new_art_rows)], ignore_index=True)
            save_articles(df_art)
            imported = len(new_art_rows)
        if new_ver_rows:
            df_ver = pd.concat([df_ver, pd.DataFrame(new_ver_rows)], ignore_index=True)
            save_versions(df_ver)

        sftp.close()
        transport.close()
        return imported, errors
    except Exception as e:
        return 0, [f"Connection error: {e}"]

# ============================================================
# MAIN VIEW (ÚNICA PANTALLA)
# ============================================================
def main():
    if not authenticate_user():
        return

    logout_button()

    user_level = st.session_state.get('editor_level', 1)
    user_login = st.session_state.get('editor_login', '')

    st.title("🧑‍⚖️ Editorial Panel")

    # --- Importar (solo Jefe) ---
    if user_level == 0:
        if st.button("📥 Import from journal", use_container_width=True, type="primary"):
            with st.spinner("Importing..."):
                n, errs = import_from_revista()
            if n > 0: st.success(f"✅ {n} new item(s) imported.")
            elif errs:
                for e in errs: st.error(e)
            else: st.info("No new manuscripts to import.")
            st.rerun()

    df_art_all = read_articles()
    if df_art_all.empty:
        st.info("No manuscripts registered yet.")
        return

    if user_level == 1:
        df_art_all = df_art_all[df_art_all["editor_asignado"] == user_login]
        if df_art_all.empty:
            st.info("You have no manuscripts assigned to you.")
            return

    # Filtro
    filtro = st.radio("Filter", ["Active", "Retired", "All"], horizontal=True, label_visibility="collapsed")
    if filtro == "Active":
        df_art = df_art_all[df_art_all["estado"].isin(ESTADOS_ACTIVOS)].reset_index(drop=True)
    elif filtro == "Retired":
        df_art = df_art_all[df_art_all["estado"].isin(ESTADOS_RETIRADOS)].reset_index(drop=True)
    else:
        df_art = df_art_all.reset_index(drop=True)

    st.markdown(f"### {len(df_art)} result(s)")
    st.dataframe(
        df_art[["articulo_id", "tipo", "titulo", "estado", "editor_asignado"]],
        use_container_width=True,
    )

    st.markdown("### Detail")
    for idx, row in df_art.iterrows():
        aid = row["articulo_id"]
        icon = "📚" if row["tipo"] == "libro" else "📄"
        header = f"{icon} {aid} — {row['titulo']}  |  status: {row['estado']}"
        
        with st.expander(header):
            c1, c2 = st.columns([4, 2])

            with c1:
                st.write(f"**ID:** `{aid}`")
                st.write(f"**Title:** {row['titulo']}")
                st.write(f"**Status:** `{row['estado']}`")

                if user_level == 1:
                    submission_data = get_submission(aid)
                    if submission_data:
                        author = submission_data.get("corresponding_author", {})
                        st.markdown("**Author Contact (Editor Only)**")
                        st.write(f"**Email:** `{author.get('email', '—')}`")
                if user_level == 0:
                    st.markdown("**Assign editor**")
                    df_ed = read_editores()
                    editores_l1 = df_ed[df_ed["level"] == "1"]["login"].tolist()
                    options = ["— Unassigned —"] + editores_l1
                    idx_asignado = options.index(row["editor_asignado"]) if row["editor_asignado"] in options else 0
                    selected = st.selectbox("Associate Editor", options, index=idx_asignado, key=f"as_{aid}")
                    if st.button("💾 Save assignment", key=f"save_{aid}"):
                        df_full = read_articles()
                        df_full.loc[df_full["articulo_id"] == aid, "editor_asignado"] = "" if selected == "— Unassigned —" else selected
                        save_articles(df_full)
                        st.rerun()

                # --- REQUEST COAUTHORS ---
                st.markdown("---")
                with st.expander("📧 Request Coauthors", expanded=False):
                    submission_data = get_submission(aid)
                    if submission_data:
                        author_email = submission_data["corresponding_author"]["email"]
                        st.write(f"Send request to author: {author_email}")
                        if st.button(f"📧 Send to {author_email}", key=f"send_co_{aid}"):
                            latest_pdf = submission_data["versions"][-1]["pdf"]
                            pdf_bytes = None
                            try:
                                transport, sftp = _sftp_connect()
                                with sftp.open(f"{st.secrets['remote_server']['dir']}/versiones/{latest_pdf}", "rb") as f:
                                    pdf_bytes = f.read()
                                sftp.close()
                                transport.close()
                            except: pass
                            
                            link = f"http://localhost:8502/?token=TOKEN_AQUI" # Simulación
                            ok = notify_generic(author_email, "Author", "Approval Required", "Please approve.", pdf_bytes, latest_pdf)
                            if ok: st.success("Email sent!")
                            else: st.error("Error sending email.")

                # --- ZENODO / DOWNLOADS ---
                st.markdown("---")
                st.markdown("**Download & Publish**")
                
                submission_data = get_submission(aid)
                if submission_data and "versions" in submission_data:
                    for v in submission_data["versions"]:
                        v_num = v["version"]
                        v_pdf = v["pdf"]
                        st.write(f"**Version v{v_num}**")
                        if st.button(f"⬇️ Download PDF v{v_num}", key=f"dl_{aid}_{v_num}"):
                            try:
                                transport, sftp = _sftp_connect()
                                pdf_bytes = None
                                for p in [f"{st.secrets['remote_server']['dir']}/versiones/{v_pdf}", f"{st.secrets['remote_server']['revista_dir']}/PDF/{v_pdf}"]:
                                    try:
                                        with sftp.open(p, "rb") as f: pdf_bytes = f.read()
                                        break
                                    except: continue
                                sftp.close()
                                transport.close()
                                if pdf_bytes:
                                    st.download_button("Save PDF", data=pdf_bytes, file_name=v_pdf, mime="application/pdf", key=f"save_{aid}_{v_num}")
                                else: st.error("PDF not found.")
                            except Exception as e: st.error(f"Error: {e}")

                    if row["estado"] == "todos_aprobados" and user_level == 0:
                        if st.button("🚀 PUBLISH TO ZENODO (Sandbox)", key=f"pub_{aid}", type="primary"):
                            with st.spinner("Uploading to Zenodo Sandbox..."):
                                submission_data = get_submission(aid)
                                if not submission_data:
                                    st.error("Metadata JSON not found.")
                                else:
                                    versions = submission_data.get("versions", [])
                                    if not versions:
                                        st.error("No PDF versions registered.")
                                    else:
                                        latest_pdf = versions[-1]["pdf"]
                                        rs = st.secrets["remote_server"]
                                        versions_dir = f"{rs['dir'].rstrip('/')}/versiones"
                                        pdf_path = f"{versions_dir}/{latest_pdf}"
                                        
                                        try:
                                            transport, sftp = _sftp_connect()
                                            with sftp.open(pdf_path, "rb") as f:
                                                pdf_bytes = f.read()
                                            sftp.close()
                                            transport.close()
                                        except Exception as e:
                                            pdf_bytes = None
                                            st.error(f"Could not read the PDF: {e}")

                                        if pdf_bytes:
                                            try:
                                                zenodo_metadata = build_manuscript_zenodo_metadata(submission_data)
                                                dep = create_deposition(zenodo_metadata, use_sandbox=True)
                                                
                                                temp_pdf = Path(f"/tmp/{aid}.pdf")
                                                temp_pdf.write_bytes(pdf_bytes)
                                                upload_file(dep, temp_pdf, use_sandbox=True)
                                                
                                                published = publish_deposition(dep["id"], use_sandbox=True)
                                                
                                                doi = published.get("doi", "")
                                                record_url = published.get("links", {}).get("record_html", "")
                                                
                                                submission_data["status"] = "published"
                                                submission_data["zenodo"] = {"doi": doi, "url": record_url, "environment": "sandbox"}
                                                try:
                                                    transport, sftp = _sftp_connect()
                                                    json_path = f"{rs['revista_dir'].rstrip('/')}/METADATA/{aid}.json"
                                                    with sftp.open(json_path, "w") as f:
                                                        f.write(json.dumps(submission_data, indent=2).encode("utf-8"))
                                                    sftp.close()
                                                    transport.close()
                                                except Exception as e:
                                                    st.warning(f"Published, but could not update the JSON: {e}")

                                                df_full = read_articles()
                                                df_full.loc[df_full["articulo_id"] == aid, "estado"] = "published"
                                                save_articles(df_full)
                                                
                                                append_log("PUBLISH-ZENODO", aid, f"DOI: {doi} (sandbox)")
                                                st.balloons()
                                                st.success(f"Publication successful in Sandbox! DOI: {doi}")
                                                st.rerun()
                                            except Exception as e:
                                                st.error(f"Error publishing to Zenodo Sandbox: {e}")

            with c2:
                # --- ESTADOS ---
                st.write("**Actions**")
                if user_level == 1:
                    if row["estado"] == "en_revision":
                        if st.button("✅ Approve Review", key=f"app_{aid}"):
                            df_full = read_articles()
                            df_full.loc[df_full["articulo_id"] == aid, "estado"] = "aprobado_por_asociado"
                            save_articles(df_full); st.rerun()
                elif user_level == 0:
                    if row["estado"] == "aprobado_por_asociado":
                        if st.button("📝 Confirm Editing", key=f"edit_{aid}"):
                            df_full = read_articles()
                            df_full.loc[df_full["articulo_id"] == aid, "estado"] = "listo_para_coautores"
                            save_articles(df_full); st.rerun()

if __name__ == "__main__":
    main()
