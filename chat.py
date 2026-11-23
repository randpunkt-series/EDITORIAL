import streamlit as st
from openai import OpenAI

st.set_page_config(page_title="DeepSeek",  layout="centered")

# ---------- Estado ----------
if "messages" not in st.session_state:
    st.session_state.messages = []

# ---------- Barra lateral ----------
with st.sidebar:
    st.header("Configuración")
    
    # Intentar leer la API key desde secrets.toml
    try:
        secret_key = st.secrets["DEEPSEEK_API_KEY"]
    except (FileNotFoundError, KeyError):
        secret_key = ""

    # Si hay una key en secrets, la usamos. Si no, mostramos el input manual.
    if secret_key:
        api_key = secret_key
        st.success("✅ API Key cargada desde secrets.toml")
    else:
        if "api_key" not in st.session_state:
            st.session_state.api_key = ""
        api_key = st.text_input(
            "DeepSeek API Key", 
            type="password", 
            value=st.session_state.api_key
        )
        st.session_state.api_key = api_key
        st.caption("No se encontró DEEPSEEK_API_KEY en secrets.toml")
    
    model = st.selectbox("Modelo", ["deepseek-chat", "deepseek-reasoner"])
    temperature = st.slider("Temperatura", 0.0, 2.0, 1.0, 0.1)
    
    st.divider()
    
    # Cargador de archivos
    uploaded_file = st.file_uploader("Sube tu archivo de código", type=["py", "doc", "docx", "jpg", "txt", "tex", "pdf", "png", "txt", "js", "html", "css"])

    if st.button("Limpiar conversación", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

    st.caption("Obtén tu key en platform.deepseek.com")

# ---------- Cabecera ----------
st.title("🏡 Code Reviewer")

# ---------- Historial ----------
# Solo mostramos el contenido que NO sea oculto
for msg in st.session_state.messages:
    if not msg.get("hidden", False):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

# ---------- Entrada ----------
if prompt := st.chat_input("Escribe tu pregunta sobre el código..."):
    if not api_key:
        st.error("Introduce tu API key de DeepSeek en la barra lateral o configúrala en secrets.toml.")
        st.stop()

    # Comprobar si hay archivo
    file_content = ""
    file_name = ""
    if uploaded_file is not None:
        file_content = uploaded_file.getvalue().decode("utf-8")
        file_name = uploaded_file.name

    # 1. Guarda y muestra SOLO el mensaje del usuario en la interfaz
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
        if file_name:
            st.caption(f"📎 Archivo adjunto: {file_name}")

    # 2. Prepara el contexto oculto para la API
    messages_for_api = []
    
    for msg in st.session_state.messages:
        if msg.get("hidden", False):
            messages_for_api.append(msg)
        elif msg["role"] == "user" and msg["content"] == prompt and file_content:
            # Mensaje combinado que SOLO ve la API (incluye el código)
            combined_content = f"Analiza el siguiente código:\n\n```\n{file_content}\n```\n\nPregunta: {prompt}"
            messages_for_api.append({"role": "user", "content": combined_content})
        else:
            messages_for_api.append(msg)

    # Llama a la API con streaming
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    try:
        with st.chat_message("assistant"):
            kwargs = {
                "model": model,
                "messages": messages_for_api,
                "stream": True,
            }
            # deepseek-reasoner no soporta temperature
            if model == "deepseek-chat":
                kwargs["temperature"] = temperature
                
            stream = client.chat.completions.create(**kwargs)
            response = st.write_stream(stream)

        # Guarda la respuesta del asistente
        st.session_state.messages.append({"role": "assistant", "content": response})

    except Exception as e:
        st.error(f"Error: {e}")
