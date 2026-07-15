"""
streamlit_app.py — Assistente de Contratações TRT4 (Streamlit Community Cloud).

Fase 1 do teste de entrega: app web gratuita no Streamlit Community Cloud (via GitHub, sem billing),
mantendo o NOSSO núcleo (contexto + rag + assistente) e o NOSSO RAG.

A chave da API entra pelos Secrets do Community Cloud (formato TOML):
    GOOGLE_API_KEY = "sua_chave"

Use apenas dados fictícios (fase de testes).
"""

import os
import sys
from pathlib import Path

import streamlit as st

# --- Caminhos e chave -------------------------------------------------------------
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "src"))
os.environ.setdefault("ASSISTENTE_BASE", str(BASE))

# No Community Cloud a chave vem de st.secrets; exportamos para o ambiente para o
# nosso núcleo (que lê os.environ) enxergá-la sem alterações.
if "GOOGLE_API_KEY" in st.secrets and not os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = st.secrets["GOOGLE_API_KEY"]

from assistente import novo_chat, responder, responder_stream          # noqa: E402

st.set_page_config(page_title="Assistente de Contratações TRT4", page_icon="📋", layout="centered")


@st.cache_resource(show_spinner="Carregando o acervo...")
def _carregar_acervo():
    """
    Carrega o índice do RAG (que vem versionado no repositório).
    NÃO indexa: a indexação é feita offline por gerar_indice.py e commitada.
    Retorna (n_trechos, status) — status: 'ok' | 'ausente' | 'desatualizado' | 'erro: ...'
    """
    try:
        from rag import _assinatura_acervo, _carregar_indice
        salvo = _carregar_indice()
        if salvo is None:
            return 0, "ausente"
        vetores, metas, sig_salva = salvo
        if sig_salva != _assinatura_acervo():
            return len(metas), "desatualizado"
        return len(metas), "ok"
    except Exception as e:
        return 0, f"erro: {e}"


def _init_chat():
    if "chat" not in st.session_state:
        st.session_state.chat = novo_chat()
        st.session_state.historico = []


def _tem_chave() -> bool:
    return bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))


with st.sidebar:
    st.header("Assistente de Contratações")
    st.caption("Fase de planejamento — TRT4")

    if _tem_chave():
        n, status = _carregar_acervo()
        # Mensagens técnicas ficam discretas; só erros que exigem ação aparecem em destaque.
        if status == "ok":
            with st.expander("Status do sistema", expanded=False):
                st.caption("✓ Chave da API detectada.")
                st.caption(f"✓ Acervo carregado: {n} trechos.")
        elif status == "ausente":
            st.error(
                "Índice do acervo não encontrado. Rode `gerar_indice.py` e suba a pasta "
                "`rag_index/` ao repositório."
            )
        elif status == "desatualizado":
            st.warning(
                f"Índice desatualizado ({n} trechos). O acervo mudou desde a última indexação. "
                "Rode `gerar_indice.py` e suba a `rag_index/` atualizada."
            )
        else:
            st.warning(f"Acervo não carregado ({status}).")
    else:
        st.error("Configure GOOGLE_API_KEY nos Secrets do app (Advanced settings).")

    st.divider()
    st.caption("⚠️ Fase de testes: use apenas dados fictícios, não sigilosos.")
    if st.button("Nova conversa"):
        for k in ("chat", "historico"):
            st.session_state.pop(k, None)
        st.rerun()


st.title("📋 Assistente de Contratações")

if not _tem_chave():
    st.info("Aguardando a chave da API (secret GOOGLE_API_KEY).")
    st.stop()

_init_chat()

if not st.session_state.historico:
    with st.chat_message("assistant"):
        st.markdown(
            "Olá! Sou seu assistente para o **planejamento de contratações** no TRT4. "
            "Posso tirar dúvidas ou te ajudar a **montar uma contratação** — do enquadramento "
            "aos documentos.\n\nComo posso ajudar? Se já sabe o que quer contratar, me conte o "
            "**objeto** e, se tiver, o **valor estimado**."
        )

for papel, texto in st.session_state.historico:
    with st.chat_message(papel):
        st.markdown(texto)

if prompt := st.chat_input("Escreva sua mensagem..."):
    st.session_state.historico.append(("user", prompt))
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        try:
            # Streaming: o texto aparece gradualmente (efeito "digitando").
            resposta = st.write_stream(responder_stream(st.session_state.chat, prompt))
        except Exception as e:
            # Fallback: se o streaming falhar, tenta a resposta de uma vez.
            try:
                resposta = responder(st.session_state.chat, prompt)
                st.markdown(resposta)
            except Exception as e2:
                resposta = f"Ocorreu um erro ao consultar o modelo: {e2}"
                st.markdown(resposta)
    st.session_state.historico.append(("assistant", resposta))
