# 📚 Módulo: analise-qualitativa-comunicacao.md

## Descrição
Pesquisa qualitativa, entrevistas, session replay, comunicação por audiência. Métodos para coletar, analisar e comunicar insights qualitativos com rigueur e clareza para stakeholders.

## Quando Ller
Ao conduzir entrevistas de usuário, analisar comportamento qualitativo, comunicar resultados para stakeholders, mapear dores e jornadas do usuário.

## Conteúdo Estruturado

### 1. Métodos de Coleta de Dados Qualitativos

#### Entrevistas Estruturadas vs Semi-Estruturadas

**Entrevista Estruturada**
- Guia fixo de perguntas, mesma ordem para todos os participantes
- Advantage: comparabilidade across participantes, menos viés de entrevista
- Desvantagem: pouca flexibilidade para explorar respostas inesperadas
- Quando usar: comparação across grupos, validação de hipóteses pré-definidas

**Entrevista Semi-Estruturada** *(mais comum em UX research)*
- Guia temático com perguntas-chave, permissão para desviar
- Advantage: pode explorar tópicos que emergem, profundidade maior
- Desvantagem: requires skilled interviewer, análise mais complexa
- Quando usar: discovery research, understanding "why" behind behaviors, persona development

**Guia de Entrevista Semi-Estruturada Exemplo**

```
1. Contextualização (5 min)
   - "Conta-me um pouco sobre seu trabalho diário..."
   - "Qual ferramenta você usa mais para [task]?"

2. Discovery da Dor (10 min)
   - "Me conta sobre a última vez que teve dificuldade com [task]..."
   - "O que teria tornado isso mais fácil?"
   - "O que você tentou fazer antes?"

3. Fluxo de Trabalho Atual (15 min)
   - "Descreva-me o passo a passo que você segue..."
   - "O que funciona bem? O que não funciona?"
   - "Onde você sente que perde tempo?"

4. Expectativas e desejos (10 min)
   - "Se pudesse mudar uma coisa sobre [sistema/ferramenta]..."
   - "Qual seria o impacto disso na sua trabalho?"
   - "O que seria 'sucesso' para você nisso?"

5. Fechamento (5 min)
   - "Há algo que não perguntamos e que gostaria de mencionar?"
   - "Permissão para entrar em contato caso surjam mais dúvias?"
```

#### Session Replay & Observação

- **Ferramentas**: Hotjar, FullStory, Microsoft Clarity, SessionCam
- **O que capturar**: clicks, scrolls, movimentos do mouse, campos de formulário preenchidos/limpos, erros de JavaScript
- **Considerações éticas**: anonimizar dados sensíveis (senhas, números de cartão), informar usuários sobre gravação
- **Análise**: identificar padrões de frustração (rage clicks, dead clicks), points of drop-off, flows completados vs abandonados

#### Questionários e Questionários Comentados

- **Likert scale**: medir intensidade de acordo (1-5 ou 1-7)
- **Open-ended questions**: "What was the most difficult part?", "If you could change one thing..."
- **Net Promoter Score (NPS)**: "De 0 a 10, quão provável é que recomende [produto] a um amigo?"
- **Post-interaction surveys**: após task completion, após erro

### 2. Análise de Dados Qualitativos

#### Codificação Temática (Thematic Analysis)

Passos (segva Braun & Clarke, 2006):

1. **Familiarização**: ler/reler dados, fazer anotações iniciais
2. **Gerar códigos**: marcar características interessantes across toda a dataset
3. **Buscar temas**: agrupar códigos em temas potencialmente relevantes
4. **Revisar temas**: verificar se temas funcionam across toda a dataset, refinar definições
5. **Definir e nomear temas**: identificar essência de cada tema, produzir nomes claros
6. **Produzir relatório**: selecionar exemplos ilustrativos, analisar relação entre temas

#### Exemplos de Códigos Comuns em Pesquisa de Usuário

| Categoria | Exemplos de Códigos |
|-----------|--------------------|
| **Facilidade de uso** | "intuitivo", "confusing", "fácil de aprender", "complexo", "clear labeling" |
| **Eficiência** | "fast", "slow", "unnecessary steps", "streamlined", "waste of time" |
| **Apesar/Interface** | "visual clutter", "clear layout", "consistent design", "confusing icons" |
| **Erro prevenção** | "prevents mistakes", "easy to recover", "fear of making mistakes", "confirmation dialogs" |
| **Personalização** | "adapts to me", "one-size-fits-all", "preferences saved", "contextual help" |
| **Acessibilidade** | "hard to read", "good contrast", "keyboard accessible", "screen reader friendly" |

#### Análise de Frequência de Temas

```python
from collections import Counter
import re

def count_theme_occurrences(interview_transcripts, theme_keywords):
    """
    Conta ocorrências de temas em transcripts de entrevista.
    entrevista_transcripts: list of str (textos limpos)
    theme_keywords: dict {theme_name: [list of related keywords/phrases]}
    Returns: dict {theme_name: count}
    """
    counts = {}
    for theme, keywords in theme_keywords.items():
        count = 0
        for transcript in interview_transcripts:
            # Count occurrences of any keyword (case-insensitive, substring match)
            for kw in keywords:
                pattern = rf'\b{re.escape(kw.lower())}\b'
                matches = len(re.findall(pattern, transcript.lower()))
                count += matches
        counts[theme] = count
    return counts

# Exemplo de uso
transcripts = [
    "O botão era confusing, eu não sabia onde clicar",
    "O fluxo foi rápido e intuitivo, consegui completar rápido",
    "O formulário had muitos campos, took too long to fill"
]

theme_keywords = {
    "usabilidade": ["confusing", "intuitivo", "fácil", "difficult"],
    "eficiencia": ["rápido", "lento", "tempo", "demorou"],
    "interface": ["botão", "layout", "design", "visual"]
}

print(count_theme_occurrences(transcripts, theme_keywords))
# Output: {'usabilidade': 2, 'eficiencia': 3, 'interface': 4}
```

#### Análise de Sentimento Qualitativo

- Codificar cada fragmento como: positivo, negativo, neutro, misto
- Identificar intensidade: leve, moderado, forte
- Agrupar por segmento de usuário (novo vs usuário veterano, cargo, etc.)

### 3. Comunicação para Stakeholders

#### Princípio Fundamental: Dados Brutos → Insights → Recomendações

**Erros Comuns (a evitar):**

1. **Dump de dados**: apresentar transcripts completos ou gráficos sem contexto
2. **Insights óbvios**: "os usuários gostam de coisas fáceis" — sem profundidade
3. **Falta de contexto**: não explicar quem disse, quantos mencionaram, em que circunstâncias
4. **Soluções prematuras**: recomendar features antes de entender a dor raiz

**Estrutura Recomendada de Apresentação**

```
1. Visão Geral (2 min)
   - Obivo da pesquisa
   - Número de participantes
   - Método resumido
   - Thesis principal em 1 frase

2. Principais Hallazgos (5-7 min)
   - 3-5 insights prioritários
   - Cada um com: citação ilustre + % participantes + contexto
   - Ordenados por importância/impacto

3. Histórias de Usuário (User Stories) (5 min)
   - 2-3 stories formatados: "Como [tipo de usuário], quero [objetivo], porque [motivo]"
   - Vincular cada story a dados/quotes reais

4. Recomendações (3-5 min)
   - Cada recomendação respondendo: "Como isso resolve o problema identificado?"
   - Priorização: now / next / later (com base em impacto × esforço)
   - Riscos/considerações de implementação

5. Perguntas & Discussão (3-5 min)
```

#### Formato de Insight Recomendado

```
⚡ INSIGHT: [Título em 6 palavras máximo]

O que foi observado: [descrição curta do que foi visto/ouvido]
Quem disse: [quantos participantes / exemplo de citação]
Impacto: [como isso afeta o negócio/usuário]
Recomendação: [ação concreta, dono, prazo se conhecido]
```

#### Exemplos de Boas Apresentações

**Exemplo 1 — Problema de Usabilidade**

```
⚡ INSIGHT: Botão de chamada ação não descoberto

O que foi observado: 6/8 participants não conseguiram encontrar o botão "Comprar" na página inicial. Todos olharam para o header primeiro, mas o botão estava abaixo da dobra e tinha cor semelhante ao background.

Quem disse: "I don't see any button to buy" (6 participants). One said: "Is there a buy button? Looks like just text."

Impacto: Taxa de conversão potentially leaving money on the table. Se 50% dos visitantes não clicam, receita diretamente afetada.

Recomendação: Mover botão acima da dobra, aumentar contraste em 40%, testar cor complementar em experimento A/B. Dono: Design System team, prazo: próxima sprint.
```

**Exemplo 2 — Fluxo Confuso**

```
⚡ INSIGHT: Checkout de 5 stages causing abandonment

O que foi observado: Session replay shows 42% of users drop off at stage 3 ("Shipping Information"). Main issue: field validation errors appear after user has already filled stages 1-2, forcing restart.

Quem disse: "I made it halfway and then got an error. Had to start over. Frustrating." (5 participants)

Impacto: Estimado 23% perda de receita potencial por abandono de checkout. A/B test sugerido: reduzir stages de 5 para 3 ou mostrar progress indicator.

Recomendação: Implementar progress indicator + inline validation. Testar redução de stages. Dono: Produto/Eng, prazo: 2 sprints.
```

### 4. Checklist de Qualidade para Entrevistas

- [ ] Guia de entrevista testado pilot test (1-2 participants) antes de full rollout
- [ ] Questions livres de leading language (evitar "não acha que X é melhor?")
- [ ] Gravação (áudio/video) consentimento obtido antecipadamente
- [ ] Ambiente silencioso, gravação de alta qualidade
- [ ] Backup manual de notas durante entrevista
- [ ] Transcrição realizada entro 24h enquanto memória ainda fresca
- [ ] Anonimato removido de transcripts antes de análise cross-participant
- [ ] Pelo menos 3 coders independentes para confiabilidade inter-rater (se research formal)
- [ ] Saturation check: novos insights ainda surgindo ou dataset reached saturation?

### 5. Referências Avançadas

- **Braun, V. & Clarke, V. (2006).** Using thematic analysis in psychology. Qualitative Research in Psychology.
- **Kvale, S. & Brinkmann, S. (2009).** InterViews: Learning the Craft of Qualitative Research Interviewing.
- **Rubin, J. (2010).** Handbook of User-Centered Design.
- **Norman, D. (2013).** The Design of Everyday Things — fundamentals de usabilidade.
- **Olson, J. & Kroggstrand, M. (2020).** Remote research best practices — guidelines para entrevistas virtuais.
- **Lieberman, W. (2021).** "The User Interview Handbook" — practical guide with templates.
- **Comunicação de Insights** — Alberto Cairo "How Charts Lie" principles para apresentação de findings qualitativos.
