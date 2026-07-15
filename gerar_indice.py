"""
gerar_indice.py — Gera (ou atualiza) o índice do RAG para ser versionado no GitHub.

USO (no Colab ou em qualquer máquina com Python):
    1. Tenha a pasta do projeto com conhecimento/acervo/ preenchida (os PDFs).
    2. Defina a chave:  export GOOGLE_API_KEY="sua_chave"   (ou os.environ no Colab)
    3. Rode:           python gerar_indice.py
    4. Suba a pasta rag_index/ (gerada) ao GitHub, junto com os PDFs novos.

O índice é INCREMENTAL: documentos já indexados são reaproveitados; só os novos/alterados
consomem chamadas de embedding.
"""

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "src"))
os.environ.setdefault("ASSISTENTE_BASE", str(BASE))

from rag import indexar, ACERVO_DIR, INDEX_DIR  # noqa: E402


def main():
    if not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")):
        print("ERRO: defina GOOGLE_API_KEY (ou GEMINI_API_KEY) antes de rodar.")
        sys.exit(1)

    docs = sorted(p.name for p in ACERVO_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in (".pdf", ".md", ".txt"))
    if not docs:
        print(f"ERRO: nenhum documento em {ACERVO_DIR}")
        sys.exit(1)

    print(f"Acervo ({len(docs)} documentos):")
    for d in docs:
        print(f"  - {d}")
    print()

    print("Indexando (só os documentos novos/alterados consomem API)...")
    n = indexar()
    print()
    print(f"✓ Índice pronto: {n} trechos.")
    print(f"✓ Arquivos gerados em: {INDEX_DIR}")
    for f in sorted(INDEX_DIR.iterdir()):
        print(f"    {f.name}  ({f.stat().st_size / 1024:.0f} KB)")
    print()
    print("PRÓXIMO PASSO: suba a pasta rag_index/ ao GitHub (junto com os PDFs novos do acervo).")


if __name__ == "__main__":
    main()
