"""
Simula un producto enviado por un AUTOR EXTERNO vía /submission (FastAPI).

NO toca registro_articulos.csv (el Chief debe importarlo con
"Import from journal"). SÍ deja los archivos en METADATA/, PDF/, ZIP/
para que el import funcione.
"""
import sys
import json
import zipfile
import io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from seeds._common import (
    sftp_connect, revista_dir, editorial_dir, books_dir,
    ensure_dir, dummy_pdf, unique_id,
)


ARTICLE_TYPE = "Research Article"


def _dummy_zip() -> bytes:
    """ZIP mínimo con un .tex dummy."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.tex", b"\\documentclass{article}\n\\begin{document}\nTest\n\\end{document}\n")
        zf.writestr("refs.bib", b"@article{test2026, title={Test}}\n")
    return buf.getvalue()


def main(article_type: str = ARTICLE_TYPE):
    transport, sftp, rs = sftp_connect()
    print("✅ Conectado.")

    art_id = unique_id("RP")
    print(f"\n📄 Creando ARTÍCULO EXTERNO: {art_id}")
    print(f"   article_type = {article_type}")

    rev = revista_dir(rs)
    ed = editorial_dir(rs)

    for d in [f"{rev}/METADATA", f"{rev}/PDF", f"{rev}/ZIP",
              f"{ed}/versiones"]:
        ensure_dir(sftp, d)

    # 1. JSON como si lo hubiera escrito app.py
    meta = {
        "tracking_id": art_id,
        "status": "submitted",
        "titulo": f"Artículo externo: {article_type}",
        "article_type": article_type,
        "original_language": "English",
        "original_format": "LaTeX",
        "abstract": "Abstract de prueba para autor externo.",
        "keywords": "test, external, submission",
        "msc": {"primary": {"area": "00", "code": "00A06"}, "secondary": []},
        "corresponding_author": {
            "name": "Autor Externo",
            "email": "cpg7000cpg@gmail.com",
            "affiliation": "Independent",
            "orcid": "0000-0000-0000-0000",
        },
        "coauthors": [],
        "versions": [{
            "version": 1,
            "pdf": f"{art_id}_v1.pdf",
            "supplementary_zip": f"SupplementaryMaterials_{art_id}_v1.zip",
            "submitted_at": "2026-01-01T00:00:00",
            "notes": "Initial submission (external)",
        }],
        "submitted_at": "2026-01-01T00:00:00",
        "zenodo": None,
    }
    with sftp.open(f"{rev}/METADATA/{art_id}.json", "w") as f:
        f.write(json.dumps(meta, indent=2).encode("utf-8"))
    print(f"  ✅ METADATA/{art_id}.json")

    # 2. PDF
    pdf_name = f"{art_id}_v1.pdf"
    with sftp.open(f"{rev}/PDF/{pdf_name}", "wb") as f:
        f.write(dummy_pdf())
    with sftp.open(f"{ed}/versiones/{pdf_name}", "wb") as f:
        f.write(dummy_pdf())
    print(f"  ✅ PDF/{pdf_name} + versiones/{pdf_name}")

    # 3. ZIP
    zip_name = f"SupplementaryMaterials_{art_id}_v1.zip"
    with sftp.open(f"{rev}/ZIP/{zip_name}", "wb") as f:
        f.write(_dummy_zip())
    with sftp.open(f"{ed}/versiones/{zip_name}", "wb") as f:
        f.write(_dummy_zip())
    print(f"  ✅ ZIP/{zip_name} + versiones/{zip_name}")

    # 4. NO se añade a registro_articulos.csv (eso lo hace el Chief
    #    con "Import from journal")

    sftp.close(); transport.close()
    print(f"\n🎉 Artículo externo creado: {art_id}")
    print(f"   Ahora el Chief debe importarlo con 'Import from journal'.")
    print(f"   (O ejecutar seed_to_registro para saltarse ese paso)")


if __name__ == "__main__":
    main()
