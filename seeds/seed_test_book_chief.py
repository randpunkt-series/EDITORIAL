"""Crea un BOOK en 'en_revision' con 2 coautores, subido por 'polanco' (Chief)."""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from seeds._common import (
    sftp_connect, revista_dir, editorial_dir, books_dir,
    ensure_dir, generate_token, dummy_pdf, dummy_cover, unique_id,
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
    print("✅ Conectado.")

    book_id = unique_id("BK-C")
    print(f"\n📚 Creando BOOK (subido por Chief): {book_id}")

    rev = revista_dir(rs)
    ed = editorial_dir(rs)
    bk = books_dir(rs)

    for d in [f"{bk}/metadata", f"{bk}/pdf", f"{bk}/covers", f"{ed}/versiones"]:
        ensure_dir(sftp, d)

    meta = {
        "id": book_id,
        "status": "en_revision",
        "title": "Libro subido por el Chief",
        "authors": "Carlos Polanco, Rocío Leonel Gómez",
        "year": 2026,
        "description": "Book creado por el Chief para probar el force-advance.",
        "original_language": "English",
        "original_format": "LaTeX",
        "isbn": "978-1111111111",
        "doi": "",
        "pdf": f"{book_id}.pdf",
        "cover": f"{book_id}.jpg",
        "created_at": "2026-01-01T00:00:00",
        "zenodo": None,
    }
    with sftp.open(f"{bk}/metadata/{book_id}.json", "w") as f:
        f.write(json.dumps(meta, indent=2).encode("utf-8"))
    print(f"  ✅ JSON: BOOKS/metadata/{book_id}.json")

    with sftp.open(f"{bk}/pdf/{book_id}.pdf", "wb") as f:
        f.write(dummy_pdf())
    v_pdf = f"{book_id}_v1.pdf"
    with sftp.open(f"{ed}/versiones/{v_pdf}", "wb") as f:
        f.write(dummy_pdf())
    with sftp.open(f"{bk}/covers/{book_id}.jpg", "wb") as f:
        f.write(dummy_cover())
    print(f"  ✅ PDF + cover")

    append_csv(sftp, f"{ed}/registro_articulos.csv", COLS_ARTICULOS, {
        "articulo_id": book_id,
        "tipo": "libro",
        "titulo": meta["title"],
        "ruta_pdf": f"versiones/{v_pdf}",
        "ruta_cover": f"{book_id}.jpg",
        "fecha_creacion": "2026-01-01T00:00:00",
        "estado": "en_revision",
        "isbn": meta["isbn"],
        "motivo_retiro": "",
        "fecha_retiro": "",
        "idioma_original": "English",
        "formato_original": "LaTeX",
        "editor_asignado": "polanco",
        "arbitraje_confirmado": "0",
    })
    print(f"  ✅ CSV: editor_asignado=polanco")

    append_csv(sftp, f"{ed}/registro_versiones.csv", COLS_VERSIONES, {
        "articulo_id": book_id,
        "fecha": "2026-01-01T00:00:00",
        "ruta_pdf": f"versiones/{v_pdf}",
        "nombre_archivo": v_pdf,
        "notas": "Seed test book (Chief)",
    })

    tokens = {}
    for c in COAUTORES:
        tok = generate_token(c["email"], book_id, c["nombre"])
        tokens[c["email"]] = tok
        append_csv(sftp, f"{ed}/registro_confirmaciones.csv", COLS_CONFIRMACIONES, {
            "articulo_id": book_id,
            "nombre": c["nombre"],
            "email": c["email"],
            "token": tok,
            "aprobado": "0",
            "fecha_aprobacion": "",
            "comentarios": "",
        })
    print(f"  ✅ CSV: 2 coautores pendientes")

    sftp.close(); transport.close()

    print(f"\n🎉 Book creado: {book_id}")
    print(f"   Asignado a: polanco (Chief)")
    print(f"   Estado: en_revision, arbitraje=0")
    print(f"\n📋 Tokens:")
    for e, t in tokens.items():
        print(f"   {e}: {t}")


if __name__ == "__main__":
    main()
