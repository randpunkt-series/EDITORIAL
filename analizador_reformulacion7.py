#!/usr/bin/env python3
r"""
analizador_reformulacion.py — Streamlit standalone.

Generates a LETTER to the author with up to 4 reformulation weaknesses.
"""

import os
import re
import io
import json
import time
import uuid
import logging
import datetime
import tempfile
import shutil
import subprocess
from difflib import SequenceMatcher

import streamlit as st

try:
    from openai import OpenAI
except ImportError:
    st.error("Missing openai: pip install openai")
    st.stop()

try:
    from docx import Document as DocxDocument
    DOCX_DISPONIBLE = True
except ImportError:
    DOCX_DISPONIBLE = False

try:
    import paramiko
    PARAMIKO_DISPONIBLE = True
except ImportError:
    PARAMIKO_DISPONIBLE = False

# ============================================================
# Configuration & Secrets Fallback
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(BASE_DIR, "debug")
os.makedirs(DEBUG_DIR, exist_ok=True)

LOGO_PATH = os.path.join(BASE_DIR, "randpunkt-logo.pdf")
LOGO_DISPONIBLE = os.path.isfile(LOGO_PATH) and os.path.getsize(LOGO_PATH) > 0

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

MAX_QUESTIONS = 4
MIN_PARAGRAPH_LENGTH = 120
MAX_FRAGMENT_LENGTH = 300
MAX_FRAGMENT_LETTER = 300
MAX_UPLOAD_MB = 25                      # [FIX] límite de tamaño de archivo
MAX_DEBUG_FILES = 50                    # [FIX] rotación de debug/
MAX_TOKENS_BUDGET = 400_000             # [FIX] presupuesto de tokens por run

def _get_secret(section, key, default=""):
    try:
        if section:
            return st.secrets[section][key]
        else:
            return st.secrets[key]
    except Exception:
        return default

# [FIX] Credenciales hardcodeadas intactas, según indicación del usuario.
FALLBACK_DEEPSEEK_KEY = _get_secret("", "DEEPSEEK_API_KEY", "sk-fe5")

CATEGORIES = {
    "ambiguedad_conceptual": (
        "Term used without definition or with two different meanings in the "
        "same paragraph."),
    "generalizacion_sin_matiz": (
        "Overreaching claim: 'most', 'all', 'always', 'never' without "
        "qualification."),
    "no_sustentada_por_texto": (
        "The paragraph claims something that cannot be inferred from what "
        "was said earlier in the manuscript itself."),
    "estructura_argumental_debil": (
        "Confusing order of ideas, missing transitions, or a paragraph that "
        "mixes two different ideas."),
    "redundancia_interna": (
        "Repeats an idea already expressed in another paragraph of the "
        "manuscript."),
    "vaguedad_cuantitativa": (
        "Vague quantification ('many studies', 'a large percentage', "
        "'frequently') without a number, range, or reference."),
}

# ============================================================
# Logging estructurado (JSONL) # [FIX]
# ============================================================
def _setup_run_logger():
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"{ts}_{uuid.uuid4().hex[:6]}"
    log_path = os.path.join(DEBUG_DIR, f"run_{run_id}.jsonl")
    logger = logging.getLogger(f"run_{run_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger, run_id, log_path


def _log(logger, **kwargs):
    if logger is None:
        return
    try:
        payload = {"ts": datetime.datetime.now().isoformat(timespec="seconds")}
        payload.update(kwargs)
        logger.info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


def _rotate_debug_dir(max_files=MAX_DEBUG_FILES):
    # [FIX] mantiene solo los últimos N .pdf y .jsonl en debug/
    try:
        entries = [
            os.path.join(DEBUG_DIR, f) for f in os.listdir(DEBUG_DIR)
            if f.endswith((".pdf", ".jsonl"))
        ]
        entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in entries[max_files:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception:
        pass


# ============================================================
# SFTP Utilities (SSH key + 'preguntas' folder)
# ============================================================
def _sanitize_filename(name):
    # [FIX] evita path traversal y caracteres problemáticos
    base = os.path.basename(name or "")
    base = re.sub(r'[^A-Za-z0-9._-]', '_', base)
    base = base.lstrip(".-") or "file"
    return base[:180]


def upload_to_remote_server(local_filepath, remote_filename):
    """Uploads a file to the remote server via SFTP using an SSH key."""
    if not PARAMIKO_DISPONIBLE:
        return False, "paramiko is not installed (pip install paramiko)."

    try:
        host = st.secrets["remote_server"]["host"]
        port = int(st.secrets["remote_server"]["port"])
        user = st.secrets["remote_server"]["user"]
        ssh_key_str = st.secrets["remote_server"].get("ssh_key", "")
    except KeyError as e:
        return False, f"Missing remote_server config in secrets.toml: {e}"

    if not ssh_key_str:
        return False, "No 'ssh_key' found in [remote_server] secrets.toml."

    remote_dir = "/home/POLANCO6/RANDPUNKTJOURNAL/preguntas"
    safe_name = _sanitize_filename(remote_filename)          # [FIX]
    remote_path = f"{remote_dir}/{safe_name}"

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        key_file = io.StringIO(ssh_key_str)
        try:
            pkey = paramiko.RSAKey.from_private_key(key_file)
        except Exception:
            key_file.seek(0)
            pkey = paramiko.Ed25519Key.from_private_key(key_file)

        ssh.connect(host, port=port, username=user, pkey=pkey, timeout=15)

        sftp = ssh.open_sftp()

        try:
            sftp.stat(remote_dir)
        except IOError:
            try:
                sftp.mkdir(remote_dir)
            except PermissionError as e:                     # [FIX]
                sftp.close(); ssh.close()
                return False, f"Cannot create remote dir (permissions): {e}"

        # [FIX] timeout en la subida
        sftp.get_channel().settimeout(60)
        sftp.put(local_filepath, remote_path)

        sftp.close()
        ssh.close()
        return True, remote_path
    except Exception as e:
        return False, str(e)


# ============================================================
# Text extraction
# ============================================================
def extract_text_docx(path):
    if not DOCX_DISPONIBLE:
        raise RuntimeError(
            "Missing python-docx. Install with: pip install python-docx")
    doc = DocxDocument(path)
    blocks = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if t:
            blocks.append(t)
    for i, table in enumerate(doc.tables):
        rows = []
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            rows.append(" | ".join(cells))
        if rows:
            blocks.append(f"[TABLE {i+1}]\n" + "\n".join(rows))
    return "\n\n".join(blocks)


def extract_text_tex(text):
    m = re.search(
        r'\\begin\{document\}(.*?)\\end\{document\}', text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def load_manuscript(uploaded_file):
    name = uploaded_file.name
    ext = os.path.splitext(name)[1].lower()

    if ext == ".docx":
        if not DOCX_DISPONIBLE:
            raise RuntimeError(
                "The file is .docx but python-docx is missing. "
                "Install with: pip install python-docx")
        tmp = os.path.join(tempfile.gettempdir(), _sanitize_filename(name))
        with open(tmp, "wb") as f:
            f.write(uploaded_file.getbuffer())
        return extract_text_docx(tmp), name, "docx"

    elif ext == ".tex":
        raw = uploaded_file.read().decode("utf-8", errors="ignore")
        return extract_text_tex(raw), name, "tex"

    else:
        raise ValueError(f"Unsupported extension: {ext}. Use .tex or .docx.")


# ============================================================
# Text utilities
# ============================================================
def split_paragraphs(body):
    paragraphs = []
    for m in re.finditer(r'([^\n](?:.*?[^\n])?)\n\s*\n', body, re.DOTALL):
        text = m.group(1).strip()
        if len(text) < MIN_PARAGRAPH_LENGTH:
            continue
        if re.match(
                r'^\\(section|subsection|begin|end|label|cite|ref|'
                r'bibliography|printbibliography)\b', text):
            continue
        paragraphs.append((m.start(1), m.end(1), text))
    return paragraphs


def locate_section(body, paragraph_start):
    section = "(no section)"
    for m in re.finditer(r'\\section\*?\{([^{}]*)\}', body[:paragraph_start]):
        section = m.group(1).strip()
    line = body[:paragraph_start].count("\n") + 1
    return section, line


def _clean_for_comparison(s):
    s = re.sub(r'\\[a-zA-Z]+\*?(\[[^\]]*\])?(\{[^{}]*\})?', '', s)
    s = re.sub(r'[{}]', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip().lower()


def _fragment_to_plain_text(fragment, max_chars=MAX_FRAGMENT_LETTER):
    if not fragment:
        return ""
    s = fragment
    s = re.sub(r'(?<!\\)%.*$', '', s, flags=re.MULTILINE)
    s = re.sub(r'\$\$(.+?)\$\$', r'\1', s, flags=re.DOTALL)
    s = re.sub(r'\$(.+?)\$', r'\1', s, flags=re.DOTALL)
    s = re.sub(r'\\[a-zA-Z]+\*?(\[[^\]]*\])?(\{[^{}]*\})?', '', s)
    s = re.sub(r'\\[a-zA-Z]+\*?', '', s)
    s = re.sub(r'[{}&~^_]', ' ', s)
    s = re.sub(r'\\', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + "…"
    return s


def _escape_latex(s):
    if not s:
        return ""
    replacements = {
        '\\': r'\textbackslash{}', '&': r'\&', '%': r'\%', '$': r'\$',
        '#': r'\#', '_': r'\_', '{': r'\{', '}': r'\}',
        '~': r'\textasciitilde{}', '^': r'\textasciicircum{}',
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    return s


def _escape_plain_text(s):
    if not s:
        return ""
    replacements = {'&': r'\&', '%': r'\%', '#': r'\#', '_': r'\_'}
    for k, v in replacements.items():
        s = s.replace(k, v)
    return s


# ============================================================
# Detection prompt (unificado) # [FIX]
# ============================================================
PROMPT_DETECTION = r"""
You are an academic editor expert in clarity and argumentative soundness.
I give you a CONTEXT and a numbered set of PARAGRAPHS from a manuscript.
Your task: identify at most 3 REFORMULATION weaknesses across those paragraphs.
A weakness is a problem solvable by REWRITING the text, WITHOUT new
experiments, data, citations, or measurements.

ALLOWED CATEGORIES:
- ambiguedad_conceptual
- generalizacion_sin_matiz
- no_sustentada_por_texto
- estructura_argumental_debil
- redundancia_interna
- vaguedad_cuantitativa

FORBIDDEN:
- Adding citations / bibliographic searches.
- New experiments, measurements, or data validations.
- Comparing methods or reporting numerical errors.

HARD RULE: if the only weakness requires new data, DO NOT flag it.

CRITICAL LANGUAGE RULE: write ALL JSON text fields (diagnostico, impacto,
pasos, reescritura_A, reescritura_B, criterio_verificacion) STRICTLY IN ENGLISH,
regardless of the manuscript language. Do NOT include LaTeX commands in those
fields; use plain text only.

OUTPUT FORMAT (strict JSON, no Markdown):
{
  "debilidades": [
    {
      "paragraph_id": 3,
      "categoria": "one_of_the_allowed_categories",
      "fragmento": "literal fragment of PARAGRAPH 3 (EXACT COPY, max 300 chars)",
      "diagnostico": "what is weak and why (1-2 sentences, IN ENGLISH)",
      "impacto": "why it matters (1 sentence, IN ENGLISH)",
      "pasos": ["step 1 (IN ENGLISH)", "step 2 (IN ENGLISH)"],
      "reescritura_A": "conservative reformulated version (IN ENGLISH)",
      "reescritura_B": "alternative reformulated version (IN ENGLISH)",
      "criterio_verificacion": "how to know it is resolved (IN ENGLISH)"
    }
  ]
}
If no weaknesses: {"debilidades": []}

CONTEXT:
{contexto}

PARAGRAPHS:
{parrafos}
"""


# ============================================================
# Post-filter: discard weaknesses that ask for new data # [FIX]
# ============================================================
_NEW_DATA_PATTERNS = [
    # English
    r'\bexperiment(s)?\b', r'\bmeasurement(s)?\b', r'\bmeasure\b',
    r'\bvalidate with data\b', r'\breal data\b',
    r'\bcompare with data\b', r'\breport error\b',
    r'\bcollect data\b', r'\bsample\b', r'\breplicate\b',
    r'\bnew data\b', r'\bsurvey(s)?\b',
    r'\bcite\b', r'\bbibliographic reference(s)?\b',
    r'\bbibliography\b', r'\bdoi\b',
    # Spanish (bilingual hardening)
    r'\bexperimento(s)?\b', r'\bmedici[oó]n(es)?\b', r'\bmedir\b',
    r'\bvalidar con datos\b', r'\bdatos reales\b',
    r'\bcomparar con datos\b', r'\breportar error\b',
    r'\brecolectar datos\b', r'\bmuestra\b', r'\breplicar\b',
    r'\bnuevos datos\b', r'\bencuesta(s)?\b',
    r'\bcitar\b', r'\breferencia(s)? bibliogr[aá]fica(s)?\b',
    r'\bbibliograf[ií]a\b',
]

_EXISTING_CITATION_PATTERNS = [
    r'\\cite[a-zA-Z]*\s*\{',
]


def _asks_for_new_data(text):
    t = (text or "").lower()
    return any(re.search(p, t) for p in _NEW_DATA_PATTERNS)


def _already_has_citation(fragment):
    return any(re.search(p, fragment) for p in _EXISTING_CITATION_PATTERNS)


def _looks_english(text, threshold=0.6):
    # [FIX] heurística mínima de idioma (sin dependencias externas)
    if not text or len(text) < 20:
        return True
    en = len(re.findall(
        r'\b(the|and|of|to|in|that|is|for|with|this|are|by|as|it|from|or)\b',
        text.lower()))
    es = len(re.findall(
        r'\b(el|la|los|las|de|que|en|y|un|una|es|por|con|para|este|esta|o)\b',
        text.lower()))
    total = en + es
    if total < 5:
        return True
    return (en / total) >= threshold


def _fuzzy_match(fragment, paragraph, threshold=0.95):
    # [FIX] matching robusto: primero exacto, luego ratio ≥ threshold
    frag_norm = _clean_for_comparison(fragment)
    parr_norm = _clean_for_comparison(paragraph)
    if not frag_norm:
        return False
    if frag_norm in parr_norm:
        return True
    if len(frag_norm) < 20:
        return False
    m = SequenceMatcher(None, frag_norm, parr_norm, autojunk=False)
    match = m.find_longest_match(0, len(frag_norm), 0, len(parr_norm))
    if match.size == 0:
        return False
    coverage = match.size / len(frag_norm)
    return coverage >= threshold


def validate_weakness(deb, paragraph):
    if not deb:
        return False, "no weakness"

    cat = deb.get("categoria", "")
    if cat not in CATEGORIES:
        return False, f"invalid category: {cat}"

    frag = (deb.get("fragmento") or "").strip()
    if not frag or len(frag) < 20:
        return False, "empty or too short fragment"

    if not _fuzzy_match(frag, paragraph):                    # [FIX]
        return False, "fragment does not appear in the paragraph (fuzzy<0.95)"

    if _already_has_citation(frag):
        if cat == "no_sustentada_por_texto":
            return False, "the fragment already contains \\cite"

    reformulation_text = " ".join([
        deb.get("diagnostico", ""),
        deb.get("impacto", ""),
        " ".join(deb.get("pasos", []) or []),
        deb.get("reescritura_A", ""),
        deb.get("reescritura_B", ""),
        deb.get("criterio_verificacion", ""),
    ])
    if _asks_for_new_data(reformulation_text):
        return False, "the proposed reformulation requires new data"

    # [FIX] verificación de idioma inglés en campos del LLM
    if not _looks_english(reformulation_text):
        return False, "LLM output is not in English"

    deb["pasos"] = [p for p in (deb.get("pasos") or []) if p and p.strip()]
    if len(deb["pasos"]) < 2:
        return False, "fewer than 2 implementation steps"

    for k in ("diagnostico", "impacto", "reescritura_A",
              "reescritura_B", "criterio_verificacion"):
        if not deb.get(k):
            return False, f"empty field: {k}"

    if len(frag) > MAX_FRAGMENT_LENGTH * 2:
        deb["fragmento"] = frag[:MAX_FRAGMENT_LENGTH] + "..."

    return True, ""


# ============================================================
# LLM (con retries, backoff y tracking de tokens) # [FIX]
# ============================================================
class _TokenTracker:
    def __init__(self, budget=MAX_TOKENS_BUDGET):
        self.prompt = 0
        self.completion = 0
        self.budget = budget

    def add(self, usage):
        if usage is None:
            return
        try:
            self.prompt += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.completion += int(getattr(usage, "completion_tokens", 0) or 0)
        except Exception:
            pass

    @property
    def total(self):
        return self.prompt + self.completion

    def over_budget(self):
        return self.total > self.budget


def _call(client, prompt, model, max_tokens=2000, temperature=0.2,
          tracker=None, max_retries=4):
    delay = 2.0
    last_err = None
    for attempt in range(max_retries):
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=90,
            )
            if tracker is not None:
                tracker.add(getattr(r, "usage", None))
            return r.choices[0].message.content.strip(), None
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # Reintentar en rate limit / server errors / timeouts
            if any(k in msg for k in ("429", "rate", "503", "502", "504",
                                       "timeout", "timed out", "overloaded")):
                if attempt < max_retries - 1:
                    time.sleep(delay + (0.1 * attempt))
                    delay = min(delay * 2, 30)
                    continue
            return None, e
    return None, last_err


def _context_summary(previous_paragraphs, max_chars=1200):
    if not previous_paragraphs:
        return "(no context: this is the first paragraph)"
    text = " ".join(previous_paragraphs)
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


# ============================================================
# Main analysis (chunked + paragraph_id explícito) # [FIX]
# ============================================================
def analyze_reformulation(client, body, model, progress_cb=None,
                          logger=None, cancel_event=None):
    paragraphs = split_paragraphs(body)
    results = []
    rejections = {"llm_empty": 0, "validation": 0, "new_data": 0,
                  "not_english": 0, "bad_paragraph_id": 0}
    previous_paragraphs = []
    tracker = _TokenTracker()

    chunk_size = 10
    chunks = [paragraphs[i:i + chunk_size]
              for i in range(0, len(paragraphs), chunk_size)]

    for chunk_idx, chunk in enumerate(chunks):
        if cancel_event is not None and cancel_event.is_set():
            _log(logger, event="cancelled", chunk=chunk_idx)
            break
        if progress_cb:
            progress_cb(chunk_idx, len(chunks))

        # Backoff ya manejado dentro de _call; este sleep es cortés
        time.sleep(1.5)

        chunk_text = ""
        for p_idx, (_, _, p_text) in enumerate(chunk, start=1):
            chunk_text += f"[PARAGRAPH {p_idx}]\n{p_text}\n\n"

        context = _context_summary(previous_paragraphs)
        prompt = PROMPT_DETECTION.replace("{contexto}", context) \
                                 .replace("{parrafos}", chunk_text)

        raw, err = _call(client, prompt, model, max_tokens=3000,
                         temperature=0.2, tracker=tracker)
        if raw is None:
            st.error(f"❌ Block {chunk_idx+1} failed after retries: {err}")
            _log(logger, event="llm_error", chunk=chunk_idx + 1, error=str(err))
            rejections["llm_empty"] += 1
            previous_paragraphs.extend(p_text for _, _, p_text in chunk)
            if tracker.over_budget():
                st.warning("Token budget exceeded — stopping early.")
                break
            continue

        try:
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
            data = json.loads(raw)
        except Exception as e:
            st.error(f"❌ JSON parse error in block {chunk_idx+1}: {e}")
            _log(logger, event="json_error", chunk=chunk_idx + 1,
                 error=str(e), raw=raw[:500])
            rejections["llm_empty"] += 1
            previous_paragraphs.extend(p_text for _, _, p_text in chunk)
            continue

        debilidades = data.get("debilidades", []) or []
        if not debilidades:
            rejections["llm_empty"] += 1
            previous_paragraphs.extend(p_text for _, _, p_text in chunk)
            continue

        for deb in debilidades:
            pid = deb.get("paragraph_id")
            if not isinstance(pid, int) or not (1 <= pid <= len(chunk)):
                rejections["bad_paragraph_id"] += 1
                _log(logger, event="bad_paragraph_id", chunk=chunk_idx + 1,
                     pid=pid)
                continue

            start, end, p_text = chunk[pid - 1]
            ok, reason = validate_weakness(deb, p_text)
            if not ok:
                rejections["validation"] += 1
                if "new data" in reason:
                    rejections["new_data"] += 1
                if "not in English" in reason:
                    rejections["not_english"] += 1
                _log(logger, event="rejected", chunk=chunk_idx + 1,
                     pid=pid, categoria=deb.get("categoria"),
                     reason=reason)
                continue

            section, line = locate_section(body, start)
            # [FIX] índice global correcto (sin depender de .index())
            global_idx = paragraphs.index((start, end, p_text))
            results.append({
                "paragraph_idx": global_idx,
                "paragraph_start": start,
                "paragraph_end": end,
                "paragraph_text": p_text,
                "section": section,
                "line": line,
                "weakness": deb,
            })

        previous_paragraphs.extend(p_text for _, _, p_text in chunk)

        if tracker.over_budget():
            st.warning(
                f"Token budget exceeded ({tracker.total} > "
                f"{tracker.budget}) — stopping early.")
            break

    # [FIX] diversificación antes de truncar
    priority = {
        "no_sustentada_por_texto": 0,
        "ambiguedad_conceptual": 1,
        "generalizacion_sin_matiz": 2,
        "estructura_argumental_debil": 3,
        "vaguedad_cuantitativa": 4,
        "redundancia_interna": 5,
    }
    results.sort(key=lambda r: (
        priority.get(r["weakness"]["categoria"], 9),
        r["paragraph_idx"],
    ))

    diversified = []
    seen_cat = {}
    seen_parr = {}
    for r in results:
        cat = r["weakness"]["categoria"]
        pid = r["paragraph_idx"]
        if seen_cat.get(cat, 0) >= 2:
            continue
        if seen_parr.get(pid, 0) >= 1:
            continue
        diversified.append(r)
        seen_cat[cat] = seen_cat.get(cat, 0) + 1
        seen_parr[pid] = seen_parr.get(pid, 0) + 1
        if len(diversified) >= MAX_QUESTIONS:
            break

    return diversified, {
        "total_paragraphs": len(paragraphs),
        "rejections": rejections,
        "selected": len(diversified),
        "tokens": {"prompt": tracker.prompt,
                   "completion": tracker.completion,
                   "total": tracker.total},
    }


# ============================================================
# LaTeX letter — centered logo + watermark + English
# ============================================================
def generate_latex_letter(stats, results, manuscript_name="manuscript.tex",
                          logo_available=LOGO_DISPONIBLE):
    date = datetime.date.today().strftime("%B %d, %Y")

    preamble_extra = ""
    if logo_available:
        preamble_extra = (
            "\\usepackage{graphicx}\n"
            "\\usepackage{fancyhdr}\n"
            "\\usepackage{draftwatermark}\n"
            "\\SetWatermarkText{RANDPUNKT}\n"
            "\\SetWatermarkScale{1.5}\n"
            "\\SetWatermarkColor[gray]{0.9}\n"
            "\\pagestyle{fancy}\n"
            "\\fancyhf{}\n"
            "\\fancyhead[C]{\\vspace{0.5cm}\\includegraphics[width=2.5cm]{randpunkt-logo.pdf}}\n"
            "\\fancyfoot[C]{\\thepage}\n"
            "\\renewcommand{\\headrulewidth}{0pt}\n"
            "\\renewcommand{\\footrulewidth}{0.4pt}\n"
            "\\fancypagestyle{plain}{\n"
            "  \\fancyhf{}\n"
            "  \\fancyhead[C]{\\vspace{0.5cm}\\includegraphics[width=2.5cm]{randpunkt-logo.pdf}}\n"
            "  \\fancyfoot[C]{\\thepage}\n"
            "  \\renewcommand{\\headrulewidth}{0pt}\n"
            "  \\renewcommand{\\footrulewidth}{0.4pt}\n"
            "}\n"
        )

    L = []
    L.append(r"""\documentclass[11pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{textcomp}
\usepackage[english]{babel}
\usepackage[margin=2.5cm]{geometry}
\usepackage{enumitem}
\usepackage{xcolor}
\usepackage{hyperref}
\hypersetup{colorlinks=true, linkcolor=blue, urlcolor=blue}
""" + preamble_extra + r"""
\title{\textbf{Reformulation Letter}\\[4pt]
\large Pedagogical report for the author}
\author{Reformulation Analyzer}
\date{""" + date + r"""}

\begin{document}
\maketitle
\thispagestyle{plain}

\noindent
\textbf{Manuscript analyzed:} \texttt{""" + _escape_latex(manuscript_name) + r"""} \\
\textbf{Date:} """ + date + r"""

\vspace{8pt}
\noindent\rule{\textwidth}{0.4pt}

\section*{1. Summary}

This report identifies up to """ + str(MAX_QUESTIONS) + r""" reformulation
weaknesses in the manuscript. These are problems that are solved by
\emph{rewriting} the text, without the need for new experiments, data,
measurements, or citations.

\vspace{6pt}
\noindent
\textbf{Total paragraphs:} """ + str(stats.get('total_paragraphs', 0)) + r""" \quad
\textbf{Selected weaknesses:} """ + str(stats.get('selected', 0)) + r"""

\vspace{6pt}
\noindent
The categories considered are:
\begin{itemize}[leftmargin=*]
""")
    for cat, desc in CATEGORIES.items():
        L.append(r"\item \texttt{" + _escape_latex(cat) + r"}: "
                 + _escape_latex(desc) + "\n")
    L.append(r"\end{itemize}" + "\n")

    L.append(r"\section*{2. Reformulation questions}" + "\n")

    if not results:
        L.append(r"""\noindent
\textit{No reformulation weaknesses were detected.} The manuscript does
not present, in the analyzed paragraphs, problems that can be solved
solely by rewriting.

""")
    else:
        for i, r_ in enumerate(results, 1):
            d = r_["weakness"]
            fragment_plain = _fragment_to_plain_text(
                d["fragmento"], max_chars=MAX_FRAGMENT_LETTER)
            section_safe = _escape_plain_text(
                r_.get('section', '(no section)'))

            L.append(r"\noindent\rule{\textwidth}{0.2pt}" + "\n")
            L.append(r"\noindent\textbf{Question " + str(i) + r"} "
                     r"\textnormal{(Section: "
                     + section_safe
                     + r", paragraph " + str(r_['paragraph_idx'] + 1)
                     + r", line $\sim$" + str(r_.get('line', '?'))
                     + r")} \quad \texttt{"
                     + _escape_latex(d['categoria']) + r"}" + "\n\n")

            if fragment_plain:
                L.append(r"\noindent\textbf{Fragment:} "
                         r"\textit{``"
                         + _escape_plain_text(fragment_plain)
                         + r"''}" + "\n\n")

            # [FIX] escapado de TODOS los campos del LLM
            L.append(r"\noindent\textbf{Diagnosis:} "
                     + _escape_latex(d['diagnostico']) + "\n\n")
            L.append(r"\noindent\textbf{Impact:} "
                     + _escape_latex(d['impacto']) + "\n\n")

            L.append(r"\noindent\textbf{Implementation steps:}" + "\n")
            L.append(r"\begin{enumerate}[leftmargin=*]" + "\n")
            for p in d["pasos"]:
                L.append(r"\item " + _escape_latex(p) + "\n")
            L.append(r"\end{enumerate}" + "\n\n")

            L.append(r"\noindent\textbf{Rewrite A (conservative):} "
                     + _escape_latex(d['reescritura_A']) + "\n\n")
            L.append(r"\noindent\textbf{Rewrite B (alternative):} "
                     + _escape_latex(d['reescritura_B']) + "\n\n")
            L.append(r"\noindent\textbf{Verification criterion:} "
                     + _escape_latex(d['criterio_verificacion']) + "\n\n")

        L.append(r"\noindent\rule{\textwidth}{0.2pt}" + "\n\n")

    L.append(r"""
\section*{3. How to use this letter}

\begin{enumerate}[leftmargin=*]
    \item For each question, locate the fragment in your manuscript
          (it appears in plain text between quotes, without LaTeX code).
    \item Choose rewrite A or B, or combine them.
    \item Apply the implementation steps if the rewrite is not enough.
    \item Verify the criterion before considering the question resolved.
    \item If the rewrite does not fit your argument, discard the
          suggestion: the final decision is yours.
\end{enumerate}

\section*{4. Methodological notice}

This report identifies \textbf{reformulation} weaknesses (clarity, scope,
structure, redundancy, vagueness). It does not detect plagiarism, does not
compute AI writing, does not search for citations, does not validate
bibliography, and does not propose new experiments. The final decision is
the author's responsibility.

\vspace{12pt}
\noindent\rule{\textwidth}{0.4pt}

\vspace{6pt}
\noindent\footnotesize
\copyright\ 2026 RANDPUNKT JOURNAL. All rights reserved, except for articles
published under the Creative Commons Attribution 4.0 International (CC BY 4.0)
license.

\end{document}
""")
    return "\n".join(L)


# ============================================================
# LaTeX compilation (multi-engine) and auto-correction
# ============================================================
def _latex_engine_available(engine="pdflatex"):
    return shutil.which(engine) is not None


def _latex_available():
    return any(_latex_engine_available(e) for e in
               ("pdflatex", "lualatex", "xelatex"))


def compile_latex(tex_str, timeout=60, engine="pdflatex", logo_path=None):
    if not _latex_engine_available(engine):
        return False, f"{engine} is not installed on the system.", None

    with tempfile.TemporaryDirectory() as td:
        tex_path = os.path.join(td, "doc.tex")
        with open(tex_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(tex_str)

        # [FIX] copia el logo si existe en el sistema, independientemente
        # de si el string lo referencia (evita fallos de includegraphics)
        if "randpunkt-logo.pdf" in tex_str:
            src = logo_path or LOGO_PATH
            if os.path.isfile(src) and os.path.getsize(src) > 0:
                try:
                    shutil.copy2(src, os.path.join(td, "randpunkt-logo.pdf"))
                except Exception as e:
                    return (False,
                            f"Could not copy the logo to the compilation "
                            f"directory: {e}", None)
            else:
                return (False,
                        "The letter references randpunkt-logo.pdf but the "
                        "file is missing.", None)

        log_total = []
        r = None
        for _ in range(2):  # two passes for \ref
            try:
                r = subprocess.run(
                    [engine, "-interaction=nonstopmode",
                     "-halt-on-error", "-file-line-error", "doc.tex"],
                    cwd=td, capture_output=True, text=False, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return False, f"{engine} exceeded the time limit.", None
            except FileNotFoundError:
                return False, f"{engine} was not found in the PATH.", None

            raw = r.stdout or b""
            for enc in ("utf-8", "latin-1"):
                try:
                    log_total.append(raw.decode(enc))
                    break
                except UnicodeDecodeError:
                    continue
            else:
                log_total.append(raw.decode("utf-8", errors="replace"))

            if r.returncode != 0:
                break

        pdf_path = os.path.join(td, "doc.pdf")
        pdf_bytes = None
        if os.path.exists(pdf_path):
            size = os.path.getsize(pdf_path)
            if size > 0:
                with open(pdf_path, "rb") as f:
                    pdf_bytes = f.read()
            else:
                log_total.append(
                    f"[warn] {engine} generated an empty PDF ({size} bytes).")

        ok = (r is not None and r.returncode == 0
              and pdf_bytes is not None and len(pdf_bytes) > 0)
        return ok, "\n".join(log_total), pdf_bytes


# ------------------------------------------------------------
# Auto-correction rules
# ------------------------------------------------------------
_AUTOFIX_RULES = [
    (re.compile(r'\\textbackslash\{\}((?:[a-zA-Z]+))'), r'\\\1'),
    (re.compile(r'\\textbackslash\{\}\{'), r'{'),
    (re.compile(r'\\textbackslash\{\}\}'), r'}'),
    (re.compile(r'\\textasciitilde\{\}\s*(\\[a-zA-Z]+)'), r'~\1'),
    (re.compile(r'(?:\\textasciitilde\{\})+'), r'~'),
    (re.compile(r'\\textbackslash\{\}(ldots|dots|LaTeX|TeX)\b'), r'\\\1'),
    (re.compile(r'(?<!\\)\\n\\n'), r'\n\n'),
    (re.compile(r"\\textbackslash\{\}'"), r"'"),
    (re.compile(r'\\textbackslash\{\}(?=\s|$|[.,;:])'), r'\\'),
    (re.compile(r'\\emph(?![{\s])\s*([^{}\n]+)'), r'\\emph{\1}'),
    (re.compile(r'\\emph\s*\\ldots'), r'\\ldots'),
    (re.compile(r'\$\s*\\textbackslash\{\}\s*\$'), r'$ $'),
]


def _fix_underscores_outside_math(tex):
    # [FIX] ignora \$ (dólar literal) al contar delimitadores
    out = []
    for line in tex.splitlines(keepends=True):
        dollars = len(re.findall(r'(?<!\\)\$', line))
        if dollars % 2 == 0 and "_" in line:
            line = re.sub(r'(?<!\\)_', r'\_', line)
        out.append(line)
    return "".join(out)


def _balance_braces(tex):
    opened = closed = 0
    for line in tex.splitlines():
        line = re.sub(r'(?<!\\)%.*$', '', line)
        for m in re.finditer(r'(?<!\\)([{}])', line):
            if m.group(1) == '{':
                opened += 1
            else:
                closed += 1
    return opened, closed


def _sanitize_latex(tex):
    if not tex:
        return tex
    lines = tex.splitlines()
    idx = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith(r"\documentclass"):
            idx = i
            break
    if idx is None:
        return tex
    return "\n".join(lines[idx:])


def _is_valid_latex(tex):
    if not tex or not isinstance(tex, str):
        return False
    if r"\documentclass" not in tex:
        return False
    if r"\begin{document}" not in tex or r"\end{document}" not in tex:
        return False
    first = tex.lstrip().splitlines()[0] if tex.strip() else ""
    if first.startswith("#!") or first.startswith("import ") \
            or first.startswith("from "):
        return False
    return True


def auto_correct_latex(tex_str, max_iter=4, verbose=False,
                       engines=("pdflatex", "lualatex", "xelatex"),
                       logo_path=None):
    corrections = []
    tex = tex_str
    log = ""
    pdf = None
    ok = False

    available_engines = [e for e in engines if _latex_engine_available(e)]
    if not available_engines:
        return (tex_str, False,
                "No LaTeX engine is installed (pdflatex/lualatex/xelatex).",
                None, [])

    for it in range(max_iter):
        tex_prev = tex
        for pattern, repl in _AUTOFIX_RULES:
            new, n = pattern.subn(repl, tex)
            if n:
                corrections.append(
                    f"iter {it+1}: rule {pattern.pattern!r} -> {n} replacement(s)")
                tex = new

        tex_fixed = _fix_underscores_outside_math(tex)
        if tex_fixed != tex:
            corrections.append(f"iter {it+1}: '_' escaped outside math")
            tex = tex_fixed

        ab, ce = _balance_braces(tex)
        if ab != ce:
            corrections.append(
                f"iter {it+1}: unbalanced braces "
                f"({ab} opened, {ce} closed)")

        for engine in available_engines:
            try:
                ok, log, pdf = compile_latex(
                    tex, engine=engine, logo_path=logo_path)
            except Exception as e:
                corrections.append(
                    f"iter {it+1} [{engine}]: exception during compilation: {e}")
                ok, log, pdf = False, str(e), None

            if ok:
                corrections.append(f"iter {it+1}: compiled with {engine}")
                break

        if ok:
            break
        if tex == tex_prev:
            break

    if verbose:
        for c in corrections:
            print("[autofix]", c)

    return tex, ok, log, pdf, corrections


# ============================================================
# Letter-centered pipeline
# ============================================================
def run_pipeline(body_text, api_key, base_url, model,
                 progress_cb=None, manuscript_name="manuscript.tex",
                 cancel_event=None, logger=None):
    client = OpenAI(api_key=api_key, base_url=base_url)

    def _cb(i, n):
        if progress_cb:
            progress_cb(i, n)

    try:
        results, stats = analyze_reformulation(
            client, body_text, model, progress_cb=_cb,
            logger=logger, cancel_event=cancel_event)
    except Exception as e:
        return {
            "letter_tex": None, "letter_ok": False, "letter_log": "",
            "letter_pdf": None, "corrections": [],
            "stats": {}, "results": [], "logo_used": False,
            "error": f"LLM analysis: {e}",
        }

    try:
        letter_tex = generate_latex_letter(
            stats, results, manuscript_name,
            logo_available=LOGO_DISPONIBLE)
    except Exception as e:
        return {
            "letter_tex": None, "letter_ok": False, "letter_log": "",
            "letter_pdf": None, "corrections": [],
            "stats": stats, "results": results,
            "logo_used": False,
            "error": f"Letter generation: {e}",
        }

    logo_used = LOGO_DISPONIBLE and "randpunkt-logo.pdf" in letter_tex
    letter_tex = _sanitize_latex(letter_tex)

    if not _is_valid_latex(letter_tex):
        return {
            "letter_tex": letter_tex, "letter_ok": False,
            "letter_log": "The generated letter is not a valid LaTeX "
                          "document (missing \\documentclass or "
                          "\\begin{document}).",
            "letter_pdf": None, "corrections": [],
            "stats": stats, "results": results,
            "logo_used": logo_used,
            "error": None,
        }

    try:
        final_letter, letter_ok, letter_log, letter_pdf, corrections = (
            auto_correct_latex(letter_tex, logo_path=LOGO_PATH))
    except Exception as e:
        return {
            "letter_tex": letter_tex, "letter_ok": False,
            "letter_log": str(e),
            "letter_pdf": None, "corrections": [],
            "stats": stats, "results": results,
            "logo_used": logo_used,
            "error": f"Compilation: {e}",
        }

    return {
        "letter_tex": final_letter,
        "letter_ok": letter_ok,
        "letter_log": letter_log,
        "letter_pdf": letter_pdf,
        "corrections": corrections,
        "stats": stats,
        "results": results,
        "logo_used": logo_used,
        "error": None,
    }


# ============================================================
# Streamlit interface (English)
# ============================================================
def _read_secret(name, default=""):
    try:
        return st.secrets[name]
    except Exception:
        return os.environ.get(name, default)


def _sidebar_has_remote_config():
    try:
        _ = st.secrets["remote_server"]["host"]
        _ = st.secrets["remote_server"]["user"]
        _ = st.secrets["remote_server"].get("ssh_key", "")
        return True
    except Exception:
        return False


def main():
    st.set_page_config(
        page_title="Reformulation Letter",
        page_icon="✍️",
        layout="wide",
    )

    # [FIX] estado de cancelación
    if "cancel_event" not in st.session_state:
        import threading
        st.session_state.cancel_event = threading.Event()
    if "run_result" not in st.session_state:
        st.session_state.run_result = None
    if "run_meta" not in st.session_state:
        st.session_state.run_meta = None

    st.title("✍️ Reformulation Letter")
    st.caption(
        "Upload your manuscript (.tex or .docx). The program detects up to 4 "
        "reformulation weaknesses and generates a **letter** for the author "
        "with diagnosis, steps, A/B rewrites, and a verification criterion. "
        "The letter is compiled with LaTeX (pdflatex/lualatex/xelatex) and "
        "auto-corrected if there are typesetting errors. The manuscript "
        "fragment is quoted in **plain text**, so no broken LaTeX is dragged in.")

    with st.sidebar:
        st.header("⚙️ Configuration")

        # [FIX] st.form evita reejecuciones en cada cambio
        with st.form("config_form"):
            default_key = FALLBACK_DEEPSEEK_KEY
            api_key = st.text_input(
                "DeepSeek API key",
                value=default_key,
                type="password",
                key="api_key_widget",
            )
            base_url = st.text_input("Base URL", value=DEFAULT_BASE_URL)
            model = st.text_input("Model", value=DEFAULT_MODEL)
            st.form_submit_button("Apply")

        st.caption(f"Max questions: {MAX_QUESTIONS}")

        engines_ok = [e for e in ("pdflatex", "lualatex", "xelatex")
                      if _latex_engine_available(e)]
        if engines_ok:
            st.caption(f"✅ LaTeX engines: {', '.join(engines_ok)}")
        else:
            st.caption("⚠️ No LaTeX engine in PATH: no PDF will be produced.")

        if LOGO_DISPONIBLE:
            st.caption(f"✅ Logo detected: `{os.path.basename(LOGO_PATH)}`")
        else:
            st.caption("ℹ️ Logo not found (`randpunkt-logo.pdf`). "
                       "The letter will be generated without a logo.")

        # [FIX] aviso temprano si falta configuración SFTP
        if not _sidebar_has_remote_config():
            st.caption("ℹ️ `[remote_server]` not configured: remote backup "
                       "disabled.")

    uploaded = st.file_uploader(
        "Upload your manuscript (.tex or .docx)",
        type=["tex", "docx"])

    if not uploaded:
        st.info("Upload a .tex or .docx file to begin.")
        return

    # [FIX] límite de tamaño
    if uploaded.size > MAX_UPLOAD_MB * 1024 * 1024:
        st.error(f"File too large ({uploaded.size/1024/1024:.1f} MB). "
                 f"Maximum is {MAX_UPLOAD_MB} MB.")
        return

    try:
        body_text, name, input_type = load_manuscript(uploaded)
    except Exception as e:
        st.error(f"Error reading the file: {e}")
        return

    st.success(f"File loaded: **{name}** "
               f"({input_type}, {len(body_text)} characters).")

    # [FIX] vista previa del manuscrito
    with st.expander("Preview detected paragraphs"):
        preview_paras = split_paragraphs(body_text)
        st.caption(f"{len(preview_paras)} paragraphs meet the minimum "
                   f"length ({MIN_PARAGRAPH_LENGTH} chars).")
        if not preview_paras:
            st.warning("No paragraphs detected — the letter will be empty. "
                       "Check that the .docx is not table-only, or that the "
                       ".tex uses blank lines between paragraphs.")
        for i, (_, _, t) in enumerate(preview_paras[:8], start=1):
            st.markdown(f"**{i}.** {t[:240]}{'…' if len(t) > 240 else ''}")

    if not api_key:
        st.warning("Missing DeepSeek API key (sidebar).")
        return

    col_btn, col_cancel = st.columns([1, 1])
    if col_btn.button("▶️ Generate letter", type="primary"):
        st.session_state.cancel_event = __import__("threading").Event()
        st.session_state.run_result = None

        progress_bar = st.progress(0.0)
        status = st.empty()

        def _cb(i, n):
            pct = (i + 1) / max(n, 1)
            progress_bar.progress(min(pct, 1.0))
            status.info(f"Analyzing paragraph block {i + 1}/{n}…")

        logger, run_id, log_path = _setup_run_logger()

        with st.spinner("Running pipeline…"):
            result = run_pipeline(
                body_text=body_text,
                api_key=api_key,
                base_url=base_url,
                model=model,
                progress_cb=_cb,
                manuscript_name=name,
                cancel_event=st.session_state.cancel_event,
                logger=logger,
            )

        progress_bar.progress(1.0)
        st.session_state.run_result = result
        st.session_state.run_meta = {
            "name": name, "input_type": input_type,
            "run_id": run_id, "log_path": log_path,
            "ts": datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
            "body_text": body_text,
        }

    if col_cancel.button("⏹ Cancel"):
        st.session_state.cancel_event.set()
        st.warning("Cancellation requested. The current LLM call will finish "
                   "first; then the pipeline stops.")

    result = st.session_state.run_result
    meta = st.session_state.run_meta
    if not result or not meta:
        return

    name = meta["name"]
    input_type = meta["input_type"]
    ts = meta["ts"]

    if result["error"]:
        st.error(f"Error: {result['error']}")
        return
    st.success("Letter generated.")

    # --------------------------------------------------------
    # Summary and diagnostics
    # --------------------------------------------------------
    st.subheader("📊 Summary")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Paragraphs analyzed",
              result["stats"].get("total_paragraphs", 0))
    c2.metric("Weaknesses selected",
              result["stats"].get("selected", 0))
    c3.metric("PDF", "Yes" if result["letter_pdf"] else "No")
    c4.metric("Logo", "Included" if result["logo_used"] else "No")
    tok = result["stats"].get("tokens", {}) or {}
    c5.metric("Tokens", f"{tok.get('total', 0):,}")

    if not result["results"]:
        st.info("No reformulation weaknesses were detected. The letter is "
                "still generated, indicating that there are no suggestions.")

    # [FIX] mostrar rechazos (diagnóstico)
    rej = result["stats"].get("rejections", {}) or {}
    if rej:
        with st.expander("Rejection stats"):
            st.json(rej)

    if result["corrections"]:
        with st.expander("Corrections applied to the letter"):
            for c in result["corrections"]:
                st.write("•", c)

    if not result["letter_pdf"]:
        if not _latex_available():
            st.warning(
                "No LaTeX engine was detected in the PATH "
                "(`pdflatex`, `lualatex`, or `xelatex`). Install TeX Live / "
                "MiKTeX / MacTeX to enable PDF generation.")
        if result["letter_log"]:
            with st.expander("LaTeX log"):
                st.code(result["letter_log"][-4000:], language="text")

    # --------------------------------------------------------
    # Local Save (debug) with rotation # [FIX]
    # --------------------------------------------------------
    if result["letter_pdf"]:
        try:
            pdf_path = os.path.join(DEBUG_DIR, f"letter_{ts}.pdf")
            with open(pdf_path, "wb") as f:
                f.write(result["letter_pdf"])
            st.caption(f"Letter saved locally to `{DEBUG_DIR}`.")
            _rotate_debug_dir()
        except Exception as e:
            st.warning(f"Could not save to debug/: {e}")

    # --------------------------------------------------------
    # Downloads (PDF + .tex + .json) # [FIX]
    # --------------------------------------------------------
    st.subheader("⬇️ Download")
    base_filename = _sanitize_filename(os.path.splitext(name)[0])

    dl1, dl2, dl3 = st.columns(3)
    with dl1:
        if result["letter_pdf"]:
            st.download_button(
                "📕 Letter .pdf",
                data=result["letter_pdf"],
                file_name=f"{base_filename}_letter_{ts}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        else:
            st.caption("PDF not available.")

    with dl2:
        if result["letter_tex"]:
            st.download_button(
                "📄 Letter .tex",
                data=result["letter_tex"].encode("utf-8"),
                file_name=f"{base_filename}_letter_{ts}.tex",
                mime="application/x-tex",
                use_container_width=True,
            )

    with dl3:
        try:
            payload = json.dumps(
                {"stats": result["stats"], "results": result["results"]},
                ensure_ascii=False, indent=2, default=str).encode("utf-8")
            st.download_button(
                "🧾 Results .json",
                data=payload,
                file_name=f"{base_filename}_results_{ts}.json",
                mime="application/json",
                use_container_width=True,
            )
        except Exception:
            pass

    # --------------------------------------------------------
    # Remote upload (explicit action, AFTER showing result) # [FIX]
    # --------------------------------------------------------
    if _sidebar_has_remote_config():
        st.subheader("☁️ Remote backup")
        st.caption("Uploads the original manuscript and (if available) the "
                   "letter PDF to the configured SFTP server.")
        if st.button("Upload to remote server"):
            tmp_dir = tempfile.mkdtemp(prefix="rp_")
            try:
                orig_local = os.path.join(
                    tmp_dir,
                    f"{base_filename}_orig.{input_type}")
                if input_type == "tex":
                    with open(orig_local, "w", encoding="utf-8") as f:
                        f.write(meta.get("body_text", ""))
                else:
                    with open(orig_local, "wb") as f:
                        f.write(uploaded.getbuffer())

                with st.spinner("Uploading manuscript…"):
                    ok_m, msg_m = upload_to_remote_server(
                        orig_local, f"{base_filename}_orig.{input_type}")

                if ok_m:
                    st.caption(f"✅ Manuscript uploaded: `{msg_m}`")
                else:
                    st.warning(f"⚠️ Manuscript upload failed: {msg_m}")

                if result["letter_pdf"]:
                    pdf_local = os.path.join(
                        tmp_dir, f"{base_filename}_letter_{ts}.pdf")
                    with open(pdf_local, "wb") as f:
                        f.write(result["letter_pdf"])
                    with st.spinner("Uploading letter…"):
                        ok_l, msg_l = upload_to_remote_server(
                            pdf_local, f"{base_filename}_letter_{ts}.pdf")
                    if ok_l:
                        st.caption(f"✅ Letter uploaded: `{msg_l}`")
                    else:
                        st.warning(f"⚠️ Letter upload failed: {msg_l}")
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================
# Entry point
# ============================================================
if __name__ == "__main__":
    main()
