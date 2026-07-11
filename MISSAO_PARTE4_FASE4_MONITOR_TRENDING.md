# MISSÃO PARTE4 — FASE 4: Tools MCP monitor_topic + get_trending + Briefing Diário

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Pré-requisito: **Fases 1 e 2 concluídas** (fontes de notícia ativas + published_at funcionando).
> A Fase 3 pode estar em andamento ou concluída.
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — REAPROVEITANDO O SCHEDULER PARA MONITORAMENTO CONTÍNUO

A ferramenta de `search_anything` (Fase 5 do Plano 3) realiza pesquisas pontuais. Para vigílias periódicas ("ficar antenado"), precisamos de monitoramento proativo.
O `ResearchScheduler` (`src/scheduler.py`) já tem persistência (`reports/scheduled_jobs.json`), agendamento e lógica de comparação de relatórios (detecta novas entidades, novos links e novas seções). 

Nesta fase, nós **não criaremos um sistema de persistência redundante do zero**. Em vez disso, exporemos e adaptaremos o `ResearchScheduler` existente na tool MCP `monitor_topic` e criaremos os endpoints REST unificados e a tool de tendências.

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `mcp-server-development` | `E:\Meus LLMs\.claude\skills\mcp-server-development\SKILL.md` | Registros de tools MCP em mcp_server.py |
| `api-patterns` | `E:\Meus LLMs\.claude\skills\api-patterns\SKILL.md` | Endpoints REST no FastAPI unificado |
| `python-pro` | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | Adaptações no scheduler.py e wiring |
| `test-driven-development` | `E:\Meus LLMs\.claude\skills\test-driven-development\SKILL.md` | Testar as novas tools e o endpoint REST |

---

## 📋 TAREFAS (em ordem obrigatória)

### TAREFA 4.1 — Integrar a tool MCP `monitor_topic` no `mcp_server.py`

**Arquivo:** `src/mcp_server.py` (dentro de `_register_mcp_tools`, no mesmo escopo das outras tools como `search_anything`)

Implementar a tool aproveitando a API do `ResearchScheduler`:

```python
        # ─────────────────────────────────────────────────────────────────
        # TOOL 17 — Monitoramento contínuo de tópicos (Vigília)
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def monitor_topic(
            action: Literal["create", "check", "list", "delete"],
            topic: str | None = None,
            check_interval_minutes: int = 60,
            monitor_id: str | None = None,
        ) -> str:
            """
            Cria ou consulta uma vigília contínua sobre um tópico usando o agendador do SRA.
            Roda buscas periódicas e retorna incrementos de conteúdo novo.
            
            Args:
                action: Ação a executar ('create', 'check', 'list', 'delete').
                topic: O tópico a monitorar (obrigatório para action='create').
                check_interval_minutes: Intervalo de vigília (default: 60).
                monitor_id: ID do monitor (obrigatório para 'check' e 'delete').
            """
            try:
                from src.scheduler import ResearchScheduler
                import json
                
                orc = container.orchestrator
                scheduler = ResearchScheduler(orchestrator=orc)
                
                if action == "create":
                    if not topic:
                        return json.dumps({"error": "Parâmetro 'topic' é obrigatório para action='create'"})
                    
                    # Converte minutos para cron simples
                    hours = max(1, check_interval_minutes // 60)
                    cron_expr = f"0 */{hours} * * *" if hours < 24 else "0 7 * * *"
                    
                    # Salva no diretório exclusivo reports/monitors
                    job_id = scheduler.schedule_research(
                        query=topic,
                        cron_expr=cron_expr,
                        output_dir="reports/monitors",
                        alert_on_changes=True
                    )
                    return json.dumps({
                        "monitor_id": job_id,
                        "status": "created",
                        "topic": topic,
                        "check_interval_minutes": check_interval_minutes,
                        "cron": cron_expr
                    })
                    
                elif action == "list":
                    jobs = scheduler._jobs
                    monitors = [
                        {
                            "monitor_id": j.id,
                            "topic": j.query,
                            "cron": j.cron,
                            "last_run": j.last_run,
                            "created_at": j.created_at,
                            "last_report_path": j.last_report_path
                        }
                        for j in jobs.values() if j.output_dir == "reports/monitors"
                    ]
                    return json.dumps({"monitors": monitors}, indent=2, ensure_ascii=False)
                    
                elif action == "delete":
                    if not monitor_id:
                        return json.dumps({"error": "Parâmetro 'monitor_id' é obrigatório para action='delete'"})
                    if monitor_id in scheduler._jobs:
                        del scheduler._jobs[monitor_id]
                        scheduler._save_jobs()
                        return json.dumps({"deleted": True, "monitor_id": monitor_id})
                    return json.dumps({"deleted": False, "error": "Monitor não encontrado"})
                    
                elif action == "check":
                    if not monitor_id:
                        return json.dumps({"error": "Parâmetro 'monitor_id' é obrigatório para action='check'"})
                    
                    job = scheduler._jobs.get(monitor_id)
                    if not job:
                        return json.dumps({"error": f"Monitor '{monitor_id}' não encontrado."})
                    
                    # Armazena o relatório anterior para podermos comparar
                    old_report_content = ""
                    if job.last_report_path and os.path.exists(job.last_report_path):
                        with open(job.last_report_path, encoding="utf-8") as f:
                            old_report_content = f.read()
                            
                    # Executa a nova rodada
                    new_report = await scheduler.run_scheduled_research(monitor_id)
                    
                    # Calcula mudanças
                    changes = []
                    if old_report_content:
                        changes = scheduler.compare_with_previous(new_report, old_report_content)
                        
                    return json.dumps({
                        "monitor_id": monitor_id,
                        "topic": job.query,
                        "last_run": job.last_run,
                        "changes_detected": changes,
                        "report_summary": new_report[:1000] + "..." if len(new_report) > 1000 else new_report
                    }, indent=2, ensure_ascii=False)
                    
            except Exception as e:
                return json.dumps({"error": str(e)})
```

---

### TAREFA 4.2 — Implementar a tool MCP `get_trending`

**Arquivo:** `src/mcp_server.py` (dentro de `_register_mcp_tools`)

Expor tendências globais do GDELT (que não requer queries/chaves específicas e monitora eventos em tempo real):

```python
        # ─────────────────────────────────────────────────────────────────
        # TOOL 18 — Tópicos em Alta (Trending)
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def get_trending(
            hours: int = 24,
            max_records: int = 10
        ) -> str:
            """
            Retorna os tópicos e notícias com maior volume de cobertura global nas últimas N horas.
            Usa a API do projeto GDELT para extrair dados sem requerer query do usuário.
            
            Args:
                hours: Janela temporal em horas (default: 24).
                max_records: Número máximo de registros para retornar (max: 20).
            """
            try:
                import httpx
                import json
                
                limit = min(max(max_records, 1), 20)
                # GDELT artlist API
                gdelt_url = (
                    f"https://api.gdeltproject.org/api/v2/doc/doc"
                    f"?mode=artlist&format=json&maxrecords={limit}&sort=hybridrel"
                    f"&timespan={hours}h"
                )
                
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(gdelt_url)
                    if resp.status_code != 200:
                        return json.dumps({"error": f"GDELT API retornou status {resp.status_code}"})
                    data = resp.json()
                    
                articles = data.get("articles", [])
                trending_topics = [
                    {
                        "title": art.get("title"),
                        "url": art.get("url"),
                        "domain": art.get("domain"),
                        "language": art.get("language"),
                        "tone": art.get("tone")
                    }
                    for art in articles
                ]
                return json.dumps({
                    "timeframe_hours": hours,
                    "topics": trending_topics
                }, indent=2, ensure_ascii=False)
                
            except Exception as e:
                return json.dumps({"error": str(e)})
```

---

### TAREFA 4.3 — Criar endpoint REST de Briefing Diário `/api/v1/briefing/latest`

**Arquivo:** `src/mcp_server.py` (dentro de `_register_rest_endpoints`)

Criar o endpoint REST que compila em um único relatório MD as novidades de todas as vigílias ativas:

```python
    @app.get("/api/v1/briefing/latest")
    async def get_latest_briefing():
        """
        Gera um compilado de novidades (Briefing) com base em todas as vigílias
        de tópicos registradas em 'reports/monitors'.
        """
        try:
            from src.scheduler import ResearchScheduler
            orc = app.state.container.orchestrator
            scheduler = ResearchScheduler(orchestrator=orc)
            
            monitors_run = []
            briefing_md = ["# 📰 Briefing Diário Automatizado SRA\n",
                           f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n",
                           "---"]
            
            for job_id, job in list(scheduler._jobs.items()):
                if job.output_dir == "reports/monitors":
                    # Roda e pega as novidades
                    new_report = await scheduler.run_scheduled_research(job_id)
                    monitors_run.append(job.query)
                    
                    briefing_md.append(f"\n## 📌 Monitoramento: {job.query}")
                    briefing_md.append(new_report[:1200] + "\n*(relatório completo salvo no disco)*\n")
                    briefing_md.append("---")
            
            if not monitors_run:
                briefing_md.append("\nNenhum monitoramento configurado ou ativo. Use a tool 'monitor_topic' para cadastrar.")
                
            return {
                "success": True,
                "monitors_checked": monitors_run,
                "briefing_md": "\n".join(briefing_md)
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
```

---

### TAREFA 4.4 — Adicionar testes de monitoramento

**Arquivo a criar:** `tests/test_monitor_topic.py`

Cobrir:
1. Chamada de `monitor_topic` action `create` adiciona um ScheduledJob correto.
2. Chamada de `monitor_topic` action `list` traz apenas os jobs da pasta `reports/monitors`.
3. Chamada de `monitor_topic` action `check` executa o job e retorna o sumário.
4. Endpoint `/api/v1/briefing/latest` retorna status 200 e compila os relatórios corretos.

---

### TAREFA 4.5 — Commit

```bash
git add src/mcp_server.py tests/test_monitor_topic.py
git commit -m "feat(parte4/fase4): expõe monitoramento recorrente e trending no MCP server + endpoint de briefing"
git push origin main
```

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 4

- [ ] Tool MCP `monitor_topic` integrada ao `ResearchScheduler` e adicionada no MCP
- [ ] Tool MCP `get_trending` com chamada GDELT e adicionada no MCP
- [ ] Endpoint REST `/api/v1/briefing/latest` adicionado e agregando relatórios de vigílias
- [ ] `tests/test_monitor_topic.py` com testes de unit/integração verdes
- [ ] `python -m pytest tests/ --tb=short -q` sem regressões
- [ ] Commit e push realizados
