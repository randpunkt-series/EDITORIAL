"""
RandPunkt Journal — Editorial panel: reviewers and versions management.
Includes role system (Level 0 = Chief, Level 1 = Associate), email filtering
and final publication in Zenodo exclusive to the Chief Editor.
"""

import io
import json
import os
import re
import time
import zipfile
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import date, datetime
from io import StringIO, BytesIO

import pandas as pd
import paramiko
import streamlit as st

from auth_utils import authenticate_user, logout_button, read_remote_csv_generic

# ============================================================
# PAGE CONFIGURATION
# ============================================================
st.set_page_config(
    page_title="RandPunkt — Reviewers Management",
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

# Add new states to the workflow
ESTADOS_ACTIVOS = [
    "en_revision", 
    "aprobado_por_asociado", 
    "listo_para_coautores", 
    "aprobado_final",
    "aceptado", 
    "todos_aprobados", 
    "published"
]
ESTADOS_RETIRADOS = ["retirado"]
MOTIVOS_RETIRO = [
    "rejected_by_reviewers",
    "withdrawn_by_author",
    "author_unresponsive",
    "duplicate",
    "out_of_scope",
    "other",
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
# EMAIL
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

def notify_generic(recipient_email: str, recipient_name: str, subject: str, mensaje: str, attachment_bytes: bytes = None, attachment_name: str = None):
    body = f"""Dear {recipient_name},

{mensaje}

Best regards,
Editorial Committee
RandPunkt Journal
"""
    return _send_email(recipient_email, subject, body, attachment_bytes, attachment_name)

# ============================================================
# ZENODO INTEGRATION
# ============================================================
def _get_zenodo_config():
    """Read Zenodo credentials from Streamlit secrets."""
    zen = st.secrets.get("zenodo", {})
    if zen.get("sandbox", False):
        return zen.get("sandbox_api", "https://sandbox.zenodo.org/api"), zen.get("sandbox_token", "")
    return zen.get("api", "https://zenodo.org/api"), zen.get("token", "")

def _zenodo_request(method: str, url: str, *, headers=None, json_data=None, data=None, timeout=30, max_retries=3):
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = requests.request(method, url, headers=headers, json=json_data, data=data, timeout=timeout)
            if r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code}: {r.text}")
            if r.status_code >= 400:
                print(f"[ZENODO ERROR] {r.status_code}: {r.text}")
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last_exc = e
            wait = 2 ** attempt
            time.sleep(wait)
    raise last_exc

def build_zenodo_metadata(submission: dict) -> dict:
    """Build the JSON metadata required by Zenodo."""
    authors = []
    ca = submission.get("corresponding_author", {})
    authors.append({
        "name": ca.get("name", "Unknown"),
        "affiliation": ca.get("affiliation", ""),
        "orcid": ca.get("orcid", "").replace("https://orcid.org/", ""),
    })
    for coauthor in submission.get("coauthors", []):
        authors.append({"name": coauthor})

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

    keywords.extend(["mathematics", "pure mathematics", "applied mathematics", "mathematical research", "peer-reviewed"])

    type_map = {
        "Short Note": "article",
        "Scoped Expository Article": "article",
        "Original Research Article": "article",
        "Research Article": "article",
        "Book": "book",
        "Book Chapter": "section",
    }

    submitted = submission.get("submitted_at", "")
    if not submitted:
        versions = submission.get("versions", [])
        if versions:
            submitted = versions[0].get("submitted_at", "")
    pub_date = submitted[:10] if len(submitted) >= 10 else "2026-01-01"

    community = st.secrets.get("zenodo", {}).get("community", "randpunkt")

    return {
        "title": submission.get("titulo", "Untitled"),
        "upload_type": "publication",
        "publication_type": type_map.get(submission.get("article_type"), "article"),
        "publication_date": pub_date,
        "description": submission.get("abstract", ""),
        "creators": authors,
        "keywords": keywords,
        "access_right": "open",
        "license": "cc-by-4.0",
        "communities": [{"identifier": community}],
    }

def publish_to_zenodo(articulo_id: str, submission_data: dict, pdf_path: str, pdf_bytes: bytes) -> tuple[bool, str]:
    """Upload the PDF to Zenodo and publish the deposition. Returns (ok, message)."""
    try:
        api_url, token = _get_zenodo_config()
        if not token:
            return False, "Zenodo token is not configured in secrets.toml"

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        
        metadata = build_zenodo_metadata(submission_data)
        payload = {"metadata": metadata}
        r = _zenodo_request("POST", f"{api_url}/deposit/depositions", headers=headers, json_data=payload)
        dep = r.json()
        dep_id = dep["id"]
        bucket_url = dep["links"]["bucket"]

        filename = f"{articulo_id}.pdf"
        file_headers = {"Authorization": f"Bearer {token}"}
        _zenodo_request("PUT", f"{bucket_url}/{filename}", headers=file_headers, data=pdf_bytes, timeout=300)

        r = _zenodo_request("POST", f"{api_url}/deposit/depositions/{dep_id}/actions/publish", headers=headers)
        published = r.json()

        doi = published.get("doi", "")
        record_url = published.get("links", {}).get("record_html", "")
        
        return True, f"Published successfully. DOI: {doi}"
    except Exception as e:
        return False, f"Error publishing to Zenodo: {e}"

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

# ============================================================
# LOG
# ============================================================
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
def _read_json_remote(sftp, path: str) -> dict | None:
    try:
        with sftp.open(path, "r") as f:
            return json.loads(f.read().decode("utf-8"))
    except Exception:
        return None

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
                if not fname.endswith(".json"):
                    continue
                data = _read_json_remote(sftp, f"{metadata_dir}/{fname}")
                if data:
                    submissions.append(data)
        except FileNotFoundError:
            pass

        df_art = read_articles()
        df_ver = read_versions()
        existing_ids = set(df_art["articulo_id"].tolist()) if not df_art.empty else set()

        new_art_rows = []
        new_ver_rows = []

        for data in submissions:
            art_id = data.get("tracking_id", "")
            if not art_id:
                continue
                
            is_new = art_id not in existing_ids
            needs_files = False
            if not is_new:
                existing_ver = df_ver[df_ver["articulo_id"] == art_id]
                if existing_ver.empty:
                    needs_files = True
            
            if not is_new and not needs_files:
                continue
            
            versions = data.get("versions", [])
            fecha = versions[0].get("submitted_at", "") if versions else ""
            
            raw_pdf_name = versions[0].get("pdf", "") if versions else ""
            raw_zip_name = versions[0].get("supplementary_zip", "") if versions else ""
            
            pdf_filename = os.path.basename(raw_pdf_name) if raw_pdf_name else ""
            zip_filename = os.path.basename(raw_zip_name) if raw_zip_name else ""

            remote_pdf_src = f"{pdf_dir}/{pdf_filename}" if pdf_filename else ""
            remote_zip_src = f"{zip_dir}/{zip_filename}" if zip_filename else ""
            
            if remote_pdf_src:
                try:
                    sftp.stat(remote_pdf_src)
                    dest_pdf = f"{versions_dir}/{pdf_filename}"
                    sftp.get(remote_pdf_src, dest_pdf)
                except Exception as e:
                    print(f"Error copying PDF {pdf_filename}: {e}")
                    errors.append(f"Could not copy PDF for {art_id}")

            if remote_zip_src:
                try:
                    sftp.stat(remote_zip_src)
                    dest_zip = f"{versions_dir}/{zip_filename}"
                    sftp.get(remote_zip_src, dest_zip)
                except Exception as e:
                    print(f"Error copying ZIP {zip_filename}: {e}")
                    errors.append(f"Could not copy ZIP for {art_id}")

            if is_new:
                new_art_rows.append({
                    "articulo_id": art_id,
                    "tipo": "articulo",
                    "titulo": data.get("titulo", ""),
                    "ruta_pdf": f"versiones/{pdf_filename}" if pdf_filename else "",
                    "ruta_cover": "",
                    "fecha_creacion": fecha,
                    "estado": "en_revision",
                    "isbn": "",
                    "motivo_retiro": "",
                    "fecha_retiro": "",
                    "idioma_original": data.get("original_language", ""),
                    "formato_original": data.get("original_format", ""),
                    "editor_asignado": "",
                })

            if pdf_filename:
                new_ver_rows.append({
                    "articulo_id": art_id,
                    "fecha": fecha,
                    "ruta_pdf": f"versiones/{pdf_filename}",
                    "nombre_archivo": pdf_filename,
                    "notas": "Initial submission (imported)",
                })

        if new_art_rows:
            df_new_art = pd.DataFrame(new_art_rows)
            df_art = pd.concat([df_art, df_new_art], ignore_index=True)
            if save_articles(df_art):
                imported = len(new_art_rows)
                for r in new_art_rows:
                    append_log("IMPORT", r["articulo_id"], r["titulo"])

        if new_ver_rows:
            df_new_ver = pd.DataFrame(new_ver_rows)
            df_ver = pd.concat([df_ver, df_new_ver], ignore_index=True)
            save_versions(df_ver)

        sftp.close()
        transport.close()

        return imported, errors

    except Exception as e:
        return 0, [f"Connection error: {e}"]

# ============================================================
# UPLOAD VERSION
# ============================================================
def upload_version(articulo_id: str, pdf_bytes: bytes, zip_bytes: bytes, pdf_name: str, zip_name: str, notas: str) -> tuple[bool, str]:
    try:
        transport, sftp = _sftp_connect()
        versions_dir = f"{st.secrets['remote_server']['dir'].rstrip('/')}/versiones"
        _ensure_remote_dir(sftp, versions_dir)
        
        submission_data = get_submission(articulo_id)
        if not submission_data:
            return False, "Metadata JSON not found."
        
        current_version = submission_data.get("current_version", 1)
        next_version = current_version + 1
        
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_pdf = re.sub(r"[^A-Za-z0-9._-]", "_", pdf_name)
        safe_zip = re.sub(r"[^A-Za-z0-9._-]", "_", zip_name)
        
        pdf_remote_name = f"{articulo_id}_v{next_version}_{ts}_{safe_pdf}"
        zip_remote_name = f"{articulo_id}_v{next_version}_{ts}_{safe_zip}"
        
        pdf_remote_path = f"{versions_dir}/{pdf_remote_name}"
        zip_remote_path = f"{versions_dir}/{zip_remote_name}"

        with sftp.open(pdf_remote_path, "wb") as f:
            f.write(pdf_bytes)
        with sftp.open(zip_remote_path, "wb") as f:
            f.write(zip_bytes)

        submission_data["current_version"] = next_version
        submission_data["versions"].append({
            "version": next_version,
            "submitted_at": datetime.now().isoformat(timespec="seconds"),
            "pdf": pdf_remote_name,
            "supplementary_zip": zip_remote_name,
            "notes": notas or f"Version {next_version} uploaded by editor",
        })
        
        rs = st.secrets["remote_server"]
        revista_base = rs['revista_dir'].rstrip('/')
        json_path = f"{revista_base}/METADATA/{articulo_id}.json"
        
        with sftp.open(json_path, "w") as f:
            f.write(json.dumps(submission_data, indent=2).encode("utf-8"))

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

        sftp.close()
        transport.close()

        append_log("UPLOAD", articulo_id, f"v{next_version} — {pdf_remote_name} + {zip_remote_name} — {notas}")
        return True, f"v{next_version}"
    except Exception as e:
        return False, str(e)

def list_versions_for(articulo_id: str) -> pd.DataFrame:
    df = read_versions()
    if df.empty:
        return df
    return df[df["articulo_id"] == articulo_id].sort_values("fecha", ascending=False)

# ============================================================
# HELPERS
# ============================================================
def days_since(iso_date_str: str) -> int:
    if not iso_date_str:
        return -1
    try:
        d = datetime.fromisoformat(iso_date_str).date()
        return (date.today() - d).days
    except Exception:
        return -1

def format_days(dias: int) -> str:
    if dias < 0:
        return "—"
    if dias == 0:
        return "today"
    if dias == 1:
        return "1 day"
    return f"{dias} days"

def unique_reviewers(df_rev: pd.DataFrame) -> pd.DataFrame:
    if df_rev.empty:
        return pd.DataFrame(
            columns=["nombre_revisor", "email_revisor", "asignaciones"]
        )
    grouped = (
        df_rev.groupby(["nombre_revisor", "email_revisor"], as_index=False)
        .size()
        .rename(columns={"size": "asignaciones"})
    )
    return grouped.sort_values("nombre_revisor").reset_index(drop=True)

def download_csv_button(df: pd.DataFrame, filename: str, label: str):
    if df.empty:
        return
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label=label,
        data=csv_bytes,
        file_name=filename,
        mime="text/csv",
        use_container_width=False,
    )

# ============================================================
# TAB 1: ARTICLES / BOOKS
# ============================================================
def tab_articles():
    st.subheader("📚 Manuscripts and books")

    if "success_message" in st.session_state:
        st.success(st.session_state["success_message"])
        del st.session_state["success_message"]

    user_level = st.session_state.get('editor_level', 1)
    user_login = st.session_state.get('editor_login', '')
    editor_email_actual = st.session_state.get('editor_email', '').strip().lower()

    if user_level == 0:
        col1, col2 = st.columns([3, 1])
        with col1:
            st.caption("Import submissions (articles) and books from the journal.")
        with col2:
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
                    st.info("No new manuscripts or books to import.")

    df_art_all = read_articles()
    if df_art_all.empty:
        st.info("No manuscripts or books registered yet.")
        return

    if user_level == 1:
        df_art_all = df_art_all[df_art_all["editor_asignado"] == user_login].reset_index(drop=True)
        if df_art_all.empty:
            st.info("You have no manuscripts assigned to you.")
            return

    if user_level == 0:
        with st.expander("➕ Add book manually", expanded=False):
            with st.form("add_book_form"):
                c1, c2 = st.columns(2)
                with c1:
                    new_id = st.text_input("Book ID", value="BK-2026-0001")
                    new_title = st.text_input("Title")
                    new_isbn = st.text_input("ISBN (optional)")
                with c2:
                    new_year = st.number_input("Year", min_value=1900, max_value=2100, value=2026)
                    new_author = st.text_input("Author(s)")
                    new_idioma = st.selectbox("Original language", ["Spanish", "English"])
                    new_formato = st.selectbox("Original format", ["LaTeX", "Microsoft Word"])
                new_desc = st.text_area("Description", height=80)
                submitted = st.form_submit_button("➕ Add book", use_container_width=True)

            if submitted:
                if not new_id.strip() or not new_title.strip():
                    st.error("ID and title are required.")
                else:
                    df_art = read_articles()
                    if not df_art.empty and new_id.strip() in df_art["articulo_id"].values:
                        st.error(f"The ID '{new_id.strip()}' already exists.")
                    else:
                        new_row = {
                            "articulo_id": new_id.strip(),
                            "tipo": "libro",
                            "titulo": new_title.strip(),
                            "ruta_pdf": "",
                            "ruta_cover": "",
                            "fecha_creacion": datetime.now().isoformat(timespec="seconds"),
                            "estado": "en_revision",
                            "isbn": new_isbn.strip(),
                            "motivo_retiro": "",
                            "fecha_retiro": "",
                            "idioma_original": new_idioma,
                            "formato_original": new_formato,
                            "editor_asignado": user_login,
                        }
                        df_art = pd.concat([df_art, pd.DataFrame([new_row])], ignore_index=True)
                        if save_articles(df_art):
                            append_log("CREATE", new_id.strip(), f"Book: {new_title.strip()}")
                            st.success(f"Book {new_id} added.")
                            st.rerun()

    filtro = st.radio(
        "Filter",
        ["Active", "Retired", "All"],
        horizontal=True,
        label_visibility="collapsed",
    )
    if filtro == "Active":
        df_art = df_art_all[df_art_all["estado"].isin(ESTADOS_ACTIVOS)].reset_index(drop=True)
    elif filtro == "Retired":
        df_art = df_art_all[df_art_all["estado"].isin(ESTADOS_RETIRADOS)].reset_index(drop=True)
    else:
        df_art = df_art_all.reset_index(drop=True)

    st.markdown(f"### {len(df_art)} result(s)")
    st.dataframe(
        df_art[[
            "articulo_id", "tipo", "titulo", "estado",
            "idioma_original", "formato_original", "editor_asignado",
        ]],
        use_container_width=True,
    )

    st.markdown("### Detail")
    for idx, row in df_art.iterrows():
        aid = row["articulo_id"]
        icon = "📚" if row["tipo"] == "libro" else "📄"
        header = f"{icon} {aid} — {row['titulo']}  |  status: {row['estado']}  |  assigned: {row['editor_asignado'] or 'None'}"
        with st.expander(header):
            c1, c2 = st.columns([4, 2])

            with c1:
                st.write(f"**ID:** `{aid}`")
                st.write(f"**Type:** {row['tipo']}")
                st.write(f"**Title:** {row['titulo']}")
                if row["tipo"] == "libro" and row["isbn"]:
                    st.write(f"**ISBN:** {row['isbn']}")
                st.write(f"**Original language:** {row['idioma_original'] or '—'}")
                st.write(f"**Original format:** {row['formato_original'] or '—'}")
                st.write(f"**Creation date:** {row['fecha_creacion']}")
                st.write(f"**Status:** `{row['estado']}`")
                if row["motivo_retiro"]:
                    st.write(f"**Retirement reason:** {row['motivo_retiro']}")
                if row["fecha_retiro"]:
                    st.write(f"**Retirement date:** {row['fecha_retiro']}")

                if user_level == 1:
                    submission_data = get_submission(aid)
                    if submission_data:
                        author = submission_data.get("corresponding_author", {})
                        st.markdown("---")
                        st.markdown("**Author Contact (Editor Only)**")
                        st.write(f"**Name:** {author.get('name', '—')}")
                        st.write(f"**Email:** `{author.get('email', '—')}`")
                        st.write(f"**Affiliation:** {author.get('affiliation', '—')}")

                if user_level == 0:
                    st.markdown("---")
                    st.markdown("**Assign editor**")
                    df_ed = read_editores()
                    editores_l1 = df_ed[df_ed["level"] == "1"]["login"].tolist()
                    current_assigned = row["editor_asignado"]
                    
                    options = ["— Unassigned —"] + editores_l1
                    try:
                        current_idx = options.index(current_assigned) if current_assigned in options else 0
                    except ValueError:
                        current_idx = 0

                    selected_editor = st.selectbox(
                        "Associate Editor (Level 1)",
                        options,
                        index=current_idx,
                        key=f"assign_{aid}"
                    )

                    if st.button("💾 Save assignment", key=f"save_assign_{aid}"):
                        new_val = "" if selected_editor == "— Unassigned —" else selected_editor
                        df_full = read_articles()
                        df_full.loc[df_full["articulo_id"] == aid, "editor_asignado"] = new_val
                        
                        if save_articles(df_full):
                            append_log("ASSIGN-EDITOR", aid, f"Assigned to {new_val or 'none'}")
                            st.success("Assignment updated.")
                            st.rerun()

            with c2:
                st.write("**Actions**")

                st.markdown("---")
                st.markdown("**Editorial Decisions**")
                
                estado_actual = row["estado"]

                if user_level == 1:
                    if estado_actual == "en_revision":
                        if st.button("✅ Approve Review (Send to Chief)", key=f"app_asoc_{aid}", use_container_width=True, type="primary"):
                            df_full = read_articles()
                            df_full.loc[df_full["articulo_id"] == aid, "estado"] = "aprobado_por_asociado"
                            if save_articles(df_full):
                                append_log("APPROVE-ASSOC", aid, "Approved by associate, sent to chief editor")
                                st.success("Approved by associate. The Chief Editor has been notified.")
                                st.rerun()
                                
                    elif estado_actual == "listo_para_coautores":
                        if st.button("✅ Give Final Approval", key=f"app_final_{aid}", use_container_width=True, type="primary"):
                            df_full = read_articles()
                            df_full.loc[df_full["articulo_id"] == aid, "estado"] = "todos_aprobados"
                            if save_articles(df_full):
                                append_log("APPROVE-FINAL", aid, "Final approval by associate, ready for coauthors")
                                st.success("Final approval recorded. Ready for the coauthors panel.")
                                st.rerun()

                elif user_level == 0:
                    if estado_actual == "aprobado_por_asociado":
                        st.info("Approved by the associate. Perform editing, translation and proofreading.")
                        if st.button("📝 Confirm Editing (Return to Associate)", key=f"edit_chief_{aid}", use_container_width=True, type="primary"):
                            df_full = read_articles()
                            df_full.loc[df_full["articulo_id"] == aid, "estado"] = "listo_para_coautores"
                            if save_articles(df_full):
                                append_log("EDIT-CHIEF", aid, "Editing complete, returned to associate")
                                st.success("Editing confirmed. The item has been returned to the associate editor.")
                                st.rerun()

                    elif estado_actual == "aprobado_final":
                        if st.button("🚀 Give Final Approval (Send to Coauthors)", key=f"app_final_chief_{aid}", use_container_width=True, type="primary"):
                            df_full = read_articles()
                            df_full.loc[df_full["articulo_id"] == aid, "estado"] = "todos_aprobados"
                            if save_articles(df_full):
                                append_log("APPROVE-FINAL-CHIEF", aid, "Final approval by chief, ready for coauthors")
                                st.success("Final approval given. The item now appears in the coauthors panel.")
                                st.rerun()

                    elif estado_actual == "todos_aprobados":
                        st.success("🎉 All coauthors have approved this manuscript.")
                        
                        if st.button("🚀 PUBLISH TO ZENODO", key=f"publish_zen_{aid}", use_container_width=True, type="primary"):
                            with st.spinner("Uploading to Zenodo..."):
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
                                            ok, msg = publish_to_zenodo(aid, submission_data, pdf_path, pdf_bytes)
                                            if ok:
                                                submission_data["status"] = "published"
                                                submission_data["zenodo"] = {"doi": msg.split("DOI: ")[-1], "url": ""}
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
                                                
                                                append_log("PUBLISH-ZENODO", aid, msg)
                                                st.balloons()
                                                st.success(f"Publication successful! {msg}")
                                                st.rerun()
                                            else:
                                                st.error(msg)

                with st.form(f"upload_version_{aid}_{idx}"):
                    st.markdown("**Upload new version (PDF + ZIP)**")
                    pdf_file = st.file_uploader(
                        "PDF",
                        type=["pdf"],
                        key=f"pdf_file_{aid}_{idx}",
                    )
                    zip_file = st.file_uploader(
                        "Supplementary ZIP",
                        type=["zip"],
                        key=f"zip_file_{aid}_{idx}",
                    )
                    notas = st.text_input(
                        "Version notes",
                        value="",
                        key=f"notas_{aid}_{idx}",
                    )
                    if st.form_submit_button("⬆️ Upload Version", use_container_width=True):
                        if pdf_file is None or zip_file is None:
                            st.error("You must select both the PDF and the ZIP.")
                        else:
                            ok, result = upload_version(
                                aid,
                                pdf_file.getbuffer(),
                                zip_file.getbuffer(),
                                pdf_file.name,
                                zip_file.name,
                                notas.strip()
                            )
                            if ok:
                                st.success(f"Version {result} uploaded successfully.")
                                st.rerun()
                            else:
                                st.error(f"Error: {result}")

                if row["estado"] in ESTADOS_ACTIVOS:
                    if st.button(
                        "🚫 Retire",
                        key=f"ret_{aid}_{idx}",
                        use_container_width=True,
                    ):
                        st.session_state[f"retirando_{aid}"] = True

                    if st.session_state.get(f"retirando_{aid}", False):
                        with st.form(f"ret_form_{aid}_{idx}"):
                            motivo = st.selectbox("Reason", MOTIVOS_RETIRO)
                            comentario = st.text_input("Comment (optional)")
                            cc1, cc2 = st.columns(2)
                            with cc1:
                                if st.form_submit_button("✅ Confirm", use_container_width=True):
                                    df_full = read_articles()
                                    m = df_full["articulo_id"] == aid
                                    df_full.loc[m, "estado"] = "retirado"
                                    df_full.loc[m, "motivo_retiro"] = motivo
                                    df_full.loc[m, "fecha_retiro"] = datetime.now().isoformat(timespec="seconds")
                                    if save_articles(df_full):
                                        append_log("RETIRE", aid, f"{motivo} — {comentario}")
                                        st.session_state[f"retirando_{aid}"] = False
                                        st.success("Retired.")
                                        st.rerun()
                            with cc2:
                                if st.form_submit_button("❌ Cancel", use_container_width=True):
                                    st.session_state[f"retirando_{aid}"] = False
                                    st.rerun()

                elif row["estado"] == "retirado":
                    if st.button(
                        "♻️ Restore",
                        key=f"rest_{aid}_{idx}",
                        use_container_width=True,
                    ):
                        df_full = read_articles()
                        m = df_full["articulo_id"] == aid
                        df_full.loc[m, "estado"] = "en_revision"
                        df_full.loc[m, "motivo_retiro"] = ""
                        df_full.loc[m, "fecha_retiro"] = ""
                        if save_articles(df_full):
                            append_log("RESTORE", aid, "Restored to en_revision")
                            st.success("Restored.")
                            st.rerun()

            if user_level == 0 and row["editor_asignado"]:
                if st.button(f"📧 Send email to {row['editor_asignado']}", key=f"email_editor_{aid}", use_container_width=True):
                    st.session_state[f"emailing_editor_{aid}"] = True

                if st.session_state.get(f"emailing_editor_{aid}", False):
                    with st.form(f"email_editor_form_{aid}"):
                        df_ed = read_editores()
                        match = df_ed[df_ed["login"] == row["editor_asignado"]]
                        editor_email = match.iloc[0]["email"] if not match.empty else ""
                        
                        st.markdown(f"**Send email to:** {row['editor_asignado']} <{editor_email}>")
                        asunto = st.text_input("Subject", value=f"Assignment: {aid} — {row['titulo']}")
                        mensaje = st.text_area("Message", height=150, placeholder="Type your message here...")
                        
                        cc1, cc2 = st.columns(2)
                        with cc1:
                            if st.form_submit_button("📧 Send", use_container_width=True):
                                if not mensaje.strip():
                                    st.error("The message cannot be empty.")
                                elif not editor_email:
                                    st.error("Editor email not found.")
                                else:
                                    ok = notify_generic(editor_email, row["editor_asignado"], asunto, mensaje.strip())
                                    if ok:
                                        st.success(f"Email sent to {editor_email}.")
                                        st.session_state[f"emailing_editor_{aid}"] = False
                                        st.rerun()
                                    else:
                                        st.error("Error sending email.")
                        with cc2:
                            if st.form_submit_button("❌ Cancel", use_container_width=True):
                                st.session_state[f"emailing_editor_{aid}"] = False
                                st.rerun()

            st.markdown("**Version history & Downloads**")
            
            rs = st.secrets["remote_server"]
            revista_base = rs['revista_dir'].rstrip('/')
            versions_dir = f"{rs['dir'].rstrip('/')}/versiones"
            
            submission_data = get_submission(aid)
            if submission_data and "versions" in submission_data and len(submission_data["versions"]) > 0:
                versions_list = submission_data["versions"]
                
                st.markdown("Select the version you want to package:")
                
                selected_pdf = None
                selected_zip = None
                selected_version_num = 1
                
                for i, ver in enumerate(versions_list):
                    v_num = ver.get("version", i + 1)
                    v_pdf = ver.get("pdf", "")
                    v_zip = ver.get("supplementary_zip", "")
                    v_date = ver.get("submitted_at", "")[:10]
                    v_notes = ver.get("notes", "")
                    
                    st.markdown(f"**Version v{v_num}** ({v_date}) - {v_notes}")
                    
                    col_pdf, col_zip = st.columns(2)
                    
                    if v_pdf:
                        with col_pdf:
                            if st.button(f"⬇️ Download PDF v{v_num}", key=f"btn_pdf_{aid}_{v_num}"):
                                with st.spinner(f"Loading PDF v{v_num}..."):
                                    try:
                                        transport, sftp = _sftp_connect()
                                        for path in [f"{revista_base}/PDF/{v_pdf}", f"{versions_dir}/{v_pdf}"]:
                                            try:
                                                with sftp.open(path, "rb") as f:
                                                    pdf_bytes = f.read()
                                                st.download_button(
                                                    label=f"Save PDF v{v_num}",
                                                    data=pdf_bytes,
                                                    file_name=v_pdf,
                                                    mime="application/pdf",
                                                    key=f"dl_pdf_{aid}_{v_num}"
                                                )
                                                break
                                            except:
                                                continue
                                        sftp.close()
                                        transport.close()
                                    except Exception as e:
                                        st.error(f"Error loading PDF: {e}")
                    
                    if v_zip:
                        with col_zip:
                            if st.button(f"⬇️ Download ZIP v{v_num}", key=f"btn_zip_{aid}_{v_num}"):
                                with st.spinner(f"Loading ZIP v{v_num}..."):
                                    try:
                                        transport, sftp = _sftp_connect()
                                        for path in [f"{revista_base}/ZIP/{v_zip}", f"{versions_dir}/{v_zip}"]:
                                            try:
                                                with sftp.open(path, "rb") as f:
                                                    zip_bytes = f.read()
                                                st.download_button(
                                                    label=f"Save ZIP v{v_num}",
                                                    data=zip_bytes,
                                                    file_name=v_zip,
                                                    mime="application/zip",
                                                    key=f"dl_zip_{aid}_{v_num}"
                                                )
                                                break
                                            except:
                                                continue
                                        sftp.close()
                                        transport.close()
                                    except Exception as e:
                                        st.error(f"Error loading ZIP: {e}")
                    
                    if st.radio(
                        f"Select v{v_num} for packaging",
                        options=["No", "Yes"],
                        index=0,
                        key=f"select_v{v_num}_{aid}",
                        horizontal=True
                    ) == "Yes":
                        selected_pdf = v_pdf
                        selected_zip = v_zip
                        selected_version_num = v_num

                st.divider()
                st.markdown("**Generate Edition Package**")
                st.caption("Creates a ZIP in /versiones/ with the PDF and ZIP of the selected version.")
                
                if selected_pdf and selected_zip:
                    st.info(f"Selected version: v{selected_version_num}")
                    
                    if st.button(f"📦 Generate Package v{selected_version_num} for {aid}", key=f"gen_pkg_{aid}", use_container_width=True):
                        with st.spinner("Packaging files..."):
                            try:
                                transport, sftp = _sftp_connect()
                                
                                pdf_data = None
                                for path in [f"{revista_base}/PDF/{selected_pdf}", f"{versions_dir}/{selected_pdf}"]:
                                    try:
                                        with sftp.open(path, "rb") as f:
                                            pdf_data = f.read()
                                        break
                                    except:
                                        continue
                                
                                zip_data = None
                                for path in [f"{revista_base}/ZIP/{selected_zip}", f"{versions_dir}/{selected_zip}"]:
                                    try:
                                        with sftp.open(path, "rb") as f:
                                            zip_data = f.read()
                                        break
                                    except:
                                        continue
                                
                                if not pdf_data or not zip_data:
                                    st.error("Could not read the files of the selected version.")
                                else:
                                    package_buffer = BytesIO()
                                    with zipfile.ZipFile(package_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                                        zf.writestr(selected_pdf, pdf_data)
                                        zf.writestr(selected_zip, zip_data)
                                    
                                    package_filename = f"{aid}_v{selected_version_num}_package.zip"
                                    remote_package_path = f"{versions_dir}/{package_filename}"
                                    
                                    _ensure_remote_dir(sftp, versions_dir)
                                    
                                    with sftp.open(remote_package_path, "wb") as f:
                                        f.write(package_buffer.getvalue())
                                    
                                    df_ver = read_versions()
                                    new_ver_row = {
                                        "articulo_id": aid,
                                        "fecha": datetime.now().isoformat(timespec="seconds"),
                                        "ruta_pdf": f"versiones/{package_filename}",
                                        "nombre_archivo": package_filename,
                                        "notas": f"Version v{selected_version_num} package generated",
                                    }
                                    df_ver = pd.concat([df_ver, pd.DataFrame([new_ver_row])], ignore_index=True)
                                    save_versions(df_ver)
                                    
                                    append_log("GENERATE_PACKAGE", aid, package_filename)
                                    st.session_state["success_message"] = f"Package {package_filename} generated and saved to /versiones/!"
                                    st.rerun()
                                    
                            except Exception as e:
                                st.error(f"Error generating package: {e}")
                else:
                    st.warning("You must select a version to package.")
            else:
                st.info("No versions registered for this manuscript.")

# ============================================================
# TAB 2: REVIEWERS
# ============================================================
def tab_reviewers():
    st.subheader("👤 Reviewers")
    st.caption(
        "A reviewer exists as long as they have at least one assignment. "
        "Editing name or email updates ALL their assignments."
    )

    df_rev = read_reviewers()
    
    if st.session_state.get('editor_level', 1) == 1:
        df_art = read_articles()
        my_manuscripts = df_art[df_art["editor_asignado"] == st.session_state['editor_login']]["articulo_id"].tolist()
        df_rev = df_rev[df_rev["articulo_id"].isin(my_manuscripts)]

    df_uniq = unique_reviewers(df_rev)

    if df_uniq.empty:
        st.info("No reviewers yet. Add one in the '🔗 Assignments' tab.")
        return

    st.markdown(f"### {len(df_uniq)} unique reviewer(s)")
    st.dataframe(df_uniq, use_container_width=True)

    st.markdown("### Detail")
    for idx, row in df_uniq.iterrows():
        name = row["nombre_revisor"]
        email = row["email_revisor"]
        n = row["asignaciones"]
        header = f"👤 {name} <{email}>  |  {n} assignment(s)"
        with st.expander(header):
            their = df_rev[
                (df_rev["nombre_revisor"] == name)
                & (df_rev["email_revisor"] == email)
            ]
            st.dataframe(
                their[["articulo_id", "titulo", "fecha_envio_revision", "version", "notas"]],
                use_container_width=True,
            )

            with st.form(f"rev_edit_{idx}"):
                e_name = st.text_input("Name", value=name, key=f"r_n_{idx}")
                e_email = st.text_input("Email", value=email, key=f"r_e_{idx}")
                if st.form_submit_button("💾 Save", use_container_width=True):
                    if not e_name.strip() or not e_email.strip():
                        st.error("Name and email are required.")
                    else:
                        mask = (
                            (df_rev["nombre_revisor"] == name)
                            & (df_rev["email_revisor"] == email)
                        )
                        df_rev.loc[mask, "nombre_revisor"] = e_name.strip()
                        df_rev.loc[mask, "email_revisor"] = e_email.strip()
                        if save_reviewers(df_rev):
                            append_log("UPDATE-REV", "-", f"{name} → {e_name.strip()}")
                            st.success("Reviewer updated.")
                            st.rerun()

# ============================================================
# TAB 3: ASSIGNMENTS
# ============================================================
def tab_assignments():
    st.subheader("🔗 Article/book ↔ reviewer assignments")
    st.caption(
        "Each row in the reviewers CSV is an assignment. "
        "Record who you sent which version to and when."
    )

    df_art_all = read_articles()
    user_level = st.session_state.get('editor_level', 1)
    user_login = st.session_state.get('editor_login', '')

    if user_level == 1:
        df_art_all = df_art_all[df_art_all["editor_asignado"] == user_login]

    df_art = df_art_all[df_art_all["estado"].isin(ESTADOS_ACTIVOS)].reset_index(drop=True)
    df_rev = read_reviewers()
    df_uniq = unique_reviewers(df_rev)

    if df_art.empty:
        st.warning("You must import or create at least one active manuscript/book.")
    else:
        with st.expander("➕ Add assignment", expanded=False):
            with st.form("add_assign_form"):
                art_options = {
                    f"{r['articulo_id']} — {r['titulo']}": (r["articulo_id"], r["titulo"])
                    for _, r in df_art.iterrows()
                }
                art_label = st.selectbox("Manuscript / Book", list(art_options.keys()))
                art_id, art_title = art_options[art_label]

                reviewer_options = ["— New reviewer —"]
                if not df_uniq.empty:
                    reviewer_options += [
                        f"{r['nombre_revisor']} <{r['email_revisor']}>"
                        for _, r in df_uniq.iterrows()
                    ]
                reviewer_choice = st.selectbox("Reviewer", reviewer_options)

                if reviewer_choice == "— New reviewer —":
                    new_name = st.text_input("Reviewer name")
                    new_email = st.text_input("Reviewer email")
                else:
                    new_name = ""
                    new_email = ""

                c1, c2 = st.columns(2)
                with c1:
                    assign_date = st.date_input("Date sent", value=date.today())
                with c2:
                    version_options = []
                    try:
                        submission_data = get_submission(art_id)
                        if submission_data and "versions" in submission_data:
                            for v in submission_data["versions"]:
                                v_num = v.get("version", 1)
                                version_options.append(f"v{v_num}")
                    except:
                        pass
                    if not version_options:
                        version_options = ["v1"]
                    
                    version = st.selectbox("Version sent", version_options)

                notas = st.text_input("Notes (optional)")

                submitted = st.form_submit_button(
                    "➕ Add assignment",
                    use_container_width=True,
                    type="primary",
                )

            if submitted:
                if reviewer_choice == "— New reviewer —":
                    if not new_name.strip() or not new_email.strip():
                        st.error("Name and email are required.")
                        return
                    r_name, r_email = new_name.strip(), new_email.strip()
                else:
                    r_name = reviewer_choice.split(" <")[0]
                    r_email = reviewer_choice.split(" <")[1].rstrip(">")

                dup = df_rev[
                    (df_rev["articulo_id"] == art_id)
                    & (df_rev["email_revisor"] == r_email)
                    & (df_rev["version"] == version.replace("v", ""))
                ]
                if not dup.empty:
                    st.error(f"That assignment already exists ({art_id}, {r_email}, {version}).")
                    return

                new_row = {
                    "articulo_id": art_id,
                    "titulo": art_title,
                    "nombre_revisor": r_name,
                    "email_revisor": r_email,
                    "fecha_envio_revision": datetime.combine(
                        assign_date, datetime.min.time()
                    ).isoformat(timespec="seconds"),
                    "version": version.replace("v", ""),
                    "notas": notas.strip(),
                }
                df_rev = pd.concat([df_rev, pd.DataFrame([new_row])], ignore_index=True)
                if save_reviewers(df_rev):
                    append_log("ASSIGN", art_id, f"{r_name} — {version}")
                    st.success(f"Assignment saved for {r_name} ({r_email}).")
                    st.rerun()

    st.divider()
    df_rev = read_reviewers()
    if df_rev.empty:
        st.info("No assignments yet.")
        return

    show = df_rev.copy()
    show["days"] = show["fecha_envio_revision"].apply(
        lambda s: format_days(days_since(s))
    )
    st.markdown(f"### {len(df_rev)} assignment(s)")
    st.dataframe(show, use_container_width=True)

    st.markdown("### Detail / edit / delete")
    for idx, row in df_rev.iterrows():
        dias_txt = format_days(days_since(row["fecha_envio_revision"]))
        header = (
            f"🔗 {row['articulo_id']} — {row['titulo']}  |  "
            f"{row['nombre_revisor']}  |  {row['version']}  |  {dias_txt}"
        )
        with st.expander(header):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.write(f"**Manuscript:** {row['articulo_id']} — {row['titulo']}")
                st.write(f"**Reviewer:** {row['nombre_revisor']}")
                st.write(f"**Email:** `{row['email_revisor']}`")
                st.write(f"**Date sent:** {row['fecha_envio_revision'][:10]} ({dias_txt})")
                st.write(f"**Version sent:** v{row['version']}")
                if row["notas"]:
                    st.write(f"**Notes:** {row['notas']}")
            with c2:
                if st.button("✏️", key=f"as_edit_{idx}", help="Edit"):
                    st.session_state[f"as_editing_{idx}"] = True

            if st.session_state.get(f"as_editing_{idx}", False):
                with st.form(f"as_edit_form_{idx}"):
                    try:
                        cur_date = datetime.fromisoformat(
                            row["fecha_envio_revision"]
                        ).date()
                    except Exception:
                        cur_date = date.today()

                    e_name = st.text_input("Reviewer name", value=row["nombre_revisor"], key=f"as_n_{idx}")
                    e_email = st.text_input("Reviewer email", value=row["email_revisor"], key=f"as_e_{idx}")
                    e_date = st.date_input("Date sent", value=cur_date, key=f"as_d_{idx}")
                    
                    version_options_edit = []
                    try:
                        submission_data_edit = get_submission(row["articulo_id"])
                        if submission_data_edit and "versions" in submission_data_edit:
                            for v in submission_data_edit["versions"]:
                                v_num = v.get("version", 1)
                                version_options_edit.append(f"v{v_num}")
                    except:
                        pass
                    if not version_options_edit:
                        version_options_edit = [f"v{row['version']}"]
                    
                    try:
                        cur_version_idx = version_options_edit.index(f"v{row['version']}")
                    except ValueError:
                        cur_version_idx = 0
                    
                    e_version = st.selectbox("Version", version_options_edit, index=cur_version_idx, key=f"as_v_{idx}")
                    e_notas = st.text_input("Notes", value=row["notas"], key=f"as_no_{idx}")

                    cs, cc = st.columns(2)
                    with cs:
                        save_btn = st.form_submit_button("💾 Save", use_container_width=True)
                    with cc:
                        cancel_btn = st.form_submit_button("Cancel", use_container_width=True)

                    if save_btn:
                        if not e_name.strip() or not e_email.strip():
                            st.error("Name and email are required.")
                        else:
                            df_rev.loc[idx, "nombre_revisor"] = e_name.strip()
                            df_rev.loc[idx, "email_revisor"] = e_email.strip()
                            df_rev.loc[idx, "fecha_envio_revision"] = datetime.combine(
                                e_date, datetime.min.time()
                            ).isoformat(timespec="seconds")
                            df_rev.loc[idx, "version"] = e_version.replace("v", "").strip()
                            df_rev.loc[idx, "notas"] = e_notas.strip()
                            if save_reviewers(df_rev):
                                st.session_state[f"as_editing_{idx}"] = False
                                append_log("UPDATE-ASSIGN", row["articulo_id"], f"{e_name} — {e_version}")
                                st.success("Assignment updated.")
                                st.rerun()
                    if cancel_btn:
                        st.session_state[f"as_editing_{idx}"] = False
                        st.rerun()

            if st.button("📧 Send email to reviewer", key=f"email_rev_{idx}", use_container_width=True):
                st.session_state[f"emailing_{idx}"] = True

            if st.session_state.get(f"emailing_{idx}", False):
                with st.form(f"email_form_{idx}"):
                    st.markdown(f"**Send email to:** {row['nombre_revisor']} <{row['email_revisor']}>")
                    st.markdown(f"**Manuscript:** {row['articulo_id']} — {row['titulo']}")
                    asunto = st.text_input("Subject", value=f"Re: {row['articulo_id']} — {row['titulo']}")
                    mensaje = st.text_area("Message", height=150, placeholder="Type your message here...")
                    
                    cc1, cc2 = st.columns(2)
                    with cc1:
                        if st.form_submit_button("📧 Send", use_container_width=True):
                            if not mensaje.strip():
                                st.error("The message cannot be empty.")
                            else:
                                package_bytes = None
                                package_filename = None
                                
                                try:
                                    transport, sftp = _sftp_connect()
                                    rs = st.secrets["remote_server"]
                                    versions_dir = f"{rs['dir'].rstrip('/')}/versiones"
                                    
                                    possible_packages = [
                                        f"{row['articulo_id']}_v{row['version']}_package.zip",
                                        f"{row['articulo_id']}_{row['version']}_package.zip",
                                        f"{row['articulo_id']}_v1_package.zip",
                                        f"{row['articulo_id']}_{row['version']}.zip"
                                    ]
                                    
                                    for pkg in possible_packages:
                                        try:
                                            with sftp.open(f"{versions_dir}/{pkg}", "rb") as f:
                                                package_bytes = f.read()
                                            package_filename = pkg
                                            break
                                        except:
                                            continue
                                    
                                    sftp.close()
                                    transport.close()
                                except Exception as e:
                                    print(f"[WARN] Could not read package: {e}")

                                ok = notify_generic(
                                    row["email_revisor"],
                                    row["nombre_revisor"],
                                    asunto,
                                    mensaje.strip(),
                                    attachment_bytes=package_bytes,
                                    attachment_name=package_filename
                                )
                                
                                if ok:
                                    st.success(f"Email sent to {row['email_revisor']}.")
                                    st.session_state[f"emailing_{idx}"] = False
                                    st.rerun()
                                else:
                                    st.error("Error sending email. Check Streamlit logs.")
                    with cc2:
                        if st.form_submit_button("❌ Cancel", use_container_width=True):
                            st.session_state[f"emailing_{idx}"] = False
                            st.rerun()

            if st.button("🗑️ Delete this assignment", key=f"as_del_{idx}"):
                df_rev = df_rev.drop(index=idx).reset_index(drop=True)
                if save_reviewers(df_rev):
                    append_log("DELETE-ASSIGN", row["articulo_id"], f"{row['nombre_revisor']} — {row['version']}")
                    st.success("Assignment deleted.")
                    st.rerun()

# ============================================================
# MAIN
# ============================================================
def main():
    if not authenticate_user():
        return

    logout_button()

    st.title("🧑‍⚖️ Reviewers Management")

    tab1, tab2, tab3 = st.tabs([
        "📄 Manuscripts and books",
        "👤 Reviewers",
        "🔗 Assignments",
    ])

    with tab1:
        tab_articles()
    with tab2:
        tab_reviewers()
    with tab3:
        tab_assignments()


if __name__ == "__main__":
    main()
