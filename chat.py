#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# chat23.py - Chat DeepSeek local con Streamlit
# ============================================================
# Version corregida y dividida en tres partes.
# Parte 1/3: configuracion, logging, tracker, adjuntos, API.
# ============================================================

from __future__ import annotations

import os
import re
import io
import json
import time
import uuid
import base64
import html
import posixpath
import stat as stat_module
import logging
import datetime
from typing import Dict, List, Optional, Sequence, Tuple, Union
from io import StringIO

import streamlit as st
import paramiko

try:
    from openai import (
        OpenAI,
        APIStatusError,
        APITimeoutError,
        APIConnectionError,
    )
except ImportError:
    st.error("Falta openai: pip install openai")
    st.stop()

try:
    from PIL import Image
except ImportError:
    st.error("Falta pillow: pip install pillow")
    st.stop()


# ============================================================
# Configuracion
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(BASE_DIR, "debug")
os.makedirs(DEBUG_DIR, exist_ok=True)

DEFAULT_BASE_URL = "https://api.deepseek.com"
#DEFAULT_MODEL = "deepseek-chat"
#DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_MODEL = "deepseek-flash"


MAX_TOKENS_BUDGET = 400_000
MAX_DEBUG_FILES = 50
MAX_CONTEXT_MESSAGES = 40
ROTATE_DEBUG_EVERY_N_TURNS = 10

FIXED_TEMPERATURE = 0.23
FIXED_MAX_TOKENS = 8192

# Configurable desde el sidebar; este es el valor por defecto.
DEFAULT_FLATTEN_HEADINGS = True

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_FILES = 20
ALLOWED_UPLOAD_EXTENSIONS = [
    "txt", "md", "markdown", "csv", "tsv",
    "log", "json", "py", "r", "tex",
    "js", "ts", "html", "css", "xml", "yaml", "yml",
    "sh", "bash", "sql", "c", "h", "cpp", "hpp",
    "java", "go", "rs", "rb", "php", "pl", "lua", "cls",
    "toml", "ini", "cfg", "conf", "env", "bib", "docx", "doc",
]

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_FILES = 10
ALLOWED_IMAGE_EXTENSIONS = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "pdf"]

# Modelos que aceptan contenido multimodal (image_url).
# Si esta vacio, las imagenes se envian solo como metadatos/thumbnail.
MULTIMODAL_MODELS: set = set()

ALLOWED_IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/jpg", "image/webp",
    "image/gif", "image/bmp",
}

DEFAULT_SYSTEM_PROMPT = (
    "Cuando devuelvas codigo, usa siempre bloques de codigo con el "
    "lenguaje explicito: ```python para Python, ```latex para LaTeX, "
    "```bash para shell, ```json, ```yaml, etc. "
    "No devuelvas HTML ni escapes el texto a entidades HTML. "
    "No uses etiquetas HTML de ningun tipo (div, span, br, b, i, img, "
    "script, style, svg, math, table, etc.). "
    "No uses entidades HTML (lt, gt, amp, nbsp, ni referencias numericas). "
    "No uses comentarios HTML. "
    "No uses autolinks con corchetes angulares; escribe la URL desnuda. "
    "No uses esquemas javascript: ni data: en enlaces. "
    "Para matematicas usa LaTeX nativo: $...$ en linea y $$...$$ en bloque. "
    "No uses comandos de tamano en LaTeX (small, large, tiny, "
    "footnotesize, huge, Huge, normalsize). "
    "No uses encabezados Markdown (almohadilla); usa negrita para "
    "titulos de seccion."
)


# ============================================================
# Helpers de secretos y rutas
# ============================================================
def _get_secret(section: str, key: str, default: str = "") -> str:
    """Lee un secreto de st.secrets. Loguea en debug si falla."""
    try:
        if section:
            return st.secrets[section][key]
        return st.secrets[key]
    except Exception as e:
        try:
            st.session_state.setdefault("_secret_warnings", []).append(
                f"secret {section!r}/{key!r}: {type(e).__name__}: {e}"
            )
        except Exception:
            pass
        return default


def _get_api_key_fallback() -> str:
    return _get_secret("", "DEEPSEEK_API_KEY", "")


def _normalize_remote_dir(raw: str) -> str:
    """Valida y normaliza un directorio remoto absoluto."""
    if not isinstance(raw, str):
        raise ValueError("dir_chat debe ser string")
    s = raw.strip()
    if not s:
        raise ValueError("dir_chat vacio")
    if not s.startswith("/"):
        raise ValueError(f"dir_chat debe ser absoluto: {s!r}")
    norm = posixpath.normpath(s)
    if norm in ("/", ""):
        raise ValueError(f"dir_chat no puede ser la raiz: {s!r}")
    if ".." in norm.split("/"):
        raise ValueError(f"dir_chat contiene '..': {s!r}")
    return norm


def _get_remote_server_cfg() -> Dict:
    try:
        rs = st.secrets["remote_server"]
        dir_chat = _normalize_remote_dir(rs["dir_chat"])
        return {
            "host": rs["host"],
            "port": int(rs["port"]),
            "user": rs["user"],
            "private_key": rs["private_key"],
            "dir_chat": dir_chat,
        }
    except Exception:
        return {}


# ============================================================
# Logging
# ============================================================
def _setup_run_logger() -> Tuple[logging.Logger, str, str]:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"{ts}_{uuid.uuid4().hex[:6]}"
    log_path = os.path.join(DEBUG_DIR, f"chat_{run_id}.jsonl")
    logger = logging.getLogger(f"chat_{run_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger, run_id, log_path


def _log(logger: Optional[logging.Logger], **kwargs) -> None:
    if logger is None:
        return
    try:
        safe = {}
        for k, v in kwargs.items():
            kl = k.lower()
            if kl in {"preview", "content", "text", "prompt", "message"}:
                if isinstance(v, str):
                    safe[f"{k}_len"] = len(v)
                else:
                    safe[f"{k}_present"] = True
            else:
                safe[k] = v
        payload = {"ts": datetime.datetime.now().isoformat(timespec="seconds")}
        payload.update(safe)
        logger.info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


def _rotate_debug_dir(max_files: int = MAX_DEBUG_FILES) -> None:
    try:
        entries = [
            os.path.join(DEBUG_DIR, f) for f in os.listdir(DEBUG_DIR)
            if f.endswith((".jsonl", ".json"))
        ]
        entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in entries[max_files:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception:
        pass


def _sanitize_filename(name: str) -> str:
    base = os.path.basename(name or "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    base = base.lstrip(".-") or "file"
    return base[:180]


# ============================================================
# Token tracker
# ============================================================
class _TokenTracker:
    def __init__(self, budget: int = MAX_TOKENS_BUDGET):
        self.prompt = 0
        self.completion = 0
        self.budget = budget

    def add(self, usage) -> None:
        if usage is None:
            return
        try:
            self.prompt += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.completion += int(getattr(usage, "completion_tokens", 0) or 0)
        except Exception:
            pass

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def over_budget(self) -> bool:
        return self.total > self.budget

    def reset(self) -> None:
        self.prompt = 0
        self.completion = 0


# ============================================================
# Lectura de adjuntos
# ============================================================
def _decode_bytes(data: bytes) -> Tuple[str, str]:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1(replace)"


def _safe_read(uf) -> bytes:
    if hasattr(uf, "seek"):
        try:
            uf.seek(0)
        except Exception:
            pass
    return uf.read()


def _unique_name(name: str, existing: Sequence[str]) -> str:
    if name not in existing:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    else:
        ext = "." + ext
    max_stem = max(1, 180 - len(ext) - 6)
    i = 2
    while True:
        suffix = f"_{i}"
        candidate = f"{stem[:max_stem]}{suffix}{ext}"
        if candidate not in existing:
            return candidate
        i += 1


def _classify_uploaded_file(uf) -> Tuple[str, str]:
    name = _sanitize_filename(getattr(uf, "name", ""))
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ALLOWED_IMAGE_EXTENSIONS:
        return "image", ext
    if ext in ALLOWED_UPLOAD_EXTENSIONS:
        return "text", ext
    return "unknown", ext


def _make_thumbnail(data: bytes, max_side: int = 256) -> bytes:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        fmt = (img.format or "PNG").upper()
        if fmt not in {"PNG", "JPEG", "WEBP"}:
            fmt = "PNG"
        if fmt == "JPEG" and img.mode in {"RGBA", "P"}:
            img = img.convert("RGB")
        img.save(buf, format=fmt)
        return buf.getvalue()
    except Exception:
        return data[:4096]


def _read_uploaded_any(
    uploaded_files: Sequence,
    logger: Optional[logging.Logger] = None,
) -> Tuple[Dict[str, Dict], Dict[str, Dict], List[str]]:
    texts: Dict[str, Dict] = {}
    images: Dict[str, Dict] = {}
    skipped: List[str] = []

    if not uploaded_files:
        return texts, images, skipped

    model_supports_images = DEFAULT_MODEL in MULTIMODAL_MODELS

    for uf in uploaded_files:
        try:
            raw_name = getattr(uf, "name", "?")
            name = _sanitize_filename(raw_name)
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            kind, detected_ext = _classify_uploaded_file(uf)

            if kind == "image":
                data = _safe_read(uf)
                if len(data) > MAX_IMAGE_BYTES:
                    skipped.append(
                        f"{name} (mayor que "
                        f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB)"
                    )
                    continue
                try:
                    img = Image.open(io.BytesIO(data))
                    img.load()
                    width, height = img.size
                    fmt = (img.format or ext or "png").upper()
                except Exception:
                    skipped.append(f"{name} (no es imagen valida)")
                    continue

                mime = Image.MIME.get(fmt, f"image/{ext or 'png'}")
                if mime not in ALLOWED_IMAGE_MIMES:
                    skipped.append(f"{name} (MIME no permitido: {mime})")
                    continue

                unique = _unique_name(
                    name, list(texts.keys()) + list(images.keys())
                )

                if model_supports_images:
                    b64 = base64.b64encode(data).decode("ascii")
                    images[unique] = {
                        "mime": mime,
                        "size": len(data),
                        "width": width,
                        "height": height,
                        "ext": ext or detected_ext or "img",
                        "b64": b64,
                        "data_uri": f"data:{mime};base64,{b64}",
                        "bytes": data,
                    }
                else:
                    images[unique] = {
                        "mime": mime,
                        "size": len(data),
                        "width": width,
                        "height": height,
                        "ext": ext or detected_ext or "img",
                        "b64": None,
                        "data_uri": None,
                        "bytes": _make_thumbnail(data),
                    }
            elif kind == "text":
                data = _safe_read(uf)
                if len(data) > MAX_UPLOAD_BYTES:
                    skipped.append(
                        f"{name} (mayor que "
                        f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"
                    )
                    continue
                text, enc = _decode_bytes(data)
                unique = _unique_name(
                    name, list(texts.keys()) + list(images.keys())
                )
                nonce = uuid.uuid4().hex
                texts[unique] = {
                    "content": text,
                    "size": len(data),
                    "encoding": enc,
                    "ext": ext or detected_ext,
                    "lines": text.count("\n") + (1 if text else 0),
                    "injection_nonce": nonce,
                }
            else:
                skipped.append(
                    f"{name} (extension no permitida: {ext or 'sin ext'})"
                )
        except Exception:
            skipped.append(f"{getattr(uf, 'name', '?')} (error)")

    return texts, images, skipped


def _build_multimodal_content(text: str, images: Dict[str, Dict]) -> List[Dict]:
    parts: List[Dict] = []
    if text:
        parts.append({"type": "text", "text": text})

    omitted = 0
    for _name, meta in images.items():
        mime = meta.get("mime")
        data_uri = meta.get("data_uri")
        if mime in ALLOWED_IMAGE_MIMES and data_uri:
            parts.append(
                {"type": "image_url", "image_url": {"url": data_uri}}
            )
        else:
            omitted += 1

    if omitted:
        parts.append({
            "type": "text",
            "text": (
                f"[{omitted} imagen(es) omitida(s) por MIME/formato "
                f"no permitido]"
            ),
        })
    return parts


def _wrap_attachment_text(name: str, meta: Dict) -> str:
    nonce = meta.get("injection_nonce") or uuid.uuid4().hex
    return (
        f"<<<ATTACHMENT_BEGIN {nonce} name={name}>>>\n"
        f"{meta.get('content', '')}\n"
        f"<<<ATTACHMENT_END {nonce} name={name}>>>"
    )


# ============================================================
# Cliente API
# ============================================================
def _make_client(api_key: str, base_url: str) -> OpenAI:
    return OpenAI(api_key=api_key, base_url=base_url, max_retries=0)


_RETRIABLE_STATUS = {429, 500, 502, 503, 504}


def _is_retriable_error(e: Exception) -> bool:
    if isinstance(e, (APITimeoutError, APIConnectionError)):
        return True
    if isinstance(e, APIStatusError):
        try:
            return int(getattr(e, "status_code", 0)) in _RETRIABLE_STATUS
        except Exception:
            return False
    return False


def _call_streaming(
    client: OpenAI,
    messages: Sequence[Dict],
    model: str,
    max_tokens: int = FIXED_MAX_TOKENS,
    temperature: float = FIXED_TEMPERATURE,
    tracker: Optional[_TokenTracker] = None,
    max_retries: int = 4,
):
    """
    Genera deltas de texto. Si el stream falla ANTES de emitir ningun
    delta, reintenta. Si falla DESPUES, propaga el error sin reintentar
    (para no duplicar contenido ya mostrado al usuario).
    """
    delay = 2.0
    for attempt in range(max_retries):
        stream = None
        emitted_any = False
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=list(messages),
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                timeout=120,
            )
            for chunk in stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta and getattr(delta, "content", None):
                        emitted_any = True
                        yield delta.content
                if tracker is not None and getattr(chunk, "usage", None):
                    tracker.add(chunk.usage)
            return
        except Exception as e:
            if (
                not emitted_any
                and _is_retriable_error(e)
                and attempt < max_retries - 1
            ):
                time.sleep(delay + 0.1 * attempt)
                delay = min(delay * 2, 30)
                continue
            raise RuntimeError(
                f"Error de API tras {attempt + 1} intentos: {e}"
            )
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass


# ============================================================
# Historial y exportacion
# ============================================================
def _trim_history(
    messages: Sequence[Dict],
    max_msgs: int = MAX_CONTEXT_MESSAGES,
) -> List[Dict]:
    if len(messages) <= max_msgs:
        return list(messages)
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    budget = max(0, max_msgs - len(system))
    if budget == 0:
        return system
    tail = rest[-budget:]
    while tail and tail[0].get("role") == "assistant":
        tail = tail[1:]
    return system + tail


def _content_to_str(content: Union[str, List]) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        n_imgs = sum(
            1 for p in content
            if isinstance(p, dict) and p.get("type") == "image_url"
        )
        txt = "\n".join(parts)
        if n_imgs:
            txt += f"\n[+{n_imgs} imagen(es)]"
        return txt
    return str(content)


def _export_conversation_json(messages: Sequence[Dict], meta: Dict) -> bytes:
    serializable = []
    for m in messages:
        content = m.get("content")
        role = m.get("role")
        if isinstance(content, str):
            serializable.append({"role": role, "content": content})
        elif isinstance(content, list):
            safe_parts = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    safe_parts.append(
                        {"type": "text", "text": p.get("text", "")}
                    )
                elif p.get("type") == "image_url":
                    url = ""
                    iu = p.get("image_url")
                    if isinstance(iu, dict):
                        url = iu.get("url", "") or ""
                    safe_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": "[imagen omitida del backup]",
                            "original_length": len(url),
                        },
                    })
            serializable.append({"role": role, "content": safe_parts})
        else:
            serializable.append({"role": role, "content": str(content)})
    payload = {"meta": meta, "messages": serializable}
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _export_conversation_markdown(messages: Sequence[Dict], meta: Dict) -> bytes:
    """
    Exporta a Markdown. El contenido de cada mensaje se envuelve en un
    fence para que un encabezado dentro de una respuesta no rompa el
    documento. Ademas se sanea el contenido para que el .md no
    contenga HTML ejecutable si se abre en un visor con HTML activo.
    """
    lines: List[str] = []
    lines.append("# Conversacion con DeepSeek\n")
    lines.append(f"- Modelo: `{meta.get('model', '?')}`")
    lines.append(f"- Fecha: {meta.get('ts', '?')}")
    lines.append(f"- Tokens totales: {meta.get('tokens_total', 0):,}\n")
    lines.append("---\n")
    for m in messages:
        role = m.get("role", "?")
        content = _content_to_str(m.get("content", ""))
        content = _escape_html_outside_fences(content)
        if role == "system":
            lines.append(f"**system**: {content}\n")
        elif role == "user":
            lines.append("## Usuario\n")
            lines.append("````text")
            lines.append(content)
            lines.append("````\n")
        elif role == "assistant":
            lines.append("## Asistente\n")
            lines.append("````markdown")
            lines.append(content)
            lines.append("````\n")
    return "\n".join(lines).encode("utf-8")

# ============================================================
# Saneado anti-HTML / anti-inyeccion
# ============================================================

# Entidades que Streamlit/Markdown decodifican y que pueden
# reconstruir etiquetas peligrosas.
_ENTITY_DECODE_MAP = {
    "&#60;": "<", "&#62;": ">",
    "&#x3c;": "<", "&#x3e;": ">",
    "&#x3C;": "<", "&#x3E;": ">",
    "<": "<", ">": ">",
}

# Esquemas peligrosos en enlaces Markdown [texto](esquema:...)
# - javascript: y vbscript: requieren ":" para evitar falsos positivos
#   con la palabra "javascript" en prosa.
# - data:text/html y data:application/xhtml+xml NO requieren ":" al
#   final porque el payload suele ir tras una coma:
#   data:text/html,<script>...
_DANGEROUS_URL_SCHEMES = re.compile(
    r"(?i)(?:"
    r"\b(?:javascript|vbscript)\s*:"
    r"|"
    r"\bdata\s*:\s*(?:text/html|application/xhtml\+xml)"
    r")"
)


# Comentarios HTML
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Autolinks con corchetes angulares
_AUTOLINK_RE = re.compile(
    r"<\s*(?:https?|ftp|mailto|tel|file)\s*:[^>\s]*\s*>",
    re.IGNORECASE,
)

# Cualquier etiqueta HTML (apertura, cierre, autocierre, doctype, PI)
_HTML_TAG_RE = re.compile(
    r"<\s*/?\s*[a-zA-Z!?][^<>]*>",
    re.DOTALL,
)

# Fences de lenguajes que pueden renderizar HTML/SVG ejecutable
_DANGEROUS_FENCE_LANGS = {
    "html", "htm", "xhtml", "svg", "xml", "mathml",
}

_FENCE_RE = re.compile(
    r"^(\s*)(`{3,}|~{3,})([^\n`~]*)\n(.*?)\n^\s*\2\s*$",
    re.DOTALL | re.MULTILINE,
)


def _decode_dangerous_entities(text: str) -> str:
    """Decodifica entidades que reconstruyen < o >, para luego escaparlas."""
    if not text:
        return text
    for _ in range(2):
        changed = False
        for ent, ch in _ENTITY_DECODE_MAP.items():
            if ent in text:
                text = text.replace(ent, ch)
                changed = True
        if not changed:
            break
    return text


def _neutralize_dangerous_fences(text: str) -> str:
    """
    Convierte fences html/svg/xml en fences text para que Streamlit
    no intente renderizarlos como HTML/SVG. El contenido se muestra
    como codigo literal, que es lo que queremos.
    """
    if not text:
        return text

    def _fix(m: re.Match) -> str:
        indent = m.group(1) or ""
        fence = m.group(2)
        lang = (m.group(3) or "").strip().lower()
        body = m.group(4)
        if lang in _DANGEROUS_FENCE_LANGS:
            lang = "text"
        return f"{indent}{fence}{lang}\n{body}\n{indent}{fence}"

    return _FENCE_RE.sub(_fix, text)


def _neutralize_dangerous_links(text: str) -> str:
    """
    Sustituye esquemas peligrosos en enlaces Markdown por about:blank.
    Cubre [texto](javascript:...) y ![alt](data:text/html,...).

    Usa un escaneo manual para manejar parentesis balanceados dentro
    de la URL (p.ej. javascript:alert(1) o
    data:text/html,<script>alert(1)</script>).
    """
    if not text:
        return text

    out = []
    i = 0
    n = len(text)
    while i < n:
        # Buscar el inicio de un enlace Markdown: [texto]( o ![alt](
        m = re.match(r"(!?\[[^\]]*\])", text[i:])
        if not m:
            out.append(text[i])
            i += 1
            continue
        prefix = m.group(1)
        j = i + len(prefix)
        if j >= n or text[j] != "(":
            out.append(text[i])
            i += 1
            continue
        # Encontrado [texto]( -> buscar el ) de cierre balanceado
        depth = 1
        k = j + 1
        while k < n and depth > 0:
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
            k += 1
        if depth != 0:
            # Parentesis sin cerrar: dejar el texto tal cual
            out.append(text[i])
            i += 1
            continue
        url = text[j + 1:k - 1]
        if _DANGEROUS_URL_SCHEMES.search(url):
            out.append(f"{prefix}(about:blank)")
        else:
            out.append(text[i:k])
        i = k

    return "".join(out)




def _escape_html_outside_fences(text: str) -> str:
    """
    Saneador unico. Garantiza que NINGUN caracter de apertura o cierre
    de etiqueta llegue a Streamlit fuera de bloques de codigo, y que
    no haya entidades que reconstruyan etiquetas.
    """
    if not text:
        return text

    # 1. Neutralizar fences peligrosas ANTES de protegerlas
    text = _neutralize_dangerous_fences(text)

    # 2. Neutralizar enlaces con esquemas peligrosos
    text = _neutralize_dangerous_links(text)

    # 3. Proteger fences de codigo (ya saneadas)
    marker = f"\x00{uuid.uuid4().hex}_"
    preserved: List[str] = []

    def _stash(m):
        preserved.append(m.group(0))
        return f"{marker}{len(preserved) - 1}\x00"

    tmp = re.sub(r"```.*?```", _stash, text, flags=re.DOTALL)
    tmp = re.sub(r"~~~.*?~~~", _stash, tmp, flags=re.DOTALL)

    # 4. Proteger LaTeX (solo si NO contiene < ni >, para no
    #    esconder inyecciones dentro de formulas)
    def _stash_math(m):
        body = m.group(0)
        if "<" in body or ">" in body:
            return body
        preserved.append(body)
        return f"{marker}{len(preserved) - 1}\x00"

    tmp = re.sub(r"\$\$.*?\$\$", _stash_math, tmp, flags=re.DOTALL)
    tmp = re.sub(
        r"(?<!\$)\$(?!\$)[^$\n]+?\$(?!\$)",
        _stash_math,
        tmp,
    )

    # 5. Neutralizar comentarios HTML
    tmp = _HTML_COMMENT_RE.sub("", tmp)

    # 6. Neutralizar autolinks con corchetes angulares
    tmp = _AUTOLINK_RE.sub(
        lambda m: m.group(0).replace("<", "<").replace(">", ">"),
        tmp,
    )

    # 7. Decodificar entidades peligrosas y volver a escapar
    tmp = _decode_dangerous_entities(tmp)

    # 8. Escapar TODOS los < y > restantes
    tmp = tmp.replace("<", "<").replace(">", ">")

    # 9. Escapar & sueltos que no formen entidad valida
    tmp = re.sub(
        r"&(?!(?:[a-zA-Z][a-zA-Z0-9]{1,31}|#\d{1,7}|#x[0-9a-fA-F]{1,6});)",
        "&",
        tmp,
    )

    # 10. Restaurar bloques protegidos
    for i, blk in enumerate(preserved):
        tmp = tmp.replace(f"{marker}{i}\x00", blk)

    return tmp


# ============================================================
# Normalizacion LaTeX
# ============================================================
_LANG_ALIASES = {
    "py": "python", "python3": "python",
    "tex": "latex", "latex": "latex",
    "sh": "bash", "shell": "bash", "zsh": "bash",
    "js": "javascript", "ts": "typescript",
    "yml": "yaml",
    "c++": "cpp", "cxx": "cpp",
    "rs": "rust",
    "md": "markdown",
    "html": "html", "css": "css",
    "json": "json", "xml": "xml",
    "sql": "sql",
    "r": "r",
}

_LATEX_INLINE_PAREN = re.compile(r"\\$(.+?)\\$", re.DOTALL)
_LATEX_BLOCK_BRACK = re.compile(r"\\[(.+?)\\]", re.DOTALL)


def _normalize_latex_delimiters(text: str) -> str:
    r"""
    Convierte delimitadores LaTeX alternativos a $...$ y $$...$$.

    Variantes que maneja:
      \[ ... \]       -> $$ ... $$
      \( ... \)       -> $ ... $
      \begin{equation} ... \end{equation} -> $$ ... $$
      \begin{align} ... \end{align}       -> $$ ... $$

    NO convierte [ ... ] a $$ ... $$: esa heuristica rompia listas
    Markdown y texto entre corchetes durante el streaming.
    """
    if not text:
        return text

    # 1. Protegemos bloques de codigo para no tocarlos
    marker = f"\x00{uuid.uuid4().hex}_"
    stash: List[str] = []

    def _stash(m):
        stash.append(m.group(0))
        return f"{marker}{len(stash) - 1}\x00"

    tmp = re.sub(r"```.*?```", _stash, text, flags=re.DOTALL)
    tmp = re.sub(r"~~~.*?~~~", _stash, tmp, flags=re.DOTALL)

    # 2. \[ ... \]  ->  [ ... ]
    tmp = re.sub(
        r"\\\[(.+?)\\\]",
        lambda m: f"$${m.group(1)}$$",
        tmp,
        flags=re.DOTALL,
    )

    # 3. \( ... \)  ->  $ ... $
    tmp = re.sub(
        r"\\\((.+?)\\\)",
        lambda m: f"${m.group(1)}$",
        tmp,
        flags=re.DOTALL,
    )

    # 4. \begin{env} ... \end{env}  ->  [ ... ]
    for env in ("equation", "equation*", "align", "align*",
                "gather", "gather*", "multline", "multline*",
                "eqnarray", "eqnarray*"):
        tmp = re.sub(
            rf"\\begin\{{{re.escape(env)}\}}(.+?)\\end\{{{re.escape(env)}\}}",
            lambda m: f"$${m.group(1)}$$",
            tmp,
            flags=re.DOTALL,
        )

    # 5. Restauramos bloques de codigo
    for i, blk in enumerate(stash):
        tmp = tmp.replace(f"{marker}{i}\x00", blk)

    return tmp


def _normalize_latex(text: str) -> str:
    if not text:
        return text

    # 1. Convertir delimitadores LaTeX alternativos
    text = _normalize_latex_delimiters(text)

    # 2. Proteger bloques de codigo para no tocarlos
    marker = f"\x00{uuid.uuid4().hex}_"
    code_blocks: List[str] = []

    def _stash(m):
        code_blocks.append(m.group(0))
        return f"{marker}{len(code_blocks) - 1}\x00"

    tmp = re.sub(r"```.*?```", _stash, text, flags=re.DOTALL)
    tmp = re.sub(r"~~~.*?~~~", _stash, tmp, flags=re.DOTALL)

    # 3. Restaurar bloques de codigo
    for i, blk in enumerate(code_blocks):
        tmp = tmp.replace(f"{marker}{i}\x00", blk)

    return tmp



def _close_open_math(text: str) -> str:
    """
    Cierra delimitadores LaTeX abiertos al final del texto para que
    KaTeX pueda renderizar durante el streaming. No altera el texto
    guardado; solo la vista parcial.
    """
    if not text:
        return text

    # Bloques $$...$$
    n_dd = len(re.findall(r"(?<!\\)\$\$", text))
    if n_dd % 2 == 1:
        return text + "$$"

    # $...$ simples (excluyendo los $$ ya contados)
    tmp = re.sub(r"(?<!\\)\$\$", "", text)
    n_s = len(re.findall(r"(?<!\\)\$", tmp))
    if n_s % 2 == 1:
        return text + "$"

    # Delimitadores \( ... \) y \[ ... \]
    if text.count(r"\(") > text.count(r"\)"):
        return text + r"\)"
    if text.count(r"\[") > text.count(r"\]"):
        return text + r"\]"

    return text


def _close_open_html(text: str) -> str:
    """Si el texto termina en un caracter de apertura de etiqueta sin
    cierre, lo escapa para que no se muestre a medias."""
    if not text:
        return text
    last_lt = text.rfind("<")
    if last_lt == -1:
        return text
    if ">" not in text[last_lt:]:
        return text[:last_lt] + text[last_lt:].replace("<", "<")
    return text


# ============================================================
# Fences y headings
# ============================================================
def _guess_fence_language(body: str) -> str:
    s = body.lstrip()
    if not s:
        return "text"
    if s.startswith(("def ", "import ", "from ", "class ", "#!")):
        return "python"
    if s.startswith(("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE ")):
        return "sql"
    if s.startswith(("<?xml", "<!DOCTYPE", "<html")):
        return "html"
    if s.startswith(("{", "[")):
        return "json"
    if s.startswith(("$ ", "sudo ", "apt ", "pip ", "npm ", "git ")):
        return "bash"
    if "\\begin{" in body or "\\end{" in body:
        return "latex"
    return "text"


def _normalize_fences(text: str) -> str:
    if not text:
        return text
    if "```" not in text and "~~~" not in text:
        return text

    def _fix(m: re.Match) -> str:
        indent = m.group(1) or ""
        fence = m.group(2)
        raw_lang = (m.group(3) or "").strip().lower()
        body = m.group(4)
        lang = _LANG_ALIASES.get(raw_lang, raw_lang)
        if not lang:
            lang = _guess_fence_language(body)
        return f"{indent}{fence}{lang}\n{body}\n{indent}{fence}"

    return _FENCE_RE.sub(_fix, text)



_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


def _flatten_headings(text: str) -> str:
    if not text:
        return text
    marker = f"\x00{uuid.uuid4().hex}_"
    preserved: List[str] = []

    def _stash(m):
        preserved.append(m.group(0))
        return f"{marker}{len(preserved) - 1}\x00"

    tmp = re.sub(r"```.*?```", _stash, text, flags=re.DOTALL)
    tmp = re.sub(r"~~~.*?~~~", _stash, tmp, flags=re.DOTALL)
    tmp = re.sub(r"\$\$.*?\$\$", _stash, tmp, flags=re.DOTALL)
    tmp = re.sub(r"(?<!\$)\$(?!\$)[^$\n]+?\$(?!\$)", _stash, tmp)

    def _to_bold(m: re.Match) -> str:
        title = m.group(2).strip()
        return f"**{title}**"

    tmp = _HEADING_RE.sub(_to_bold, tmp)
    for i, blk in enumerate(preserved):
        tmp = tmp.replace(f"{marker}{i}\x00", blk)
    return tmp


# ============================================================
# Renderizado
# ============================================================
def _prepare_for_render(text: str, flatten_headings: bool = True) -> str:
    text = _normalize_latex(text)
    text = _normalize_fences(text)
    if flatten_headings:
        text = _flatten_headings(text)
    text = _escape_html_outside_fences(text)
    return text


def _render_markdown_safe(text: str, flatten_headings: bool = True) -> None:
    if not text:
        return
    prepared = _prepare_for_render(text, flatten_headings=flatten_headings)
    # Cinturon de seguridad: si algo se colo, mostrarlo como codigo
    # en lugar de arriesgar renderizado.
    if re.search(r"<\s*/?\s*[a-zA-Z!?]", prepared):
        st.code(prepared, language="text")
        return
    st.markdown(prepared, unsafe_allow_html=False)


def _show_response_text(text: str) -> None:
    """
    Muestra la respuesta en un expander con st.code (que ya trae boton
    de copiar nativo).
    """
    if not text:
        return
    with st.expander("Ver / copiar respuesta", expanded=False):
        st.code(text, language="text")


def _self_test_latex() -> Tuple[bool, str]:
    try:
        sample = (
            r"Inline: $a^2 + b^2 = c^2$" + "\n\n"
            + "Bloque:\n"
            + r"$$" + "\n"
            + r"\int_0^1 x^2 dx = 1/3" + "\n"
            + r"$$" + "\n"
        )
        out = _normalize_latex(sample)
        if "$a^2 + b^2 = c^2$" not in out:
            return False, r"No se convirtio \$...\$ a $...$"
        if "$$" not in out:
            return False, r"No se convirtio \[...\] a $$...$$"
        return True, "OK: delimitadores alternativos convertidos"
    except Exception as e:
        return False, f"Excepcion en _normalize_latex: {e}"


def _self_test_html() -> Tuple[bool, str]:
    try:
        sample = (
            "Hola <script>alert(1)</script> mundo\n"
            "Entidad: <img src=x onerror=alert(1)>\n"
            "Comentario: <!-- oculto -->\n"
            "Autolink: <https://example.com>\n"
            "Enlace: [click](javascript:alert(1))\n"
        )
        out = _escape_html_outside_fences(sample)
        if "<script" in out or "<img" in out:
            return False, "Quedo una etiqueta sin escapar"
        if "<!--" in out:
            return False, "Quedo un comentario HTML"
        if "javascript:" in out:
            return False, "Quedo un esquema javascript:"
        return True, "OK: HTML neutralizado"
    except Exception as e:
        return False, f"Excepcion en _escape_html_outside_fences: {e}"


# ============================================================
# SFTP y respaldos
# ============================================================
def _sftp_connect():
    rs = _get_remote_server_cfg()
    if not rs:
        raise RuntimeError(
            "Falta la seccion [remote_server] en secrets.toml "
            "(host, port, user, private_key, dir_chat) o dir_chat es invalido."
        )
    transport = paramiko.Transport((rs["host"], int(rs["port"])))
    key = paramiko.Ed25519Key.from_private_key(StringIO(rs["private_key"]))
    transport.connect(username=rs["user"], pkey=key)
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp


def _remote_path(filename: str) -> str:
    rs = _get_remote_server_cfg()
    if not rs:
        raise RuntimeError("Falta [remote_server] en secrets.toml.")
    return f"{rs['dir_chat'].rstrip('/')}/{filename}"


def _ensure_remote_dir(sftp, path: str) -> None:
    if not path.startswith("/"):
        raise RuntimeError(f"Ruta remota no absoluta: {path!r}")
    norm = posixpath.normpath(path)
    if ".." in norm.split("/"):
        raise RuntimeError(f"Ruta remota contiene '..': {path!r}")
    parts = norm.rstrip("/").split("/")
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
            except OSError as e:
                raise RuntimeError(
                    f"No se pudo crear el directorio remoto '{current}': {e}"
                ) from e


def _backup_dir() -> str:
    rs = _get_remote_server_cfg()
    if not rs:
        raise RuntimeError("Falta [remote_server] en secrets.toml.")
    return f"{rs['dir_chat'].rstrip('/')}/backups"


def _safe_backup_timestamp(ts: str) -> str:
    if not re.fullmatch(r"\d{8}_\d{6}", ts):
        raise ValueError(f"Timestamp invalido: {ts}")
    return ts


_TS_SUFFIX_RE = re.compile(r"^\d{8}_\d{6}(_[0-9a-f]{4})?$")


def _safe_backup_timestamp_ext(ts: str) -> str:
    if not _TS_SUFFIX_RE.fullmatch(ts):
        raise ValueError(f"Timestamp invalido: {ts}")
    return ts


def _remote_backup_exists(sftp, bdir: str, ts: str) -> bool:
    try:
        sftp.stat(f"{bdir}/chat_backup_{ts}.json")
        return True
    except IOError:
        return False


def _pick_unique_timestamp(sftp, bdir: str, base_ts: str) -> str:
    if not _remote_backup_exists(sftp, bdir, base_ts):
        return base_ts
    for _ in range(5):
        candidate = f"{base_ts}_{uuid.uuid4().hex[:4]}"
        if not _remote_backup_exists(sftp, bdir, candidate):
            return candidate
    raise RuntimeError(
        f"No se pudo encontrar un timestamp libre para {base_ts} "
        f"tras 5 intentos."
    )


def _is_regular_remote_file(sftp, path: str) -> bool:
    try:
        stt = sftp.lstat(path)
        return stat_module.S_ISREG(stt.st_mode)
    except Exception:
        return False


_TITLE_ESCAPE_MAP = {"&": "&", "<": "<", ">": ">"}


def _sanitize_title_for_storage(title: str) -> str:
    t = title or ""
    t = re.sub(r"[\x00-\x1f\x7f]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    for k, v in _TITLE_ESCAPE_MAP.items():
        t = t.replace(k, v)
    return t[:200]


def upload_backup_to_server(
    timestamp: str,
    json_bytes: bytes,
    md_bytes: bytes,
    chat_title: str = "",
) -> Tuple[bool, str, str]:
    try:
        base_ts = _safe_backup_timestamp(timestamp)
        transport, sftp = _sftp_connect()
        try:
            bdir = _backup_dir()
            _ensure_remote_dir(sftp, bdir)
            ts = _pick_unique_timestamp(sftp, bdir, base_ts)
            json_name = f"chat_backup_{ts}.json"
            md_name = f"chat_backup_{ts}.md"
            with sftp.open(f"{bdir}/{json_name}", "wb") as f:
                f.write(json_bytes)
            with sftp.open(f"{bdir}/{md_name}", "wb") as f:
                f.write(md_bytes)
            if chat_title:
                meta_name = f"chat_backup_{ts}.title"
                safe_title = _sanitize_title_for_storage(chat_title)
                with sftp.open(f"{bdir}/{meta_name}", "wb") as f:
                    f.write(safe_title.encode("utf-8"))
            return True, f"Respaldos subidos: {json_name}", ts
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            try:
                transport.close()
            except Exception:
                pass
    except Exception as e:
        return False, f"Error al subir respaldos: {e}", timestamp


def _list_remote_backups_uncached() -> List[Dict]:
    backups: List[Dict] = []
    try:
        transport, sftp = _sftp_connect()
        try:
            bdir = _backup_dir()
            try:
                files = sftp.listdir_attr(bdir)
            except IOError:
                return []

            grouped: Dict[str, Dict] = {}
            for f in files:
                m = re.match(
                    r"chat_backup_(\d{8}_\d{6}(?:_[0-9a-f]{4})?)\.(json|md|title)$",
                    f.filename,
                )
                if not m:
                    continue
                ts, ext = m.group(1), m.group(2)
                full_path = f"{bdir}/{f.filename}"
                if not _is_regular_remote_file(sftp, full_path):
                    continue
                grouped.setdefault(
                    ts,
                    {"timestamp": ts, "json": None, "md": None, "title": ""},
                )
                if ext == "json":
                    grouped[ts]["json"] = f.filename
                    grouped[ts]["size"] = f.st_size
                elif ext == "md":
                    grouped[ts]["md"] = f.filename
                elif ext == "title":
                    try:
                        with sftp.open(full_path, "r") as tf:
                            raw = tf.read()
                            if isinstance(raw, bytes):
                                raw = raw.decode("utf-8", errors="replace")
                            grouped[ts]["title"] = raw
                    except Exception:
                        pass

            for ts, info in grouped.items():
                if not info.get("json"):
                    continue
                base_for_display = ts[:15] if len(ts) >= 15 else ts
                try:
                    dt = datetime.datetime.strptime(
                        base_for_display, "%Y%m%d_%H%M%S"
                    )
                    human = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    human = ts
                title = (info.get("title") or "").strip() or "(sin titulo)"
                size_kb = (info.get("size", 0) or 0) / 1024
                backups.append({
                    "timestamp": ts,
                    "json_file": info["json"],
                    "md_file": info.get("md"),
                    "title": title,
                    "display": f"{human} - {title} ({size_kb:.1f} KB)",
                    "size": info.get("size", 0),
                })
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            try:
                transport.close()
            except Exception:
                pass
    except Exception as e:
        st.error(f"Error al listar respaldos: {e}")
        return []

    return sorted(backups, key=lambda x: x["timestamp"], reverse=True)


@st.cache_data(ttl=30, show_spinner=False)
def _list_remote_backups_cached(_cache_buster: int = 0) -> List[Dict]:
    """
    Cache de 30s. El parametro _cache_buster permite forzar refresco
    incrementandolo desde la UI.
    """
    return _list_remote_backups_uncached()


def list_remote_backups(force_refresh: bool = False) -> List[Dict]:
    if force_refresh:
        st.session_state["_backup_cache_buster"] = (
            int(st.session_state.get("_backup_cache_buster", 0)) + 1
        )
    buster = int(st.session_state.get("_backup_cache_buster", 0))
    return _list_remote_backups_cached(buster)


def download_backup_from_server(filename: str) -> Optional[bytes]:
    if not re.fullmatch(
        r"chat_backup_\d{8}_\d{6}(?:_[0-9a-f]{4})?\.(json|md|title)",
        filename,
    ):
        st.error(f"Nombre de archivo no permitido: {filename}")
        return None
    try:
        transport, sftp = _sftp_connect()
        try:
            bdir = _backup_dir()
            full_path = f"{bdir}/{filename}"
            if not _is_regular_remote_file(sftp, full_path):
                st.error(f"El archivo {filename} no es un archivo regular.")
                return None
            with sftp.open(full_path, "rb") as f:
                data = f.read()
            return data
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            try:
                transport.close()
            except Exception:
                pass
    except Exception as e:
        st.error(f"Error al descargar {filename}: {e}")
        return None


def delete_backup_from_server(timestamp: str) -> Tuple[bool, str]:
    try:
        ts = _safe_backup_timestamp_ext(timestamp)
        transport, sftp = _sftp_connect()
        try:
            bdir = _backup_dir()
            deleted = 0
            for ext in ("json", "md", "title"):
                path = f"{bdir}/chat_backup_{ts}.{ext}"
                if not _is_regular_remote_file(sftp, path):
                    continue
                try:
                    sftp.remove(path)
                    deleted += 1
                except IOError:
                    pass
            return True, f"Eliminados {deleted} archivo(s)."
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            try:
                transport.close()
            except Exception:
                pass
    except Exception as e:
        return False, f"Error al eliminar: {e}"


_TITLE_MAX_LEN = 60
_TITLE_STRIP_RE = re.compile(r"[\x00-\x1f\x7f]")


def _derive_title_from_messages(
    messages: Sequence[Dict],
    max_len: int = _TITLE_MAX_LEN,
) -> str:
    for m in messages:
        if m.get("role") == "user":
            txt = _content_to_str(m.get("content", "")).strip()
            txt = _TITLE_STRIP_RE.sub(" ", txt)
            txt = re.sub(r"\s+", " ", txt).strip()
            if txt:
                raw = txt[:max_len] + ("..." if len(txt) > max_len else "")
                return _sanitize_title_for_storage(raw)
    return datetime.datetime.now().strftime("Chat del %Y-%m-%d %H:%M")


# ============================================================
# Carga de un respaldo en la sesion actual
# ============================================================
_VALID_ROLES = {"system", "user", "assistant"}
_VALID_PART_TYPES = {"text", "image_url"}

_MAX_BACKUP_MESSAGES = 500
_MAX_BACKUP_TOTAL_BYTES = 20 * 1024 * 1024

_ALLOWED_IMAGE_URL_SCHEMES = ("data:", "http://", "https://")


def _validate_messages(msgs) -> Tuple[bool, str]:
    if not isinstance(msgs, list):
        return False, "messages no es lista"
    if len(msgs) > _MAX_BACKUP_MESSAGES:
        return False, (
            f"Backup con demasiados mensajes: {len(msgs)} "
            f"(max {_MAX_BACKUP_MESSAGES})"
        )
    total_bytes = 0
    for m in msgs:
        if not isinstance(m, dict):
            return False, "mensaje no es dict"
        if m.get("role") not in _VALID_ROLES:
            return False, f"role invalido: {m.get('role')!r}"
        c = m.get("content")
        if not isinstance(c, (str, list)):
            return False, "content debe ser str o list"
        if isinstance(c, str):
            total_bytes += len(c.encode("utf-8", errors="ignore"))
        elif isinstance(c, list):
            for p in c:
                if not isinstance(p, dict):
                    return False, "parte no es dict"
                if p.get("type") not in _VALID_PART_TYPES:
                    return False, f"tipo de parte invalido: {p.get('type')!r}"
                if p.get("type") == "text":
                    t = p.get("text")
                    if not isinstance(t, str):
                        return False, "text debe ser str"
                    total_bytes += len(t.encode("utf-8", errors="ignore"))
                elif p.get("type") == "image_url":
                    iu = p.get("image_url")
                    if not isinstance(iu, dict):
                        return False, "image_url debe ser dict"
                    url = iu.get("url")
                    if not isinstance(url, str):
                        return False, "image_url.url debe ser str"
                    if not url.startswith(_ALLOWED_IMAGE_URL_SCHEMES):
                        return False, (
                            f"image_url.url con esquema no permitido: "
                            f"{url[:32]!r}"
                        )
                    total_bytes += len(url)
        if total_bytes > _MAX_BACKUP_TOTAL_BYTES:
            return False, (
                f"Backup excede {_MAX_BACKUP_TOTAL_BYTES // (1024 * 1024)} MB"
            )
    return True, "OK"


def load_backup_into_session(json_bytes: bytes) -> Tuple[bool, str]:
    try:
        payload = json.loads(json_bytes.decode("utf-8"))
    except Exception as e:
        return False, f"JSON invalido: {e}"

    if not isinstance(payload, dict):
        return False, "El JSON no es un objeto valido."

    messages = payload.get("messages", [])
    meta = payload.get("meta", {})
    if not isinstance(meta, dict):
        meta = {}

    ok, reason = _validate_messages(messages)
    if not ok:
        return False, f"Backup invalido: {reason}"

    messages = [m for m in messages if m.get("role") != "system"]
    messages.insert(0, {
        "role": "system",
        "content": DEFAULT_SYSTEM_PROMPT,
    })

    raw_tokens = meta.get("tokens_total", 0)
    try:
        tokens_total = int(raw_tokens)
        if tokens_total < 0:
            tokens_total = 0
    except Exception:
        tokens_total = 0

    st.session_state.messages = messages


    st.session_state.conversation_meta = {
        "ts": meta.get("ts") or datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
        "model": meta.get("model", DEFAULT_MODEL),
        "tokens_total": tokens_total,
    }
    st.session_state.tracker.reset()
    if tokens_total > 0:
        st.session_state.tracker.completion = tokens_total
    st.session_state["_turn_counter"] = len(messages)
    return True, f"Cargados {len(messages)} mensaje(s) del respaldo."


# ============================================================
# CSS global
# ============================================================
_GLOBAL_CSS = """
<style>
[data-testid="stChatMessage"] pre,
[data-testid="stChatMessage"] pre code,
[data-testid="stChatMessage"] code {
    white-space: pre-wrap !important;
    word-wrap: break-word !important;
    overflow-wrap: break-word !important;
    max-width: 100% !important;
    font-size: 1em !important;
}
[data-testid="stChatMessage"] h1,
[data-testid="stChatMessage"] h2,
[data-testid="stChatMessage"] h3,
[data-testid="stChatMessage"] h4,
[data-testid="stChatMessage"] h5,
[data-testid="stChatMessage"] h6 {
    font-size: 1.05em !important;
    font-weight: 600 !important;
    margin: 0.6em 0 0.3em 0 !important;
}
[data-testid="stChatMessage"] p,
[data-testid="stChatMessage"] li {
    font-size: 1em !important;
}
.katex-display,
.katex {
    max-width: 100% !important;
    overflow-x: auto;
    overflow-y: hidden;
}
[data-testid="stChatMessage"] p {
    white-space: normal !important;
    overflow-wrap: break-word;
}
</style>
"""


def _inject_global_css() -> None:
    if not st.session_state.get("_css_injected", False):
        st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)
        st.session_state["_css_injected"] = True


# ============================================================
# Autoscroll real (JS)
# ============================================================
def _inject_autoscroll() -> None:
    """
    Hace scroll al final del chat. Se ejecuta en cada rerun; el
    componente es invisible y no interfiere con el layout.
    """
    st.components.v1.html(
        """
        <script>
        (function() {
            const doc = window.parent.document;
            const main = doc.querySelector('section.main');
            if (main) {
                main.scrollTo({top: main.scrollHeight, behavior: 'smooth'});
            }
        })();
        </script>
        """,
        height=0,
    )


# ============================================================
# Inicializacion de sesion
# ============================================================
def _init_session_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "tracker" not in st.session_state:
        st.session_state.tracker = _TokenTracker()
    if "logger" not in st.session_state:
        logger, run_id, log_path = _setup_run_logger()
        st.session_state.logger = logger
        st.session_state.run_id = run_id
        st.session_state.log_path = log_path
        for w in st.session_state.get("_secret_warnings", []):
            _log(logger, event="secret_warning", detail=w)
    if "conversation_meta" not in st.session_state:
        st.session_state.conversation_meta = {
            "ts": datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
            "model": DEFAULT_MODEL,
            "tokens_total": 0,
        }
    if "show_uploader" not in st.session_state:
        st.session_state.show_uploader = False
    if "_turn_counter" not in st.session_state:
        st.session_state["_turn_counter"] = 0
    if "_uploader_key" not in st.session_state:
        st.session_state["_uploader_key"] = 0
    if "show_backups" not in st.session_state:
        st.session_state["show_backups"] = False
    if "_css_injected" not in st.session_state:
        st.session_state["_css_injected"] = False
    if "_run_self_tests" not in st.session_state:
        st.session_state["_run_self_tests"] = False
    if "_ephemeral_texts" not in st.session_state:
        st.session_state["_ephemeral_texts"] = {}
    if "_ephemeral_images" not in st.session_state:
        st.session_state["_ephemeral_images"] = {}
    if "_seen_uploads" not in st.session_state:
        st.session_state["_seen_uploads"] = set()
    if "_backup_cache_buster" not in st.session_state:
        st.session_state["_backup_cache_buster"] = 0
    if "flatten_headings" not in st.session_state:
        st.session_state["flatten_headings"] = DEFAULT_FLATTEN_HEADINGS

    if not any(m.get("role") == "system" for m in st.session_state.messages):
        st.session_state.messages.insert(0, {
            "role": "system",
            "content": DEFAULT_SYSTEM_PROMPT,
        })


def _reset_conversation() -> None:
    st.session_state.messages = [{
        "role": "system",
        "content": DEFAULT_SYSTEM_PROMPT,
    }]
    st.session_state.tracker.reset()
    logger, run_id, log_path = _setup_run_logger()
    st.session_state.logger = logger
    st.session_state.run_id = run_id
    st.session_state.log_path = log_path
    st.session_state.conversation_meta = {
        "ts": datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
        "model": DEFAULT_MODEL,
        "tokens_total": 0,
    }
    st.session_state["_turn_counter"] = 0
    st.session_state["_uploader_key"] += 1
    st.session_state["_ephemeral_texts"] = {}
    st.session_state["_ephemeral_images"] = {}
    st.session_state["_seen_uploads"] = set()
    st.session_state.show_uploader = False
    _log(st.session_state.logger, event="conversation_reset")


# ============================================================
# Render de mensajes
# ============================================================
def _render_message(role: str, content: Union[str, List]) -> None:
    if isinstance(content, list):
        text_parts = [
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        n_imgs = sum(
            1 for p in content
            if isinstance(p, dict) and p.get("type") == "image_url"
        )
        text = "\n".join(text_parts)
        if n_imgs:
            text += f"\n\n[+{n_imgs} imagen(es) adjunta(s)]"
    else:
        text = content or ""

    flatten = bool(st.session_state.get("flatten_headings", True))

    if role == "user":
        with st.chat_message("user", avatar="👤"):
            _render_markdown_safe(text, flatten_headings=flatten)
    elif role == "assistant":
        with st.chat_message("assistant", avatar="🤖"):
            _render_markdown_safe(text, flatten_headings=flatten)
    elif role == "system":
        return


# ============================================================
# Sidebar: controles (devuelve cfg)
# ============================================================
def _render_sidebar_controls() -> Dict:
    with st.sidebar:
        st.header("Configuracion")

        api_key = st.text_input(
            "DeepSeek API Key",
            value=_get_api_key_fallback(),
            type="password",
            key="api_key_input",
            help="Se usa solo en esta sesion. No se persiste en disco.",
        )
        base_url = st.text_input(
            "Base URL",
            value=DEFAULT_BASE_URL,
            key="base_url_input",
        )

        st.checkbox(
            "Aplanar headings",
            key="flatten_headings",
            help=(
                "Convierte encabezados Markdown en negrita. "
                "Util si los headings se ven mal en el chat."
            ),
        )

        return {
            "api_key": api_key.strip(),
            "base_url": base_url.strip(),
        }


# ============================================================
# Sidebar: acciones (botones que mutan estado)
# ============================================================
def _render_sidebar_actions() -> None:
    with st.sidebar:
        st.divider()
        st.subheader("Sesion")

        if st.button(
            "Proxima conversacion",
            use_container_width=True,
            type="primary",
            help="Guarda el chat actual en el servidor y empieza uno nuevo.",
        ):
            _save_and_start_new()

        if st.button(
            "Descartar sin guardar",
            use_container_width=True,
            help="Borra el chat actual sin subirlo al servidor.",
        ):
            _reset_conversation()
            st.rerun()

        if st.button(
            "Recuperar conversacion del servidor",
            use_container_width=True,
            help="Lista los respaldos guardados en el servidor remoto.",
        ):
            st.session_state["show_backups"] = not st.session_state.get(
                "show_backups", False
            )
            st.rerun()

        if st.session_state.get("show_backups", False):
            _render_backup_browser()

        if st.button("Copiar historial", use_container_width=True):
            txt = "\n".join(
                f"{m['role']}: {_content_to_str(m['content'])}"
                for m in st.session_state.messages
                if m.get("role") != "system"
            )
            st.session_state["_copied"] = txt
            st.success("Historial copiado (ver abajo).")

        if "_copied" in st.session_state:
            with st.expander("Historial copiado", expanded=False):
                st.code(st.session_state["_copied"], language="bash")
            del st.session_state["_copied"]

        with st.expander("Debug", expanded=False):
            rs = _get_remote_server_cfg()
            server_dir = rs.get("dir_chat", "(no configurado)")
            st.code(
                f"run_id:      {st.session_state.run_id}\n"
                f"log_local:   {st.session_state.log_path}\n"
                f"debug_dir:   {DEBUG_DIR}\n"
                f"server_dir:  {server_dir}\n"
                f"temperature: {FIXED_TEMPERATURE}\n"
                f"max_tokens:  {FIXED_MAX_TOKENS}\n"
                f"flatten_h:   {st.session_state.get('flatten_headings')}",
                language="bash",
            )

            if st.button(
                "Ejecutar self-tests",
                use_container_width=True,
                help="Verifica el fix LaTeX y el saneado HTML.",
            ):
                st.session_state["_run_self_tests"] = True

            if st.session_state.get("_run_self_tests", False):
                ok_latex, msg_latex = _self_test_latex()
                if ok_latex:
                    st.success(f"LaTeX: {msg_latex}")
                else:
                    st.error(f"LaTeX: {msg_latex}")
                ok_html, msg_html = _self_test_html()
                if ok_html:
                    st.success(f"HTML: {msg_html}")
                else:
                    st.error(f"HTML: {msg_html}")

            if st.button(
                "Limpiar carpeta debug/",
                use_container_width=True,
            ):
                try:
                    for f in os.listdir(DEBUG_DIR):
                        p = os.path.join(DEBUG_DIR, f)
                        if os.path.isfile(p):
                            os.remove(p)
                    st.success("Carpeta debug/ limpiada.")
                except Exception as e:
                    st.warning(f"No se pudo limpiar: {e}")


# ============================================================
# Guardar y empezar nueva conversacion
# ============================================================
def _save_and_start_new() -> None:
    real_messages = [
        m for m in st.session_state.messages if m.get("role") != "system"
    ]
    if not real_messages:
        st.info("La conversacion esta vacia; no hay nada que guardar.")
        _reset_conversation()
        st.rerun()
        return

    meta = dict(st.session_state.conversation_meta)
    meta["tokens_total"] = st.session_state.tracker.total
    title = _derive_title_from_messages(st.session_state.messages)

    json_bytes = _export_conversation_json(st.session_state.messages, meta)
    md_bytes = _export_conversation_markdown(st.session_state.messages, meta)

    with st.spinner("Guardando conversacion en el servidor..."):
        ok, msg, ts_efectivo = upload_backup_to_server(
            meta["ts"], json_bytes, md_bytes, title,
        )

    if ok:
        if ts_efectivo != meta["ts"]:
            st.success(
                f"Guardado: {title} "
                f"(timestamp ajustado a {ts_efectivo} por colision)"
            )
        else:
            st.success(f"Guardado: {title}")
        time.sleep(0.8)
        _reset_conversation()
        st.rerun()
        return
    else:
        st.error(f"No se pudo guardar: {msg}")


# ============================================================
# Navegador de respaldos
# ============================================================
def _render_backup_browser() -> None:
    with st.container(border=True):
        st.markdown("### Respaldos en el servidor")

        col_refresh, _ = st.columns([1, 3])
        with col_refresh:
            if st.button("Refrescar lista", use_container_width=True):
                list_remote_backups(force_refresh=True)
                st.rerun()

        with st.spinner("Listando respaldos..."):
            backups = list_remote_backups()

        if not backups:
            st.code(
                "# No hay respaldos guardados todavia.",
                language="bash",
            )
            return

        opciones = {b["display"]: b for b in backups}
        seleccion = st.selectbox(
            "Selecciona una conversacion:",
            options=list(opciones.keys()),
            key="select_backup",
        )

        col1, col2 = st.columns(2)
        with col1:
            if st.button(
                "Cargar",
                use_container_width=True,
                type="primary",
            ):
                b = opciones[seleccion]
                data = download_backup_from_server(b["json_file"])
                if data is None:
                    return
                ok, msg = load_backup_into_session(data)
                if ok:
                    st.success(msg)
                    st.session_state["show_backups"] = False
                    st.rerun()
                else:
                    st.error(msg)

        with col2:
            if st.button("Borrar", use_container_width=True):
                b = opciones[seleccion]
                ok, msg = delete_backup_from_server(b["timestamp"])
                if ok:
                    list_remote_backups(force_refresh=True)
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)


# ============================================================
# Panel de adjuntos
# ============================================================
def _render_simple_upload_panel() -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    st.markdown("### Adjuntar archivos")

    uploader_key = f"simple_uploader_{st.session_state['_uploader_key']}"

    uploaded_files = st.file_uploader(
        "Selecciona cualquier archivo (imagenes, texto, codigo...)",
        type=(ALLOWED_IMAGE_EXTENSIONS + ALLOWED_UPLOAD_EXTENSIONS),
        accept_multiple_files=True,
        key=uploader_key,
    )

    if not uploaded_files:
        return {}, {}

    if len(uploaded_files) > MAX_UPLOAD_FILES + MAX_IMAGE_FILES:
        st.code(
            f"[ERROR] Demasiados archivos: {len(uploaded_files)}. "
            f"Maximo combinado: {MAX_UPLOAD_FILES + MAX_IMAGE_FILES}.",
            language="bash",
        )
        return {}, {}

    seen = st.session_state.get("_seen_uploads", set())
    new_files = []
    for uf in uploaded_files:
        sig = (getattr(uf, "name", "?"), getattr(uf, "size", 0))
        if sig in seen:
            continue
        new_files.append(uf)

    if not new_files:
        return {}, {}

    with st.spinner("Procesando archivos..."):
        texts, images, skipped = _read_uploaded_any(
            new_files,
            logger=st.session_state.logger,
        )

    for uf in new_files:
        seen.add((getattr(uf, "name", "?"), getattr(uf, "size", 0)))
    st.session_state["_seen_uploads"] = seen

    msgs = []
    if texts:
        msgs.append(f"{len(texts)} texto(s)")
    if images:
        msgs.append(f"{len(images)} imagen(es)")
    if msgs:
        st.success(
            "Listos: " + " y ".join(msgs)
            + ". Se enviaran con tu proximo mensaje."
        )
    if skipped:
        st.code(
            "[WARN] Omitidos:\n"
            + "\n".join(f"  - {s}" for s in skipped),
            language="bash",
        )
    if not texts and not images and not skipped:
        st.code(
            "[WARN] No se pudo procesar ningun archivo.",
            language="bash",
        )

    if texts or images:
        with st.expander("Previsualizar adjuntos", expanded=False):
            if texts:
                st.markdown("**Textos / codigo:**")
                for name, meta in texts.items():
                    st.code(
                        f"{name} - {meta.get('size', 0):,} bytes - "
                        f"{meta.get('lines', 0):,} lineas - "
                        f"encoding: {meta.get('encoding', '?')}",
                        language="bash",
                    )
            if images:
                st.markdown("**Imagenes:**")
                cols = st.columns(min(len(images), 4))
                for i, (name, meta) in enumerate(images.items()):
                    with cols[i % len(cols)]:
                        st.image(
                            meta.get("bytes") or b"",
                            caption=name,
                            use_container_width=True,
                        )

    return texts, images


def _render_pending_chips(pending_t: Dict, pending_i: Dict) -> None:
    if not pending_t and not pending_i:
        return

    st.markdown("**Adjuntos listos para el proximo mensaje:**")
    chip_cols = st.columns(
        min(max(len(pending_t) + len(pending_i), 1), 4)
    )
    idx = 0
    for name, meta in pending_t.items():
        with chip_cols[idx % len(chip_cols)]:
            with st.container(border=True):
                st.code(
                    f"{name}\n"
                    f"{meta.get('size', 0):,} B - "
                    f"{meta.get('lines', 0):,} lineas",
                    language="bash",
                )
        idx += 1
    for name, meta in pending_i.items():
        with chip_cols[idx % len(chip_cols)]:
            with st.container(border=True):
                img_bytes = meta.get("bytes") or b""
                if img_bytes:
                    st.image(
                        img_bytes,
                        caption=name,
                        use_container_width=True,
                    )
                else:
                    st.code(f"{name}\n(metadatos)", language="bash")
        idx += 1


def _compose_user_text_with_attachments(
    prompt: str,
    texts_to_send: Dict[str, Dict],
) -> str:
    if not texts_to_send:
        return prompt
    bloques = [_wrap_attachment_text(name, meta)
               for name, meta in texts_to_send.items()]
    joined = "\n\n".join(bloques)
    if not prompt:
        return joined
    return prompt + "\n\n" + joined


# ============================================================
# Chat: manejo de input y streaming
# ============================================================
def _handle_user_input(
    prompt: str,
    model: str,
    api_key: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
) -> None:
    texts_to_send = dict(st.session_state["_ephemeral_texts"])
    imgs_to_send = dict(st.session_state["_ephemeral_images"])
    st.session_state["_ephemeral_texts"] = {}
    st.session_state["_ephemeral_images"] = {}
    st.session_state["_uploader_key"] += 1
    st.session_state["_seen_uploads"] = set()
    st.session_state.show_uploader = False

    user_text = _compose_user_text_with_attachments(prompt, texts_to_send)

    if imgs_to_send and model in MULTIMODAL_MODELS:
        user_content: Union[str, List] = _build_multimodal_content(
            user_text, imgs_to_send,
        )
    elif imgs_to_send:
        user_content = (
            user_text
            + f"\n\n[Se adjuntaron {len(imgs_to_send)} imagen(es) "
            + f"pero el modelo {model} no las soporta. "
            + f"Nombres: {', '.join(imgs_to_send.keys())}]"
        )
    else:
        user_content = user_text

    tracker: _TokenTracker = st.session_state.tracker
    if tracker.over_budget():
        st.code(
            "[ERROR] Presupuesto de tokens excedido. "
            "Reinicia la conversacion para seguir.",
            language="bash",
        )
        return

    user_msg = {"role": "user", "content": user_content}
    st.session_state.messages.append(user_msg)

    _render_message("user", user_content)

    _log(
        st.session_state.logger,
        event="user_message",
        length=len(prompt),
        n_texts=len(texts_to_send),
        n_images=len(imgs_to_send),
    )

    context = _trim_history(st.session_state.messages)
    client = _make_client(api_key, base_url)

    full_response = _stream_assistant_response(
        client=client,
        context=context,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        tracker=tracker,
    )

    if full_response is None:
        if (
            st.session_state.messages
            and st.session_state.messages[-1] is user_msg
        ):
            st.session_state.messages.pop()
        return

    assistant_msg = {"role": "assistant", "content": full_response}
    st.session_state.messages.append(assistant_msg)
    st.session_state.conversation_meta["tokens_total"] = tracker.total
    st.session_state.conversation_meta["model"] = model

    _log(
        st.session_state.logger,
        event="assistant_message",
        length=len(full_response),
        tokens_total=tracker.total,
        model=model,
    )

    st.session_state["_turn_counter"] = (
        int(st.session_state.get("_turn_counter", 0)) + 1
    )

    if st.session_state["_turn_counter"] % ROTATE_DEBUG_EVERY_N_TURNS == 0:
        _rotate_debug_dir()

    st.rerun()


def _stream_assistant_response(
    client: OpenAI,
    context: Sequence[Dict],
    model: str,
    max_tokens: int,
    temperature: float,
    tracker: _TokenTracker,
) -> Optional[str]:
    """
    Devuelve el texto completo o None si hubo error (en cuyo caso ya
    se ha mostrado el error al usuario).
    """
    flatten = bool(st.session_state.get("flatten_headings", True))
    with st.chat_message("assistant", avatar="🤖"):
        placeholder = st.empty()
        full_response = ""
        last_render = 0.0
        try:
            for delta in _call_streaming(
                client=client,
                messages=context,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                tracker=tracker,
            ):
                full_response += delta
                now = time.monotonic()
                if now - last_render < 0.08:
                    continue
                last_render = now
                placeholder.markdown(
                    _prepare_for_render(
                        _close_open_html(
                            _close_open_math(full_response)
                        ) + "▌",
                        flatten_headings=flatten,
                    ),
                    unsafe_allow_html=False,
                )
            placeholder.markdown(
                _prepare_for_render(
                    full_response, flatten_headings=flatten,
                ),
                unsafe_allow_html=False,
            )
        except Exception as e:
            st.code(
                f"[ERROR] Error al llamar a la API: {e}",
                language="bash",
            )
            _log(
                st.session_state.logger,
                event="api_error",
                error=str(e)[:300],
            )
            return None

        _show_response_text(full_response)
    return full_response


# ============================================================
# Tab principal del chat
# ============================================================
def _render_chat_tab(cfg: Dict) -> None:
    api_key = cfg["api_key"]
    base_url = cfg["base_url"]
    temperature = FIXED_TEMPERATURE
    max_tokens = FIXED_MAX_TOKENS
    model = DEFAULT_MODEL

    if not api_key or not api_key.startswith("sk-"):
        st.code(
            "[WARN] Configura una API Key valida de DeepSeek (sk-...) "
            "en la barra lateral.",
            language="bash",
        )
        return
    if not base_url.startswith("http"):
        st.code(
            "[ERROR] La Base URL debe empezar con http:// o https://",
            language="bash",
        )
        return

    for msg in st.session_state.messages:
        if msg.get("role") == "system":
            continue
        _render_message(msg.get("role", "user"), msg.get("content", ""))

    pending_t = st.session_state["_ephemeral_texts"]
    pending_i = st.session_state["_ephemeral_images"]

    clip_col, info_col = st.columns([1, 9])
    with clip_col:
        if st.button(
            "📎",
            key="clip_toggle",
            help=(
                "Adjuntar archivos (imagenes, texto o codigo) "
                "al proximo mensaje"
            ),
            use_container_width=True,
            type=(
                "primary" if st.session_state.show_uploader else "secondary"
            ),
        ):
            st.session_state.show_uploader = (
                not st.session_state.show_uploader
            )
            st.rerun()
    with info_col:
        info_lines = []
        n_pending = len(pending_t) + len(pending_i)
        if n_pending:
            partes = []
            if pending_t:
                partes.append(f"{len(pending_t)} texto(s)")
            if pending_i:
                partes.append(f"{len(pending_i)} imagen(es)")
            info_lines.append(
                "# adjuntos para el proximo mensaje: " + " + ".join(partes)
            )
        else:
            info_lines.append(
                "# sin adjuntos - pulsa el clip para anadir archivos"
            )
        st.code("\n".join(info_lines), language="bash")

    if st.session_state.show_uploader:
        with st.container(border=True):
            new_texts, new_images = _render_simple_upload_panel()
            if new_texts or new_images:
                pending_t.update(new_texts)
                pending_i.update(new_images)
                st.session_state["_ephemeral_texts"] = pending_t
                st.session_state["_ephemeral_images"] = pending_i

    if pending_t or pending_i:
        _render_pending_chips(pending_t, pending_i)

    if prompt := st.chat_input("Escribe tu mensaje..."):
        _handle_user_input(
            prompt=prompt,
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    _inject_autoscroll()

    st.divider()


# ============================================================
# main
# ============================================================
def main() -> None:
    st.set_page_config(
        page_title="Chat DeepSeek (local)",
        page_icon="💬",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    _init_session_state()
    _inject_global_css()
    cfg = _render_sidebar_controls()
    _render_sidebar_actions()
    _render_chat_tab(cfg)


if __name__ == "__main__":
    main()

