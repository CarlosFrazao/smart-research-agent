# 📚 Módulo: diagnostico-producao-checklist.md

## Descrição
Protocolo DACI, diagnóstico abdutivo, checklist completo. Métodos para investigar anomalias de métrica em produção, conduzir postmortems, executar auditoria completa de rigor científico.

## Quando Ller
Ao investigar anomalias de métrica em produção, conduzir postmortems, executar auditoria completa de rigor científico, identificar causas raiz de falhas de sistema ou degradação de performance.

## Conteúdo Estruturado

### 1. Protocolo DACI (Decision & Action Command Investigation)

#### Roles Definidos

| Role | Responsabilidade | Quando Atribuir |
|------|-----------------|-----------------|
| **D — Driver** | Impulsionar o investigation, garantir prazos, coordenar participantes | Sempre — quem tem autoridade de decisão e responsabilidade final |
| **A — Approver** | Aprovar findings e recomendações antes de ação executiva | Quando findings podem impactar product direction ou resource allocation |
| **C — Contributor** | Fornecer expertise, dados, análise específica | Quando necessário conhecimento domain-specific (eng, design, analytics) |
| **I — Informed** | Mantidos informados, não tomam decisões stakeholders, exec team | Manter transparency sem bloquear fluxo de decisão |

#### Estrutura de Reunião DACI (80 min típicas)

```
0-5 min: Context Setting (Driver)
  - What metric went out of bounds?
  - When was it first detected?
  - Current status: investigating / monitoring / resolved?

5-15 min: Data Dump (Contributors)
  - Key metrics timeline (last 7-30 days)
  - Recent deployments, changes, incidents
  - Relevant monitoring alerts

15-40 min: Root Cause Analysis (Everyone)
  - Abductive reasoning: what hypothesis best explains the data?
  - Alternative hypotheses ranked by plausibility
  - Evidence for/against each

40-55 min: Decision Time (Driver + Approver)
  - What's the confirmed root cause?
  - What actions are decided?
  - What's deferred/cancelled?

55-70 min: Action Planning
  - Owner, deadline, success criteria for each action
  - Prevention measures to avoid recurrence

70-80 min: Handoff & Documentation
  - Document findings in postmortem repo
  - Communicate to affected teams
  - Schedule follow-up check-in
```

### 2. Diagnóstico Abdutivo (Abductive Reasoning)

#### Conceito
- Abduction = "inferir a melhor explicação" para conjunto de observações
- Diferente de dedução (lógica certa, se premissas então conclusão)
- Diferente de indução (generalizar a partir de exemplos)
- Abduction = criar hipótese novata que explica observações de forma satisfatória

#### Framework de 4 Passos

```
Passo 1: Observações (os fatos, sem interpretação)
  - Métrica M caiu de valor V1 para V2 entre tempo T1 e T2
  - N intervalos de confiança, N variações observadas
  - A/B test rodando simultaneamente? Deploy recente?

Passo 2: Hipóteses Candidateis (os "melhores relatos de causa")
  - H1: Deploy X introduziu bug em código de cálculo
  - H2: Tráfego mudou — nova segmentação trouxe usuários de menor qualidade
  - H3: Mudança em provedor terceiro (CDN, banco, API externa)
  - H4: Sazonalidade/external factor (fim de mês, feriado, evento)
  - H5: Limite de recurso (rate limit, quota excedida, connection pool)

Passo 3: Testar Hipóteses (evidence gathering)
  - Cada hipótes gets: predictions + como validar/falsificar
  - Qual teste mais barato/crível primeiro?
  - O que dados existentes já respondem?

Passo 4: Conclusão (a melhor explicação corrente)
  - "A melhor hipótese corrente é H2, porque..."
  - Nível de confiança: alta / média / baixa (baseado em evidence)
  - Próximos passos se for falsa: voltar ao Passo 2
```

#### Exemplo Prático — Queda de Taxa de Conversão

```
Observações:
- Taxa de checkout caiu de 4.2% para 3.1% nos últimos 7 dias
- Deploy na terça-feira introduziu novo fluxo de pagamento
- Nenhuma mudança de tráfego ou campanha nova
- Métrica de carrinho abandonado estável

Hipóteses:
H1: Novo fluxo de pagamento tem campo obrigatório causando erro
H2: Integração com gateway de pagamento mudou resposta de erro
H3: Mudança sazonal — fim de mês afeta tipo de usuário
H4: Rate limit do gateway está bloqueando transações

Testes:
- H1: Reverter deploy parcial, ver se taxa se recupera (custo: alto)
- H2: Testar sandbox do gateway com transações simuladas (custo: baixo)
- H3: Comparar com dados do mesmo período do ano passado (custo: médio)
- H3: Monitorar taxas de error do gateway em tempo real (custo: baixo)

Conclusão após testes:
- H2 confirmada: gateway mudou formato de resposta de erro em 15% dos casos
- Causa raiz: código não trata novo formato, transactions silently fail
- Ação: ajustar parser de resposta + adicionar validação de fallback
- Confiança: alta (3/3 testes de validação passaram)
```

### 3. Checklist Completo — Pós-Mortem de Rigor Científico

#### Seção A: O Que Aconteceu (Fatos Puros)

- [ ] Timestamp exato quando anomalia primeira foi detectada
- [ ] Métrica afetada: nome, unidade, valor baseline, valor observado
- [ ] Intervalo de confiança da métrica naquele período
- [ ] Alcance: afeta todos os usuários, segmento específico, região?
- [ ] Duração: quanto tempo a anomalia persistiu?
- [ ] Quem primeiro notou? Quem foi notificado?

#### Seção B: O Que Mudou (Mudanças Recentes)

- [ ] Deploy(s) nos últimos 30 dias: quais, quando, quais serviços
- [ ] Mudança em provedor externo (API, banco, CDN, third-party)
- [ ] Mudança em configuração (feature flag, A/B test, percentage rollout)
- [ ] Mudança em tráfego/público (campanha nova, seasonal, viral)
- [ ] Mudança em infraestrutura (servidores, containers, recursos)
- [ ] Eventos externos (feriados, notícias, clima, concorrência)

#### Seção C: Análise de Evidence (Proof)

For each hypothesis H:

- [ ] Prediction feita antes de testar: "Se H for verdade, então esperamos ver X"
- [ ] Teste realizado (qual, como, resultados)
- [ ] Dados que confirmam H vs falsifica H
- [ ] Força da evidence: empirical (dados observacionais) vs theoretical (lógica) vs anecdotal (storytelling)
- [ ] Confidence level: high (≥3 independent evidence sources), medium (1-2 sources), low (anecdotal/single source)

#### Seção D: Ação Corretiva

- [ ] Root cause confirmada (não "sintomas" ou "provavelmente")
- [ ] Ação corretiva específica (o que será feito, não "consertar bug")
- [ ] Owner asignado (quem é responsável)
- [ ] Deadline (quando será concluído)
- [ ] Success criteria (como saber quando resolved)
- [ ] Prevention measure (como evitar recorrência — processo, test, monitor)

#### Seção E: Lições Aprendidas

- [ ] O que seria feito diferente se acontecesse novamente
- [ ] O que o sistema/processo não teve que pegou esta anomalia
- [ ] Melhorias recomendadas (monitoramento, alertas, testes, documentação)
- [ ] Comunicado a quem precisa saber (teams, stakeholders, customers se aplicável)

### 4. Patrões de Anomalia e Diagnóstico Rápido

#### Padrão 1: Queda Imediata após Deploy

```
Sintomas: Métrica caiu X% dentro de Y minutos/horas após deploy Z
Provável causa: Código novo introduziu regressão
Checklist rápido:
- [] Rever diff do deploy Z vs versão anterior
- [ ] Testes automatizados passando? (qual última vez que falharam?)
- [ ] Feature flags related à mudança estão na posição correta?
- [ ] Rollback testado? Quanto tempo levaria?
- [] Monitorar métrica pós-rollback se disponível
```

#### Padrão 2: Degradação Gradual (semana/mês)

```
Sintomas: Métrica caiu gradualmente, não abrupto
Provável causa: Acumulo de technical debt, mudança de política, sazonalidade
Checklist rápido:
- [] Comparar com mesmo período do ano passado (sazonalidade?)
- [] Decompor por segmento/região — qual parte conduz o drop?
- [ ] Qualquer configuração mudado silenciosamente ao tempo?
- [ ] Volume de dados changed? (mais dados = mesma métrica pode parecer menor)
- [ ] Talk com team que tem conhecimento domain histórico
```

#### Padrão 3: Picos Intermitentes / Event Driven

```
Sintomas: Picos esporádicos de erro ou métrica incomum
Provável causa: Eventos externos, lote jobs, campanhas esporádicas
Checklist rápido:
- [ ] Calendar: quais eventos esperados nesta data/hora?
- [ ] Coincide com campanha de marketing, promoção, lançamento?
- [ ] Lote jobs ou maintenance windows que rodam nesta hora?
- [ ] Spikes de tráfego inesperados (bot, scraper, viral content)?
- [ ] Verificar logs de acesso para padrões incomuns
```

### 5. Auditoria Científica de Produção — 10 Pontos

Todo postmortem deve satisfizer estes 10 critérios para ser considerado "científico":

1. **Facts over narratives** — dados observacionais precedem interpretações
2. **Baseline comparada** — métrica comparada a período histórico pré-anomalia
3. **Effect size reportado** — magnitude do efeito, não apenas "significante" ou "não"
4. **Hypotheses explicitly listed** — todas as hipóteses considered, não apenas a "óbvia"
5. **Evidence hierarchy** — explicit which evidence type supports cada conclusão
6. **Confidence calibrated** — confidence level stated e justificado
7. **Action items SMART** — Specific, Measurable, Achievable, Relevant, Time-bound
8. **Prevention identified** — what will change para evitar recorrência
9. **No blame postmortem** — foco no sistema/processo, não em indivíduos
10. **Follow-up scheduled** — check-in definido para validar que ação correu bem

### 6. Modelo de Relatório Padrão (Fill-in-the-Blanks)

```
POSTMORTEM REPORT — [Sistema/Componente]

1. RESUMO EM 2 FRASES
   O que aconteceu: [descrição factual, sem jargão]
   Impacto: [métricas afetadas, alcance]

2. LINE DO TEMPO
   - [HH:MM DD/MM]: Anomalia detectada
   - [HH:MM DD/MM]: Primeiro debug iniciado
   - [HH:MM DD/MM]: Root cause confirmada
   - [HH:MM DD/MM]: Ação corretiva implementada
   - [HH:MM DD/MM]: Métrica retornou ao normal
   - [HH:MM DD/MM]: Postmortem realizada

3. ROOT CAUSE CONFIRMADA
   Declarative statement: [what, not how]
   Evidence: [dados que confirmam]
   Confidence: [alta/média/baixa] — por quê

4. AÇÕES CORRETIVAS
   | Ação | Owner | Prazo | Status |
   |------|-------|-------|--------|
   | [ação específica] | [nome] | [data] | [em andamento/concluído] |

5. PREVENÇÃO
   - [mudança de processo]
   - [melhoria de monitoramento]
   - [atualização de testes]
   - [documentação necessária]

6. LIÇÕES APRENDIDAS
   - [o que pegou errado no processo]
   - [o que funcionou bem]
   - [recomendações para evitar recorrência]

7. Aprovação
   Driver: _______________ Date: ___________
   Approver: _______________ Date: ___________
```

### 7. Checklist Pré-Postmortem (Antes da Reunião)

- [ ] Dados agregados preparados (métricas, logs, deploy history)
- [ ] Timeline construída (o mais detalhada possível)
- [ ] Hipóteses principais identificadas (3-5 max)
- [ ] Evidence catalogada por hipóteses (o que confirma/falsifica cada uma)
- [ ] Stakeholders chave identificados (who precisa estar na reunião)
- [ ] Formato de reunião decidido (DACI roles assigned)
- [ ] Modelo de relatório carregado e compreendido por driver
- [ ] Tempo bloqueado (80 min típicos, ajuste conforme escopo)

### 8. Referências Avançadas

- **DACI Framework** — Atlassian, Google Engineering Practices
- **Postmortem Blameless** — Principles from Incident Management (SRE books)
- **Abductive Reasoning** — Josephson & Josephson (1995), "Abductive Inference"
- **The Five Whys** — Toyota Production System, Toyota Industries
- **Root Cause Analysis** — ICAM method, TapRooT
- **Blameless Postmortems** — Charity Majors, Liz Fong-Jones practice
- **Production Incident Management** — SRE Handbook (Google), "Site Reliability Engineering"
- **Scientific Method in Engineering** — Popper, "The Logic of Scientific Discovery"
- **Error Analysis in ML Systems** — Nguyen et al., "End-to-End System Understanding"
