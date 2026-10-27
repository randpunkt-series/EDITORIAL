"""Crea un ARTÍCULO en 'en_revision' con 2 coautores, asignado a 'luis'."""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from seeds._common import (
    sftp_connect, revista_dir, editorial_dir, books_dir,
    ensure_dir, generate_token, dummy_pdf, unique_id,
    append_csv,
)

COLS_ARTICULOS = [
    "articulo_id", "tipo", "titulo", "ruta_pdf", "ruta_cover",
    "fecha_creacion", "estado", "isbn", "motivo_retiro", "fecha_retiro",
    "idioma_original", "formato_original", "editor_asignado",
    "arbitraje_confirmado",
]
COLS_CONFIRMACIONES = [
    "articulo_id", "nombre", "email", "token",
    "aprobado", "fecha_aprobacion", "comentarios",
]
COLS_VERSIONES = [
    "articulo_id", "fecha", "ruta_pdf", "nombre_archivo", "notas",
]

COAUTORES = [
    {"nombre": "Carlos Polanco", "email": "polanco@unam.mx"},
    {"nombre": "Rocío Leonel Gómez", "email": "cpg7000cpg@gmail.com"},
]


def main():
    transport, sftp, rs = sftp_connect()
    print("✅ Conectado al servidor remoto.")

    art_id = unique_id("RP")
    print(f"\n📄 Creando ARTÍCULO: {art_id}")

    rev = revista_dir(rs)
    ed = editorial_dir(rs)

    for d in [f"{rev}/METADATA", f"{rev}/PDF", f"{rev}/ZIP", f"{ed}/versiones"]:
        ensure_dir(sftp, d)

    meta = {
        "tracking_id": art_id,
        "status": "submitted",
        "titulo": "Artículo de prueba con 2 coautores",
        "article_type": "Research Article",
        "original_language": "English",
        "original_format": "LaTeX",
        "abstract": "Abstract de prueba para el flujo automatizado.",
        "keywords": "test, automation, workflow",
        "msc": {"primary": {"area": "00", "code": "00A06"}, "secondary": []},
        "corresponding_author": {
            "name": COAUTORES[0]["nombre"],
            "email": COAUTORES[0]["email"],
            "affiliation": "UNAM",
            "orcid": "0000-0000-0000-0000",
        },
        "coauthors": [COAUTORES[1]["nombre"]],
        "versions": [{"version": 1, "pdf": f"{art_id}_v1.pdf",
                      "submitted_at": "2026-01-01T00:00:00"}],
        "submitted_at": "2026-01-01T00:00:00",
        "zenodo": None,
    }
    with sftp.open(f"{rev}/METADATA/{art_id}.json", "w") as f:
        f.write(json.dumps(meta, indent=2).encode("utf-8"))
    print(f"  ✅ JSON: METADATA/{art_id}.json")

    pdf_name = f"{art_id}_v1.pdf"
    with sftp.open(f"{rev}/PDF/{pdf_name}", "wb") as f:
        f.write(dummy_pdf())
    with sftp.open(f"{ed}/versiones/{pdf_name}", "wb") as f:
        f.write(dummy_pdf())
    print(f"  ✅ PDF: PDF/{pdf_name} + versiones/{pdf_name}")

    append_csv(sftp, f"{ed}/registro_articulos.csv", COLS_ARTICULOS, {
        "articulo_id": art_id,
        "tipo": "articulo",
        "titulo": meta["titulo"],
        "ruta_pdf": f"versiones/{pdf_name}",
        "ruta_cover": "",
        "fecha_creacion": "2026-01-01T00:00:00",
        "estado": "en_revision",
        "isbn": "",
        "motivo_retiro": "",
        "fecha_retiro": "",
        "idioma_original": "English",
        "formato_original": "LaTeX",
        "editor_asignado": "luis",
        "arbitraje_confirmado": "0",
    })
    print(f"  ✅ CSV: registro_articulos.csv (tipo=articulo, estado=en_revision)")

    append_csv(sftp, f"{ed}/registro_versiones.csv", COLS_VERSIONES, {
        "articulo_id": art_id,
        "fecha": "2026-01-01T00:00:00",
        "ruta_pdf": f"versiones/{pdf_name}",
        "nombre_archivo": pdf_name,
        "notas": "Seed test article",
    })
    print(f"  ✅ CSV: registro_versiones.csv")

    tokens = {}
    for c in COAUTORES:
        tok = generate_token(c["email"], art_id, c["nombre"])
        tokens[c["email"]] = tok
        append_csv(sftp, f"{ed}/registro_confirmaciones.csv", COLS_CONFIRMACIONES, {
            "articulo_id": art_id,
            "nombre": c["nombre"],
            "email": c["email"],
            "token": tok,
            "aprobado": "0",
            "fecha_aprobacion": "",
            "comentarios": "",
        })
    print(f"  ✅ CSV: registro_confirmaciones.csv (2 coautores pendientes)")

    sftp.close(); transport.close()

    print(f"\n🎉 Artículo creado: {art_id}")
    print(f"   Asignado a: luis (Associate)")
    print(f"   Estado: en_revision, arbitraje=0")
    print(f"\n📋 Tokens de coautores:")
    for e, t in tokens.items():
        print(f"   {e}: {t}")


if __name__ == "__main__":
    main()
