"""
Guarda tokens de aprobación en /tmp/tokens_test_<run_id>.json para que
el operador humano los pueda abrir desde su celular.
"""
import json
import os
from datetime import datetime
from pathlib import Path

TOKENS_DIR = Path("/tmp")
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")


def _path_for(run_id: str = None) -> Path:
    return TOKENS_DIR / f"tokens_test_{run_id or RUN_ID}.json"


def register_token(art_id: str, email: str, name: str, token: str,
                    base_url: str = "http://localhost:8502",
                    run_id: str = None) -> str:
    """Añade (o actualiza) un token en el archivo de la corrida actual."""
    path = _path_for(run_id)
    data = {"run_id": run_id or RUN_ID, "tokens": []}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            pass

    url = f"{base_url}/?token={token}"
    entry = {
        "art_id": art_id,
        "email": email,
        "name": name,
        "token": token,
        "url": url,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    # dedup
    data["tokens"] = [t for t in data["tokens"] if t.get("token") != token]
    data["tokens"].append(entry)
    path.write_text(json.dumps(data, indent=2))

    # imprimir bonito para que el humano lo copie
    print("\n" + "═" * 70)
    print(f"🔗 TOKEN GUARDADO — {name} <{email}>")
    print(f"   Producto: {art_id}")
    print(f"   URL:      {url}")
    print(f"   Archivo:  {path}")
    print("═" * 70 + "\n")
    return url


def load_all(run_id: str = None) -> list:
    path = _path_for(run_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("tokens", [])
    except Exception:
        return []


def find_token(art_id: str, email: str = None, run_id: str = None) -> dict:
    """Devuelve el token más reciente para un art_id/email."""
    candidates = [t for t in load_all(run_id)
                  if t.get("art_id") == art_id]
    if email:
        candidates = [t for t in candidates if t.get("email") == email]
    if not candidates:
        return None
    return candidates[-1]  # último (más reciente)
