# Política de Privacidade — Sistema de Feedback por Fonte

## O que é armazenado

O SRA armazena, por usuário, quais fontes de busca geraram resultados
aproveitados ou ignorados. Os dados armazenados são:

- `user_id` — identificador anônimo da sessão (não vinculado a dados pessoais)
- `source_name` — nome da fonte (ex: "github", "wikipedia")
- `domain` — categoria da query (ex: "dev_tools", "universal")
- `was_useful` — boolean (aprovado/ignorado)
- `timestamp` — data/hora do feedback

## O que NÃO é armazenado

- Conteúdo da query
- Conteúdo dos resultados
- IP, email ou qualquer dado identificador pessoal

## Como resetar

Para resetar seu perfil de preferências de fonte, use o endpoint:
`DELETE /api/feedback/sources/{user_id}`
ou chame `feedback_store.clear_source_feedback(user_id)`.

## Volume mínimo

Pesos de fontes só passam a influenciar o resultado após 5+ feedbacks
por fonte/usuário, para evitar viés por amostra pequena.
