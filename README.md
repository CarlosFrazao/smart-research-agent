# Smart Research Agent (SRA) 🚀

Smart Research Agent (SRA) é um assistente de pesquisa autônomo projetado para buscar, analisar, filtrar e sintetizar informações em paralelo de até 8 fontes distintas (GitHub, Reddit, HackerNews, ArXiv, ProductHunt, Web, etc.).

## ⚡ NOTA IMPORTANTE PARA SISTEMAS OPERACIONAIS WINDOWS (POWERSHELL)

> [!WARNING]
> No ambiente Windows utilizando o **PowerShell**, o operador de concatenação lógica clássico `&&` **NÃO é suportado**. 
> Em seu lugar, utilize o caractere `;` para sequenciar múltiplos comandos.

### Exemplos de uso correto no PowerShell:

* **Ao invés de:**
  ```bash
  git add -A && git commit -m "feat: my commit"
  ```
* **Utilize:**
  ```powershell
  git add -A; git commit -m "feat: my commit"
  ```

---

## 🛠️ Requisitos e Configuração

Consulte o [README_DE_IMPLEMENTACAO.md](file:///e:/Meus%20LLMs/smart-research-agent/README_DE_IMPLEMENTACAO.md) para obter o guia passo a passo completo de fundação, clients, inteligência e orquestração.
