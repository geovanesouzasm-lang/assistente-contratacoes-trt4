# Assistente de Contratações TRT4 (protótipo)

Assistente para a fase de planejamento de contratações no TRT4
(Lei 14.133/2021 · Portaria GP.TRT4 1.737/2023). Fase de testes — usar dados fictícios.

App em Streamlit, hospedado no Streamlit Community Cloud.

## Configuração (no Community Cloud)
Em "Advanced settings" → Secrets, cole:
```
GOOGLE_API_KEY = "sua_chave_do_gemini"
```
E coloque a Portaria (PDF) em `conhecimento/acervo/` para o RAG indexar.
