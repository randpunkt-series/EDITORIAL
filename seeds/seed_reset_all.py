"""
Limpia TODO el estado de prueba del servidor remoto.

- Vacía CSVs de productos y firmas (deja cabecera).
- Vacía log.txt.
- Borra PDFs, ZIPs, JSONs y covers.
- NO toca registro_editores.csv (conserva usuarios).
- NO toca registro_coautores.csv (es una lista compartida).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from seeds._common import sftp_connect, revista_dir, editorial_dir


def _truncate_keep_header(sftp, path: str):
    try:
        with sftp.open(path, "r") as f:
            first = f.readline()
        with sftp.open(path, "w") as f:
            f.write(first)
        print(f"  ✅ {path.split('/')[-1]}")
    except FileNotFoundError:
        print(f"  ⚠️  {path.split('/')[-1]} no existe")
    except Exception as e:
        print(f"  ❌ {path.split('/')[-1]}: {e}")


def _empty_dir(sftp, path: str):
    try:
        files = sftp.listdir(path)
        for f in files:
            sftp.remove(f"{path}/{f}")
        print(f"  ✅ {path.split('/')[-1]}/ ({len(files)} archivos)")
    except FileNotFoundError:
        print(f"  ⚠️  {path} no existe")
    except Exception as e:
        print(f"  ❌ {path}: {e}")


def main():
    transport, sftp, rs = sftp_connect()
    ed = editorial_dir(rs)
    rev = revista_dir(rs)

    print("\n🧹 Vaciando CSVs (dejando cabecera)...")
    for fname in ["registro_articulos.csv", "registro_confirmaciones.csv",
                  "registro_revisiones.csv", "registro_versiones.csv",
                  "log.txt"]:
        _truncate_keep_header(sftp, f"{ed}/{fname}")

    print("\n🗑️  Borrando archivos de prueba...")
    _empty_dir(sftp, f"{ed}/versiones")
    _empty_dir(sftp, f"{rev}/METADATA")
    _empty_dir(sftp, f"{rev}/PDF")
    _empty_dir(sftp, f"{rev}/ZIP")
    _empty_dir(sftp, f"{rev}/BOOKS/metadata")
    _empty_dir(sftp, f"{rev}/BOOKS/pdf")
    _empty_dir(sftp, f"{rev}/BOOKS/covers")

    print("\n🎉 Remoto limpio (editores y coautores conservados)")
    sftp.close(); transport.close()


if __name__ == "__main__":
    main()
