"""
RandPunkt Journal — Editorial panel: reviewers and versions management.
Incluye sistema de roles (Level 0 = Jefe, Level 1 = Asociado) y filtro de correos.
"""

import io
import json
import os
import re
import time
from datetime import date, datetime
from io import StringIO

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

ESTADOS_ACTIVOS = ["en_revision", "aceptado", "todos_aprobados", "publicado"]
ESTADOS_RETIRADOS = ["retirado"]
MOTIVOS_RETIRO = [
    "rechazado_por_revisores",
    "retirado_por_autor",
    "no_respuesta_autor",
    "duplicado",
    "fuera_de_alcance",
    "otro",
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
# IMPORT FROM JOURNAL (CON DESCARGA DE ARCHIVOS)
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

        # Carpetas de origen en randpunkt.org
        revista_base = rs['revista_dir'].rstrip('/')
        metadata_dir = f"{revista_base}/METADATA"
        
        # IMPORTANTE: Carpetas en singular (PDF y ZIP)
        pdf_dir = f"{revista_base}/PDF" 
        zip_dir = f"{revista_base}/ZIP"
        
        # Carpeta destino en el servidor del panel de revisores
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

        books = []
        books_meta_dir = f"{revista_base}/BOOKS/metadata"
        try:
            for fname in sorted(sftp.listdir(books_meta_dir)):
                if not fname.endswith(".json"):
                    continue
                data = _read_json_remote(sftp, f"{books_meta_dir}/{fname}")
                if data:
                    books.append(data)
        except FileNotFoundError:
            pass

        df_art = read_articles()
        df_ver = read_versions()
        existing_ids = set(df_art["articulo_id"].tolist()) if not df_art.empty else set()

        new_art_rows = []
        new_ver_rows = []

        # --- PROCESAR ARTÍCULOS ---
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
            
            # COPIAR EL PDF Y ZIP
            if remote_pdf_src:
                try:
                    sftp.stat(remote_pdf_src)
                    dest_pdf = f"{versions_dir}/{pdf_filename}"
                    sftp.get(remote_pdf_src, dest_pdf)
                except Exception as e:
                    print(f"Error copiando PDF {pdf_filename}: {e}")
                    errors.append(f"Could not copy PDF for {art_id}")

            if remote_zip_src:
                try:
                    sftp.stat(remote_zip_src)
                    dest_zip = f"{versions_dir}/{zip_filename}"
                    sftp.get(remote_zip_src, dest_zip)
                except Exception as e:
                    print(f"Error copiando ZIP {zip_filename}: {e}")
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

        # --- PROCESAR LIBROS ---
        for data in books:
            book_id = data.get("id", "")
            if not book_id or book_id in existing_ids:
                continue
            
            raw_pdf_name = data.get("pdf", "")
            pdf_filename = os.path.basename(raw_pdf_name) if raw_pdf_name else ""
            
            remote_pdf_src = f"{revista_base}/BOOKS/pdfs/{pdf_filename}" if pdf_filename else ""

            if remote_pdf_src:
                try:
                    sftp.stat(remote_pdf_src)
                    dest_pdf = f"{versions_dir}/{pdf_filename}"
                    sftp.get(remote_pdf_src, dest_pdf)
                except Exception as e:
                    print(f"Error copiando libro PDF {pdf_filename}: {e}")

            new_art_rows.append({
                "articulo_id": book_id,
                "tipo": "libro",
                "titulo": data.get("title", ""),
                "ruta_pdf": f"versiones/{pdf_filename}" if pdf_filename else "",
                "ruta_cover": "",
                "fecha_creacion": data.get("created_at", ""),
                "estado": "en_revision",
                "isbn": data.get("isbn", ""),
                "motivo_retiro": "",
                "fecha_retiro": "",
                "idioma_original": data.get("original_language", ""),
                "formato_original": data.get("original_format", ""),
                "editor_asignado": "",
            })

            if pdf_filename:
                new_ver_rows.append({
                    "articulo_id": book_id,
                    "fecha": data.get("created_at", ""),
                    "ruta_pdf": f"versiones/{pdf_filename}",
                    "nombre_archivo": pdf_filename,
                    "notas": "Initial book file (imported)",
                })

        # Guardar cambios
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
def upload_version(articulo_id: str, file_bytes: bytes, original_name: str, notas: str) -> tuple[bool, str]:
    try:
        transport, sftp = _sftp_connect()
        versions_dir = f"{st.secrets['remote_server']['dir'].rstrip('/')}/versiones"
        _ensure_remote_dir(sftp, versions_dir)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", original_name)
        remote_name = f"{articulo_id}_{ts}_{safe_name}"
        remote_path = f"{versions_dir}/{remote_name}"

        with sftp.open(remote_path, "wb") as f:
            f.write(file_bytes)

        sftp.close()
        transport.close()

        df_ver = read_versions()
        new_row = {
            "articulo_id": articulo_id,
            "fecha": datetime.now().isoformat(timespec="seconds"),
            "ruta_pdf": f"versiones/{remote_name}",
            "nombre_archivo": remote_name,
            "notas": notas,
        }
        df_ver = pd.concat([df_ver, pd.DataFrame([new_row])], ignore_index=True)
        save_versions(df_ver)

        append_log("UPLOAD", articulo_id, f"{remote_name} — {notas}")
        return True, remote_name
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

    user_level = st.session_state.get('editor_level', 1)
    user_login = st.session_state.get('editor_login', '')
    editor_email_actual = st.session_state.get('editor_email', '').strip().lower()

    # Import button (only for Level 0)
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

    # Filter by level
    df_art_all = read_articles()
    if df_art_all.empty:
        st.info("No manuscripts or books registered yet.")
        return

    if user_level == 1:
        df_art_all = df_art_all[df_art_all["editor_asignado"] == user_login].reset_index(drop=True)
        if df_art_all.empty:
            st.info("You have no manuscripts assigned to you.")
            return

    # Add book manually (only Level 0)
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

    # Filter active/retired
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

                # Assignment (only for Level 0)
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
                        "Editor Asociado (Level 1)",
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

                with st.form(f"upload_version_{aid}_{idx}"):
                    file = st.file_uploader(
                        "Upload version (PDF)",
                        type=["pdf"],
                        key=f"file_{aid}_{idx}",
                    )
                    notas = st.text_input(
                        "Version notes",
                        value="",
                        key=f"notas_{aid}_{idx}",
                    )
                    if st.form_submit_button("⬆️ Upload", use_container_width=True):
                        if file is None:
                            st.error("Select a PDF.")
                        else:
                            ok, result = upload_version(
                                aid, file.getbuffer(), file.name, notas.strip()
                            )
                            if ok:
                                st.success(f"Uploaded: {result}")
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

            st.markdown("**Version history**")
            df_ver = list_versions_for(aid)
            if df_ver.empty:
                st.info("No versions uploaded yet.")
            else:
                # --- NUEVA LÓGICA DE DESCARGA ---
                for _, v_row in df_ver.iterrows():
                    filename = v_row["nombre_archivo"]
                    # Ruta completa en el servidor del panel de revisores
                    remote_path = f"{st.secrets['remote_server']['dir'].rstrip('/')}/versiones/{filename}"
                    
                    st.write(f"📄 **{filename}** ({v_row['fecha'][:10]})")
                    st.write(f"Notas: {v_row['notas']}")
                    
                    # Intentar descargar el archivo vía SFTP
                    try:
                        transport, sftp = _sftp_connect()
                        with sftp.open(remote_path, "rb") as f:
                            file_bytes = f.read()
                        sftp.close()
                        transport.close()
                        
                        # Determinar MIME type según la extensión
                        if filename.endswith(".zip"):
                            mime = "application/zip"
                        elif filename.endswith(".pdf"):
                            mime = "application/pdf"
                        else:
                            mime = "application/octet-stream"

                        st.download_button(
                            label=f"⬇️ Download {filename}",
                            data=file_bytes,
                            file_name=filename,
                            mime=mime,
                            key=f"dl_{aid}_{filename}"
                        )
                    except Exception as e:
                        st.error(f"No se pudo cargar el archivo: {e}")
                # ---------------------------------

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
                    version = st.text_input("Version sent", value="v1")

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
                    & (df_rev["version"] == version)
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
                    "version": version.strip(),
                    "notas": notas.strip(),
                }
                df_rev = pd.concat([df_rev, pd.DataFrame([new_row])], ignore_index=True)
                if save_reviewers(df_rev):
                    append_log("ASSIGN", art_id, f"{r_name} — {version}")
                    st.success(f"Assignment added: {art_id} → {r_name} ({version}).")
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
                st.write(f"**Version sent:** {row['version']}")
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
                    e_version = st.text_input("Version", value=row["version"], key=f"as_v_{idx}")
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
                            df_rev.loc[idx, "version"] = e_version.strip()
                            df_rev.loc[idx, "notas"] = e_notas.strip()
                            if save_reviewers(df_rev):
                                st.session_state[f"as_editing_{idx}"] = False
                                append_log("UPDATE-ASSIGN", row["articulo_id"], f"{e_name} — {e_version}")
                                st.success("Assignment updated.")
                                st.rerun()
                    if cancel_btn:
                        st.session_state[f"as_editing_{idx}"] = False
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
