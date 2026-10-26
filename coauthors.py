"""
RandPunkt Journal — Editorial panel: coauthor approval management.
Incluye sistema de roles (Level 0 = Jefe, Level 1 = Asociado) y filtro de correos.
"""

import io
import os
import smtplib
import time
import json
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from io import StringIO

import pandas as pd
import paramiko
import streamlit as st
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature

from auth_utils import authenticate_user, logout_button, read_remote_csv_generic

# ============================================================
# PAGE CONFIGURATION
# ============================================================
st.set_page_config(
    page_title="RandPunkt — Approval Management",
    page_icon="randpunkt-favicon.png",
    layout="wide",
)

# ============================================================
# CSV COLUMNS
# ============================================================
COLS_ARTICULOS = [
    "articulo_id", "tipo", "titulo", "ruta_pdf", "ruta_cover",
    "fecha_creacion", "estado", "isbn", "motivo_retiro", "fecha_retiro",
    "idioma_original", "formato_original", "editor_asignado",
]
COLS_CONFIRMACIONES = [
    "articulo_id", "nombre", "email", "token",
    "aprobado", "fecha_aprobacion", "comentarios",
]
COLS_COAUTORES = ["Nombre", "Email"]
COLS_VERSIONES = [
    "articulo_id", "fecha", "ruta_pdf", "nombre_archivo", "notas",
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

def _ensure_directory(sftp, path):
    try:
        sftp.stat(path)
    except IOError:
        sftp.mkdir(path)

def _cleanup_old_backups(sftp, base_name: str, keep_days: int = 30):
    import re
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

def read_remote_csv(filename: str, columns: list) -> pd.DataFrame:
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
        st.error(f"Error reading {filename} from remote server: {e}")
        return pd.DataFrame(columns=columns)

def save_remote_csv(filename: str, df: pd.DataFrame) -> bool:
    try:
        transport, sftp = _sftp_connect()
        path = _remote_path(filename)
        tmp_path = f"{path}.tmp"

        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{path}.{timestamp}.bak"
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
        st.error(f"Error saving {filename} to remote server: {e}")
        return False

def read_articles() -> pd.DataFrame:
    return read_remote_csv(st.secrets["csv"]["articulos_file"], COLS_ARTICULOS)

def read_confirmations() -> pd.DataFrame:
    return read_remote_csv(st.secrets["csv"]["confirmaciones_file"], COLS_CONFIRMACIONES)

def read_versions() -> pd.DataFrame:
    return read_remote_csv(st.secrets["csv"]["versiones_file"], COLS_VERSIONES)

def save_articles(df: pd.DataFrame) -> bool:
    return save_remote_csv(st.secrets["csv"]["articulos_file"], df)

def save_confirmations(df: pd.DataFrame) -> bool:
    return save_remote_csv(st.secrets["csv"]["confirmaciones_file"], df)

def read_coauthors() -> pd.DataFrame:
    try:
        transport, sftp = _sftp_connect()
        path = _remote_path(st.secrets["csv"]["coautores_file"])
        try:
            with sftp.open(path, "r") as f:
                content = f.read().decode("utf-8")
            df = pd.read_csv(
                io.StringIO(content),
                dtype=str,
                skipinitialspace=True,
            ).fillna("")
            df.columns = [c.strip() for c in df.columns]
            if "Nombre" not in df.columns or "Email" not in df.columns:
                st.error("The coauthors CSV must have 'Nombre' and 'Email' columns.")
                sftp.close()
                transport.close()
                return pd.DataFrame(columns=COLS_COAUTORES)
            df["Nombre"] = df["Nombre"].str.strip()
            df["Email"] = df["Email"].str.strip()
            df["Email"] = df["Email"].str.replace(r"^<|>$", "", regex=True).str.strip()
            df = df[(df["Nombre"] != "") & (df["Email"] != "")].reset_index(drop=True)
            sftp.close()
            transport.close()
            return df[COLS_COAUTORES]
        except FileNotFoundError:
            sftp.close()
            transport.close()
            return pd.DataFrame(columns=COLS_COAUTORES)
    except Exception as e:
        st.error(f"Error reading coauthors from remote server: {e}")
        return pd.DataFrame(columns=COLS_COAUTORES)

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
        print(f"[ERROR] No se pudo leer {tracking_id}.json: {e}")
        return None

# ============================================================
# TOKENS
# ============================================================
def get_serializer():
    return URLSafeTimedSerializer(st.secrets["app"]["secret_key"])

def generate_token(email: str, article_id: str, name: str) -> str:
    s = get_serializer()
    return s.dumps(
        {"email": email, "articulo_id": article_id, "nombre": name},
        salt="aprobacion-autor",
    )

def validate_token(token: str):
    s = get_serializer()
    try:
        data = s.loads(token, salt="aprobacion-autor", max_age=2592000)
        return data, None
    except SignatureExpired:
        return None, "The link has expired (more than 30 days)."
    except BadTimeSignature:
        return None, "The link is invalid or has been tampered with."

# ============================================================
# EMAIL
# ============================================================
def build_html_email(name, title, link, article_id, is_reminder=False):
    cfg = st.secrets["app"]
    email_cfg = st.secrets["email"]

    logo_url = cfg["logo_url"]
    journal = cfg["journal_name"]
    series = cfg["journal_series"]
    issn = cfg["journal_issn"]
    editor_name = email_cfg["sender_name"]
    editor_email = email_cfg["notification_email"]

    prefix = "Reminder: " if is_reminder else ""
    intro = (
        "We would like to remind you that we have not yet received your "
        "approval confirmation for the following manuscript. Please review "
        "the link below."
        if is_reminder
        else
        "As part of the editorial process, we kindly ask you to review and "
        "approve the manuscript before its publication."
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{prefix}Manuscript approval</title>
</head>
<body style="margin:0; padding:0; background:#F5F4F0;
             font-family: Georgia, 'Times New Roman', serif;
             color:#1B2A4A; line-height:1.6;">
  <div style="max-width:620px; margin:0 auto; padding:24px 12px;">

    <div style="background:#FFFFFF; padding:24px 28px;
                border-bottom:3px solid #5B7A99; text-align:center;
                border-radius:2px 2px 0 0;">
      <img src="{logo_url}" alt="{journal}"
           style="max-width:240px; height:auto; display:block;
                  margin:0 auto 10px auto;">
      <p style="margin:0; font-size:12px; letter-spacing:0.05em;
                color:#8A8F98; text-transform:uppercase;">
        {series} &nbsp;&bull;&nbsp; ISSN {issn}
      </p>
    </div>

    <div style="background:#FFFFFF; padding:32px 28px;">
      <p style="margin-top:0;">Dear <strong>{name}</strong>,</p>

      <p>{intro}</p>

      <p style="margin:24px 0 8px 0;">Manuscript:</p>
      <p style="margin:0 0 24px 0; padding:12px 16px;
                background:#F5F4F0; border-left:3px solid #B08D57;
                font-style:italic;">
        &ldquo;{title}&rdquo;<br>
        <span style="font-size:12px; color:#8A8F98;
                     font-style:normal;">Reference: {article_id}</span>
      </p>

      <p>To confirm that you have read the manuscript and agree with its
         content, please click the button below:</p>

      <p style="text-align:center; margin:32px 0;">
        <a href="{link}"
           style="display:inline-block; background:#5B7A99; color:#FFFFFF;
                  padding:13px 32px; text-decoration:none;
                  border-radius:2px; font-family: Arial, sans-serif;
                  font-weight:bold; font-size:15px;
                  letter-spacing:0.03em;">
          Confirm approval
        </a>
      </p>

      <p style="font-size:13px; color:#8A8F98; margin-top:24px;">
        The link is personal and non-transferable, and expires in 30 days.
        If you have comments or suggestions, you may include them when
        confirming your approval.
      </p>

      <p style="margin-top:28px;">Best regards,<br>
         <strong>{editor_name}</strong><br>
         <span style="color:#8A8F98; font-size:13px;">{journal}</span>
      </p>
    </div>

    <div style="background:#F5F4F0; padding:16px 28px; font-size:11px;
                color:#8A8F98; text-align:center;
                border-top:1px solid #E8E6E1;
                border-radius:0 0 2px 2px;">
      This email was sent automatically by the editorial system of
      {journal}.<br>
      If you received it in error, please notify
      <a href="mailto:{editor_email}" style="color:#5B7A99;
         text-decoration:none;">{editor_email}</a>.
    </div>

  </div>
</body>
</html>"""

def build_plain_email(name, title, link, article_id, is_reminder=False):
    cfg = st.secrets["app"]
    email_cfg = st.secrets["email"]
    journal = cfg["journal_name"]
    editor_email = email_cfg["notification_email"]
    editor_name = email_cfg["sender_name"]

    prefix = "REMINDER: " if is_reminder else ""
    intro = (
        "We would like to remind you that we have not yet received your "
        "approval confirmation for the following manuscript."
        if is_reminder
        else
        "As part of the editorial process, we kindly ask you to review and "
        "approve the manuscript before its publication."
    )

    return f"""{prefix}Manuscript approval — {journal}

Dear {name},

{intro}

Manuscript: "{title}"
Reference: {article_id}

To confirm that you have read the manuscript and agree with its content,
please visit the following link:

{link}

The link is personal and non-transferable, and expires in 30 days.
If you have comments or suggestions, you may include them when
confirming your approval.

Best regards,
{editor_name}
{journal}

---
This email was sent automatically by the editorial system of {journal}.
If you received it in error, please notify {editor_email}.
"""

def send_email(recipient, name, link, title, article_id, is_reminder=False) -> bool:
    try:
        cfg = st.secrets["email"]
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{cfg['sender_name']} <{cfg['sender_email']}>"
        msg["To"] = recipient
        prefix = "Reminder — " if is_reminder else ""
        msg["Subject"] = f"{prefix}[RandPunkt] Approval required: {title}"

        plain = build_plain_email(name, title, link, article_id, is_reminder)
        html = build_html_email(name, title, link, article_id, is_reminder)

        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        server = smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]))
        server.starttls()
        server.login(cfg["sender_email"], cfg["sender_password"])
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        st.error(f"Error sending email to {recipient}: {e}")
        return False

def send_editor_notification(message: str, to_email: str = None):
    try:
        cfg = st.secrets["email"]
        recipient = to_email or cfg["notification_email"]
        
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{cfg['sender_name']} <{cfg['sender_email']}>"
        msg["To"] = recipient
        msg["Subject"] = "[RandPunkt] All authors have approved a manuscript"
        msg.attach(MIMEText(message, "plain", "utf-8"))

        server = smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]))
        server.starttls()
        server.login(cfg["sender_email"], cfg["sender_password"])
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        st.warning(f"Could not notify the editor: {e}")
        return False

# ============================================================
# EDITOR VIEW
# ============================================================
def editor_view():
    user_level = st.session_state.get('editor_level', 1)
    user_login = st.session_state.get('editor_login', '')
    editor_email_actual = st.session_state.get('editor_email', '').strip().lower()

    st.title("📚 Editorial Approval Management")

    tab1, tab2, tab3 = st.tabs([
        "📤 Request approvals",
        "📋 Status by manuscript",
        "📊 Full log",
    ])

    # ---------- TAB 1: REQUEST APPROVALS ----------
    with tab1:
        st.subheader("Associate coauthors with a manuscript or book")
        
        df_art = read_articles()

        if df_art.empty:
            st.warning("No manuscripts or books found.")
            return

        if user_level == 1:
            df_art = df_art[df_art["editor_asignado"] == user_login]
            if df_art.empty:
                st.info("You have no manuscripts assigned to you.")
                return

        art_options = {
            f"{r['articulo_id']} — {r['titulo']}": r["articulo_id"]
            for _, r in df_art.iterrows()
        }
        art_label = st.selectbox(
            "Manuscript / Book", list(art_options.keys()), key="selected_article"
        )
        article_id = art_options[art_label]
        art_row = df_art[df_art["articulo_id"] == article_id].iloc[0]

        st.markdown(
            f"**Reference:** `{article_id}`  \n"
            f"**Type:** {art_row['tipo']}  \n"
            f"**Title:** {art_row['titulo']}  \n"
            f"**Status:** `{art_row['estado']}`"
        )

        idioma = art_row.get("idioma_original", "")
        formato = art_row.get("formato_original", "")
        if idioma or formato:
            st.info(
                f"**Original language:** {idioma or '—'}  \n"
                f"**Original format:** {formato or '—'}"
            )

        df_conf_existing = read_confirmations()
        existing = df_conf_existing[
            df_conf_existing["articulo_id"] == article_id
        ]
        if not existing.empty:
            st.info(
                f"{len(existing)} coauthor(s) already invited for this item."
            )
            st.dataframe(
                existing[["nombre", "email", "aprobado", "fecha_aprobacion"]],
                use_container_width=True,
            )

        st.divider()

        # --- INCLUIR AL AUTOR PARA CORRESPONDENCIA ---
        df_coauthors = read_coauthors()
        submission_data = get_submission(article_id)
        
        if submission_data:
            author = submission_data.get("corresponding_author", {})
            author_name = author.get("name", "").strip()
            author_email = author.get("email", "").strip()
            if author_name and author_email:
                # Añadir al autor si no está ya en la lista
                if author_email not in df_coauthors["Email"].values:
                    new_author_row = pd.DataFrame([{"Nombre": author_name, "Email": author_email}])
                    df_coauthors = pd.concat([df_coauthors, new_author_row], ignore_index=True)

        if df_coauthors.empty:
            st.warning("No coauthors found in the remote coauthors CSV.")
        else:
            st.info(
                f"{len(df_coauthors)} coauthor(s) available (including corresponding author)."
            )

        st.markdown("**Select the coauthors to notify:**")

        selected = []
        existing_emails = (
            set(existing["email"].tolist()) if not existing.empty else set()
        )

        select_all = st.checkbox("Select all", value=False)

        for i, row in df_coauthors.iterrows():
            name = row["Nombre"]
            email = row["Email"]
            key = f"coauthor_{i}_{email}"

            # FILTRO: No mostrar al editor actual en la lista de coautores
            if email.strip().lower() == editor_email_actual:
                continue

            already = email in existing_emails

            if select_all and not already:
                st.session_state[key] = True

            checked = st.checkbox(
                f"{name}  <{email}>"
                + ("  ✅ already invited" if already else ""),
                key=key,
                disabled=already,
            )
            if checked and not already:
                selected.append((name, email))

        if st.button(
            "📨 Send approval requests",
            use_container_width=True,
            type="primary",
        ):
            if not selected:
                st.error("You must select at least one coauthor.")
            else:
                df_conf = read_confirmations()
                base_url = st.secrets["app"]["base_url"].rstrip("/")
                successes = 0
                for name, email in selected:
                    token = generate_token(email, article_id, name)
                    link = f"{base_url}/?token={token}"
                    if send_email(
                        email, name, link,
                        art_row["titulo"], article_id,
                    ):
                        row = {
                            "articulo_id": article_id,
                            "nombre": name,
                            "email": email,
                            "token": token,
                            "aprobado": "0",
                            "fecha_aprobacion": "",
                            "comentarios": "",
                        }
                        df_conf = pd.concat(
                            [df_conf, pd.DataFrame([row])],
                            ignore_index=True,
                        )
                        successes += 1

                save_confirmations(df_conf)

                if art_row["estado"] == "publicado":
                    st.warning("Note: this item is already marked as 'publicado'.")
                elif art_row["estado"] != "pendiente_aprobacion":
                    df_art.loc[
                        df_art["articulo_id"] == article_id, "estado"
                    ] = "en_revision"
                    save_articles(df_art)

                st.success(
                    f"✅ Sent {successes}/{len(selected)} emails for {article_id}."
                )
                st.rerun()

    # ---------- TAB 2: STATUS BY MANUSCRIPT ----------
    with tab2:
        st.subheader("Approval status")
        df_art = read_articles()
        df_conf = read_confirmations()

        if user_level == 1:
            df_art = df_art[df_art["editor_asignado"] == user_login]
            my_manuscripts = df_art["articulo_id"].tolist()
            df_conf = df_conf[df_conf["articulo_id"].isin(my_manuscripts)]

        if df_art.empty:
            st.info("No manuscripts or books registered yet.")
        else:
            for _, art in df_art.iterrows():
                article_authors = df_conf[df_conf["articulo_id"] == art["articulo_id"]]
                total = len(article_authors)
                approved = (article_authors["aprobado"] == "1").sum() if total else 0

                icon = "✅" if total > 0 and approved == total else "⏳"
                with st.expander(
                    f"{icon} {art['articulo_id']} — {art['titulo']}  "
                    f"({approved}/{total} approved)"
                ):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.write(f"**Status:** `{art['estado']}`")
                        st.write(f"**Creation date:** {art['fecha_creacion']}")

                    if total == 0:
                        st.info("No coauthors associated yet.")
                    else:
                        table = article_authors[[
                            "nombre", "email", "aprobado",
                            "fecha_aprobacion", "comentarios",
                        ]].copy()
                        table["aprobado"] = table["aprobado"].map(
                            {"1": "✅ Yes", "0": "⏳ Pending"}
                        )
                        st.dataframe(table, use_container_width=True)

                        pending = article_authors[article_authors["aprobado"] == "0"]
                        if len(pending) > 0:
                            if st.button(
                                f"📧 Send reminder to "
                                f"{len(pending)} pending coauthor(s)",
                                key=f"rem_{art['articulo_id']}",
                            ):
                                base_url = st.secrets["app"]["base_url"].rstrip("/")
                                sent = 0
                                for _, p in pending.iterrows():
                                    link = f"{base_url}/?token={p['token']}"
                                    if send_email(
                                        p["email"], p["nombre"], link,
                                        art["titulo"], art["articulo_id"],
                                        is_reminder=True,
                                    ):
                                        sent += 1
                                st.success(f"Reminders sent: {sent}")

                    # --- BOTÓN DE PUBLICAR (SOLO PARA EL EDITOR EN JEFE) ---
                    if total > 0 and approved == total and art["estado"] != "publicado":
                        if user_level == 0: # JEFE
                            if st.button(
                                f"🚀 Publicar {art['articulo_id']}",
                                key=f"pub_{art['articulo_id']}",
                                use_container_width=True,
                                type="primary"
                            ):
                                df_art.loc[
                                    df_art["articulo_id"] == art["articulo_id"],
                                    "estado",
                                ] = "publicado"
                                save_articles(df_art)
                                st.success(f"{art['articulo_id']} publicado. Ahora aparece en el panel de libros/artículos finales.")
                                st.rerun()
                        else: # ASOCIADO
                            st.info("Todos los coautores han aprobado. Esperando la publicación final del Editor en Jefe.")

    # ---------- TAB 3: FULL LOG ----------
    with tab3:
        st.subheader("Full confirmation log")
        df_conf = read_confirmations()
        
        if user_level == 1:
            df_art = read_articles()
            my_manuscripts = df_art[df_art["editor_asignado"] == user_login]["articulo_id"].tolist()
            df_conf = df_conf[df_conf["articulo_id"].isin(my_manuscripts)]

        if df_conf.empty:
            st.info("No confirmations registered yet.")
        else:
            df_display = df_conf.drop(columns=["token"])
            st.dataframe(df_display, use_container_width=True)
            csv_data = df_conf.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ Download confirmations CSV",
                data=csv_data,
                file_name=f"confirmations_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
            )

# ============================================================
# AUTHOR VIEW (public)
# ============================================================
def author_view(token: str):
    st.title("✍️ Manuscript Approval")
    data, error = validate_token(token)
    if error:
        st.error(error)
        return

    email = data["email"]
    article_id = data["articulo_id"]
    name = data["nombre"]

    df_art = read_articles()
    df_conf = read_confirmations()

    art_row = df_art[df_art["articulo_id"] == article_id]
    if art_row.empty:
        st.error("The associated manuscript no longer exists.")
        return
    art = art_row.iloc[0]

    st.write(
        f"Dear **{name}**, you have been invited to approve "
        f"the following item:"
    )
    st.markdown(f"### {art['titulo']}")
    st.caption(f"Editorial reference: {article_id}")

    # --- MOSTRAR EL PDF PARA DESCARGA ---
    st.markdown("### Manuscript File")
    df_ver = read_versions()
    versions_for_art = df_ver[df_ver["articulo_id"] == article_id]
    
    if versions_for_art.empty:
        st.warning("⚠️ No manuscript file has been uploaded yet.")
    else:
        # Tomar la versión más reciente (ordenada por fecha descendente)
        latest_version = versions_for_art.sort_values("fecha", ascending=False).iloc[0]
        pdf_filename = latest_version["nombre_archivo"]
        
        # Intentar leer el archivo desde el servidor SFTP
        try:
            transport, sftp = _sftp_connect()
            versions_dir = f"{st.secrets['remote_server']['dir'].rstrip('/')}/versiones"
            remote_path = f"{versions_dir}/{pdf_filename}"
            
            with sftp.open(remote_path, "rb") as f:
                pdf_bytes = f.read()
                
            sftp.close()
            transport.close()
            
            st.download_button(
                label="⬇️ Download Manuscript (PDF)",
                data=pdf_bytes,
                file_name=pdf_filename,
                mime="application/pdf",
                use_container_width=True,
            )
            
        except Exception as e:
            st.error(f"Could not load the manuscript file: {e}")

    row = df_conf[
        (df_conf["articulo_id"] == article_id)
        & (df_conf["email"] == email)
    ]
    if row.empty:
        st.error("Your author record for this manuscript was not found.")
        return

    already_approved = row.iloc[0]["aprobado"] == "1"

    if already_approved:
        st.success(
            f"✅ You have already approved this item on "
            f"{row.iloc[0]['fecha_aprobacion']}."
        )
        st.info("Thank you for your confirmation.")
        return

    with st.form("approval_form"):
        st.write("Please confirm the following:")
        accept = st.checkbox(
            "I have read the manuscript in full and I agree with its "
            "content for publication."
        )
        comments = st.text_area(
            "Optional comments for the editor", height=120
        )
        submit = st.form_submit_button(
            "✅ Confirm approval", use_container_width=True
        )

    if submit:
        if not accept:
            st.warning("You must check the box to confirm your approval.")
            return

        idx = df_conf.index[
            (df_conf["articulo_id"] == article_id) & (df_conf["email"] == email)
        ]
        df_conf.loc[idx, "aprobado"] = "1"
        df_conf.loc[idx, "fecha_aprobacion"] = datetime.now().isoformat(
            timespec="seconds"
        )
        df_conf.loc[idx, "comentarios"] = comments
        save_confirmations(df_conf)

        st.success("Thank you! Your approval has been recorded.")
        st.balloons()

        article_authors = df_conf[df_conf["articulo_id"] == article_id]
        if (article_authors["aprobado"] == "1").all():
            df_art.loc[df_art["articulo_id"] == article_id, "estado"] = "todos_aprobados"
            save_articles(df_art)

            # Obtener el email del editor asociado asignado
            editor_asignado_login = art.get("editor_asignado", "")
            editor_email_dest = None
            if editor_asignado_login:
                df_editores = read_remote_csv_generic(
                    st.secrets["csv"]["editores_file"], 
                    ["login", "email"]
                )
                match_ed = df_editores[df_editores["login"] == editor_asignado_login]
                if not match_ed.empty:
                    editor_email_dest = match_ed.iloc[0]["email"]

            send_editor_notification(
                f"All authors of {article_id} "
                f"('{art['titulo']}') have approved the item. "
                f"You may proceed to publish it.",
                to_email=editor_email_dest
            )

# ============================================================
# MAIN ROUTER
# ============================================================
def main():
    query_params = st.query_params
    token = query_params.get("token")

    if token:
        author_view(token)
    else:
        if authenticate_user():
            logout_button()
            editor_view()


if __name__ == "__main__":
    main()
