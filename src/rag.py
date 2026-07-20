"""
rag.py — Indexação e consulta do ACERVO TEÓRICO (versão leve, sem Chroma).

Migrado para o SDK novo `google-genai` (google.genai). A biblioteca antiga `google.generativeai`
foi descontinuada em 30/11/2025.

Por que sem Chroma: o Chroma depende de onnxruntime, que ainda não suporta Python 3.13. Para o
nosso caso (acervo pequeno: algumas portarias/guias), uma busca vetorial simples é mais que
suficiente e não traz dependências problemáticas.

Como funciona:
- Lê os documentos de conhecimento/acervo (md, txt, pdf).
- Faz chunking com sobreposição.
- Gera embeddings com a API do Gemini.
- Guarda vetores + textos num arquivo local (rag_index/).
- Busca por similaridade de cosseno (numpy).

Requer GOOGLE_API_KEY (mesma chave do Gemini) para gerar embeddings.

ATENÇÃO: o índice é gerado com um modelo e uma DIMENSÃO específicos (EMBED_MODELO +
EMBED_DIM). Se qualquer um mudar, o índice precisa ser REGERADO — vetores de dimensões
diferentes não se comparam, e a busca quebra.
"""

from __future__ import annotations
import os
import glob
import json
from pathlib import Path

import numpy as np

from google import genai
from google.genai import types

BASE = Path(os.environ.get("ASSISTENTE_BASE", Path(__file__).resolve().parent.parent))
ACERVO_DIR = BASE / "conhecimento" / "acervo"
INDEX_DIR = BASE / "rag_index"
VEC_FILE = INDEX_DIR / "acervo_vectors.npy"
META_FILE = INDEX_DIR / "acervo_meta.json"
SIG_FILE = INDEX_DIR / "acervo_sig.json"

CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200

# Modelo de embedding e DIMENSÃO fixa. A dimensão precisa ser a MESMA na indexação e na consulta.
EMBED_MODELO = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = int(os.environ.get("GEMINI_EMBED_DIM", "768"))


def _client() -> genai.Client:
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Defina GOOGLE_API_KEY (ou GEMINI_API_KEY) para gerar embeddings.")
    return genai.Client(api_key=api_key)


def _ler_documentos() -> list[dict]:
    docs = []
    for path in glob.glob(str(ACERVO_DIR / "**" / "*.*"), recursive=True):
        ext = Path(path).suffix.lower()
        nome = Path(path).name
        try:
            if ext in (".md", ".txt"):
                docs.append({"fonte": nome, "texto": Path(path).read_text(encoding="utf-8")})
            elif ext == ".pdf":
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(path)
                    texto = "\n".join((p.extract_text() or "") for p in reader.pages)
                    docs.append({"fonte": nome, "texto": texto})
                except ImportError:
                    print(f"[rag] pypdf nao instalado; PDF ignorado: {nome}")
        except Exception as e:
            print(f"[rag] erro lendo {nome}: {e}")
    return docs


def _chunk(texto: str) -> list[str]:
    chunks, i, n = [], 0, len(texto)
    while i < n:
        chunks.append(texto[i : i + CHUNK_SIZE])
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return [c.strip() for c in chunks if c.strip()]


def _embed(client: genai.Client, textos: list[str], task_type: str) -> np.ndarray:
    """
    Gera embeddings com o SDK novo. task_type: "RETRIEVAL_DOCUMENT" (indexação) ou
    "RETRIEVAL_QUERY" (consulta). Dimensão fixada em EMBED_DIM.
    """
    vetores = []
    LOTE = 100
    for k in range(0, len(textos), LOTE):
        lote = textos[k : k + LOTE]
        res = client.models.embed_content(
            model=EMBED_MODELO,
            contents=lote,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=EMBED_DIM,
            ),
        )
        for e in res.embeddings:
            vetores.append(e.values)
    return np.array(vetores, dtype=np.float32)


def _assinatura_acervo() -> dict:
    """Impressão digital do acervo: nome -> tamanho. Detecta inclusão/remoção/alteração."""
    assinatura = {}
    if ACERVO_DIR.exists():
        for p in sorted(ACERVO_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in (".pdf", ".md", ".txt"):
                assinatura[p.name] = p.stat().st_size
    return assinatura


def _carregar_indice() -> tuple | None:
    """Retorna (vetores, metas, assinatura) do índice salvo, ou None se não houver."""
    if not (VEC_FILE.exists() and META_FILE.exists()):
        return None
    try:
        metas = json.loads(META_FILE.read_text(encoding="utf-8"))
        vetores = np.load(VEC_FILE)
        assinatura = {}
        if SIG_FILE.exists():
            assinatura = json.loads(SIG_FILE.read_text(encoding="utf-8"))
        return vetores, metas, assinatura
    except Exception:
        return None


def indexar(reindexar: bool = False) -> int:
    """
    Indexa o acervo de forma PERSISTENTE e INCREMENTAL.
    - Se o índice salvo estiver atualizado (mesma assinatura do acervo) E com a dimensão correta,
      apenas o carrega: rápido, sem custo de embeddings. É o caso normal do boot.
    - Se documentos foram adicionados/removidos/alterados, reaproveita os embeddings dos que não
      mudaram e calcula SÓ os novos.
    - Se a dimensão do índice salvo não bate com EMBED_DIM, força reindexação total.
    - reindexar=True força recálculo de tudo.
    """
    sig_atual = _assinatura_acervo()
    salvo = None if reindexar else _carregar_indice()

    # Caso 1: índice existe, está atualizado e tem a dimensão certa → só carrega.
    if salvo is not None:
        vetores_old, metas_old, sig_old = salvo
        dim_ok = vetores_old.ndim == 2 and vetores_old.shape[1] == EMBED_DIM
        if sig_old == sig_atual and len(metas_old) == len(vetores_old) and dim_ok:
            return len(metas_old)
        # dimensão errada → descarta o índice antigo e reindexa tudo.
        if not dim_ok:
            vetores_old, metas_old, sig_old = np.empty((0, 0), dtype=np.float32), [], {}
    else:
        vetores_old, metas_old, sig_old = np.empty((0, 0), dtype=np.float32), [], {}

    client = _client()
    docs = _ler_documentos()
    if not docs:
        print("[rag] Nenhum documento no acervo para indexar.")
        return 0

    # Fontes inalteradas (mesmo nome e tamanho) podem ser reaproveitadas.
    inalteradas = {
        nome for nome, tam in sig_atual.items()
        if sig_old.get(nome) == tam
    } if len(metas_old) == len(vetores_old) and len(metas_old) > 0 else set()

    metas_final, vetores_reuso, textos_novos, metas_novos = [], [], [], []

    # 1) Reaproveita o que não mudou.
    if inalteradas:
        for i, m in enumerate(metas_old):
            if m["fonte"] in inalteradas:
                metas_final.append(m)
                vetores_reuso.append(vetores_old[i])

    # 2) Calcula embeddings só dos documentos novos/alterados.
    for d in docs:
        if d["fonte"] in inalteradas:
            continue
        for j, ch in enumerate(_chunk(d["texto"])):
            textos_novos.append(ch)
            metas_novos.append({"fonte": d["fonte"], "chunk": j, "texto": ch})

    if textos_novos:
        print(f"[rag] Indexando {len(textos_novos)} trechos novos "
              f"({len(vetores_reuso)} reaproveitados).")
        vetores_novos = _embed(client, textos_novos, task_type="RETRIEVAL_DOCUMENT")
    else:
        vetores_novos = np.empty((0, EMBED_DIM), dtype=np.float32)

    # 3) Junta reaproveitados + novos.
    if vetores_reuso and len(vetores_novos):
        vetores = np.vstack([np.array(vetores_reuso, dtype=np.float32), vetores_novos])
    elif vetores_reuso:
        vetores = np.array(vetores_reuso, dtype=np.float32)
    else:
        vetores = vetores_novos

    metas_final.extend(metas_novos)
    if not len(metas_final):
        return 0

    INDEX_DIR.mkdir(exist_ok=True)
    np.save(VEC_FILE, vetores)
    META_FILE.write_text(json.dumps(metas_final, ensure_ascii=False), encoding="utf-8")
    SIG_FILE.write_text(json.dumps(sig_atual, ensure_ascii=False), encoding="utf-8")
    return len(metas_final)


def buscar(consulta: str, n: int = 4) -> list[dict]:
    if not (VEC_FILE.exists() and META_FILE.exists()):
        return []
    client = _client()
    metas = json.loads(META_FILE.read_text(encoding="utf-8"))
    vetores = np.load(VEC_FILE)
    q = _embed(client, [consulta], task_type="RETRIEVAL_QUERY")[0]
    vn = vetores / (np.linalg.norm(vetores, axis=1, keepdims=True) + 1e-9)
    qn = q / (np.linalg.norm(q) + 1e-9)
    scores = vn @ qn
    idx = np.argsort(-scores)[:n]
    return [
        {"fonte": metas[i]["fonte"], "texto": metas[i]["texto"], "score": float(scores[i])}
        for i in idx
    ]


if __name__ == "__main__":
    total = indexar(reindexar=True)
    print(f"[rag] Indexados {total} chunks do acervo.")
    if total:
        for r in buscar("prazo de vigencia da ata de registro de precos"):
            print(f"\n--- {r['fonte']} (score={r['score']:.3f}) ---\n{r['texto'][:300]}...")
