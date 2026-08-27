# 📚 Módulo: experimentos-ab.md

## Descrição
Tipos de experimento A/B, amostragem (Python/NumPy), Sequential Probability Ratio Test (SPRT), template de pré-registro. Conceitos estatísticos para validação de hipóteses com controle de erro tipo I e II.

## Quando Ller
Ao desenhar ou analisar experimentos A/B, calcular tamanho de amostra, implementar monitoramento estatístico sem p-hacking.

## Conteúdo Estruturado

### 1. Tipos de Experimento A/B

#### Experimento Padrão (Binary Outcome)
- Hipóteses: H0 (nenhuma diferença) vs H1 (diferença existe)
- Métrica: taxa de conversão, clique, adesão
- Teste: proporção de duas amostras (z-test ou chi-squared)
- Exemplos: botão cor/texto, fluxo de checkout, preço display

#### Experimento de Continuação (Metric A/B)
- Métrica: tempo médio, receita por usuário, duration
- Teste: t-test de Welch (variâncias não iguais) ou Mann-Whitney U (não-paramétrico)
- Exemplos: tempo de página carregada, valor do carrinho, completude de flow

#### Experimento de Múltiplas Variantes
- Mais de 2 versões (A, B, C, ...)
- Correção de múltiplos testes: Bonferroni, Holm, FDR (False Discovery Rate)
- Teste global primeiro (chi-squared ANOVA), então pairwise se significativo

### 2. Cálculo de Tamanho de Amostra (Python/NumPy)

```python
from statsmodels.stats.power importzttest_power
import numpy as np

# Parâmetros
alpha = 0.05        # erro tipo I (significância)
power = 0.80        # 1 - erro tipo II (potência)
effect_size = 0.10  # diferença esperada em proporções (p1 - p0)

# Teste de proporção única vs baseline
n = ztest_power(effect_size=effect_size, alpha=alpha, alpha=alpha, ratio=1.0)
print(f"Tamanho de amostra necessário: {n:.0f} usuários por grupo")

# Teste de duas proporções (A vs B)
n_per_group = ztest_power(effect_size=effect_size, alpha=alpha, ratio=1.0)
print(f"Amostra por grupo: {n_per_group:.0f}")
```

### 3. Sequential Probability Ratio Test (SPRT)

#### Conceito
- Testagem sequencial: dados coletados um a um ou em lote pequeno
- Continua collecting até atingir razão de verossimilhança crítica
- Advantage: menas observações esperadas vs teste fixo n, detecção mais precoce de efeitos

#### Fórmula SPRT
Para proporções de Bernoulli:
- LR após n observações: `Λ_n = ∏(p1^x_i * (1-p1)^(1-x_i)) / (p0^x_i * (1-p0)^(1-x_i))`
- Parar e aceitar H1 se `Λ_n > B` (threshold upper)
- Parar e aceitar H0 se `Λ_n < A` (threshold lower)
- Senão: continuar coletando

#### Implementação Simplificada

```python
def sprt_proportion(x, n, p0, p1, alpha=0.05, beta=0.20):
    """
    x: número de sucessos observados
    n: número total de observações
    p0: proporção under H0 (baseline)
    p1: proporção under H1 (effect size)
    alpha: erro tipo I (false positive rate)
    beta: erro tipo II (false negative rate)

    Returns: 'H0', 'H1', or 'continue'
    """
    import math

    # Log thresholds
    A = math.log(beta / (1 - alpha))  # lower boundary
    B = math.log((1 - beta) / alpha)  # upper boundary

    # Log likelihood ratio
    log_lr = x * math.log(p1 / p0) + (n - x) * math.log((1 - p1) / (1 - p0))

    if log_lr <= A:
        return 'H0'  # aceitar hipótese nula
    elif log_lr >= B:
        return 'H1'  # aceitar hipótese alternativa
    else:
        return 'continue'  # coletar mais dados
```

### 4. Template de Pré-Registro

```
EXPERIMENT PRE-REGISTRATION TEMPLATE

1. Research Question: What is the effect of [treatment] on [metric]?

2. Primary Hypothesis:
   H0: μ_treatment = μ_control (no difference)
   H1: μ_treatment ≠ μ_control (difference exists)
   Directional: H1: μ_treatment > μ_control [or <]

3. Primary Metric: [definition, unit, measurement method]

4. Expected Effect Size: [Cohen's d or proportion difference]

5. Sample Size Justification:
   - Alpha = 0.05
   - Power = 0.80
   - Calculation: [formula reference]
   - Total N required: [number]

6. Data Collection Period: [start date] to [end date]

7. Exclusion Criteria: [what data will be excluded and why]

8. Analysis Plan:
   - Test statistic: [z-test, t-test, chi-squared, etc.]
   - Correction for multiple comparisons: [if applicable]
   - Subgroup analyses: [list if pre-specified]

9. Stopping Rules:
   - Interim analysis at [X]% of sample
   - SPRT thresholds: A = [value], B = [value]
   - futility boundary: [if applicable]

10. Authorizations: [who can approve deviations]
```

### 5. Boas Práticas — Controle de Erros

| Conceito | Recomendação |
|----------|-------------|
| **p-hacking** | NUNCA parar em n=observação significativa. Fixar n antecipadamente ou usar SPRT com boundaries definidos. |
| **Power insuficiente** | Calcular n antes. Se power < 0.80, aumentar sample size ou reduzir efeito esperado. |
| **Multiple comparisons** | Se testar k hipóteses, usar alpha ajustada: alpha_k = alpha / k (Bonferroni) ou FDR. |
| **Optimal stopping** | SPRT boundaries devem ser calculados baseado em alpha e beta desejados ANTES de coleta. |
| **Pós-hoc storytelling** | Evitar. Todo corte de p < 0.05 após visualização de dados constitui p-hacking. |

### 6. Exemplo Prático Completo

```python
from statsmodels.stats.power importzttest_power
import matplotlib.pyplot as plt
import numpy as np

# Cenário: testar se novo fluxo aumenta taxa de conversão de 10% para 12.5%
# Diferença = 2.5 pontos percentuais = effect_size = 0.025

# Calcular amostra necessária para power=0.8, alpha=0.05
n_required = ztest_power(effect_size=0.025, alpha=0.05, ratio=1.0)
print(f"Necessário: {n_required:.0f} por grupo (total {n_required*2:.0f})")

# Simular experimento
np.random.seed(42)
n = int(n_required)
p_control = 0.10
p_treatment = 0.125

# Gerar resultados observados
control_conversions = np.random.binomial(1, p_control, n).sum()
treatment_conversions = np.random.binomial(1, p_treatment, n).sum()

# Teste z de duas proporções
from statsmodels.stats.proportion import ztest
stat, pvalue = ztest([treatment_conversions, control_conversions], [n, n])
print(f"Z-statistic: {stat:.3f}, p-value: {pvalue:.4f}")
print(f"Significativo (p<0.05): {pvalue < 0.05}")

# SPRT sequential
result = sprt_proportion(x=treatment_conversions, n=n, p0=p_control, p1=p_treatment)
print(f"SPRT result: {result}")
```

### 7. Checklist de Implementação

- [ ] Definir H0 e H1 antes da coleta de dados
- [ ] Calcular tamanho de amostra baseado em effect size esperado
- [ ] Escolher teste estatístico adequado (paramétrico vs não-paramétrico)
- [ ] Definir boundaries do SPRT se usar testagem sequencial
- [ ] Documentar plano de análise no pré-registro
- [ ] Evitar peek at data / interim analysis não planejado
- [ ] Ajustar alpha para múltiplos comparisons se necessário
- [ ] Reportar effect size com intervalo de confiança, não apenas p-value
- [ ] Pré-registrar em Open Science Framework ou similar se applicable

### 8. Referências Avançadas

- **Statistical Power Analysis** (Cohen, 1988) — efeito size conventions (d=0.2 small, 0.5 medium, 0.8 large)
- **SPRT** (Wald, 1945) — sequential analysis fundamentals
- **Multiple Comparisons Problem** — Dunnett, Holm, Benjamini-Hochberg
- **Pre-registration** — OSF, ClinicalTrials.gov conventions
- **A/B Testing Excellence** — Evan Miller's AB Testing Stats Guide (online resource)
