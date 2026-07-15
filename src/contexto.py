"""
contexto.py — Carrega o CONHECIMENTO OPERACIONAL no contexto do assistente.

Diferente do acervo teórico (que vai para o RAG/Chroma), o conhecimento operacional é núcleo e
deve estar SEMPRE disponível ao assistente:
- o Prompt de Sistema (o comportamento);
- o esquema do Anexo Único (tipo de contratação → formulários);
- os 22 formulários (CLC) — estrutura, campos, notas de rodapé.

Estratégia de contexto (importante para não estourar tokens):
- O Prompt de Sistema e o Anexo Único entram SEMPRE.
- Os formulários são muitos e longos. Em vez de despejar todos, expomos um ÍNDICE sempre, e o
  CONTEÚDO de um formulário é carregado sob demanda (quando o enquadramento aponta para ele).
  Isso respeita a lógica do projeto: o assistente conhece todos, mas foca no que o caso exige.
"""

from __future__ import annotations
import json
from pathlib import Path

import os
BASE = Path(os.environ.get("ASSISTENTE_BASE", Path(__file__).resolve().parent.parent))
CONH = BASE / "conhecimento"
FORM_DIR = CONH / "formularios"


def carregar_prompt_sistema() -> str:
    return (CONH / "PROMPT_SISTEMA.md").read_text(encoding="utf-8")


def carregar_anexo() -> dict:
    return json.loads((CONH / "anexo_unico.json").read_text(encoding="utf-8"))


def indice_formularios() -> str:
    """Texto curto listando os formulários disponíveis (código + nome), para o contexto fixo."""
    anexo = carregar_anexo()
    linhas = ["FORMULÁRIOS DISPONÍVEIS (carregue o conteúdo sob demanda pelo código):"]
    for cod, info in anexo["formularios"].items():
        linhas.append(f"- {cod}: {info['nome']}")
    return "\n".join(linhas)


def carregar_formulario(codigo: str) -> str | None:
    """Conteúdo integral de um formulário (ex.: 'CLC-5A'). None se não existir."""
    p = FORM_DIR / f"{codigo}.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


def _formata_lista_formularios(itens: list) -> str:
    """
    Formata uma lista de formulários, agrupando os que são ALTERNATIVOS entre si
    (mesmo 'grupo_alternativa') numa expressão "CLC-X OU CLC-Y".
    """
    # separa itens em grupos de alternativa e itens normais, preservando ordem
    grupos = {}          # nome_grupo -> lista de rótulos
    ordem = []           # sequência de saída: ("grupo", nome) ou ("item", rótulo)
    vistos_grupo = set()

    for i in itens:
        rotulo = i["formulario"] + (f" [{i['condicao']}]" if "condicao" in i else "")
        g = i.get("grupo_alternativa")
        if g:
            grupos.setdefault(g, []).append(rotulo)
            if g not in vistos_grupo:
                vistos_grupo.add(g)
                ordem.append(("grupo", g))
        else:
            ordem.append(("item", rotulo))

    partes = []
    for tipo, val in ordem:
        if tipo == "grupo":
            partes.append("(" + " OU ".join(grupos[val]) + ")")
        else:
            partes.append(val)
    return ", ".join(partes) if partes else "—"


def resumo_anexo_para_contexto() -> str:
    """
    Versão textual e enxuta do anexo (casos + formulários por caso), para o assistente raciocinar
    sobre enquadramento sem precisar do JSON bruto inteiro.
    Formulários alternativos (ex.: TR ou Projeto Básico) aparecem agrupados com "OU".
    """
    anexo = carregar_anexo()
    linhas = ["ESQUEMA DO ANEXO ÚNICO (tipo de contratação → documentos):",
              "(Itens entre parênteses ligados por 'OU' são ALTERNATIVOS: basta preencher UM deles;",
              " preenchido um, o grupo está satisfeito e o(s) outro(s) NÃO devem ser exigidos.)"]
    for caso in anexo["casos"]:
        obrig = _formata_lista_formularios(caso["obrigatorios"])
        facs = _formata_lista_formularios(caso["facultativos"])
        valor = " ⚖️(depende de valor)" if caso.get("depende_de_valor") else ""
        linhas.append(
            f"\n• {caso['tipo']}{valor}\n"
            f"  Fundamento: {caso['fundamento']}\n"
            f"  Obrigatórios: {obrig}\n"
            f"  Facultativos: {facs}"
        )
    return "\n".join(linhas)


def montar_contexto_base() -> str:
    """
    Monta o bloco de contexto que acompanha SEMPRE o assistente:
    prompt de sistema + esquema do anexo + índice de formulários.
    (O conteúdo de cada formulário e o acervo teórico entram sob demanda.)
    """
    partes = [
        carregar_prompt_sistema(),
        "\n\n═══ ESQUEMA OPERACIONAL (Anexo Único) ═══\n",
        resumo_anexo_para_contexto(),
        "\n\n═══ ÍNDICE DE FORMULÁRIOS ═══\n",
        indice_formularios(),
    ]
    return "\n".join(partes)


if __name__ == "__main__":
    ctx = montar_contexto_base()
    print(f"Contexto base: {len(ctx)} caracteres")
    print("\n--- amostra do esquema ---")
    print(resumo_anexo_para_contexto()[:800])
