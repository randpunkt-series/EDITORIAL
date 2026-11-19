#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# chat13.py — Chat DeepSeek local con Streamlit
# - LaTeX nativo (KaTeX), sin HTML inyectado.
# - Bloques de código con lenguaje correcto (python, latex, bash…).
# - Adjuntos y respaldos SFTP.
# ============================================================

from __future__ import annotations

import os
import re
import io
import json
import time
import uuid
import base64
import logging
import datetime
from typing import Dict, List, Optional, Sequence, Tuple, Union
from io import StringIO

import streamlit as st
import requests
import paramiko

try:
    from openai import OpenAI
except ImportError:
    st.error("Falta openai: pip install openai")
    st.stop()

try:
    from PIL import Image
except ImportError:
    st.error("Falta pillow: pip install pillow")
    st.stop()

# ============================================================
# GUARDIA: detectar entidades HTML residuales en el propio fuente.
# ============================================================
def _check_source_not_html_escaped() -> None:
    try:
        with open(__file__, "r", encoding="utf-8") as fh:
            src = fh.read()
    except Exception:
        return
    pat = re.compile(r"&(quot|amp|lt|gt|#x27|#39|apos|nbsp);")
    hits = pat.findall(src)
    if hits:
        st.error(
            "El archivo fuente contiene entidades HTML escapadas "
            f"({len(hits)} ocurrencias de {sorted(set(hits))}). "
            "Cópialo/guárdalo como texto plano UTF-8, no desde un "
            "editor con vista enriquecida."
        )
        st.stop()

_check_source_not_html_escaped()

# ============================================================
# Configuración
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(BASE_DIR, "debug")
os.makedirs(DEBUG_DIR, exist_ok=True)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

MAX_TOKENS_BUDGET = 400_000
MAX_DEBUG_FILES = 50
MAX_CONTEXT_MESSAGES = 40
MAX_TOKENS_RESPONSE = 4096

# --- Textos ---
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_FILES = 20
ALLOWED_UPLOAD_EXTENSIONS = [
    "txt", "md", "markdown", "csv", "tsv",
    "log", "json", "py", "r", "tex",
    "js", "ts", "html", "css", "xml", "yaml", "yml",
    "sh", "bash", "sql", "c", "h", "cpp", "hpp",
    "java", "go", "rs", "rb", "php", "pl", "lua", "cls",
    "toml", "ini", "cfg", "conf", "env", "bib", "docx", "doc"
]

# --- Imágenes ---
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_FILES = 10
ALLOWED_IMAGE_EXTENSIONS = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "pdf"]

MULTIMODAL_MODELS = {"deepseek-chat"}

AVAILABLE_MODELS = [
    "deepseek-chat",
    "deepseek-reasoner",
]

# System prompt por defecto (solo se usa si el usuario no define uno).
DEFAULT_SYSTEM_PROMPT = (
    "Cuando devuelvas código, usa siempre bloques de código con el "
    "lenguaje explícito: ```python para Python, ```latex para LaTeX, "
    "```bash para shell, ```json, ```yaml, etc. "
    "No devuelvas HTML ni escapes el texto a entidades HTML. "
    "Para matemáticas usa LaTeX nativo: $...$ en línea y $$...$$ en bloque."
)


def _get_secret(section: str, key: str, default: str = "") -> str:
    try:
        if section:
            return st.secrets[section][key]
        return st.secrets[key]
    except Exception:
        return default


# Sin key hardcodeada: solo secrets.toml.
FALLBACK_DEEPSEEK_KEY = _get_secret("", "DEEPSEEK_API_KEY", "")


# ============================================================
# Logging estructurado (JSONL)
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
        payload = {"ts": datetime.datetime.now().isoformat(timespec="seconds")}
        payload.update(kwargs)
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
    base = re.sub(r'[^A-Za-z0-9._-]', '_', base)
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
# Lectura unificada de archivos subidos (texto + imágenes)
# ============================================================
def _decode_bytes(data: bytes) -> Tuple[str, str]:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1(replace)"


def _classify_uploaded_file(uf) -> Tuple[str, str]:
    name = _sanitize_filename(getattr(uf, "name", ""))
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ALLOWED_IMAGE_EXTENSIONS:
        return "image", ext
    if ext in ALLOWED_UPLOAD_EXTENSIONS:
        return "text", ext
    try:
        data = uf.getvalue() if hasattr(uf, "getvalue") else uf.read()
        if hasattr(uf, "seek"):
            try:
                uf.seek(0)
            except Exception:
                pass
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
            return "image", ext or "img"
        except Exception:
            pass
    except Exception:
        pass
    return "unknown", ext


def _read_uploaded_any(
    uploaded_files: Sequence,
    logger: Optional[logging.Logger] = None,
) -> Tuple[Dict[str, Dict], Dict[str, Dict], List[str]]:
    texts: Dict[str, Dict] = {}
    images: Dict[str, Dict] = {}
    skipped: List[str] = []

    if not uploaded_files:
        return texts, images, skipped

    for uf in uploaded_files:
        try:
            raw_name = getattr(uf, "name", "?")
            name = _sanitize_filename(raw_name)
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            kind, detected_ext = _classify_uploaded_file(uf)

            if kind == "image":
                data = uf.read()
                if hasattr(uf, "seek"):
                    try:
                        uf.seek(0)
                    except Exception:
                        pass
                if len(data) > MAX_IMAGE_BYTES:
                    skipped.append(f"{name} (> {MAX_IMAGE_BYTES // (1024*1024)} MB)")
                    continue
                try:
                    img = Image.open(io.BytesIO(data))
                    img.load()
                    width, height = img.size
                    fmt = (img.format or ext or "png").upper()
                except Exception:
                    skipped.append(f"{name} (no es imagen válida)")
                    continue
                mime = Image.MIME.get(fmt, f"image/{ext or 'png'}")
                b64 = base64.b64encode(data).decode("ascii")
                images[name] = {
                    "mime": mime,
                    "size": len(data),
                    "width": width,
                    "height": height,
                    "ext": ext or detected_ext or "img",
                    "b64": b64,
                    "data_uri": f"data:{mime};base64,{b64}",
                    "bytes": data,
                }
            elif kind == "text":
                data = uf.read()
                if len(data) > MAX_UPLOAD_BYTES:
                    skipped.append(f"{name} (> {MAX_UPLOAD_BYTES // (1024*1024)} MB)")
                    continue
                text, enc = _decode_bytes(data)
                texts[name] = {
                    "content": text,
                    "size": len(data),
                    "encoding": enc,
                    "ext": ext or detected_ext,
                    "lines": text.count("\n") + (1 if text else 0),
                }
            else:
                data = uf.read()
                if len(data) <= MAX_UPLOAD_BYTES:
                    text, enc = _decode_bytes(data)
                    printable = sum(
                        1 for ch in text[:2000]
                        if ch.isprintable() or ch in "\n\r\t"
                    )
                    if printable >= max(1, min(len(text), 2000)) * 0.9:
                        texts[name] = {
                            "content": text,
                            "size": len(data),
                            "encoding": enc,
                            "ext": ext or "txt",
                            "lines": text.count("\n") + (1 if text else 0),
                        }
                        continue
                skipped.append(f"{name} (tipo desconocido)")
        except Exception:
            skipped.append(f"{getattr(uf, 'name', '?')} (error)")

    return texts, images, skipped


# ============================================================
# Construcción de contenido multimodal
# ============================================================
def _build_multimodal_content(text: str, images: Dict[str, Dict]) -> List[Dict]:
    parts: List[Dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    for _name, meta in images.items():
        parts.append({"type": "image_url", "image_url": {"url": meta["data_uri"]}})
    return parts


# ============================================================
# Consulta de saldo (DeepSeek API oficial)
# ============================================================
def get_deepseek_balance(api_key: str, base_url: str = DEFAULT_BASE_URL) -> dict:
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/user/balance",
            headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("balance_infos"):
            return {"error": "La respuesta no contiene balance_infos."}
        info = data["balance_infos"][0]
        return {
            "is_available": data.get("is_available", False),
            "currency": info.get("currency", "?"),
            "total_balance": info.get("total_balance", "?"),
            "granted_balance": info.get("granted_balance", "?"),
            "topped_up_balance": info.get("topped_up_balance", "?"),
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# Cliente API con reintentos y streaming
# ============================================================
def _make_client(api_key: str, base_url: str) -> OpenAI:
    return OpenAI(api_key=api_key, base_url=base_url)


def _call_streaming(
    client: OpenAI,
    messages: Sequence[Dict],
    model: str,
    max_tokens: int = MAX_TOKENS_RESPONSE,
    temperature: float = 0.7,
    tracker: Optional[_TokenTracker] = None,
    max_retries: int = 4,
):
    delay = 2.0
    for attempt in range(max_retries):
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
                        yield delta.content
                if tracker is not None and getattr(chunk, "usage", None):
                    tracker.add(chunk.usage)
            return
        except Exception as e:
            msg = str(e).lower()
            retriable = any(
                k in msg
                for k in (
                    "429", "rate", "503", "502", "504",
                    "timeout", "timed out", "overloaded",
                )
            )
            if retriable and attempt < max_retries - 1:
                time.sleep(delay + 0.1 * attempt)
                delay = min(delay * 2, 30)
                continue
            raise RuntimeError(f"Error de API tras {attempt+1} intentos: {e}")


# ============================================================
# Utilidades de historial y exportación
# ============================================================
def _trim_history(
    messages: Sequence[Dict],
    max_msgs: int = MAX_CONTEXT_MESSAGES,
) -> List[Dict]:
    if len(messages) <= max_msgs:
        return list(messages)
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    return system + rest[-(max_msgs - len(system)):]


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
    serializable = [
        {"role": m.get("role"), "content": m.get("content")}
        for m in messages
    ]
    payload = {"meta": meta, "messages": serializable}
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _export_conversation_markdown(messages: Sequence[Dict], meta: Dict) -> bytes:
    lines: List[str] = []
    lines.append("# Conversación con DeepSeek\n")
    lines.append(f"- Modelo: `{meta.get('model', '?')}`")
    lines.append(f"- Fecha: {meta.get('ts', '?')}")
    lines.append(f"- Tokens totales: {meta.get('tokens_total', 0):,}\n")
    lines.append("---\n")
    for m in messages:
        role = m.get("role", "?")
        content = _content_to_str(m.get("content", ""))
        if role == "system":
            lines.append(f"**system**: {content}\n")
        elif role == "user":
            lines.append(f"## 👤 Usuario\n\n{content}\n")
        elif role == "assistant":
            lines.append(f"## 🤖 Asistente\n\n{content}\n")
    return "\n".join(lines).encode("utf-8")


# ============================================================
# Renderizado de Markdown / LaTeX / bloques de código
# ============================================================

# Alias de lenguajes → nombre canónico que resalta Streamlit.
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

_LATEX_INLINE_PAREN = re.compile(r"\\\((.+?)\\\)", re.DOTALL)
_LATEX_BLOCK_BRACK = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
_FENCE_RE = re.compile(r"^(\s*)```([^\n`]*)\n(.*?)\n^\s*```\s*$", re.DOTALL | re.MULTILINE)


def _normalize_latex(text: str) -> str:
    """
    Convierte delimitadores LaTeX alternativos que el modelo a veces emite
    (\\( ... \\)  y  \\[ ... \\]) a la forma que Streamlit/KaTeX entiende
    ($...$ y $$...$$). Protege bloques ```...``` para no tocar su interior.
    """
    if not text:
        return text
    code_blocks: List[str] = []

    def _stash(m):
        code_blocks.append(m.group(0))
        return f"\x00CODE{len(code_blocks)-1}\x00"

    text = re.sub(r"```.*?```", _stash, text, flags=re.DOTALL)
    text = _LATEX_INLINE_PAREN.sub(lambda m: f"${m.group(1)}$", text)
    text = _LATEX_BLOCK_BRACK.sub(lambda m: f"$${m.group(1)}$$", text)
    for i, blk in enumerate(code_blocks):
        text = text.replace(f"\x00CODE{i}\x00", blk)
    return text


def _guess_fence_language(body: str) -> str:
    """Heurística para etiquetar un bloque ```sin_lenguaje```."""
    if re.search(r"^\s*(def |class |import |from \w+ import |print\()", body, re.MULTILINE):
        return "python"
    if re.search(r"\\(documentclass|begin\{|end\{|frac|sqrt|sum_|int_)", body):
        return "latex"
    if re.search(r"^\s*(#!/bin/(ba)?sh|sudo |apt(-get)? |pip |\$ )", body, re.MULTILINE):
        return "bash"
    if re.search(r"^\s*\{[\s\S]*\}\s*$", body) and '"' in body:
        return "json"
    if re.search(r"^\s*[-\w]+\s*:\s*.+$", body, re.MULTILINE) and ":" in body:
        return "yaml"
    if re.search(r"^\s*<[\w!?]", body, re.MULTILINE):
        return "html"
    if re.search(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP)\b", body, re.IGNORECASE):
        return "sql"
    return "text"


def _normalize_fences(text: str) -> str:
    """
    Garantiza que cada bloque de código tenga un lenguaje correcto.
    - Normaliza alias (py→python, tex→latex, sh→bash, etc.).
    - Si un bloque no tiene lenguaje, lo adivina por heurística.
    """
    if "```" not in text:
        return text

    def _fix(m: re.Match) -> str:
        indent = m.group(1) or ""
        raw_lang = (m.group(2) or "").strip().lower()
        body = m.group(3)
        lang = _LANG_ALIASES.get(raw_lang, raw_lang)
        if not lang:
            lang = _guess_fence_language(body)
        return f"{indent}```{lang}\n{body}\n{indent}```"

    return _FENCE_RE.sub(_fix, text)


# Etiquetas de HTML potencialmente peligroso que el modelo podría colar.
_DANGEROUS_HTML_RE = re.compile(
    r"<\s*(script|iframe|object|embed|style|link|meta|form|input)\b",
    re.IGNORECASE,
)
_EVENT_ATTR_RE = re.compile(r"\son\w+\s*=", re.IGNORECASE)


def _strip_dangerous_html(text: str) -> str:
    """
    Si el modelo devuelve etiquetas HTML peligrosas (script/iframe/onerror=…),
    las neutralizamos mostrándolas dentro de un bloque ```html para que se
    vean como texto, no se ejecuten. No escapamos el resto: Markdown normal
    sigue funcionando y LaTeX sigue intacto.
    """
    if not text:
        return text
    if not (_DANGEROUS_HTML_RE.search(text) or _EVENT_ATTR_RE.search(text)):
        return text

    # Sacamos los bloques ```...``` y los preservamos tal cual.
    preserved: List[str] = []

    def _stash(m):
        preserved.append(m.group(0))
        return f"\x00PRES{len(preserved)-1}\x00"

    tmp = re.sub(r"```.*?```", _stash, text, flags=re.DOTALL)

    # Envolvemos las líneas con HTML peligroso en un fence ```html.
    lines = tmp.splitlines()
    out_lines: List[str] = []
    in_html_block = False
    for ln in lines:
        if _DANGEROUS_HTML_RE.search(ln) or _EVENT_ATTR_RE.search(ln):
            if not in_html_block:
                out_lines.append("```html")
                in_html_block = True
            out_lines.append(ln)
        else:
            if in_html_block:
                out_lines.append("```")
                in_html_block = False
            out_lines.append(ln)
    if in_html_block:
        out_lines.append("```")
    tmp = "\n".join(out_lines)

    for i, blk in enumerate(preserved):
        tmp = tmp.replace(f"\x00PRES{i}\x00", blk)
    return tmp


def _prepare_for_render(text: str) -> str:
    """Pipeline completo de saneado antes de st.markdown."""
    text = _normalize_latex(text)
    text = _normalize_fences(text)
    text = _strip_dangerous_html(text)
    return text


def _render_markdown_safe(text: str, *, allow_html: bool = False) -> None:
    """
    Renderiza texto del chat:
      - Markdown estándar.
      - Bloques ```python / ```latex / ```bash con resaltado nativo.
      - LaTeX nativo (KaTeX) para $...$ y $$...$$.
      - Por defecto NO interpreta HTML crudo del modelo.
    """
    if not text:
        return
    st.markdown(_prepare_for_render(text), unsafe_allow_html=allow_html)


def _render_plain_copyable(text: str, label: str = "📋 Copiar") -> None:
    """Bloque nativo copiable que preserva Unicode y LaTeX tal cual."""
    with st.expander(label, expanded=False):
        st.code(text, language="text")

# ============================================================
# CONEXIÓN SFTP Y GESTIÓN DE RESPALDOS EN SERVIDOR REMOTO
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
    return f"{rs['dir_chat'].rstrip('/')}/{filename}"


def _ensure_remote_dir(sftp, path: str):
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


def _backup_dir() -> str:
    rs = st.secrets["remote_server"]
    return f"{rs['dir_chat'].rstrip('/')}/backups"


def _safe_backup_timestamp(ts: str) -> str:
    if not re.fullmatch(r"\d{8}_\d{6}", ts):
        raise ValueError(f"Timestamp inválido: {ts}")
    return ts


def upload_backup_to_server(
    timestamp: str,
    json_bytes: bytes,
    md_bytes: bytes,
    chat_title: str = "",
) -> Tuple[bool, str]:
    try:
        ts = _safe_backup_timestamp(timestamp)
        transport, sftp = _sftp_connect()
        bdir = _backup_dir()
        _ensure_remote_dir(sftp, bdir)

        json_name = f"chat_backup_{ts}.json"
        md_name = f"chat_backup_{ts}.md"

        with sftp.open(f"{bdir}/{json_name}", "wb") as f:
            f.write(json_bytes)
        with sftp.open(f"{bdir}/{md_name}", "wb") as f:
            f.write(md_bytes)

        if chat_title:
            meta_name = f"chat_backup_{ts}.title"
            with sftp.open(f"{bdir}/{meta_name}", "wb") as f:
                f.write(chat_title[:200].encode("utf-8"))

        sftp.close()
        transport.close()
        return True, f"Respaldos subidos: {json_name}"
    except Exception as e:
        return False, f"Error al subir respaldos: {e}"


def list_remote_backups() -> List[Dict]:
    backups: List[Dict] = []
    try:
        transport, sftp = _sftp_connect()
        bdir = _backup_dir()
        try:
            files = sftp.listdir_attr(bdir)
        except IOError:
            sftp.close()
            transport.close()
            return []

        grouped: Dict[str, Dict] = {}
        for f in files:
            m = re.match(
                r"chat_backup_(\d{8}_\d{6})\.(json|md|title)$",
                f.filename,
            )
            if not m:
                continue
            ts, ext = m.group(1), m.group(2)
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
                    with sftp.open(f"{bdir}/{f.filename}", "r") as tf:
                        grouped[ts]["title"] = tf.read().decode(
                            "utf-8", errors="replace"
                        )
                except Exception:
                    pass

        for ts, info in grouped.items():
            if not info.get("json"):
                continue
            try:
                dt = datetime.datetime.strptime(ts, "%Y%m%d_%H%M%S")
                human = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                human = ts
            title = (info.get("title") or "").strip() or "(sin título)"
            size_kb = (info.get("size", 0) or 0) / 1024
            backups.append({
                "timestamp": ts,
                "json_file": info["json"],
                "md_file": info.get("md"),
                "title": title,
                "display": f"{human} — {title} ({size_kb:.1f} KB)",
                "size": info.get("size", 0),
            })

        sftp.close()
        transport.close()
    except Exception as e:
        st.error(f"Error al listar respaldos: {e}")
        return []

    return sorted(backups, key=lambda x: x["timestamp"], reverse=True)


def download_backup_from_server(filename: str) -> Optional[bytes]:
    try:
        transport, sftp = _sftp_connect()
        bdir = _backup_dir()
        with sftp.open(f"{bdir}/{filename}", "rb") as f:
            data = f.read()
        sftp.close()
        transport.close()
        return data
    except Exception as e:
        st.error(f"Error al descargar {filename}: {e}")
        return None


def delete_backup_from_server(timestamp: str) -> Tuple[bool, str]:
    try:
        ts = _safe_backup_timestamp(timestamp)
        transport, sftp = _sftp_connect()
        bdir = _backup_dir()
        deleted = 0
        for ext in ("json", "md", "title"):
            path = f"{bdir}/chat_backup_{ts}.{ext}"
            try:
                sftp.remove(path)
                deleted += 1
            except IOError:
                pass
        sftp.close()
        transport.close()
        return True, f"Eliminados {deleted} archivo(s)."
    except Exception as e:
        return False, f"Error al eliminar: {e}"


def _derive_title_from_messages(
    messages: Sequence[Dict],
    max_len: int = 60,
) -> str:
    for m in messages:
        if m.get("role") == "user":
            txt = _content_to_str(m.get("content", "")).strip()
            txt = re.sub(r"\s+", " ", txt)
            if txt:
                return txt[:max_len] + ("…" if len(txt) > max_len else "")
    return datetime.datetime.now().strftime("Chat del %Y-%m-%d %H:%M")


# ============================================================
# CARGA DE UN RESPALDO EN LA SESIÓN ACTUAL
# ============================================================
def load_backup_into_session(json_bytes: bytes) -> Tuple[bool, str]:
    try:
        payload = json.loads(json_bytes.decode("utf-8"))
    except Exception as e:
        return False, f"JSON inválido: {e}"

    messages = payload.get("messages", [])
    meta = payload.get("meta", {})
    if not isinstance(messages, list):
        return False, "El JSON no contiene una lista de mensajes válida."

    st.session_state.messages = messages
    st.session_state.conversation_meta = {
        "ts": meta.get("ts") or datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
        "model": meta.get("model", DEFAULT_MODEL),
        "tokens_total": meta.get("tokens_total", 0),
    }
    st.session_state.tracker.reset()
    if isinstance(meta.get("tokens_total"), int):
        st.session_state.tracker.completion = meta["tokens_total"]
    st.session_state["_scroll_to_bottom"] = True
    st.session_state["_turn_counter"] = len(messages)
    return True, f"Cargados {len(messages)} mensaje(s) del respaldo."


# ============================================================
# CSS GLOBAL — no rompe LaTeX de bloque, no inyecta HTML del modelo
# ============================================================
_GLOBAL_CSS = """
<style>
/* Solo forzamos el wrap en bloques de código, NO en párrafos:
   white-space:pre-wrap sobre .stMarkdown rompe $$...$$ en KaTeX. */
.assistant-msg-wrap pre,
.assistant-msg-wrap pre code,
.assistant-msg-wrap code {
    white-space: pre-wrap !important;
    word-wrap: break-word !important;
    overflow-wrap: break-word !important;
    max-width: 100% !important;
}

/* Bloques KaTeX no deben desbordar el contenedor. */
.katex-display,
.katex {
    max-width: 100% !important;
    overflow-x: auto;
    overflow-y: hidden;
}

/* El texto normal fluye como Markdown normal. */
.assistant-msg-wrap p {
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
# Interfaz Streamlit — inicialización y utilidades de render
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
    if "conversation_meta" not in st.session_state:
        st.session_state.conversation_meta = {
            "ts": datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
            "model": DEFAULT_MODEL,
            "tokens_total": 0,
        }
    if "system_prompt" not in st.session_state:
        st.session_state.system_prompt = ""
    if "balance_cache" not in st.session_state:
        st.session_state.balance_cache = None
    if "balance_last_check" not in st.session_state:
        st.session_state.balance_last_check = None
    if "show_uploader" not in st.session_state:
        st.session_state.show_uploader = False
    if "_scroll_to_bottom" not in st.session_state:
        st.session_state["_scroll_to_bottom"] = False
    if "_turn_counter" not in st.session_state:
        st.session_state["_turn_counter"] = 0
    if "_uploader_key" not in st.session_state:
        st.session_state["_uploader_key"] = 0
    if "show_backups" not in st.session_state:
        st.session_state["show_backups"] = False
    if "_css_injected" not in st.session_state:
        st.session_state["_css_injected"] = False


def _reset_conversation() -> None:
    st.session_state.messages = []
    st.session_state.tracker.reset()
    logger, run_id, log_path = _setup_run_logger()
    st.session_state.logger = logger
    st.session_state.run_id = run_id
    st.session_state.log_path = log_path
    st.session_state.conversation_meta = {
        "ts": datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
        "model": st.session_state.get("model_input", DEFAULT_MODEL),
        "tokens_total": 0,
    }
    st.session_state["_turn_counter"] = 0
    st.session_state["_scroll_to_bottom"] = False
    st.session_state["_uploader_key"] += 1
    _log(st.session_state.logger, event="conversation_reset")


def _render_message(role: str, content: Union[str, List]) -> None:
    """
    Renderiza un mensaje del historial:
      - Texto normal → Markdown sin HTML (LaTeX y fences nativos).
      - ```python / ```latex / ```bash con resaltado correcto.
      - Nunca inyecta HTML del modelo.
    """
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

    if role == "user":
        with st.chat_message("user", avatar="👤"):
            _render_markdown_safe(text)
    elif role == "assistant":
        with st.chat_message("assistant", avatar="🤖"):
            _render_markdown_safe(text)
            _render_plain_copyable(text)
    elif role == "system":
        with st.chat_message("system", avatar="⚙️"):
            st.markdown(f"**[system]** {text}")

# ============================================================
# Sidebar
# ============================================================
def _render_sidebar() -> Dict:
    with st.sidebar:
        st.header("⚙️ Configuración")

        api_key = st.text_input(
            "DeepSeek API Key",
            value=FALLBACK_DEEPSEEK_KEY,
            type="password",
            key="api_key_input",
            help="Se usa solo en esta sesión. No se persiste en disco.",
        )
        base_url = st.text_input(
            "Base URL",
            value=DEFAULT_BASE_URL,
            key="base_url_input",
        )
        model = st.selectbox(
            "Modelo",
            options=AVAILABLE_MODELS,
            index=0,
            key="model_input",
        )
        temperature = st.slider(
            "Temperatura",
            min_value=0.0, max_value=2.0, value=0.7, step=0.1,
            key="temperature_input",
        )
        max_tokens = st.number_input(
            "Máx. tokens por respuesta",
            min_value=256, max_value=8192,
            value=MAX_TOKENS_RESPONSE, step=256,
            key="max_tokens_input",
        )

        with st.expander("System prompt (opcional)"):
            st.caption(
                "Si lo dejas vacío, se usa un prompt por defecto que pide "
                "bloques ```python / ```latex / ```bash y prohíbe HTML."
            )
            system_prompt = st.text_area(
                "Instrucciones de sistema",
                value=st.session_state.system_prompt,
                height=120,
                key="system_prompt_input",
                placeholder=(
                    "Ejemplo: Eres un asistente experto en matemática "
                    "computacional. Devuelve código en bloques ```python "
                    "y fórmulas en LaTeX."
                ),
            )
            if st.button("Aplicar system prompt", use_container_width=True):
                st.session_state.system_prompt = system_prompt.strip()
                st.session_state.messages = [
                    m for m in st.session_state.messages
                    if m.get("role") != "system"
                ]
                effective = (
                    st.session_state.system_prompt or DEFAULT_SYSTEM_PROMPT
                )
                st.session_state.messages.insert(0, {
                    "role": "system",
                    "content": effective,
                })
                st.success("System prompt aplicado.")

        st.divider()
        st.subheader("📊 Estado")

        tracker: _TokenTracker = st.session_state.tracker
        col_a, col_b = st.columns(2)
        col_a.metric("Mensajes", len(st.session_state.messages))
        col_b.metric("Tokens", f"{tracker.total:,}")
        st.progress(
            min(tracker.total / tracker.budget, 1.0),
            text=f"{tracker.total:,} / {tracker.budget:,} tokens",
        )
        if tracker.over_budget():
            st.code(
                "[ERROR] Presupuesto de tokens excedido.",
                language="bash",
            )

        st.divider()
        st.subheader("💰 Saldo")
        bal = st.session_state.balance_cache
        if bal is None:
            st.code("# saldo: sin consultar", language="bash")
        elif "error" in bal:
            st.code(f"[ERROR] {bal['error'][:60]}", language="bash")
        else:
            sym = "$" if bal.get("currency") == "USD" else "¥"
            available = bal.get("is_available", False)
            st.metric(
                "Saldo total",
                f"{sym}{bal.get('total_balance', '?')}",
                delta="Disponible" if available else "Insuficiente",
                delta_color="normal" if available else "inverse",
            )
            if st.session_state.balance_last_check:
                st.code(
                    f"# actualizado: {st.session_state.balance_last_check}",
                    language="bash",
                )

        st.divider()
        st.subheader("💾 Sesión")

        if st.button(
            "🔄 Próxima conversación",
            use_container_width=True,
            type="primary",
            help="Guarda el chat actual en el servidor y empieza uno nuevo.",
        ):
            _save_and_start_new()

        if st.button(
            "🗑️ Descartar sin guardar",
            use_container_width=True,
            help="Borra el chat actual sin subirlo al servidor.",
        ):
            _reset_conversation()
            st.rerun()

        if st.button(
            "📂 Recuperar conversación del servidor",
            use_container_width=True,
            help="Lista los respaldos guardados en el servidor remoto.",
        ):
            st.session_state["show_backups"] = not st.session_state.get(
                "show_backups", False
            )
            st.rerun()

        if st.session_state.get("show_backups", False):
            _render_backup_browser()

        if st.button("📋 Copiar historial", use_container_width=True):
            txt = "\n\n".join(
                f"{m['role']}: {_content_to_str(m['content'])}"
                for m in st.session_state.messages
            )
            st.session_state["_copied"] = txt
            st.success("Historial copiado (ver abajo).")

        if "_copied" in st.session_state:
            with st.expander("Historial copiado"):
                st.code(st.session_state["_copied"], language="bash")

        with st.expander("🔧 Debug"):
            st.code(
                f"run_id:      {st.session_state.run_id}\n"
                f"log_local:   {st.session_state.log_path}\n"
                f"debug_dir:   {DEBUG_DIR}\n"
                f"server_dir:  {st.secrets['remote_server']['dir_chat']}",
                language="bash",
            )
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

        return {
            "api_key": api_key.strip(),
            "base_url": base_url.strip(),
            "model": model,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }


# ============================================================
# Guardar + empezar nueva conversación
# ============================================================
def _save_and_start_new() -> None:
    if not st.session_state.messages:
        st.info("La conversación está vacía; no hay nada que guardar.")
        _reset_conversation()
        st.rerun()

    meta = dict(st.session_state.conversation_meta)
    meta["tokens_total"] = st.session_state.tracker.total
    title = _derive_title_from_messages(st.session_state.messages)

    json_bytes = _export_conversation_json(st.session_state.messages, meta)
    md_bytes = _export_conversation_markdown(st.session_state.messages, meta)

    with st.spinner("Guardando conversación en el servidor…"):
        ok, msg = upload_backup_to_server(
            meta["ts"], json_bytes, md_bytes, title,
        )

    if ok:
        st.success(f"✅ Guardado: {title}")
        time.sleep(0.8)
        _reset_conversation()
        st.rerun()
    else:
        st.error(f"No se pudo guardar: {msg}")


# ============================================================
# Navegador de respaldos (lista + cargar + borrar)
# ============================================================
def _render_backup_browser() -> None:
    with st.container(border=True):
        st.markdown("### 📂 Respaldos en el servidor")
        with st.spinner("Listando respaldos…"):
            backups = list_remote_backups()

        if not backups:
            st.code(
                "# No hay respaldos guardados todavía.",
                language="bash",
            )
            return

        opciones = {b["display"]: b for b in backups}
        seleccion = st.selectbox(
            "Selecciona una conversación:",
            options=list(opciones.keys()),
            key="select_backup",
        )

        col1, col2 = st.columns(2)
        with col1:
            if st.button(
                "📥 Cargar",
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
            if st.button("🗑️ Borrar", use_container_width=True):
                b = opciones[seleccion]
                ok, msg = delete_backup_from_server(b["timestamp"])
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)


# ============================================================
# Panel de subida (clip 📎)
# ============================================================
def _render_simple_upload_panel() -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    st.markdown("### 📎 Adjuntar archivos")
    st.caption(
        "Acepta **imágenes** (png, jpg, webp, gif, bmp) y **archivos de "
        "texto/código** (txt, md, csv, json, py, js, html, css, yaml, "
        "sql, c, cpp, java, go, rs, rb, php, sh, …). "
        "Los archivos se adjuntan al **próximo mensaje** que envíes."
    )

    uploader_key = f"simple_uploader_{st.session_state['_uploader_key']}"

    uploaded_files = st.file_uploader(
        "Selecciona cualquier archivo (imágenes, texto, código…)",
        type=(ALLOWED_IMAGE_EXTENSIONS + ALLOWED_UPLOAD_EXTENSIONS),
        accept_multiple_files=True,
        key=uploader_key,
    )

    if not uploaded_files:
        st.code(
            "# Sin archivos seleccionados.\n"
            "# Arrastra archivos aquí o usa «Browse files».",
            language="bash",
        )
        return {}, {}

    if len(uploaded_files) > MAX_UPLOAD_FILES + MAX_IMAGE_FILES:
        st.code(
            f"[ERROR] Demasiados archivos: {len(uploaded_files)}. "
            f"Máximo combinado: {MAX_UPLOAD_FILES + MAX_IMAGE_FILES}.",
            language="bash",
        )
        return {}, {}

    with st.spinner("Procesando archivos…"):
        texts, images, skipped = _read_uploaded_any(
            uploaded_files,
            logger=st.session_state.logger,
        )

    msgs = []
    if texts:
        msgs.append(f"{len(texts)} texto(s)")
    if images:
        msgs.append(f"{len(images)} imagen(es)")
    if msgs:
        st.success(
            "✅ Listos: " + " y ".join(msgs)
            + ". Se enviarán con tu próximo mensaje."
        )
    if skipped:
        st.code(
            "[WARN] Omitidos:\n" + "\n".join(f"  - {s}" for s in skipped),
            language="bash",
        )
    if not texts and not images and not skipped:
        st.code(
            "[WARN] No se pudo procesar ningún archivo.",
            language="bash",
        )

    if texts or images:
        with st.expander("👁️ Previsualizar adjuntos", expanded=False):
            if texts:
                st.markdown("**Textos / código:**")
                for name, meta in texts.items():
                    st.code(
                        f"{name} — {meta.get('size', 0):,} bytes · "
                        f"{meta.get('lines', 0):,} líneas · "
                        f"encoding: {meta.get('encoding', '?')}",
                        language="bash",
                    )
            if images:
                st.markdown("**Imágenes:**")
                cols = st.columns(min(len(images), 4))
                for i, (name, meta) in enumerate(images.items()):
                    with cols[i % len(cols)]:
                        st.image(
                            meta["bytes"],
                            caption=name,
                            use_container_width=True,
                        )

    return texts, images


# ============================================================
# Chips de adjuntos pendientes
# ============================================================
def _render_pending_chips(pending_t: Dict, pending_i: Dict) -> None:
    if not pending_t and not pending_i:
        return

    st.markdown("**📌 Adjuntos listos para el próximo mensaje:**")
    chip_cols = st.columns(
        min(max(len(pending_t) + len(pending_i), 1), 4)
    )
    idx = 0
    for name, meta in pending_t.items():
        with chip_cols[idx % len(chip_cols)]:
            with st.container(border=True):
                st.code(
                    f"📄 {name}\n"
                    f"{meta.get('size', 0):,} B · "
                    f"{meta.get('lines', 0):,} líneas",
                    language="bash",
                )
        idx += 1
    for name, meta in pending_i.items():
        with chip_cols[idx % len(chip_cols)]:
            with st.container(border=True):
                st.image(
                    meta["bytes"],
                    caption=name,
                    use_container_width=True,
                )
        idx += 1


# ============================================================
# Pestaña de Chat
# ============================================================
def _render_chat_tab(cfg: Dict) -> None:
    """
    Pestaña principal de chat con streaming en vivo.
    - Streaming en Markdown normal, sin HTML.
    - Al terminar, se aplica _prepare_for_render (LaTeX + fences).
    """
    api_key = cfg["api_key"]
    base_url = cfg["base_url"]
    model = cfg["model"]
    temperature = cfg["temperature"]
    max_tokens = cfg["max_tokens"]

    if not api_key or not api_key.startswith("sk-"):
        st.code(
            "[WARN] Configura una API Key válida de DeepSeek (sk-...) "
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

    bal = st.session_state.balance_cache
    if bal and "error" not in bal and not bal.get("is_available", True):
        st.code(
            "[ERROR] Tu cuenta no tiene saldo disponible "
            "(is_available=false).\n"
            "        Recarga en https://platform.deepseek.com/top_up",
            language="bash",
        )

    # ------------------------------------------------------------
    # Historial
    # ------------------------------------------------------------
    for msg in st.session_state.messages:
        _render_message(msg.get("role", "user"), msg.get("content", ""))

    # ------------------------------------------------------------
    # Estado local de adjuntos
    # ------------------------------------------------------------
    if "_ephemeral_texts" not in st.session_state:
        st.session_state["_ephemeral_texts"] = {}
    if "_ephemeral_images" not in st.session_state:
        st.session_state["_ephemeral_images"] = {}

    pending_t = st.session_state["_ephemeral_texts"]
    pending_i = st.session_state["_ephemeral_images"]

    # ------------------------------------------------------------
    # Barra superior del composer
    # ------------------------------------------------------------
    clip_col, info_col = st.columns([1, 9])
    with clip_col:
        if st.button(
            "📎",
            key="clip_toggle",
            help=(
                "Adjuntar archivos (imágenes, texto o código) "
                "al próximo mensaje"
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
                "# adjuntos para el próximo mensaje: " + " + ".join(partes)
            )
        else:
            info_lines.append(
                "# sin adjuntos — pulsa 📎 para añadir archivos"
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

        col_a, col_b = st.columns(2)
        with col_a:
            if st.button(
                "✖️ Limpiar adjuntos",
                key="clear_ephemeral",
                use_container_width=True,
                disabled=not (pending_t or pending_i),
            ):
                st.session_state["_ephemeral_texts"] = {}
                st.session_state["_ephemeral_images"] = {}
                st.session_state["_uploader_key"] += 1
                st.rerun()
        with col_b:
            if st.button(
                "✔️ Cerrar panel",
                key="close_uploader",
                use_container_width=True,
            ):
                st.session_state.show_uploader = False
                st.rerun()

    if pending_t or pending_i:
        _render_pending_chips(pending_t, pending_i)

    # ------------------------------------------------------------
    # Chat input
    # ------------------------------------------------------------
    if prompt := st.chat_input("Escribe tu mensaje…"):
        texts_to_send = dict(st.session_state["_ephemeral_texts"])
        imgs_to_send = dict(st.session_state["_ephemeral_images"])
        st.session_state["_ephemeral_texts"] = {}
        st.session_state["_ephemeral_images"] = {}
        st.session_state["_uploader_key"] += 1
        st.session_state.show_uploader = False

        user_text = prompt
        if texts_to_send:
            bloques = []
            for name, meta in texts_to_send.items():
                bloques.append(
                    f"[Archivo adjunto: {name}]\n"
                    f"{meta.get('content', '')}\n"
                    f"[Fin de {name}]"
                )
            user_text = user_text + "\n\n" + "\n\n".join(bloques)

        if imgs_to_send and model in MULTIMODAL_MODELS:
            user_content: Union[str, List] = _build_multimodal_content(
                user_text, imgs_to_send,
            )
        elif imgs_to_send:
            user_content = (
                user_text
                + f"\n\n[Se adjuntaron {len(imgs_to_send)} imagen(es) "
                f"pero el modelo «{model}» no las soporta. "
                f"Nombres: {', '.join(imgs_to_send.keys())}]"
            )
        else:
            user_content = user_text

        user_msg = {"role": "user", "content": user_content}
        st.session_state.messages.append(user_msg)

        if texts_to_send or imgs_to_send:
            aviso = []
            if texts_to_send:
                aviso.append(f"{len(texts_to_send)} texto(s)")
            if imgs_to_send:
                aviso.append(f"{len(imgs_to_send)} imagen(es)")
            _render_message(
                "user",
                prompt + f"\n\n[Adjuntos: {', '.join(aviso)}]",
            )
        else:
            _render_message("user", prompt)

        _log(
            st.session_state.logger,
            event="user_message",
            length=len(prompt),
            preview=prompt[:120],
            n_texts=len(texts_to_send),
            n_images=len(imgs_to_send),
        )

        tracker: _TokenTracker = st.session_state.tracker
        if tracker.over_budget():
            st.code(
                "[ERROR] Presupuesto de tokens excedido. "
                "Reinicia la conversación para seguir.",
                language="bash",
            )
            return

        context = _trim_history(st.session_state.messages)
        client = _make_client(api_key, base_url)

        with st.chat_message("assistant", avatar="🤖"):
            placeholder = st.empty()
            full_response = ""
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
                    # Streaming: Markdown plano sin HTML.
                    # Evita inyectar HTML parcial mientras llega el texto.
                    placeholder.markdown(full_response + "▌")
                # Render final con pipeline completo (LaTeX + fences + saneo).
                placeholder.markdown(_prepare_for_render(full_response))
            except Exception as e:
                st.code(
                    f"[ERROR] Error al llamar a la API: {e}",
                    language="bash",
                )
                _log(
                    st.session_state.logger,
                    event="api_error",
                    error=str(e),
                )
                if (
                    st.session_state.messages
                    and st.session_state.messages[-1] is user_msg
                ):
                    st.session_state.messages.pop()
                return

            _render_plain_copyable(full_response, "📋 Copiar respuesta")

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

        _rotate_debug_dir()

        st.session_state["_scroll_to_bottom"] = True
        st.session_state["_turn_counter"] = (
            st.session_state.get("_turn_counter", 0) + 1
        )
        st.rerun()

    # ------------------------------------------------------------
    # Ancla de scroll
    # ------------------------------------------------------------
    if st.session_state.get("_scroll_to_bottom"):
        _inject_scroll_anchor()
        st.session_state["_scroll_to_bottom"] = False
    else:
        _inject_scroll_anchor()

    st.divider()

# ============================================================
# Ancla de scroll al final del chat
# ============================================================
def _inject_scroll_anchor() -> None:
    anchor_id = f"__chat_bottom_{st.session_state.get('_turn_counter', 0)}__"
    st.markdown(
        f'<div id="{anchor_id}" style="height:1px;"></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <script>
        (function() {{
            const el = document.getElementById("{anchor_id}");
            if (el && el.scrollIntoView) {{
                el.scrollIntoView({{behavior: "smooth", block: "end"}});
            }}
            const main = document.querySelector('section.main');
            if (main) {{
                main.scrollTo({{top: main.scrollHeight, behavior: "smooth"}});
            }}
        }})();
        </script>
        """,
        unsafe_allow_html=True,
    )


# ============================================================
# Pestaña de Saldo
# ============================================================
def _render_balance_tab(cfg: Dict) -> None:
    """Pestaña de consulta de saldo de la cuenta DeepSeek."""
    api_key = cfg["api_key"]
    base_url = cfg["base_url"]

    st.header("💰 Saldo de la cuenta DeepSeek")
    st.caption(
        "Consulta el saldo vía `GET /user/balance`. El saldo se descuenta "
        "según los tokens que consumes en cada llamada a la API. Si "
        "`is_available` es `false`, la API rechazará nuevas peticiones con "
        "error 402."
    )

    if not api_key or not api_key.startswith("sk-"):
        st.code(
            "[WARN] Configura una API Key válida de DeepSeek (sk-...) "
            "en la barra lateral antes de consultar el saldo.",
            language="bash",
        )
        return

    col_btn, col_auto = st.columns([1, 2])
    with col_btn:
        consultar = st.button(
            "🔄 Consultar saldo",
            type="primary",
            use_container_width=True,
        )
    with col_auto:
        auto_refresh = st.checkbox(
            "Actualizar automáticamente cada 60 s",
            value=False,
            help="Útil si estás chateando y quieres ver el saldo en vivo.",
        )

    if consultar or auto_refresh:
        with st.spinner("Consultando saldo…"):
            bal = get_deepseek_balance(api_key, base_url)
            st.session_state.balance_cache = bal
            st.session_state.balance_last_check = (
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            _log(
                st.session_state.logger,
                event="balance_check",
                result=bal,
            )

    bal = st.session_state.balance_cache

    if bal is None:
        st.code(
            "# Aún no has consultado el saldo.\n"
            "# Pulsa «🔄 Consultar saldo» para obtenerlo.",
            language="bash",
        )
        return

    if "error" in bal:
        st.code(
            f"[ERROR] No se pudo consultar el saldo: {bal['error']}\n"
            "# Posibles causas: API Key inválida, sin conexión a internet,\n"
            "# o el endpoint /user/balance no está disponible en tu\n"
            "# región/plan.",
            language="bash",
        )
        return

    currency = bal.get("currency", "?")
    sym = "$" if currency == "USD" else ("¥" if currency == "CNY" else "")
    available = bal.get("is_available", False)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric(
            "Saldo total",
            f"{sym}{bal.get('total_balance', '?')}",
            delta="Disponible" if available else "Insuficiente",
            delta_color="normal" if available else "inverse",
        )
    with col2:
        st.metric(
            "Saldo recargado",
            f"{sym}{bal.get('topped_up_balance', '?')}",
            help="Dinero real que has abonado a la cuenta.",
        )
    with col3:
        st.metric(
            "Crédito gratuito",
            f"{sym}{bal.get('granted_balance', '?')}",
            help="Crédito promocional o gratuito no expirado (si lo hay).",
        )

    st.divider()
    if available:
        st.code(
            "[OK] Saldo disponible. Puedes seguir llamando a la API.",
            language="bash",
        )
    else:
        st.code(
            "[ERROR] Saldo insuficiente (is_available=false).\n"
            "        La API rechazará nuevas peticiones con error 402\n"
            "        hasta que recargues.",
            language="bash",
        )

    st.code(
        "# Recargar saldo:\n"
        "#   https://platform.deepseek.com/top_up\n"
        "# Acepta Alipay, WeChat Pay, PayPal y tarjeta bancaria.\n"
        "# El saldo de recarga no expira.",
        language="bash",
    )

    st.divider()
    st.subheader("📈 Consumo de esta sesión")
    tracker: _TokenTracker = st.session_state.tracker
    col_s1, col_s2, col_s3 = st.columns(3)
    col_s1.metric("Tokens prompt", f"{tracker.prompt:,}")
    col_s2.metric("Tokens completion", f"{tracker.completion:,}")
    col_s3.metric("Tokens totales", f"{tracker.total:,}")


# ============================================================
# main() con pestañas
# ============================================================
def main() -> None:
    st.set_page_config(
        page_title="Chat DeepSeek (local)",
        page_icon="💬",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    _init_session_state()

    # Inyecta el CSS global una sola vez por sesión.
    _inject_global_css()

    cfg = _render_sidebar()

    tab_chat, tab_balance = st.tabs(["💬 Chat", "💰 Saldo"])

    with tab_chat:
        _render_chat_tab(cfg)

    with tab_balance:
        _render_balance_tab(cfg)


# ============================================================
# Entry point
# ============================================================
if __name__ == "__main__":
    main()
    
