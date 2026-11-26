import streamlit as st
from openai import OpenAI, APIError, AuthenticationError, RateLimitError
import io

# ---------- Configuración de página ----------
st.set_page_config(page_title="Code Reviewer", page_icon="🏡", layout="centered")

# Límite de caracteres del archivo para no reventar el contexto (~50k tokens)
MAX_CHARS = 200_000

# Límite de tokens de salida por respuesta (8192 en DeepSeek).
MAX_OUTPUT_TOKENS = 8192

# Número máximo de continuaciones automáticas si la respuesta se corta.
# Reducido a 0: la auto-continuación multiplica el consumo de tokens.
MAX_CONTINUATIONS = 0

# Número de rondas (user+assistant) que se conservan en el historial enviado a la API.
MAX_HISTORY_ROUNDS = 4

# ---------- Estado ----------
if "messages" not in st.session_state:
    st.session_state.messages = []
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0
# Guarda el contenido del archivo ya enviado para no reenviarlo en cada ronda.
if "file_context" not in st.session_state:
    st.session_state.file_context = None
if "file_context_name" not in st.session_state:
    st.session_state.file_context_name = None
# Última info de uso de tokens reportada por la API.
if "last_usage" not in st.session_state:
    st.session_state.last_usage = None

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


def build_api_messages():
    """
    Construye la lista de mensajes para la API.

    Estrategia anti-consumo:
      - Solo se envían las últimas MAX_HISTORY_ROUNDS rondas (user+assistant).
      - El contenido del archivo se envía como mensaje de sistema UNA SOLA VEZ,
        al principio, y no se vuelve a repetir en cada ronda.
      - Los mensajes ocultos (hidden) del historial NO se reenvían.
    """
    # 1) Filtrar mensajes ocultos del historial visible
    visible = [m for m in st.session_state.messages if not m.get("hidden", False)]

    # 2) Recortar a las últimas N rondas.
    #    Una "ronda" = un mensaje user + los assistant que le sigan.
    #    Para simplificar, recortamos por pares user/assistant desde el final.
    trimmed = visible[-MAX_HISTORY_ROUNDS * 2:] if MAX_HISTORY_ROUNDS > 0 else visible

    api_messages = []

    # 3) Inyectar el contexto del archivo como mensaje de sistema (una sola vez).
    if st.session_state.file_context:
        api_messages.append({
            "role": "system",
            "content": (
                f"El usuario ha adjuntado el archivo `{st.session_state.file_context_name}`. "
                f"Su contenido es el siguiente:\n\n```\n{st.session_state.file_context}\n```"
            ),
        })

    # 4) Añadir el historial recortado (sin los hidden).
    for m in trimmed:
        api_messages.append({"role": m["role"], "content": m["content"]})

    return api_messages


# ---------- Barra lateral ----------
with st.sidebar:
    st.header("Configuración")

    try:
        secret_key = st.secrets["DEEPSEEK_API_KEY"]
    except (FileNotFoundError, KeyError):
        secret_key = ""

    if secret_key:
        api_key = secret_key
        st.success("✅ API Key cargada desde secrets.toml")
    else:
        st.text_input("DeepSeek API Key", type="password", key="api_key")
        api_key = st.session_state.get("api_key", "")
        st.caption("No se encontró DEEPSEEK_API_KEY en secrets.toml")

    model = st.selectbox("Modelo", ["deepseek-chat", "deepseek-reasoner"])

    temperature = st.slider(
        "Temperatura",
        0.0, 2.0, 1.0, 0.1,
        disabled=(model == "deepseek-reasoner"),
    )
    if model == "deepseek-reasoner":
        st.caption("ℹ️ deepseek-reasoner ignora la temperatura.")

    st.divider()

    uploaded_file = st.file_uploader(
        "Sube tu archivo (código, PDF, DOCX o PNG)",
        type=[
            "py", "txt", "tex", "js", "html", "css", "md", "json", "yml", "yaml", "sql", "cls"
            "pdf", "docx", "png",
        ],
        key=f"file_{st.session_state.uploader_key}",
    )

    if st.button("Limpiar conversación", use_container_width=True):
        st.session_state.messages = []
        st.session_state.file_context = None
        st.session_state.file_context_name = None
        st.session_state.last_usage = None
        st.session_state.uploader_key += 1
        st.rerun()

    st.divider()

    # Mostrar uso de tokens de la última llamada (para diagnosticar caché)
    if st.session_state.last_usage:
        u = st.session_state.last_usage
        st.subheader("Uso de tokens (última llamada)")
        st.write(f"Prompt: **{u.get('prompt_tokens', '?')}**")
        st.write(f"Completion: **{u.get('completion_tokens', '?')}**")
        st.write(f"Total: **{u.get('total_tokens', '?')}**")
        hit = u.get("prompt_cache_hit_tokens")
        miss = u.get("prompt_cache_miss_tokens")
        if hit is not None or miss is not None:
            st.write(f"Cache hit: **{hit}**")
            st.write(f"Cache miss: **{miss}**")
            total_cache = (hit or 0) + (miss or 0)
            if total_cache > 0:
                ratio = (hit or 0) / total_cache * 100
                st.progress(min(ratio / 100.0, 1.0), text=f"Acierto de caché: {ratio:.1f}%")
        st.caption(
            "Si el acierto de caché es bajo, revisa el orden del historial "
            "y el contenido del archivo."
        )

    st.caption("Obtén tu key en platform.deepseek.com")

# ---------- Cabecera ----------
st.title("🏡 Code Reviewer")

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

    # 2. Si hay archivo, extraerlo y guardarlo en el contexto persistente
    #    (SOLO UNA VEZ). No se añade como mensaje hidden al historial.
    if uploaded_file is not None:
        # Evitar re-extraer si ya está cargado el mismo archivo
        if st.session_state.file_context_name != uploaded_file.name:
            file_content = read_uploaded_file(uploaded_file)

            if len(file_content) > MAX_CHARS:
                st.warning(
                    f"⚠️ El contenido extraído es muy grande ({len(file_content):,} caracteres). "
                    f"Se enviarán solo los primeros {MAX_CHARS:,} a la API."
                )
                file_content = file_content[:MAX_CHARS]

            st.session_state.file_context = file_content
            st.session_state.file_context_name = uploaded_file.name

    # 3. Payload para la API (historial recortado + contexto de archivo único)
    messages_for_api = build_api_messages()

    # 4. Llamada a la API con streaming (sin auto-continuación)
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    try:
        with st.chat_message("assistant"):
            placeholder = st.empty()
            full_content, reasoning = "", ""
            finish_reason = None

            kwargs = {
                "model": model,
                "messages": messages_for_api,
                "stream": True,
                "max_tokens": MAX_OUTPUT_TOKENS,
            }
            if model != "deepseek-reasoner":
                kwargs["temperature"] = temperature
            # Pedir info de uso en el stream (DeepSeek lo soporta con stream_options)
            kwargs["stream_options"] = {"include_usage": True}

            stream = client.chat.completions.create(**kwargs)

            usage = None
            for chunk in stream:
                # El chunk de uso final puede venir sin choices
                if getattr(chunk, "usage", None):
                    usage = chunk.usage

                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    reasoning += rc

                if delta.content:
                    full_content += delta.content
                    placeholder.markdown(full_content + "▌")

                if choice.finish_reason:
                    finish_reason = choice.finish_reason

            # Aviso si se cortó (sin reintentar automáticamente)
            if finish_reason == "length":
                placeholder.markdown(
                    full_content
                    + "\n\n---\n⚠️ *La respuesta se cortó por límite de tokens. "
                      "Pide al modelo que continúe manualmente si lo necesitas.*"
                )
            else:
                placeholder.markdown(full_content)

            if reasoning:
                with st.expander("🧠 Razonamiento del modelo"):
                    st.markdown(reasoning)

        # Guardar uso de tokens
        if usage is not None:
            st.session_state.last_usage = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
                "prompt_cache_hit_tokens": getattr(usage, "prompt_cache_hit_tokens", None),
                "prompt_cache_miss_tokens": getattr(usage, "prompt_cache_miss_tokens", None),
            }

        st.session_state.messages.append({
            "role": "assistant",
            "content": full_content,
            "reasoning": reasoning,
        })

        st.rerun()

    except AuthenticationError:
        st.error("API key inválida. Verifícala en platform.deepseek.com.")
    except RateLimitError:
        st.error("Límite de peticiones alcanzado. Reintenta en unos segundos.")
    except APIError as e:
        st.error(f"Error de la API: {e}")
    except Exception as e:
        st.error(f"Error inesperado: {e}")
