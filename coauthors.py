"""
RandPunkt — Public coauthor approval view.
Only the author_view (token-based signature) is kept.
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


def validate_token(token):
    try:
        data = get_serializer().loads(token, salt="aprobacion-autor", max_age=2592000)
        return data, None
    except SignatureExpired:
        return None, "The link has expired (more than 30 days)."
    except BadTimeSignature:
        return None, "The link is invalid or has been tampered with."


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

        # Auto-avance si todos firmaron (sin arbitraje)
        df_art_full = read_articles()
        df_conf_full = read_confirmations()
        res = wf.try_auto_advance_after_signature(df_art_full, df_conf_full, article_id)
        
        if res.ok:
            save_articles(df_art_full)
            st.info("✅ All coauthors signed. Item advanced to 'todos_aprobados'.")
        else:
            row2 = df_art_full[df_art_full["articulo_id"] == article_id].iloc[0]
            if row2["estado"] == wf.STATE_EN_REVISION:
                pend = wf.pending_coauthors(df_conf_full, article_id)
                if not pend.empty:
                    st.info(f"⏳ {len(pend)} coauthor(s) still pending. Please wait for them to sign.")
                else:
                    st.info("✅ All coauthors have signed. The item is being processed.")


# ============================================================
# MAIN ROUTER
# ============================================================
def main():
    qp = st.query_params
    token = qp.get("token")
    if token:
        author_view(token)
    else:
        st.info("This page is only accessible via a personal approval link.")


if __name__ == "__main__":
    main()
