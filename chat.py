import streamlit as st
from openai import OpenAI, APIError, AuthenticationError, RateLimitError
import io

# ---------- Configuración de página ----------
st.set_page_config(page_title="Code Reviewer", page_icon="🏡", layout="centered")

# Límite de caracteres del archivo para no reventar el contexto (~50k tokens)
MAX_CHARS = 200_000
MAX_OUTPUT_TOKENS = 4096

# ---------- Estado ----------
if "messages" not in st.session_state:
    st.session_state.messages = []
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0

# ---------- Utilidades de extracción de texto ----------
def extract_pdf_text(raw: bytes) -> str:
    """Extrae texto de un PDF usando pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:
        st.error("Falta la librería `pypdf`. Instálala con: pip install pypdf")
        st.stop()
    reader = PdfReader(io.BytesIO(raw))
    parts = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception as e:
            text = f"[Error extrayendo página {i}: {e}]"
        if text.strip():
            parts.append(f"--- Página {i} ---\n{text}")
    return "\n\n".join(parts)


def extract_docx_text(raw: bytes) -> str:
    """Extrae texto de un .docx usando python-docx."""
    try:
        from docx import Document
    except ImportError:
        st.error("Falta la librería `python-docx`. Instálala con: pip install python-docx")
        st.stop()
    doc = Document(io.BytesIO(raw))
    parts = []
    for p in doc.paragraphs:
        if p.text.strip():
            parts.append(p.text)
    # Incluir también texto de tablas
    for t_idx, table in enumerate(doc.tables, 1):
        parts.append(f"--- Tabla {t_idx} ---")
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_png_text(raw: bytes) -> str:
    """Extrae texto de un PNG usando pytesseract + Pillow (OCR)."""
    try:
        from PIL import Image
    except ImportError:
        st.error("Falta la librería `Pillow`. Instálala con: pip install Pillow")
        st.stop()
    try:
        import pytesseract
    except ImportError:
        st.error(
            "Falta la librería `pytesseract`. Instálala con: pip install pytesseract\n\n"
            "También necesitas el binario de Tesseract OCR instalado en el sistema."
        )
        st.stop()

    image = Image.open(io.BytesIO(raw))
    # Convertir a RGB por si es RGBA/P y evitar problemas con OCR
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    try:
        text = pytesseract.image_to_string(image)
    except Exception as e:
        st.error(f"No se pudo aplicar OCR al PNG: {e}")
        st.stop()
    if not text.strip():
        st.warning("⚠️ No se detectó texto en la imagen PNG.")
    return text


def read_uploaded_file(uploaded_file):
    """Devuelve el texto extraído del archivo subido según su extensión."""
    raw = uploaded_file.getvalue()
    name = uploaded_file.name.lower()

    if name.endswith(".pdf"):
        return extract_pdf_text(raw)
    if name.endswith(".docx"):
        return extract_docx_text(raw)
    if name.endswith(".png"):
        return extract_png_text(raw)

    # Texto plano / código (comportamiento original)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1", errors="replace")
            st.warning(
                "⚠️ El archivo no estaba en UTF-8; se ha decodificado como "
                "latin-1 (puede haber caracteres corruptos en acentos)."
            )
            return text
        except Exception as e:
            st.error(f"No se pudo leer el archivo como texto: {e}")
            st.stop()


# ---------- Barra lateral ----------
with st.sidebar:
    st.header("Configuración")

    # Intentar leer la API key desde secrets.toml
    try:
        secret_key = st.secrets["DEEPSEEK_API_KEY"]
    except (FileNotFoundError, KeyError):
        secret_key = ""

    # Si hay key en secrets, la usamos. Si no, input manual.
    if secret_key:
        api_key = secret_key
        st.success("✅ API Key cargada desde secrets.toml")
    else:
        # Usamos key="api_key": Streamlit sincroniza widget y session_state
        st.text_input("DeepSeek API Key", type="password", key="api_key")
        api_key = st.session_state.get("api_key", "")
        st.caption("No se encontró DEEPSEEK_API_KEY en secrets.toml")

    #model = st.selectbox("Modelo", ["deepseek-chat", "deepseek-reasoner"])
    model = st.selectbox("Modelo", ["deepseek-v4-flash", "deepseek-reasoner"])
    temperature = st.slider(
        "Temperatura",
        0.0, 2.0, 1.0, 0.1,
        disabled=(model == "deepseek-reasoner"),
    )
    if model == "deepseek-reasoner":
        st.caption("ℹ️ deepseek-reasoner ignora la temperatura.")

    st.divider()

    # Cargador de archivos: texto/código + pdf, docx, png
    uploaded_file = st.file_uploader(
        "Sube tu archivo (código, PDF, DOCX o PNG)",
        type=[
            "py", "txt", "tex", "js", "html", "css", "md", "json", "yml", "yaml", "sql",
            "pdf", "docx", "png",
        ],
        key=f"file_{st.session_state.uploader_key}",
    )

    if st.button("Limpiar conversación", use_container_width=True):
        st.session_state.messages = []
        st.session_state.uploader_key += 1   # resetea el file_uploader
        st.rerun()

    st.caption("Obtén tu key en platform.deepseek.com")

# ---------- Cabecera ----------
st.title("🏡 Code Reviewer")

# ---------- Bienvenida si no hay historial ----------
#if not st.session_state.messages:
#    st.info("👋 Sube un archivo (código, PDF, DOCX o PNG) y pregúntame lo que quieras sobre él.")

# ---------- Historial visible ----------
for msg in st.session_state.messages:
    if not msg.get("hidden", False):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("reasoning"):
                with st.expander("🧠 Razonamiento del modelo"):
                    st.markdown(msg["reasoning"])

# ---------- Entrada ----------
if prompt := st.chat_input("Escribe tu pregunta sobre el código..."):
    if not api_key:
        st.error(
            "Introduce tu API key de DeepSeek en la barra lateral "
            "o configúrala en secrets.toml."
        )
        st.stop()

    # 1. Mensaje visible del usuario
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
        if uploaded_file is not None:
            st.caption(f"📎 Archivo adjunto: {uploaded_file.name}")

    # 2. Si hay archivo, lo extraemos según su tipo y lo añadimos
    #    como mensaje OCULTO al historial, una sola vez.
    if uploaded_file is not None:
        file_content = read_uploaded_file(uploaded_file)

        if len(file_content) > MAX_CHARS:
            st.warning(
                f"⚠️ El contenido extraído es muy grande ({len(file_content):,} caracteres). "
                f"Se enviarán solo los primeros {MAX_CHARS:,} a la API."
            )
            file_content = file_content[:MAX_CHARS]

        st.session_state.messages.append({
            "role": "user",
            "content": (
                f"Contenido del archivo `{uploaded_file.name}`:\n\n"
                f"```\n{file_content}\n```"
            ),
            "hidden": True,
        })

    # 3. Payload para la API: directo, sin lógica condicional frágil
    messages_for_api = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state.messages
    ]

    # 4. Llamada a la API con streaming
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    try:
        with st.chat_message("assistant"):
            kwargs = {
                "model": model,
                "messages": messages_for_api,
                "stream": True,
                "max_tokens": MAX_OUTPUT_TOKENS,
            }
            # deepseek-reasoner no soporta temperature
            if model == "deepseek-chat":
                kwargs["temperature"] = temperature

            stream = client.chat.completions.create(**kwargs)

            # Iteración manual: separamos content de reasoning_content
            placeholder = st.empty()
            full_content, reasoning = "", ""
            for chunk in stream:
                delta = chunk.choices[0].delta
                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    reasoning += rc
                if delta.content:
                    full_content += delta.content
                    placeholder.markdown(full_content + "▌")
            placeholder.markdown(full_content)

            if reasoning:
                with st.expander("🧠 Razonamiento del modelo"):
                    st.markdown(reasoning)

        # Guardamos el contenido final Y el razonamiento en el historial
        # (el razonamiento se muestra en un expander al repintar el historial).
        st.session_state.messages.append({
            "role": "assistant",
            "content": full_content,
            "reasoning": reasoning,
        })

        # Forzamos un rerun para que la respuesta quede "fijada" en el historial
        # y no desaparezca al reaccionar a eventos del widget (ej. el file_uploader).
        # Así la respuesta persiste hasta que el usuario pulse "Limpiar conversación".
        st.rerun()

    except AuthenticationError:
        st.error("API key inválida. Verifícala en platform.deepseek.com.")
    except RateLimitError:
        st.error("Límite de peticiones alcanzado. Reintenta en unos segundos.")
    except APIError as e:
        st.error(f"Error de la API: {e}")
    except Exception as e:
        st.error(f"Error inesperado: {e}")
