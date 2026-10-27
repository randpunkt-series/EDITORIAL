"""
Helpers compartidos por todos los seeds.
Centraliza la conexión SFTP, la carga de secrets y la generación de tokens.
"""
from __future__ import annotations

import os
import toml
import paramiko
from io import StringIO
from datetime import datetime
from itsdangerous import URLSafeTimedSerializer


SECRETS_PATH = os.path.expanduser(
    "~/POLANCO/EDITORIAL/.streamlit/secrets.toml"
)


# ============================================================
# CONEXIÓN SFTP
# ============================================================
def load_secrets():
    return toml.load(SECRETS_PATH)


def sftp_connect():
    secrets = load_secrets()
    rs = secrets["remote_server"]
    key = paramiko.Ed25519Key.from_private_key(StringIO(rs["private_key"]))
    transport = paramiko.Transport((rs["host"], int(rs["port"])))
    transport.connect(username=rs["user"], pkey=key)
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp, rs


def revista_dir(rs: dict) -> str:
    if "revista_dir" in rs:
        return rs["revista_dir"].rstrip("/")
    return rs["dir"].rsplit("/editorial", 1)[0].rstrip("/")


def editorial_dir(rs: dict) -> str:
    return f"{revista_dir(rs)}/editorial"


def books_dir(rs: dict) -> str:
    return f"{revista_dir(rs)}/BOOKS"


def ensure_dir(sftp, path: str):
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


# ============================================================
# TOKENS
# ============================================================
def get_serializer():
    return URLSafeTimedSerializer(load_secrets()["app"]["secret_key"])


def generate_token(email: str, art_id: str, name: str) -> str:
    return get_serializer().dumps(
        {"email": email, "articulo_id": art_id, "nombre": name},
        salt="aprobacion-autor",
    )


# ============================================================
# ARCHIVOS DUMMY
# ============================================================
def dummy_pdf() -> bytes:
    return (
        b"%PDF-1.4\n%Seed test PDF\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        b"xref\n0 3\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n"
        b"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n110\n%%EOF"
    )


def dummy_cover() -> bytes:
    return bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb00430008060607060508070707"
        "090908070a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c"
        "1c28372b2c30313434341f27393d38323c2e333432ffc0000b080001000101011100ff"
        "c40014000100000000000000000000000000000000ffda0008010100003f00d2cf20ff"
        "d9"
    )


def unique_id(prefix: str) -> str:
    stamp = datetime.now().strftime("%H%M%S%f")[:9]
    return f"{prefix}-2026-{stamp}"


# ============================================================
# READ/WRITE CSV REMOTOS
# ============================================================
def read_csv(sftp, path: str, columns: list):
    import pandas as pd
    try:
        with sftp.open(path, "r") as f:
            content = f.read().decode("utf-8")
        df = pd.read_csv(StringIO(content), dtype=str).fillna("")
        for c in columns:
            if c not in df.columns:
                df[c] = ""
        return df[columns]
    except FileNotFoundError:
        return pd.DataFrame(columns=columns)


def write_csv(sftp, path: str, df):
    with sftp.open(path, "w") as f:
        f.write(df.to_csv(index=False).encode("utf-8"))


def append_csv(sftp, path: str, columns: list, new_row: dict):
    import pandas as pd
    df = read_csv(sftp, path, columns)
    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    write_csv(sftp, path, df)
    return df
