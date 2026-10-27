"""
Crea 4 productos, uno por cada formato:
- Short Note
- Scoped Expository Article (Survey)
- Original Research Article
- Book
Todos en en_revision, asignados a luis.
"""
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

# Coautor para que el test de firma funcione si se requiere
COAUTORES = [
    {"nombre": "Carlos Polanco", "email": "polanco@unam.mx"},
]

FORMATOS = [
    ("Short Note", "RP"),
    ("Scoped Expository Article", "RP"),
    ("Original Research Article", "RP"),
    ("Book", "BK"),
]


def main():
    transport, sftp, rs = sftp_connect()
    print("✅ Conectado.")

    rev = revista_dir(rs)
    ed = editorial_dir(rs)
    bk = books_dir(rs)

    for d in [f"{rev}/METADATA", f"{rev}/PDF", f"{rev}/ZIP", f"{ed}/versiones",
              f"{bk}/metadata", f"{bk}/pdf", f"{bk}/covers"]:
        ensure_dir(sftp, d)

    created_ids = []

    for article_type, prefix in FORMATOS:
        art_id = unique_id(prefix)
        is_book = (article_type == "Book")
        tipo = "libro" if is_book else "articulo"
        
        print(f"\n📄 Creando {article_type} ({tipo}): {art_id}")

        pdf_name = f"{art_id}_v1.pdf"
        cover_name = f"{art_id}.jpg" if is_book else ""
        
        # Metadatos JSON
        meta = {
            "tracking_id" if not is_book else "id": art_id,
            "status": "en_revision",
            "titulo" if not is_book else "title": f"Prueba de formato: {article_type}",
            "article_type": article_type,
            "original_language": "English",
            "original_format": "LaTeX",
            "abstract": f"Abstract de prueba para {article_type}.",
            "keywords": "test, formats, automation",
            "corresponding_author": {
                "name": "Carlos Polanco",
                "email": "polanco@unam.mx",
                "affiliation": "UNAM",
            },
            "coauthors": [],
            "versions": [{"version": 1, "pdf": pdf_name,
                          "submitted_at": "2026-01-01T00:00:00"}],
            "submitted_at": "2026-01-01T00:00:00",
            "zenodo": None,
        }
        
        # Guardar JSON
        if is_book:
            with sftp.open(f"{bk}/metadata/{art_id}.json", "w") as f:
                f.write(json.dumps(meta, indent=2).encode("utf-8"))
            with sftp.open(f"{bk}/pdf/{art_id}.pdf", "wb") as f:
                f.write(dummy_pdf())
            with sftp.open(f"{bk}/covers/{art_id}.jpg", "wb") as f:
                f.write(dummy_cover())
            with sftp.open(f"{ed}/versiones/{pdf_name}", "wb") as f:
                f.write(dummy_pdf())
        else:
            with sftp.open(f"{rev}/METADATA/{art_id}.json", "w") as f:
                f.write(json.dumps(meta, indent=2).encode("utf-8"))
            with sftp.open(f"{rev}/PDF/{pdf_name}", "wb") as f:
                f.write(dummy_pdf())
            with sftp.open(f"{ed}/versiones/{pdf_name}", "wb") as f:
                f.write(dummy_pdf())

        # Añadir al CSV registro_articulos
        append_csv(sftp, f"{ed}/registro_articulos.csv", COLS_ARTICULOS, {
            "articulo_id": art_id,
            "tipo": tipo,
            "titulo": f"Prueba de formato: {article_type}",
            "ruta_pdf": f"versiones/{pdf_name}",
            "ruta_cover": cover_name,
            "fecha_creacion": "2026-01-01T00:00:00",
            "estado": "en_revision",
            "isbn": "978-0000000000" if is_book else "",
            "motivo_retiro": "",
            "fecha_retiro": "",
            "idioma_original": "English",
            "formato_original": "LaTeX",
            "editor_asignado": "luis",
            "arbitraje_confirmado": "0",
        })

        # Añadir a registro_versiones
        append_csv(sftp, f"{ed}/registro_versiones.csv", COLS_VERSIONES, {
            "articulo_id": art_id,
            "fecha": "2026-01-01T00:00:00",
            "ruta_pdf": f"versiones/{pdf_name}",
            "nombre_archivo": pdf_name,
            "notas": f"Seed {article_type}",
        })

        created_ids.append((art_id, article_type, tipo))
        print(f"  ✅ Creado {art_id} ({article_type})")

    sftp.close(); transport.close()
    
    print("\n" + "=" * 60)
    print("🎉 4 productos creados exitosamente.")
    print("=" * 60)
    for aid, atype, atipo in created_ids:
        print(f"  {aid} | {atipo:8s} | {atype}")

if __name__ == "__main__":
    main()
