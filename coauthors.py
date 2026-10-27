"""
RandPunkt — Editorial panel: coauthor approval management (minimalista v2).

Usa workflow.py como fuente única de verdad.
"""
from __future__ import annotations

import io
import json
import os
import re
import smtplib
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from io import StringIO

import pandas as pd
import paramiko
import streamlit as st
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature

from auth_utils import authenticate_user, logout_button
import workflow as wf


st.set_page_config(
    page_title="RandPunkt — Approval Management",
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
COLS_COAUTORES = ["Nombre", "Email"]


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


def _cleanup_old_backups(sftp, base_name, keep_days=30):
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
        st.error(f"Error reading {filename}: {e}")
        return pd.DataFrame(columns=columns)


def save_remote_csv(filename, df):
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
        _cleanup_old_backups(sftp, os.path.basename(filename))
        sftp.close(); transport.close()
        return True
    except Exception as e:
        st.error(f"Error saving {filename}: {e}")
        return False


def read_articles():
    return read_remote_csv(st.secrets["csv"]["articulos_file"], COLS_ARTICULOS)

def save_articles(df):
    return save_remote_csv(st.secrets["csv"]["articulos_file"], df)

def read_confirmations():
    return read_remote_csv(st.secrets["csv"]["confirmaciones_file"], COLS_CONFIRMACIONES)

def save_confirmations(df):
    return save_remote_csv(st.secrets["csv"]["confirmaciones_file"], df)

def read_versions():
    return read_remote_csv(st.secrets["csv"]["versiones_file"], COLS_VERSIONES)

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
        print(f"[ERROR] get_submission: {e}")
        return None


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


def validate_token(token):
    try:
        data = get_serializer().loads(token, salt="aprobacion-autor", max_age=2592000)
        return data, None
    except SignatureExpired:
        return None, "The link has expired (more than 30 days)."
    except BadTimeSignature:
        return None, "The link is invalid or has been tampered with."


# ============================================================
# EMAIL
# ============================================================
def send_email(recipient, name, link, title, article_id, is_reminder=False):
    try:
        cfg = st.secrets["email"]
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{cfg['sender_name']} <{cfg['sender_email']}>"
        msg["To"] = recipient
        prefix = "Reminder — " if is_reminder else ""
        msg["Subject"] = f"{prefix}[RandPunkt] Approval required: {title}"
        intro = ("Please review and approve the following item."
                 if not is_reminder else
                 "Reminder: we have not received your approval yet.")
        plain = f"""Dear {name},

{intro}

Manuscript/Book: "{title}"
Reference: {article_id}

Please click the link below to confirm your approval:

{link}

Best regards,
{cfg['sender_name']}
"""
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        server = smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]))
        server.starttls()
        server.login(cfg["sender_email"], cfg["sender_password"])
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        st.error(f"Error sending email to {recipient}: {e}")
        return False


# ============================================================
# EDITOR VIEW
# ============================================================
def editor_view():
    is_chief = st.session_state.get("editor_level", 1) == 0
    user_login = st.session_state.get("editor_login", "")
    user_email = st.session_state.get("editor_email", "").strip().lower()

    st.title("📚 Editorial Approval Management")

    tab1, tab2, tab3 = st.tabs([
        "📤 Request approvals",
        "📋 Status by manuscript",
        "📊 Full log",
    ])

    with tab1:
        st.subheader("Invite coauthors to approve a manuscript/book")
        df_art = read_articles()
        if df_art.empty:
            st.warning("No manuscripts or books found.")
            return
        if not is_chief:
            df_art = df_art[df_art["editor_asignado"] == user_login]
            if df_art.empty:
                st.info("You have no manuscripts assigned to you.")
                return

        art_options = {f"{r['articulo_id']} — {r['titulo']}": r["articulo_id"]
                       for _, r in df_art.iterrows()}
        art_label = st.selectbox("Manuscript / Book", list(art_options.keys()))
        article_id = art_options[art_label]
        art_row = df_art[df_art["articulo_id"] == article_id].iloc[0]

        st.markdown(
            f"**Reference:** `{article_id}`  \n"
            f"**Title:** {art_row['titulo']}  \n"
            f"**Status:** `{art_row['estado']}`"
        )

        df_conf_existing = read_confirmations()
        existing = df_conf_existing[df_conf_existing["articulo_id"] == article_id]
        if not existing.empty:
            st.info(f"{len(existing)} coauthor(s) already invited.")
            st.dataframe(existing[["nombre", "email", "aprobado", "fecha_aprobacion"]],
                         use_container_width=True)

        df_coauthors = read_coauthors()
        sub = get_submission(article_id)
        if sub:
            a = sub.get("corresponding_author", {})
            a_name = a.get("name", "").strip()
            a_email = a.get("email", "").strip()
            if a_name and a_email and a_email not in df_coauthors["Email"].values:
                df_coauthors = pd.concat(
                    [df_coauthors, pd.DataFrame([{"Nombre": a_name, "Email": a_email}])],
                    ignore_index=True,
                )

        if df_coauthors.empty:
            st.warning("No coauthors available.")
        else:
            selected = []
            existing_emails = set(existing["email"].tolist()) if not existing.empty else set()
            select_all = st.checkbox("Select all")
            for i, r in df_coauthors.iterrows():
                name, email = r["Nombre"], r["Email"]
                key = f"coauthor_{i}_{email}"
                if email.strip().lower() == user_email:
                    continue
                already = email in existing_emails
                if select_all and not already:
                    st.session_state[key] = True
                checked = st.checkbox(f"{name} <{email}>" + (" ✅" if already else ""),
                                      key=key, disabled=already)
                if checked and not already:
                    selected.append((name, email))

            # Auto-seleccionar a todos los coautores nuevos (útil para
            # envíos masivos y para tests E2E que no pueden manejar los
            # checkboxes de Streamlit con facilidad).
            auto_send_all = st.checkbox(
                "Auto-select all new coauthors (skip manual selection)",
                value=True,
                key="auto_send_all",
                help="If checked, all coauthors who haven't been invited yet "
                     "will be included automatically, ignoring the manual "
                     "checkboxes above.",
            )

            if st.button("📨 Send approval requests", use_container_width=True, type="primary"):
                # Si auto_send_all, añadir todos los coautores nuevos a `selected`
                if auto_send_all:
                    for i, r in df_coauthors.iterrows():
                        name, email = r["Nombre"], r["Email"]
                        if email.strip().lower() == user_email:
                            continue  # no invitar al usuario logueado
                        if email in existing_emails:
                            continue  # ya invitado
                        # Evitar duplicados con lo ya seleccionado manualmente
                        if (name, email) not in selected:
                            selected.append((name, email))

                if not selected:
                    st.error("Select at least one coauthor (or enable auto-select).")
                else:
                    df_conf = read_confirmations()
                    base_url = st.secrets["app"]["base_url"].rstrip("/")
                    ok = 0
                    for name, email in selected:
                        token = generate_token(email, article_id, name)
                        link = f"{base_url}/?token={token}"
                        if send_email(email, name, link, art_row["titulo"], article_id):
                            df_conf = pd.concat([df_conf, pd.DataFrame([{
                                "articulo_id": article_id,
                                "nombre": name, "email": email, "token": token,
                                "aprobado": "0",
                                "fecha_aprobacion": "", "comentarios": "",
                            }])], ignore_index=True)
                            ok += 1
                    save_confirmations(df_conf)
                    st.success(f"✅ Sent {ok}/{len(selected)} emails.")
                    st.rerun()

    with tab2:
        st.subheader("Approval status")
        df_art = read_articles()
        df_conf = read_confirmations()
        if not is_chief:
            df_art = df_art[df_art["editor_asignado"] == user_login]
            my = df_art["articulo_id"].tolist()
            df_conf = df_conf[df_conf["articulo_id"].isin(my)]

        if df_art.empty:
            st.info("No manuscripts or books yet.")
        else:
            for _, art in df_art.iterrows():
                co = df_conf[df_conf["articulo_id"] == art["articulo_id"]]
                total = len(co)
                approved = int((co["aprobado"] == "1").sum()) if total else 0
                icon = "✅" if total > 0 and approved == total else "⏳"
                with st.expander(
                    f"{icon} {art['articulo_id']} — {art['titulo']}  "
                    f"({approved}/{total})  |  status: {art['estado']}"
                ):
                    st.write(f"**Status:** `{art['estado']}`")
                    st.write(f"**Arbitration confirmed:** `{art.get('arbitraje_confirmado', '0')}`")
                    if total == 0:
                        st.info("No coauthors associated yet.")
                    else:
                        table = co[["nombre", "email", "aprobado", "fecha_aprobacion", "comentarios"]].copy()
                        table["aprobado"] = table["aprobado"].map({"1": "✅ Yes", "0": "⏳ Pending"})
                        st.dataframe(table, use_container_width=True)

                        pending = co[co["aprobado"] == "0"]
                        if len(pending) > 0:
                            if st.button(f"📧 Remind {len(pending)} coauthor(s)",
                                         key=f"rem_{art['articulo_id']}"):
                                base_url = st.secrets["app"]["base_url"].rstrip("/")
                                sent = 0
                                for _, p in pending.iterrows():
                                    link = f"{base_url}/?token={p['token']}"
                                    if send_email(p["email"], p["nombre"], link,
                                                  art["titulo"], art["articulo_id"],
                                                  is_reminder=True):
                                        sent += 1
                                st.success(f"Reminders sent: {sent}")

    with tab3:
        st.subheader("Full confirmation log")
        df_conf = read_confirmations()
        if not is_chief:
            df_art = read_articles()
            my = df_art[df_art["editor_asignado"] == user_login]["articulo_id"].tolist()
            df_conf = df_conf[df_conf["articulo_id"].isin(my)]
        if df_conf.empty:
            st.info("No confirmations yet.")
        else:
            st.dataframe(df_conf.drop(columns=["token"]), use_container_width=True)
            st.download_button(
                "⬇️ Download CSV",
                data=df_conf.to_csv(index=False).encode("utf-8"),
                file_name=f"confirmations_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
            )


# ============================================================
# AUTHOR VIEW (público)
# ============================================================
def author_view(token):
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

    st.write(f"Dear **{name}**, you have been invited to approve:")
    st.markdown(f"### {art['titulo']}")
    st.caption(f"Reference: {article_id}")

    st.markdown("### Manuscript File")
    df_ver = read_versions()
    vfa = df_ver[df_ver["articulo_id"] == article_id]
    if vfa.empty:
        st.warning("⚠️ No file uploaded yet.")
    else:
        latest = vfa.sort_values("fecha", ascending=False).iloc[0]
        pdf_name = latest["nombre_archivo"]
        try:
            transport, sftp = _sftp_connect()
            versions_dir = f"{st.secrets['remote_server']['dir'].rstrip('/')}/versiones"
            with sftp.open(f"{versions_dir}/{pdf_name}", "rb") as f:
                pdf_bytes = f.read()
            sftp.close(); transport.close()
            st.download_button(
                "⬇️ Download Manuscript (PDF)",
                data=pdf_bytes, file_name=pdf_name, mime="application/pdf",
                use_container_width=True,
            )
        except Exception as e:
            st.error(f"Could not load file: {e}")

    # Buscar por token (único), no por email (puede repetirse si varios
    # coautores comparten el mismo buzón, p.ej. cpg7000cpg@gmail.com).
    row = df_conf[(df_conf["articulo_id"] == article_id) & (df_conf["token"] == token)]
    if row.empty:
        st.error("Your record was not found.")
        return

    if row.iloc[0]["aprobado"] == "1":
        st.success(f"✅ You already approved this on {row.iloc[0]['fecha_aprobacion']}.")
        return

    with st.form("approval_form"):
        st.write("Please confirm:")
        accept = st.checkbox(
            "I have read the manuscript in full and I agree with its content for publication."
        )
        comments = st.text_area("Optional comments for the editor", height=120)
        submit = st.form_submit_button("✅ Confirm approval", use_container_width=True)

    if submit:
        if not accept:
            st.warning("You must check the box.")
            return

        idx = df_conf.index[(df_conf["articulo_id"] == article_id) & (df_conf["token"] == token)]
        df_conf.loc[idx, "aprobado"] = "1"
        df_conf.loc[idx, "fecha_aprobacion"] = datetime.now().isoformat(timespec="seconds")
        df_conf.loc[idx, "comentarios"] = comments
        save_confirmations(df_conf)

        st.success("Thank you! Your approval has been recorded.")
        st.balloons()

        # Intentar auto-avance
        df_art_full = read_articles()
        df_conf_full = read_confirmations()
        res = wf.try_auto_advance_after_signature(df_art_full, df_conf_full, article_id)
        if res.ok:
            save_articles(df_art_full)
            st.info("All coauthors signed AND arbitration confirmed. "
                    "Item advanced to 'todos_aprobados'.")
        else:
            row2 = df_art_full[df_art_full["articulo_id"] == article_id].iloc[0]
            if row2["estado"] == wf.STATE_EN_REVISION:
                pend = wf.pending_coauthors(df_conf_full, article_id)
                if not pend.empty:
                    st.info(f"{len(pend)} coauthor(s) still pending.")
                elif not wf.is_arbitraje_confirmado(row2):
                    st.info("Waiting for the editor to confirm arbitration.")


# ============================================================
# MAIN ROUTER
# ============================================================
def main():
    qp = st.query_params
    token = qp.get("token")
    if token:
        author_view(token)
    else:
        if authenticate_user():
            logout_button()
            editor_view()


if __name__ == "__main__":
    main()
