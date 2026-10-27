"""
Helper que imprime un link, abre el JSON de tokens y espera (con polling)
a que el operador humano firme en el navegador del celular.

Se usa SOLO en tests manuales. Los tests de CI deben saltarse este paso.
"""
import sys
import time
import webbrowser
from pathlib import Path

# Imports locales
sys.path.insert(0, str(Path(__file__).parent.parent))
from seeds._common import (
    sftp_connect, editorial_dir, read_csv,
)
from utils.tokens_manager import load_all, RUN_ID


COLS_CONFIRMACIONES = [
    "articulo_id", "nombre", "email", "token", "aprobado",
    "fecha_aprobacion", "comentarios",
]


def wait_for_signature(art_id: str, expected_email: str = None,
                       timeout_sec: int = 300, poll_sec: int = 5,
                       try_open_browser: bool = False) -> bool:
    """
    Espera hasta `timeout_sec` a que la fila (art_id, expected_email)
    tenga aprobado == "1" en el CSV remoto.

    Retorna True si firmó, False si timeout.
    """
    print("\n" + "═" * 70)
    print("🖐  ESPERANDO FIRMA MANUAL")
    print("═" * 70)
    print(f"Producto: {art_id}")
    if expected_email:
        print(f"Email:    {expected_email}")

    # Mostrar el link
    tokens = load_all()
    match = None
    for t in tokens:
        if t.get("art_id") == art_id:
            if not expected_email or t.get("email") == expected_email:
                match = t
                break
    if match:
        print(f"\n👉 Abre este link en tu celular:\n")
        print(f"   {match['url']}\n")
        if try_open_browser:
            try:
                webbrowser.open(match["url"])
            except Exception:
                pass
    else:
        print(f"\n⚠️  No hay token guardado para {art_id} / {expected_email}")
        print(f"   Revisa: /tmp/tokens_test_{RUN_ID}.json")

    print(f"\nEsperando hasta {timeout_sec}s (poll cada {poll_sec}s)...")
    print("═" * 70 + "\n")

    transport, sftp, rs = sftp_connect()
    ed = editorial_dir(rs)
    start = time.time()
    try:
        while time.time() - start < timeout_sec:
            df = read_csv(sftp, f"{ed}/registro_confirmaciones.csv",
                          COLS_CONFIRMACIONES)
            if not df.empty:
                mask = df["articulo_id"] == art_id
                if expected_email:
                    mask &= (df["email"] == expected_email)
                rows = df[mask]
                if not rows.empty and (rows["aprobado"] == "1").any():
                    elapsed = int(time.time() - start)
                    print(f"\n✅ ¡Firma detectada! ({elapsed}s)")
                    return True
            print(f"   … esperando ({(int(time.time()-start))}s)", end="\r")
            time.sleep(poll_sec)
    finally:
        sftp.close()
        transport.close()

    print(f"\n❌ Timeout ({timeout_sec}s) sin firma detectada.")
    return False
