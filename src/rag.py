"""
rag.py — Indexação e consulta do ACERVO TEÓRICO (versão leve, sem Chroma).

Por que sem Chroma: o Chroma depende de onnxruntime, que ainda não suporta Python 3.13. Para o
nosso caso (acervo pequeno: algumas portarias/guias), uma busca vetorial simples é mais que
suficiente e não traz dependências problemáticas.

Como funciona:
- Lê os documentos de conhecimento/acervo (md, txt, pdf).
- Faz chunking com sobreposição.
- Gera embeddings com a API do Gemini (models/text-embedding-004).
- Guarda vetores + textos num arquivo local (rag_index/).
- Busca por similaridade de cosseno (numpy).

Requer GOOGLE_API_KEY (mesma chave do Gemini) para gerar embeddings.
"""

from __future__ import annotations
import os
import glob
import json
from pathlib import Path

import numpy as np

BASE = Path(os.environ.get("ASSISTENTE_BASE", Path(__file__).resolve().parent.parent))
ACERVO_DIR = BASE / "conhecimento" / "acervo"
INDEX_DIR = BASE / "rag_index"
VEC_FILE = INDEX_DIR / "acervo_vectors.npy"
META_FILE = INDEX_DIR / "acervo_meta.json"
SIG_FILE = INDEX_DIR / "acervo_sig.json"

CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200
# Modelos de embedding candidatos, em ordem de preferência. O primeiro que a conta suportar é usado.
EMBED_CANDIDATOS = [
    "models/gemini-embedding-001",
    "models/gemini-embedding-2",
    "models/gemini-embedding-2-preview",
    "models/text-embedding-004",
]
_EMBED_ESCOLHIDO = None


def _configurar():
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Defina GOOGLE_API_KEY (ou GEMINI_API_KEY) para gerar embeddings.")
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    return genai


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


def _descobrir_modelo(genai) -> str:
    """Escolhe o primeiro modelo de embedding que a conta realmente suporta."""
    global _EMBED_ESCOLHIDO
    if _EMBED_ESCOLHIDO:
        return _EMBED_ESCOLHIDO
    # Tenta listar os modelos disponiveis e achar um que suporte embedContent
    disponiveis = set()
    try:
        for m in genai.list_models():
            metodos = getattr(m, "supported_generation_methods", []) or []
            if "embedContent" in metodos:
                disponiveis.add(m.name)
    except Exception:
        pass
    for cand in EMBED_CANDIDATOS:
        # aceita tanto "models/x" quanto "x"
        if cand in disponiveis or f"models/{cand}" in disponiveis or not disponiveis:
            _EMBED_ESCOLHIDO = cand if cand.startswith("models/") else f"models/{cand}"
            # se descobrimos a lista, garanta que o escolhido esta nela
            if disponiveis and _EMBED_ESCOLHIDO not in disponiveis:
                continue
            return _EMBED_ESCOLHIDO
    # fallback final
    _EMBED_ESCOLHIDO = "models/gemini-embedding-001"
    return _EMBED_ESCOLHIDO


def _embed(genai, textos: list[str], task_type: str) -> np.ndarray:
    modelo = _descobrir_modelo(genai)
    vetores = []
    LOTE = 100
    for k in range(0, len(textos), LOTE):
        lote = textos[k : k + LOTE]
        res = genai.embed_content(model=modelo, content=lote, task_type=task_type)
        emb = res["embedding"]
        if isinstance(emb[0], (int, float)):
            emb = [emb]
        vetores.extend(emb)
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

    - Se o índice salvo estiver atualizado (mesma assinatura do acervo), apenas o carrega: rápido,
      sem custo de embeddings. É o caso normal do boot.
    - Se documentos foram adicionados/removidos/alterados, reaproveita os embeddings dos que não
      mudaram e calcula SÓ os novos. Isso torna viável um acervo grande (dezenas de documentos).
    - reindexar=True força recálculo de tudo.
    """
    sig_atual = _assinatura_acervo()
    salvo = None if reindexar else _carregar_indice()

    # Caso 1: índice existe e está atualizado → só carrega.
    if salvo is not None:
        vetores_old, metas_old, sig_old = salvo
        if sig_old == sig_atual and len(metas_old) == len(vetores_old):
            return len(metas_old)
    else:
        vetores_old, metas_old, sig_old = np.empty((0, 0), dtype=np.float32), [], {}

    genai = _configurar()
    docs = _ler_documentos()
    if not docs:
        print("[rag] Nenhum documento no acervo para indexar.")
        return 0

    # Fontes que permanecem inalteradas (mesmo nome e tamanho) podem ser reaproveitadas.
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
        vetores_novos = _embed(genai, textos_novos, task_type="retrieval_document")
    else:
        vetores_novos = np.empty((0, 0), dtype=np.float32)

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
    genai = _configurar()
    metas = json.loads(META_FILE.read_text(encoding="utf-8"))
    vetores = np.load(VEC_FILE)

    q = _embed(genai, [consulta], task_type="retrieval_query")[0]
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
