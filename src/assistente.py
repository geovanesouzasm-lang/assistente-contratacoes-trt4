"""
assistente.py — O núcleo que conversa com o Gemini.

Junta as peças:
- contexto base (prompt de sistema + anexo + índice de formulários) — sempre presente;
- ferramentas que o assistente pode acionar:
    * consultar_acervo(consulta): busca semântica nas portarias/guias (RAG/Chroma);
    * abrir_formulario(codigo): carrega o conteúdo integral de um formulário sob demanda.
- histórico da conversa.

Nesta versão de protótipo, as "ferramentas" são expostas ao modelo via function calling do
Gemini, para que ele decida quando consultar o acervo ou abrir um formulário — em linha com o
comportamento (consulta sob demanda, foco no formulário do caso).
"""

from __future__ import annotations
import os

import google.generativeai as genai

from contexto import montar_contexto_base, carregar_formulario
from rag import buscar as rag_buscar, indexar as rag_indexar

MODELO = os.environ.get("GEMINI_MODEL", "models/gemini-2.5-flash")


# ---- Ferramentas expostas ao modelo -------------------------------------------------

def consultar_acervo(consulta: str) -> str:
    """
    Consulta o acervo teórico (Portarias 1.737/2023 e 1.633/2025, guias, manuais) por busca
    semântica. Use quando precisar do texto exato de uma norma interna do TRT4.
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
    """
    conteudo = carregar_formulario(codigo.strip().upper())
    if conteudo is None:
        return f"Formulário {codigo} não encontrado. Verifique o código."
    return conteudo


def buscar_na_web(consulta: str) -> str:
    """
    Busca informação ATUALIZADA na web (via Google Search) para dados que mudam no tempo e não
    estão na biblioteca interna nem são conhecimento estável — por exemplo: o limite de valor
    vigente para dispensa do art. 75 (atualizado anualmente por decreto), índices, ou fatos
    recentes. Retorna um resumo ancorado em fontes reais. Use com parcimônia, só quando o dado
    depende de atualização externa. Sempre trate o resultado como "externo, a conferir".
    """
    # IMPORTANTE: o grounding (Google Search) só funciona com a biblioteca NOVA `google-genai`
    # (google.genai). A biblioteca antiga `google.generativeai` NÃO suporta grounding — dá o erro
    # "Unknown field for FunctionDeclaration: google_search". Por isso a busca web usa a lib nova,
    # isoladamente, apenas aqui.
    try:
        from google import genai as genai_novo
        from google.genai import types as genai_types
    except Exception as e:
        return (f"Não consegui carregar a biblioteca de busca web ({e}). "
                "Informe ao usuário que não foi possível obter o dado externo agora.")

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return ("Não há chave de API configurada para a busca web. "
                "Informe ao usuário que não foi possível obter o dado externo agora.")

    prompt = (
        "Busque na web e responda de forma objetiva, citando as fontes (com URL quando "
        f"possível), à seguinte questão: {consulta}\n"
        "Se for um valor legal/normativo que muda por ano, deixe claro o ano de referência."
    )

    # Nome do modelo sem o prefixo "models/" (a lib nova aceita o id direto).
    modelo_web = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").replace("models/", "")

    try:
        client = genai_novo.Client(api_key=api_key)
        ferramenta_busca = genai_types.Tool(google_search=genai_types.GoogleSearch())
        resp = client.models.generate_content(
            model=modelo_web,
            contents=prompt,
            config=genai_types.GenerateContentConfig(tools=[ferramenta_busca]),
        )
        if resp and getattr(resp, "text", None):
            return resp.text
        return ("A busca web não retornou texto. Sugira ao usuário conferir a fonte oficial.")
    except Exception as e:
        return (f"Não consegui concluir a busca web ({e}). "
                "Informe ao usuário que não foi possível obter o dado externo agora e sugira "
                "conferir a fonte oficial.")


FERRAMENTAS = [consultar_acervo, abrir_formulario, buscar_na_web]


# ---- Configuração do modelo ---------------------------------------------------------

def _configurar():
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Defina a variável de ambiente GOOGLE_API_KEY (ou GEMINI_API_KEY) com sua chave do Gemini."
        )
    genai.configure(api_key=api_key)


def criar_modelo():
    _configurar()
    system_instruction = montar_contexto_base()
    return genai.GenerativeModel(
        model_name=MODELO,
        system_instruction=system_instruction,
        tools=FERRAMENTAS,
    )


def novo_chat():
    """Cria uma sessão de chat com histórico e ferramentas automáticas."""
    modelo = criar_modelo()
    # enable_automatic_function_calling: o SDK executa as ferramentas e devolve o resultado ao modelo.
    return modelo.start_chat(enable_automatic_function_calling=True)


def responder(chat, mensagem_usuario: str) -> str:
    """Envia a mensagem do usuário e retorna a resposta em texto."""
    resposta = chat.send_message(mensagem_usuario)
    return resposta.text


# ---- Teste rápido de linha de comando ----------------------------------------------

if __name__ == "__main__":
    # Garante índice do acervo (se houver documentos)
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
