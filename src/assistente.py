"""
assistente.py — O núcleo que conversa com o Gemini.

SDK: google-genai (google.genai). A biblioteca antiga google.generativeai foi descontinuada em
30/11/2025. Modelo padrão: gemini-3.5-flash.

Peças:
- contexto base (prompt de sistema + anexo + índice de formulários) — como system_instruction;
- ferramentas acionadas por function calling automático:
    * consultar_acervo(consulta): busca semântica nas portarias/guias (RAG);
    * abrir_formulario(codigo): carrega um formulário CLC sob demanda;
    * buscar_na_web(consulta): busca externa (grounding) para o que muda no tempo.
- histórico da conversa (client.chats mantém o histórico da sessão).

NOTA DE PROJETO — parâmetros opcionais nas ferramentas:
Os parâmetros das ferramentas são declarados com valor padrão None, de propósito. O modelo, ao
decidir usar uma ferramenta, ocasionalmente emite a primeira chamada SEM os argumentos. Se a
função exigisse o argumento (sem default), ela lançaria TypeError; o automatic function calling
capturaria a exceção e a devolveria ao modelo como erro — e o modelo, em vez de corrigir, tende a
desistir e alegar "problema técnico". Com o parâmetro opcional, a chamada vazia retorna uma
orientação curta ("forneça o parâmetro X e chame de novo"), o modelo se corrige e refaz a chamada
com o argumento correto. Isso foi diagnosticado e validado em teste (chamada vazia era a causa das
"instabilidades" e das alucinações de formulário).
"""

from __future__ import annotations
import os

from google import genai
from google.genai import types

from contexto import montar_contexto_base, carregar_formulario
from rag import buscar as rag_buscar, indexar as rag_indexar

# Id do modelo sem o prefixo "models/" (o SDK novo aceita o id direto).
MODELO = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").replace("models/", "")

# thinking_level só existe na geração 3.x; o 2.5 e anteriores rejeitam o parâmetro.
_MODELO_SUPORTA_THINKING = MODELO.startswith("gemini-3")


def _api_key() -> str:
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Defina a variável de ambiente GOOGLE_API_KEY (ou GEMINI_API_KEY) com sua chave do Gemini."
        )
    return api_key


# Client único do módulo: precisa permanecer vivo enquanto o objeto `chat` existir (o chat, guardado
# no session_state do Streamlit, mantém uma referência a ele). Criá-lo dentro de cada função faria o
# objeto sair de escopo e quebrar com "Cannot send a request, as the client has been closed".
_CLIENT = None


def _client() -> genai.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = genai.Client(api_key=_api_key())
    return _CLIENT


# ---- Ferramentas expostas ao modelo -------------------------------------------------

def consultar_acervo(consulta: str = None) -> str:
    """
    Consulta o acervo normativo interno do TRT4 (Portarias 1.737/2023 e 1.633/2025, guias e
    manuais) por busca semântica. Use quando precisar do texto de uma norma interna do TRT4.

    Args:
        consulta: o tema ou pergunta a buscar na biblioteca interna. Obrigatório.
    """
    if not consulta:
        return ("Para consultar o acervo, forneça o parâmetro 'consulta' (o tema a buscar) e chame "
                "a função novamente.")
    try:
        resultados = rag_buscar(consulta, n=4)
    except Exception as e:
        return (f"ERRO ao consultar o acervo interno: {type(e).__name__}: {e}. NÃO invente conteúdo "
                "nem chame isto de 'instabilidade'. Informe ao usuário, com honestidade, que não foi "
                "possível acessar a biblioteca interna agora.")
    if not resultados:
        return ("O acervo não retornou resultados para essa consulta. Se fizer sentido, reformule. "
                "NÃO invente conteúdo de norma interna.")
    return "\n\n---\n\n".join(f"[Fonte interna: {r['fonte']}]\n{r['texto']}" for r in resultados)


def abrir_formulario(codigo: str = None) -> str:
    """
    Carrega o conteúdo integral de um formulário CLC (ex.: 'CLC-5A') para ajudar no preenchimento
    ou na revisão. Use quando o enquadramento já apontou qual formulário será trabalhado.

    Args:
        codigo: o código do formulário, ex.: 'CLC-1A'. Obrigatório.
    """
    if not codigo:
        return ("Para abrir um formulário, forneça o parâmetro 'codigo' (ex.: 'CLC-1A') e chame a "
                "função novamente.")
    conteudo = carregar_formulario(codigo.strip().upper())
    if conteudo is None:
        return f"Formulário {codigo} não encontrado. Verifique o código."
    return conteudo


def buscar_na_web(consulta: str = None) -> str:
    """
    Busca informação ATUALIZADA na web (via Google Search) para dados que mudam no tempo e não
    estão na biblioteca interna — por exemplo: o limite de valor vigente para dispensa do art. 75
    (atualizado anualmente por decreto), o teor de dispositivos de normas externas (Lei 14.133,
    Resoluções CNJ/CSJT, INs), índices ou fatos recentes. Trate o resultado como "externo, a
    conferir".

    Args:
        consulta: a pergunta a pesquisar na web. Obrigatório.
    """
    if not consulta:
        return ("Para buscar na web, forneça o parâmetro 'consulta' e chame a função novamente.")
    prompt = (
        "Busque na web e responda de forma objetiva, citando as fontes (com URL quando possível), "
        f"à seguinte questão: {consulta}\n"
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
        return (f"Não consegui concluir a busca web ({e}). Informe ao usuário que não foi possível "
                "obter o dado externo agora e sugira conferir a fonte oficial.")


FERRAMENTAS = [consultar_acervo, abrir_formulario, buscar_na_web]


# ---- Chat -------------------------------------------------------------------------

def novo_chat():
    """
    Cria uma sessão de chat com histórico e function calling automático. O SDK invoca as
    ferramentas, devolve o resultado ao modelo e continua a geração.

    thinking_level="low" (só nos modelos 3.x): mantém o raciocínio enxuto, o que reduz a chance do
    problema de function calling do 3.x com respostas longas, e é mais rápido. Em modelos 2.5 e
    anteriores o parâmetro não é enviado (eles não o suportam).
    """
    config_kwargs = dict(
        system_instruction=montar_contexto_base(),
        tools=FERRAMENTAS,
    )
    if _MODELO_SUPORTA_THINKING:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")

    client = _client()
    return client.chats.create(model=MODELO, config=types.GenerateContentConfig(**config_kwargs))


def responder(chat, mensagem_usuario: str) -> str:
    """Envia a mensagem do usuário e retorna a resposta em texto."""
    resposta = chat.send_message(mensagem_usuario)
    return resposta.text


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
