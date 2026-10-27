"""Crea un BOOK ya publicado (Zenodo simulado) para probar /books público."""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from seeds._common import (
    sftp_connect, revista_dir, editorial_dir, books_dir,
    ensure_dir, dummy_pdf, dummy_cover, unique_id, append_csv,
)

COLS_ARTICULOS = [
    "articulo_id", "tipo", "titulo", "ruta_pdf", "ruta_cover",
    "fecha_creacion", "estado", "isbn", "motivo_retiro", "fecha_retiro",
    "idioma_original", "formato_original", "editor_asignado",
    "arbitraje_confirmado",
]


def main():
    transport, sftp, rs = sftp_connect()
    print("✅ Conectado.")

    book_id = unique_id("BK-P")
    print(f"\n📚 Creando BOOK publicado: {book_id}")

    rev = revista_dir(rs)
    ed = editorial_dir(rs)
    bk = books_dir(rs)

    for d in [f"{bk}/metadata", f"{bk}/pdf", f"{bk}/covers"]:
        ensure_dir(sftp, d)

    meta = {
        "id": book_id,
        "status": "published",
        "title": "Libro ya publicado (seed)",
        "authors": "Carlos Polanco",
        "year": 2026,
        "description": "Libro ya publicado con Zenodo simulado.",
        "original_language": "English",
        "original_format": "LaTeX",
        "isbn": "978-9999999999",
        "doi": "10.5281/zenodo.test-pub",
        "pdf": f"{book_id}.pdf",
        "cover": f"{book_id}.jpg",
        "created_at": "2026-01-01T00:00:00",
        "zenodo": {
            "doi": "10.5281/zenodo.test-pub",
            "url": f"https://zenodo.org/records/test-{book_id.lower()}",
            "environment": "simulated",
            "published_at": "2026-01-01T00:00:00",
        },
    }
    with sftp.open(f"{bk}/metadata/{book_id}.json", "w") as f:
        f.write(json.dumps(meta, indent=2).encode("utf-8"))
    print(f"  ✅ JSON: BOOKS/metadata/{book_id}.json (status=published)")

    with sftp.open(f"{bk}/pdf/{book_id}.pdf", "wb") as f:
        f.write(dummy_pdf())
    with sftp.open(f"{bk}/covers/{book_id}.jpg", "wb") as f:
        f.write(dummy_cover())
    print(f"  ✅ PDF + cover")

    append_csv(sftp, f"{ed}/registro_articulos.csv", COLS_ARTICULOS, {
        "articulo_id": book_id,
        "tipo": "libro",
        "titulo": meta["title"],
        "ruta_pdf": "",
        "ruta_cover": f"{book_id}.jpg",
        "fecha_creacion": "2026-01-01T00:00:00",
        "estado": "published",
        "isbn": meta["isbn"],
        "motivo_retiro": "",
        "fecha_retiro": "",
        "idioma_original": "English",
        "formato_original": "LaTeX",
        "editor_asignado": "polanco",
        "arbitraje_confirmado": "1",
    })
    print(f"  ✅ CSV: estado=published")

    sftp.close(); transport.close()
    print(f"\n🎉 Book publicado: {book_id}")
    print(f"   URL esperada: http://localhost:8000/books/{book_id}")


if __name__ == "__main__":
    main()
