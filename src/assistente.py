"""
assistente.py — O núcleo que conversa com o Gemini.

Migrado para o SDK novo `google-genai` (google.genai) e para o modelo gemini-3.5-flash.
A biblioteca antiga `google.generativeai` foi descontinuada em 30/11/2025.

Junta as peças:
- contexto base (prompt de sistema + anexo + índice de formulários) — como system_instruction;
- ferramentas que o modelo aciona por function calling automático:
    * consultar_acervo(consulta): busca semântica nas portarias/guias (RAG);
    * abrir_formulario(codigo): carrega um formulário CLC sob demanda;
    * buscar_na_web(consulta): busca externa (grounding) para o que muda no tempo.
- histórico da conversa (client.chats mantém o histórico da sessão).

No google-genai, passar funções Python em `tools` ativa o automatic function calling: o SDK
invoca a ferramenta, devolve o resultado ao modelo e continua a geração — equivalente ao antigo
enable_automatic_function_calling.
"""

from __future__ import annotations
import os

from google import genai
from google.genai import types

from contexto import montar_contexto_base, carregar_formulario
from rag import buscar as rag_buscar, indexar as rag_indexar

# Id do modelo sem o prefixo "models/" (o SDK novo aceita o id direto).
MODELO = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").replace("models/", "")


def _api_key() -> str:
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Defina a variável de ambiente GOOGLE_API_KEY (ou GEMINI_API_KEY) com sua chave do Gemini."
        )
    return api_key


# Client ÚNICO do módulo — precisa continuar vivo enquanto o chat existir.
# Se o client for criado dentro de uma função e sair de escopo, o objeto `chat` guardado no
# session_state do Streamlit falha com "Cannot send a request, as the client has been closed".
_CLIENT = None


def _client() -> genai.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = genai.Client(api_key=_api_key())
    return _CLIENT


# ---- Ferramentas expostas ao modelo -------------------------------------------------

def consultar_acervo(consulta: str) -> str:
    """
    Consulta o acervo teórico (Portarias 1.737/2023 e 1.633/2025, guias, manuais) por busca
    semântica. Use quando precisar do texto exato de uma norma interna do TRT4.

    Args:
        consulta: o texto ou tema a buscar na biblioteca interna do TRT4.
    """
    resultados = rag_buscar(consulta, n=4)
    if not resultados:
        return ("O acervo teórico ainda não foi indexado ou não retornou resultados. "
                "Responda com seu conhecimento geral, sinalizando que não confirmou na norma interna.")
    blocos = []
    for r in resultados:
        blocos.append(f"[Fonte interna: {r['fonte']}]\n{r['texto']}")
    return "\n\n---\n\n".join(blocos)


def abrir_formulario(codigo: str) -> str:
    """
    Carrega o conteúdo integral de um formulário CLC (ex.: 'CLC-5A') para ajudar no preenchimento
    ou revisão. Use quando o enquadramento já apontou qual formulário será trabalhado.

    Args:
        codigo: o código do formulário (ex.: "CLC-5B").
    """
    conteudo = carregar_formulario(codigo.strip().upper())
    if conteudo is None:
        return f"Formulário {codigo} não encontrado. Verifique o código."
    return conteudo


def buscar_na_web(consulta: str) -> str:
    """
    Busca informação ATUALIZADA na web (via Google Search) para dados que mudam no tempo e não
    estão na biblioteca interna nem são conhecimento estável — por exemplo: o limite de valor
    vigente para dispensa do art. 75 (atualizado anualmente por decreto), o TEOR de dispositivos
    de normas externas (Lei 14.133, Resoluções CNJ/CSJT, INs), índices ou fatos recentes.
    Retorna um resumo ancorado em fontes reais. Sempre trate o resultado como "externo, a conferir".

    Args:
        consulta: a pergunta a pesquisar na web.
    """
    prompt = (
        "Busque na web e responda de forma objetiva, citando as fontes (com URL quando "
        f"possível), à seguinte questão: {consulta}\n"
        "Se for um valor legal/normativo que muda por ano, deixe claro o ano de referência."
    )
    try:
        client = _client()
        ferramenta_busca = types.Tool(google_search=types.GoogleSearch())
        resp = client.models.generate_content(
            model=MODELO,
            contents=prompt,
            config=types.GenerateContentConfig(tools=[ferramenta_busca]),
        )
        if resp and getattr(resp, "text", None):
            return resp.text
        return "A busca web não retornou texto. Sugira ao usuário conferir a fonte oficial."
    except Exception as e:
        return (f"Não consegui concluir a busca web ({e}). "
                "Informe ao usuário que não foi possível obter o dado externo agora e sugira "
                "conferir a fonte oficial.")


FERRAMENTAS = [consultar_acervo, abrir_formulario, buscar_na_web]


# ---- Chat -------------------------------------------------------------------------

def novo_chat():
    """
    Cria uma sessão de chat com histórico e function calling automático.
    O SDK invoca as ferramentas, devolve o resultado ao modelo e continua a geração.
    """
    client = _client()
    system_instruction = montar_contexto_base()
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=FERRAMENTAS,
    )
    return client.chats.create(model=MODELO, config=config)


def responder(chat, mensagem_usuario: str) -> str:
    """Envia a mensagem do usuário e retorna a resposta em texto."""
    resposta = chat.send_message(mensagem_usuario)
    return resposta.text


# NOTA: a função de streaming foi removida nesta migração. O streaming foi testado em 15/07/2026
# e revertido: com function calling, corrompia a saída (repetição, mistura de idiomas, tabelas
# cortadas). Se um dia voltar, testar com cuidado neste novo SDK antes de subir.


# ---- Teste rápido de linha de comando ----------------------------------------------

if __name__ == "__main__":
    try:
        n = rag_indexar()
        print(f"[assistente] Acervo: {n} chunks indexados.")
    except Exception as e:
        print(f"[assistente] Aviso: acervo não indexado ({e}).")

    chat = novo_chat()
    print("Assistente pronto. Digite 'sair' para encerrar.\n")
    while True:
        try:
            msg = input("Você: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if msg.lower() in ("sair", "exit", "quit"):
            break
        if not msg:
            continue
        print("\nAssistente:", responder(chat, msg), "\n")
