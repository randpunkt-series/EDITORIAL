"""Ejecuta todos los seeds en orden e imprime el resumen."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from seeds import (
    seed_test_article,
    seed_test_book,
    seed_test_book_chief,
    seed_test_book_published,
)


def main():
    print("=" * 60)
    print("SEED ALL — generando datos de prueba")
    print("=" * 60)

    print("\n[1/4] Artículo (asignado a luis)...")
    seed_test_article.main()

    print("\n[2/4] Book (asignado a luis)...")
    seed_test_book.main()

    print("\n[3/4] Book (subido por Chief)...")
    seed_test_book_chief.main()

    print("\n[4/4] Book ya publicado...")
    seed_test_book_published.main()

    print("\n" + "=" * 60)
    print("✅ Todos los seeds ejecutados")
    print("=" * 60)


if __name__ == "__main__":
    main()
