#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
chat_deepseek.py — Cliente de chat local con Streamlit para DeepSeek API
con pestaña de saldo integrada.

Reutiliza la configuración, el logging JSONL, el cliente con reintentos y el
token tracker de `analizador_reformulacion.py`. Añade una pestaña de saldo
que consulta GET /user/balance para mostrar el estado de la cuenta.

Uso:
    streamlit run chat_deepseek.py

Requisitos:
    pip install streamlit openai requests
"""

from __future__ import annotations

import os
import re
import json
import time
import uuid
import logging
import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import streamlit as st
import requests

try:
    from openai import OpenAI
except ImportError:
    st.error("Falta openai: pip install openai")
    st.stop()

# ============================================================
# Configuración (idéntica a analizador_reformulacion.py)
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

AVAILABLE_MODELS = [
    "deepseek-chat",
    "deepseek-reasoner",
]


def _get_secret(section: str, key: str, default: str = "") -> str:
    try:
        if section:
            return st.secrets[section][key]
        return st.secrets[key]
    except Exception:
        return default


FALLBACK_DEEPSEEK_KEY = _get_secret("", "DEEPSEEK_API_KEY", "sk-fe5")


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
# Consulta de saldo (DeepSeek API oficial)
# ============================================================
def get_deepseek_balance(api_key: str, base_url: str = DEFAULT_BASE_URL) -> dict:
    """
    Consulta el saldo de la cuenta DeepSeek vía GET /user/balance.
    Devuelve un dict con:
      - is_available, currency, total_balance, granted_balance, topped_up_balance
    o un dict con 'error' si la consulta falla.
    """
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/user/balance",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
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
    messages: Sequence[Dict[str, str]],
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
            retriable = any(k in msg for k in (
                "429", "rate", "503", "502", "504",
                "timeout", "timed out", "overloaded",
            ))
            if retriable and attempt < max_retries - 1:
                time.sleep(delay + 0.1 * attempt)
                delay = min(delay * 2, 30)
                continue
            raise RuntimeError(f"Error de API tras {attempt+1} intentos: {e}")


# ============================================================
# Utilidades de historial y exportación
# ============================================================
def _trim_history(
    messages: Sequence[Dict[str, str]],
    max_msgs: int = MAX_CONTEXT_MESSAGES,
) -> List[Dict[str, str]]:
    if len(messages) <= max_msgs:
        return list(messages)
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    return system + rest[-(max_msgs - len(system)):]


def _export_conversation_json(
    messages: Sequence[Dict[str, str]],
    meta: Dict,
) -> bytes:
    payload = {"meta": meta, "messages": list(messages)}
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _export_conversation_markdown(
    messages: Sequence[Dict[str, str]],
    meta: Dict,
) -> bytes:
    lines: List[str] = []
    lines.append("# Conversación con DeepSeek\n")
    lines.append(f"- Modelo: `{meta.get('model', '?')}`")
    lines.append(f"- Fecha: {meta.get('ts', '?')}")
    lines.append(f"- Tokens totales: {meta.get('tokens_total', 0):,}\n")
    lines.append("---\n")
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if role == "system":
            lines.append(f"**system**: {content}\n")
        elif role == "user":
            lines.append(f"## 👤 Usuario\n\n{content}\n")
        elif role == "assistant":
            lines.append(f"## 🤖 Asistente\n\n{content}\n")
    return "\n".join(lines).encode("utf-8")

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
    _log(st.session_state.logger, event="conversation_reset")


def _render_message(role: str, content: str) -> None:
    if role == "user":
        with st.chat_message("user", avatar="👤"):
            st.markdown(content)
    elif role == "assistant":
        with st.chat_message("assistant", avatar="🤖"):
            st.markdown(content)
    elif role == "system":
        with st.chat_message("system", avatar="⚙️"):
            st.markdown(f"*{content}*")


# ============================================================
# Barra lateral
# ============================================================
def _render_sidebar() -> Dict:
    """
    Renderiza la barra lateral y devuelve un dict con la configuración
    activa: api_key, base_url, model, temperature, max_tokens.
    """
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
            system_prompt = st.text_area(
                "Instrucciones de sistema",
                value=st.session_state.system_prompt,
                height=120,
                key="system_prompt_input",
                placeholder=(
                    "Ejemplo: Eres un asistente experto en biología "
                    "computacional. Responde en español, con rigor y "
                    "sin inventar referencias."
                ),
            )
            if st.button("Aplicar system prompt", use_container_width=True):
                st.session_state.system_prompt = system_prompt.strip()
                st.session_state.messages = [
                    m for m in st.session_state.messages
                    if m.get("role") != "system"
                ]
                if st.session_state.system_prompt:
                    st.session_state.messages.insert(0, {
                        "role": "system",
                        "content": st.session_state.system_prompt,
                    })
                st.success("System prompt aplicado.")
                _log(st.session_state.logger,
                     event="system_prompt_set",
                     length=len(st.session_state.system_prompt))

        # --- Estado de la conversación ---
        st.divider()
        st.subheader("📊 Estado")

        tracker: _TokenTracker = st.session_state.tracker
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Mensajes", len(st.session_state.messages))
        col_b.metric("Tokens", f"{tracker.total:,}")
        col_c.metric(
            "Presupuesto",
            f"{tracker.total / tracker.budget * 100:.1f}%",
        )
        st.progress(
            min(tracker.total / tracker.budget, 1.0),
            text=f"{tracker.total:,} / {tracker.budget:,} tokens",
        )
        if tracker.over_budget():
            st.error(
                "Presupuesto de tokens excedido. Reinicia la conversación "
                "o aumenta `MAX_TOKENS_BUDGET` en el código."
            )

        # --- Saldo resumido en barra lateral ---
        st.divider()
        st.subheader("💰 Saldo")
        bal = st.session_state.balance_cache
        if bal is None:
            st.caption("Sin consultar. Ve a la pestaña 💰 Saldo.")
        elif "error" in bal:
            st.caption(f"⚠️ {bal['error'][:60]}")
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
                st.caption(
                    f"Actualizado: {st.session_state.balance_last_check}"
                )

        # --- Acciones ---
        st.divider()
        col_r1, col_r2 = st.columns(2)
        with col_r1:
            if st.button("🔄 Nueva conversación",
                         use_container_width=True,
                         type="primary"):
                _reset_conversation()
                st.rerun()
        with col_r2:
            if st.button("📋 Copiar historial",
                         use_container_width=True,
                         help="Copia el historial como texto plano"):
                txt = "\n\n".join(
                    f"**{m['role']}**: {m['content']}"
                    for m in st.session_state.messages
                )
                st.session_state["_copied"] = txt
                st.success("Historial copiado (ver abajo).")

        if "_copied" in st.session_state:
            with st.expander("Historial copiado"):
                st.code(st.session_state["_copied"], language="markdown")

        # --- Exportación ---
        st.divider()
        st.subheader("⬇️ Exportar")
        meta = dict(st.session_state.conversation_meta)
        meta["tokens_total"] = tracker.total
        meta["model"] = model

        col_e1, col_e2 = st.columns(2)
        with col_e1:
            st.download_button(
                "🧾 JSON",
                data=_export_conversation_json(
                    st.session_state.messages, meta),
                file_name=f"chat_{meta['ts']}.json",
                mime="application/json",
                use_container_width=True,
            )
        with col_e2:
            st.download_button(
                "📄 Markdown",
                data=_export_conversation_markdown(
                    st.session_state.messages, meta),
                file_name=f"chat_{meta['ts']}.md",
                mime="text/markdown",
                use_container_width=True,
            )

        # --- Debug ---
        with st.expander("🔧 Debug"):
            st.caption(f"Run ID: `{st.session_state.run_id}`")
            st.caption(f"Log: `{st.session_state.log_path}`")
            st.caption(f"Debug dir: `{DEBUG_DIR}`")
            if st.button("Limpiar carpeta debug/", use_container_width=True):
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
# Pestaña de Chat
# ============================================================
def _render_chat_tab(cfg: Dict) -> None:
    """Pestaña principal de chat con streaming en vivo."""
    api_key = cfg["api_key"]
    base_url = cfg["base_url"]
    model = cfg["model"]
    temperature = cfg["temperature"]
    max_tokens = cfg["max_tokens"]

    st.header("💬 Chat con DeepSeek")
    st.caption(
        "Conversación libre usando tu API Key contra `api.deepseek.com`. "
        "Sin límite diario de conversaciones; el único límite es el saldo "
        "de tu cuenta y la concurrencia de la API."
    )

    if not api_key or not api_key.startswith("sk-"):
        st.warning(
            "⚠️ Configura una API Key válida de DeepSeek (`sk-...`) en la "
            "barra lateral. Si no tienes una, crea una en "
            "https://platform.deepseek.com/api_keys"
        )
        return
    if not base_url.startswith("http"):
        st.error("La Base URL debe empezar con `http://` o `https://`.")
        return

    # Aviso proactivo si el saldo no está disponible
    bal = st.session_state.balance_cache
    if bal and "error" not in bal and not bal.get("is_available", True):
        st.error(
            "⚠️ Tu cuenta no tiene saldo disponible (`is_available=false`). "
            "Las llamadas a la API fallarán con error 402. "
            "Recarga en https://platform.deepseek.com/top_up"
        )

    # Render del historial
    for msg in st.session_state.messages:
        _render_message(msg.get("role", "user"), msg.get("content", ""))

    # Entrada del usuario
    if prompt := st.chat_input("Escribe tu mensaje…"):
        user_msg = {"role": "user", "content": prompt}
        st.session_state.messages.append(user_msg)
        _render_message("user", prompt)
        _log(st.session_state.logger, event="user_message",
             length=len(prompt), preview=prompt[:120])

        tracker: _TokenTracker = st.session_state.tracker
        if tracker.over_budget():
            st.error(
                "Presupuesto de tokens excedido. Reinicia la conversación "
                "para seguir."
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
                    placeholder.markdown(full_response + "▌")
                placeholder.markdown(full_response)
            except Exception as e:
                st.error(f"❌ Error al llamar a la API: {e}")
                _log(st.session_state.logger, event="api_error",
                     error=str(e))
                if st.session_state.messages and \
                        st.session_state.messages[-1] is user_msg:
                    st.session_state.messages.pop()
                return

        assistant_msg = {"role": "assistant", "content": full_response}
        st.session_state.messages.append(assistant_msg)
        st.session_state.conversation_meta["tokens_total"] = tracker.total
        st.session_state.conversation_meta["model"] = model

        _log(st.session_state.logger, event="assistant_message",
             length=len(full_response),
             tokens_total=tracker.total,
             model=model)

        _rotate_debug_dir()

    st.divider()
    st.caption(
        "💡 **Consejos:** (1) El historial se envía completo en cada turno, "
        "sujeto a `MAX_CONTEXT_MESSAGES`. (2) Si crece mucho, reinícialo para "
        "reducir costo y latencia. (3) `deepseek-chat` es rápida y barata; "
        "`deepseek-reasoner` razona mejor pero consume más tokens."
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
        st.warning(
            "⚠️ Configura una API Key válida de DeepSeek (`sk-...`) en la "
            "barra lateral antes de consultar el saldo."
        )
        return

    # --- Botón de consulta ---
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

    # --- Ejecutar consulta si se pidió ---
    if consultar or auto_refresh:
        with st.spinner("Consultando saldo…"):
            bal = get_deepseek_balance(api_key, base_url)
            st.session_state.balance_cache = bal
            st.session_state.balance_last_check = (
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            _log(st.session_state.logger,
                 event="balance_check",
                 result=bal)

    bal = st.session_state.balance_cache

    # --- Render del resultado ---
    if bal is None:
        st.info(
            "Aún no has consultado el saldo. Pulsa **🔄 Consultar saldo** "
            "para obtenerlo."
        )
        return

    if "error" in bal:
        st.error(f"No se pudo consultar el saldo: {bal['error']}")
        st.caption(
            "Posibles causas: API Key inválida, sin conexión a internet, "
            "o el endpoint `/user/balance` no está disponible en tu "
            "región/plan."
        )
        return

    # --- Métricas principales ---
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

    # --- Estado de disponibilidad ---
    st.divider()
    if available:
        st.success(
            "✅ **Saldo disponible.** Puedes seguir llamando a la API."
        )
    else:
        st.error(
            "⚠️ **Saldo insuficiente (`is_available=false`).** La API "
            "rechazará nuevas peticiones con error 402 hasta que recargues."
        )

    # --- Enlace de recarga ---
    st.markdown(
        "### Recargar saldo\n"
        "Puedes recargar tu cuenta en la plataforma de DeepSeek:\n\n"
        "👉 [https://platform.deepseek.com/top_up]"
        "(https://platform.deepseek.com/top_up)\n\n"
        "Acepta Alipay, WeChat Pay, PayPal y tarjeta bancaria. "
        "**El saldo de recarga no expira.**"
    )

    # --- Estimación de consumo ---
    st.divider()
    st.subheader("📈 Consumo de esta sesión")
    tracker: _TokenTracker = st.session_state.tracker
    col_s1, col_s2, col_s3 = st.columns(3)
    col_s1.metric("Tokens prompt", f"{tracker.prompt:,}")
    col_s2.metric("Tokens completion", f"{tracker.completion:,}")
    col_s3.metric("Tokens totales", f"{tracker.total:,}")

    st.caption(
        "El consumo se refiere solo a esta sesión de Streamlit. "
        "El saldo real de la cuenta refleja todo el uso histórico "
        "de la API Key, no solo esta sesión."
    )

    # --- Historial de consultas ---
    with st.expander("🕓 Historial de consultas en esta sesión"):
        history = st.session_state.get("balance_history", [])
        if not history:
            st.caption("No hay consultas registradas todavía.")
        else:
            for entry in history[-10:]:
                st.write(
                    f"`{entry['ts']}` — saldo: "
                    f"{entry.get('total_balance', '?')} "
                    f"{entry.get('currency', '?')} — "
                    f"{'disponible' if entry.get('is_available') else 'insuficiente'}"
                )

    # --- Nota metodológica ---
    st.divider()
    st.caption(
        "ℹ️ **Nota:** el endpoint `/user/balance` es oficial de DeepSeek. "
        "No todos los planes o regiones lo exponen con la misma latencia. "
        "Si la consulta falla repetidamente, verifica tu API Key y tu "
        "conexión."
    )


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

    st.title("💬 Chat DeepSeek — cliente local")
    st.caption(
        "Cliente de chat que usa **tu API Key** contra `api.deepseek.com`. "
        "Sin límite diario de conversaciones: el único límite es el saldo "
        "de tu cuenta y la concurrencia de la API. El historial vive solo "
        "en tu sesión de Streamlit; nada se envía a terceros."
    )

    # Barra lateral (común a ambas pestañas)
    cfg = _render_sidebar()

    # Pestañas
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
