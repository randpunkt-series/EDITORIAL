"""
Toma el artículo externo más reciente de METADATA/ y lo añade a
registro_articulos.csv (simula el botón 'Import from journal' del Chief).
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from seeds._common import (
    sftp_connect, revista_dir, editorial_dir, read_csv, append_csv,
)

COLS_ARTICULOS = [
    "articulo_id", "tipo", "titulo", "ruta_pdf", "ruta_cover",
    "fecha_creacion", "estado", "isbn", "motivo_retiro", "fecha_retiro",
    "idioma_original", "formato_original", "editor_asignado",
    "arbitraje_confirmado",
]
COLS_VERSIONES = [
    "articulo_id", "fecha", "ruta_pdf", "nombre_archivo", "notas",
]


def main():
    transport, sftp, rs = sftp_connect()
    rev = revista_dir(rs)
    ed = editorial_dir(rs)

    # Listar METADATA/ ordenado desc
    files = sorted(
        [f for f in sftp.listdir(f"{rev}/METADATA") if f.endswith(".json")],
        reverse=True,
    )
    if not files:
        print("⚠️  No hay JSONs en METADATA/")
        sftp.close(); transport.close()
        return

    fname = files[0]
    art_id = fname.replace(".json", "")
    print(f"Importando: {art_id}")

    with sftp.open(f"{rev}/METADATA/{fname}", "r") as f:
        meta = json.loads(f.read().decode("utf-8"))

    # Verificar si ya está en registro
    df = read_csv(sftp, f"{ed}/registro_articulos.csv", COLS_ARTICULOS)
    if art_id in df["articulo_id"].values:
        print(f"⚠️  {art_id} ya está en registro_articulos.csv")
        sftp.close(); transport.close()
        return

    versions = meta.get("versions", [])
    pdf_name = versions[0].get("pdf", "") if versions else ""
    fecha = versions[0].get("submitted_at", "") if versions else ""

    append_csv(sftp, f"{ed}/registro_articulos.csv", COLS_ARTICULOS, {
        "articulo_id": art_id,
        "tipo": "articulo",
        "titulo": meta.get("titulo", ""),
        "ruta_pdf": f"versiones/{pdf_name}" if pdf_name else "",
        "ruta_cover": "",
        "fecha_creacion": fecha,
        "estado": "en_revision",
        "isbn": "",
        "motivo_retiro": "",
        "fecha_retiro": "",
        "idioma_original": meta.get("original_language", ""),
        "formato_original": meta.get("original_format", ""),
        "editor_asignado": "luis",
        "arbitraje_confirmado": "0",
    })
    print(f"  ✅ Añadido a registro_articulos.csv (en_revision, luis)")

    if pdf_name:
        append_csv(sftp, f"{ed}/registro_versiones.csv", COLS_VERSIONES, {
            "articulo_id": art_id,
            "fecha": fecha,
            "ruta_pdf": f"versiones/{pdf_name}",
            "nombre_archivo": pdf_name,
            "notas": "External submission (imported)",
        })

    sftp.close(); transport.close()
    print(f"\n🎉 Listo: {art_id}")


if __name__ == "__main__":
    main()
