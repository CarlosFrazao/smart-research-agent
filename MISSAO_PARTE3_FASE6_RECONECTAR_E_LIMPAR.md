# MISSÃO PARTE3 — FASE 6: Reconectar Componentes Ocultos e Limpeza de Código Morto

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 6 (adicional) do plano derivado do [NOVO PLANO_SRA_PARTE_3.md](file:///e:/Meus%20LLMs/Conversa/NOVO%20PLANO_SRA_PARTE_3.md).
> Pré-requisito: **Fase 5 concluída**.
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — APROVEITANDO O QUE JÁ EXISTE

Durante a varredura sistemática final da Parte 3, descobrimos que várias capacidades propostas para serem construídas do zero (como processamento de imagem/mídia rica e análise quantitativa) **já estão construídas** no projeto, rotuladas como "Fase 2" na estrutura interna, mas estão desconectadas (órfãs). Além disso, encontramos arquivos de infraestrutura antigos e superados (código morto) que precisam ser removidos para manter a saúde e limpeza do repositório.

Esta fase foca em **conectar essas peças prontas** e **limpar o código morto** de forma cirúrgica.

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | Todo código de religação em Python |
| `clean-code` | `E:\Meus LLMs\.claude\skills\clean-code\SKILL.md` | Garantir que o wiring seja simples e siga os padrões existentes |
| `test-driven-development` | `E:\Meus LLMs\.claude\skills\test-driven-development\SKILL.md` | Escrever testes de integração para as peças religadas |

---

## 📋 TAREFAS (em ordem obrigatória)

### TAREFA 6.1 — Limpeza de Código Morto (Dívida Técnica)

**Contexto (§15.1, §17.2):** O arquivo `src/model_router.py` (legacy `ModelRouter`) e o diretório `src/search/common/` estão completamente órfãos (zero importações reais em produção ou testes).

**O que fazer:**
1. Confirmar com `grep` que nenhum arquivo importa `src/model_router.py` ou `src/search/common/`.
2. Remover o arquivo `src/model_router.py`.
3. Remover o diretório `src/search/common/` inteiro.

---

### TAREFA 6.2 — Reconectar a Camada de Verificação e QA

**Contexto (§17.3, §18.3):** As classes `MultiLLMFactChecker` (`src/reasoning/multi_llm_fact_checker.py`), `RagasEvaluator` (`src/evaluation/ragas_integration.py`) e `TruLensRecorder` (`src/evaluation/trulens_integration.py`) estão implementadas mas desconectadas do fluxo real de execução do pipeline.

**O que fazer:**
1. Abrir `src/pipeline/stages/verify_stage.py` (ou criar se não existir, ou integrar no `SynthesizeStage`/`ReportStage`).
2. Adicionar o `MultiLLMFactChecker` no fluxo de síntese/relatório quando o auditor/verificador estiver habilitado (`enable_auditor=True`).
3. Integrar RAGAS e TruLens na instrumentação de observabilidade do pipeline, gravando as métricas coletadas em `context.extra['ragas_metrics']` e `context.extra['trulens_metrics']` ao final de cada execução.

---

### TAREFA 6.3 — Religar o Processamento de Mídia Rica

**Contexto (§18.1):** Módulos como `OCRExtractor` (`src/extractors/ocr_extractor.py`), `PDFParser` (`src/extractors/pdf_parser.py`), `VideoTranscriber` (`src/extractors/video_transcriber.py`) e `VisionAnalyzer` (`src/vision_analyzer.py`) já estão prontos para uso.

**O que fazer:**
1. Integrar os extratores no pipeline de ingestão de arquivos. Sempre que o usuário ou o `ScrapingSearcher` encontrar um arquivo suportado (PDF, Imagem, Vídeo), rotear para o extrator correspondente em vez de falhar ou apenas extrair metadados brutos.
2. Garantir que o `VisionAnalyzer` receba a `vision_fn` necessária para analisar screenshots e diagramas de forma agnóstica de provedor LLM.

---

### TAREFA 6.4 — Religar a Análise Quantitativa (`DataAnalyzer`)

**Contexto (§18.2):** O `DataAnalyzer` (`src/data_analyzer.py`) já implementa a lógica para rodar scripts Pandas em sandbox Docker seguro via `CodeExecutionAgent`.

**O que fazer:**
1. Integrar o `DataAnalyzer` como uma etapa opcional de análise quantitativa após a coleta de resultados estruturados (ex: tabelas, estatísticas de benchmarks).
2. Adicionar suporte para acioná-lo via flag ou decisão do roteador do pipeline quando a query exigir consolidação numérica/gráfica de dados.

---

### TAREFA 6.5 — Religar a Formatação Acadêmica/Legal (`DomainPersona`)

**Contexto (§18.4):** A classe `DomainPersona` (`src/domain_personas.py`) implementa formatação de citação acadêmica e legal (APA, IEEE, Bluebook).

**O que fazer:**
1. Abrir `src/report/report_generator.py` (ou onde as citações são montadas).
2. Adicionar suporte para ler a persona do domínio do contexto de busca e usar `DomainPersona` para formatar a seção de referências/bibliografia final do relatório com base no estilo selecionado.

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 6

- [ ] Código morto removido (`src/model_router.py` e `src/search/common/`)
- [ ] `MultiLLMFactChecker`, `RagasEvaluator` e `TruLensRecorder` acionados sob demanda no pipeline de verificação
- [ ] `OCRExtractor`, `PDFParser`, `VideoTranscriber` e `VisionAnalyzer` mapeados e rodando na extração de mídia rica
- [ ] `DataAnalyzer` integrado e executando análises estruturadas via Pandas em sandbox
- [ ] `DomainPersona` formatando referências no relatório final em estilos padrão (APA/IEEE/Bluebook)
- [ ] Testes unitários e de integração criados em `tests/` cobrindo as religações (ex: `tests/test_qa_reconnection.py`, `tests/test_media_reconnection.py`)
- [ ] `python -m pytest tests/ --tb=short -q` → todos passam sem novas regressões
- [ ] **INDICE_MISSOES_PARTE3.md** e **CLAUDE.md** atualizados

---

## 🚫 FORA DO ESCOPO DESTA FASE

- Criar novas mecânicas de OCR ou parsing de PDF do zero (apenas religar as classes existentes).
- Corrigir a cadeia de feedback inteira (`LearnedRanker` / `FeedbackStore`) — mantido de menor prioridade conforme §17.4.
