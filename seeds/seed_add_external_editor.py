"""
Añade el usuario 'sujeto' (Level 1) a registro_editores.csv si no existe.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from seeds._common import (
    sftp_connect, editorial_dir, read_csv, write_csv,
)

COLS = ["login", "password", "level", "email"]


def main():
    transport, sftp, rs = sftp_connect()
    ed = editorial_dir(rs)
    path = f"{ed}/registro_editores.csv"

    df = read_csv(sftp, path, COLS)
    print(f"Editores actuales: {df['login'].tolist()}")

    if "sujeto" in df["login"].tolist():
        print("✅ 'sujeto' ya existe, no se modifica")
        sftp.close(); transport.close()
        return

    import pandas as pd
    new_row = {
        "login": "sujeto",
        "password": "sujeto",
        "level": "1",
        "email": "cpg7000cpg@gmail.com",
    }
    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    write_csv(sftp, path, df)
    print(f"✅ 'sujeto' añadido (Level 1)")

    sftp.close(); transport.close()


if __name__ == "__main__":
    main()
