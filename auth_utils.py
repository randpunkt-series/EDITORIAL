import io
import pandas as pd
import streamlit as st
from io import StringIO
import paramiko

def get_sftp_connection():
    """Conexión SFTP genérica (reutilizable)."""
    rs = st.secrets["remote_server"]
    transport = paramiko.Transport((rs["host"], int(rs["port"])))
    key = paramiko.Ed25519Key.from_private_key(StringIO(rs["private_key"]))
    transport.connect(username=rs["user"], pkey=key)
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp

def read_remote_csv_generic(filename: str, columns: list) -> pd.DataFrame:
    """Lee un CSV del servidor remoto."""
    try:
        transport, sftp = get_sftp_connection()
        rs = st.secrets["remote_server"]
        path = f"{rs['dir'].rstrip('/')}/{filename}"
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

def authenticate_user():
    """
    Lógica de login centralizada.
    Lee registro_editores.csv y guarda login, level y email en st.session_state.
    """
    if "editor_authenticated" not in st.session_state:
        st.session_state.editor_authenticated = False

    if st.session_state.editor_authenticated:
        return True

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.title("🔒 Editorial Panel")
        st.caption("RandPunkt — Sistema de Gestión")
        
        with st.form("login_form"):
            login = st.text_input("Login")
            password = st.text_input("Password", type="password")
            submit = st.form_submit_button("Sign in", use_container_width=True)

        if submit:
            # Leer editores del servidor remoto (incluyendo email)
            df_editores = read_remote_csv_generic(
                st.secrets["csv"]["editores_file"], 
                ["login", "password", "level", "email"]
            )
            
            # Verificar credenciales
            match = df_editores[
                (df_editores["login"] == login.strip()) & 
                (df_editores["password"] == password.strip())
            ]
            
            if not match.empty:
                st.session_state.editor_authenticated = True
                st.session_state.editor_login = login.strip()
                st.session_state.editor_level = int(match.iloc[0]["level"])
                st.session_state.editor_email = match.iloc[0]["email"].strip()
                st.rerun()
            else:
                st.error("Credenciales incorrectas.")
    return False

def logout_button():
    """Botón de cerrar sesión en el sidebar."""
    with st.sidebar:
        st.write(f"**Usuario:** {st.session_state.get('editor_login', '—')}")
        level = st.session_state.get('editor_level', -1)
        role = "Jefe (Level 0)" if level == 0 else "Asociado (Level 1)" if level == 1 else "Desconocido"
        st.write(f"**Rol:** {role}")
        st.write(f"**Email:** {st.session_state.get('editor_email', '—')}")
        st.divider()
        if st.button("🔒 Sign out", use_container_width=True):
            st.session_state.editor_authenticated = False
            st.session_state.editor_login = None
            st.session_state.editor_level = None
            st.session_state.editor_email = None
            st.rerun()
