import time
import requests
from pathlib import Path

from config import (
    ZENODO_TOKEN, ZENODO_API, ZENODO_COMMUNITY,
    ZENODO_SANDBOX_TOKEN, ZENODO_SANDBOX_API,
)


# ==================================================================
# CONFIGURACIÓN SEGÚN ENTORNO
# ==================================================================
def _get_config(use_sandbox: bool = False):
    if use_sandbox:
        return ZENODO_SANDBOX_API, ZENODO_SANDBOX_TOKEN
    return ZENODO_API, ZENODO_TOKEN


# ==================================================================
# REINTENTOS
# ==================================================================
def _request_with_retries(method: str, url: str, *, headers=None,
                          json=None, data=None, timeout=30, max_retries=3):
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = requests.request(
                method, url, headers=headers, json=json, data=data, timeout=timeout
            )
            if r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code}: {r.text}")
            if r.status_code >= 400:
                print(f"[ZENODO ERROR] {r.status_code}: {r.text}")
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last_exc = e
            wait = 2 ** attempt
            print(f"[ZENODO RETRY] Intento {attempt + 1}/{max_retries} falló: {e}. Esperando {wait}s...")
            time.sleep(wait)
    raise last_exc


# ==================================================================
# CREAR DEPÓSITO
# ==================================================================
def create_deposition(metadata: dict, use_sandbox: bool = False) -> dict:
    api, token = _get_config(use_sandbox)

    if not token:
        raise RuntimeError(
            f"Token de Zenodo no configurado para "
            f"{'sandbox' if use_sandbox else 'producción'}. "
            f"Revisa tu archivo .env."
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {"metadata": metadata}
    url = f"{api}/deposit/depositions"

    r = _request_with_retries("POST", url, headers=headers, json=payload, timeout=30)
    return r.json()


# ==================================================================
# SUBIR ARCHIVO
# ==================================================================
def upload_file(deposition: dict, file_path: Path, use_sandbox: bool = False) -> dict:
    api, token = _get_config(use_sandbox)
    bucket_url = deposition["links"]["bucket"]
    filename = file_path.name

    if not file_path.exists() or file_path.stat().st_size == 0:
        raise ValueError(f"El archivo {file_path} no existe o está vacío.")

    with open(file_path, "rb") as fp:
        r = _request_with_retries(
            "PUT",
            f"{bucket_url}/{filename}",
            data=fp,
            headers={"Authorization": f"Bearer {token}"},
            timeout=300,
        )
    return r.json()


# ==================================================================
# PUBLICAR DEPÓSITO
# ==================================================================
def publish_deposition(deposition_id: int, use_sandbox: bool = False) -> dict:
    api, token = _get_config(use_sandbox)

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{api}/deposit/depositions/{deposition_id}/actions/publish"

    r = _request_with_retries("POST", url, headers=headers, timeout=30)
    return r.json()


# ==================================================================
# CONSTRUIR METADATOS DE MANUSCRITO
# ==================================================================
def build_manuscript_zenodo_metadata(metadata: dict) -> dict:
    authors = []
    ca = metadata["corresponding_author"]
    authors.append({
        "name": ca["name"],
        "affiliation": ca.get("affiliation", ""),
        "orcid": ca.get("orcid", "").replace("https://orcid.org/", ""),
    })
    for coauthor in metadata.get("coauthors", []):
        authors.append({"name": coauthor})

    keywords = [k.strip() for k in metadata.get("keywords", "").split(",") if k.strip()]
    msc = metadata.get("msc", {})
    if msc.get("primary", {}).get("code"):
        keywords.append(f"MSC {msc['primary']['code']}")
    for sec in msc.get("secondary", []):
        if sec.get("code"):
            keywords.append(f"MSC {sec['code']}")

    if metadata.get("original_language"):
        keywords.append(f"Original language: {metadata['original_language']}")
    if metadata.get("original_format"):
        keywords.append(f"Original format: {metadata['original_format']}")

    keywords.extend(["mathematics", "pure mathematics", "applied mathematics", "mathematical research", "peer-reviewed"])

    type_map = {
        "Short Note": "article",
        "Scoped Expository Article": "article",
        "Original Research Article": "article",
        "Research Article": "article",
        "Book": "book",
        "Book Chapter": "section",
    }

    submitted = metadata.get("submitted_at", "")
    if not submitted:
        versions = metadata.get("versions", [])
        if versions:
            submitted = versions[0].get("submitted_at", "")
    pub_date = submitted[:10] if len(submitted) >= 10 else "2026-01-01"

    return {
        "title": metadata["titulo"],
        "upload_type": "publication",
        "publication_type": type_map.get(metadata.get("article_type"), "article"),
        "publication_date": pub_date,
        "description": metadata.get("abstract", ""),
        "creators": authors,
        "keywords": keywords,
        "access_right": "open",
        "license": "cc-by-4.0",
        "communities": [{"identifier": ZENODO_COMMUNITY}],
    }
