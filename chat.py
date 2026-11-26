import streamlit as st
from openai import OpenAI, APIError, AuthenticationError, RateLimitError
import io

# ---------- Configuración de página ----------
st.set_page_config(page_title="Code Reviewer (Económico)", page_icon="🏡", layout="centered")

# Límite de caracteres del archivo para no reventar el contexto.
MAX_CHARS = 200_000

# Límite de tokens de salida por respuesta (conciso y económico).
MAX_OUTPUT_TOKENS = 2_048

# Sin auto-continuación: cada respuesta es una sola llamada.
MAX_CONTINUATIONS = 0

# Número de rondas (user+assistant) que se conservan en el historial.
MAX_HISTORY_ROUNDS = 2

# Límite máximo de tokens del prompt (archivo + historial). Muy agresivo.
MAX_PROMPT_TOKENS = 8_000

# Umbral de tokens del archivo para generar resumen agresivo.
FILE_SUMMARY_THRESHOLD = 4_000

# Tokens máximos para el resumen del archivo.
SUMMARY_MAX_TOKENS = 400

# ---------- Estado ----------
if "messages" not in st.session_state:
    st.session_state.messages = []
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0
if "file_context" not in st.session_state:
    st.session_state.file_context = None
if "file_context_name" not in st.session_state:
    st.session_state.file_context_name = None
if "file_sent" not in st.session_state:
    st.session_state.file_sent = False
if "last_usage" not in st.session_state:
    st.session_state.last_usage = None
# Aviso de recorte de historial
if "history_truncated" not in st.session_state:
    st.session_state.history_truncated = False

# ---------- Utilidades de extracción de texto ----------
def extract_pdf_text(raw: bytes) -> str:
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


def estimate_tokens(text: str) -> int:
    """Estimación aproximada: ~4 caracteres = 1 token."""
    return len(text) // 4


def summarize_file(client, text: str, model: str) -> str:
    """Genera un resumen agresivo del archivo para ahorrar tokens."""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Resume el siguiente archivo en menos de 150 palabras. "
                        "Incluye: propósito, estructura principal, funciones clave, "
                        "y problemas potenciales detectados."
                    ),
                },
                {"role": "user", "content": text[:MAX_CHARS]},
            ],
            max_tokens=SUMMARY_MAX_TOKENS,
        )
        content = resp.choices[0].message.content or ""
        finish = resp.choices[0].finish_reason
        if finish == "length":
            st.warning("⚠️ El resumen del archivo se cortó por límite de tokens.")
        return content
    except Exception as e:
        st.warning(f"No se pudo generar el resumen: {e}. Se usará texto truncado.")
        return text[:MAX_CHARS]


def build_api_messages(include_file: bool = False):
    """
    Construye la lista de mensajes para la API con recorte agresivo.
    """
    visible = [m for m in st.session_state.messages if not m.get("hidden", False)]

    if MAX_HISTORY_ROUNDS > 0:
        visible = visible[-MAX_HISTORY_ROUNDS * 2:]

    trimmed = []
    total_tokens = 0
    for m in reversed(visible):
        t = estimate_tokens(m["content"])
        if total_tokens + t > MAX_PROMPT_TOKENS:
            st.session_state.history_truncated = True
            break
        trimmed.insert(0, m)
        total_tokens += t

    api_messages = []

    if include_file and st.session_state.file_context:
        api_messages.append({
            "role": "system",
            "content": (
                f"Archivo `{st.session_state.file_context_name}` (resumen):\n\n"
                f"```\n{st.session_state.file_context}\n```"
            ),
        })

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
            "py", "txt", "tex", "js", "html", "css", "md", "json", "yml", "yaml", "sql", "cls",
            "pdf", "docx", "png",
        ],
        key=f"file_{st.session_state.uploader_key}",
    )

    if st.button("Limpiar conversación", use_container_width=True):
        st.session_state.messages = []
        st.session_state.file_context = None
        st.session_state.file_context_name = None
        st.session_state.file_sent = False
        st.session_state.last_usage = None
        st.session_state.history_truncated = False
        st.session_state.uploader_key += 1
        st.rerun()

    st.divider()

    # Modo económico: información visible
    st.info(
        "💰 **Modo económico activo**\n\n"
        f"- Prompt máx: {MAX_PROMPT_TOKENS:,} tokens\n"
        f"- Salida máx: {MAX_OUTPUT_TOKENS:,} tokens\n"
        f"- Historial: últimas {MAX_HISTORY_ROUNDS} rondas\n"
        f"- Sin auto-continuación"
    )

    # Mostrar uso de tokens
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

    st.caption("Obtén tu key en platform.deepseek.com")

# ---------- Cabecera ----------
st.title("🏡 Code Reviewer")
st.caption("💰 Modo económico: respuestas concisas, contexto mínimo, máximo ahorro de tokens.")

# Aviso si el historial se recortó
if st.session_state.history_truncated:
    st.warning(
        "⚠️ El historial se ha recortado por límite de tokens. "
        "Las preguntas muy antiguas ya no se envían al modelo. "
        "Usa 'Limpiar conversación' para empezar de cero."
    )

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

    # 2. Si hay archivo NUEVO, extraerlo, resumirlo (agresivo) y guardarlo
    if uploaded_file is not None:
        if st.session_state.file_context_name != uploaded_file.name:
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            file_content = read_uploaded_file(uploaded_file)

            if len(file_content) > MAX_CHARS:
                st.warning(
                    f"⚠️ El contenido extraído es muy grande ({len(file_content):,} caracteres). "
                    f"Se enviarán solo los primeros {MAX_CHARS:,} a la API."
                )
                file_content = file_content[:MAX_CHARS]

            # Resumen agresivo si supera FILE_SUMMARY_THRESHOLD tokens
            if estimate_tokens(file_content) > FILE_SUMMARY_THRESHOLD:
                with st.spinner("Generando resumen del archivo para ahorrar tokens..."):
                    file_content = summarize_file(client, file_content, model)
                    st.info("📝 Se ha generado un resumen del archivo para reducir el consumo.")

            st.session_state.file_context = file_content
            st.session_state.file_context_name = uploaded_file.name
            st.session_state.file_sent = False

    # 3. Payload para la API
    messages_for_api = build_api_messages(include_file=not st.session_state.file_sent)

    # 4. Llamada a la API (sin auto-continuación)
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    try:
        with st.chat_message("assistant"):
            placeholder = st.empty()
            full_content, reasoning = "", ""
            finish_reason = None
            usage = None

            kwargs = {
                "model": model,
                "messages": messages_for_api,
                "stream": True,
                "max_tokens": MAX_OUTPUT_TOKENS,
            }
            if model != "deepseek-reasoner":
                kwargs["temperature"] = temperature
            kwargs["stream_options"] = {"include_usage": True}

            stream = client.chat.completions.create(**kwargs)

            for chunk in stream:
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

            # Aviso si se cortó (sin reintentar)
            if finish_reason == "length":
                placeholder.markdown(
                    full_content
                    + "\n\n---\n⚠️ *La respuesta se cortó por límite de tokens "
                      "(modo económico: 2048 tokens). Pide 'continúa' si lo necesitas.*"
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

        # Marcar el archivo como ya enviado
        if not st.session_state.file_sent and st.session_state.file_context:
            st.session_state.file_sent = True

        st.rerun()

    except AuthenticationError:
        st.error("API key inválida. Verifícala en platform.deepseek.com.")
    except RateLimitError:
        st.error("Límite de peticiones alcanzado. Reintenta en unos segundos.")
    except APIError as e:
        st.error(f"Error de la API: {e}")
    except Exception as e:
        st.error(f"Error inesperado: {e}")
