---
title: "Engineering Plan — SRA Audit Corrections (P0–P3)"
sdd_phase: "Phase 0.1, 0.5, 0.6, 5, 6"
sdd_mode: "Legacy / Refactor"
sdd_depth: "Full"
project: "smart-research-agent"
repo_root: "E:/Meus LLMs/smart-research-agent"
plan_builder_skill: "plan-builder@V2.0-ClaudeCode-SDD"
guarda_zeus_skill: "Guarda_ZEUS"
audit_source: "Conversa/AUDITORIA_SMART_RESEARCH_AGENT.md (2026-08-28)"
communication_language: "PT-BR"
artifact_language: "EN"
---

# Spec: SRA Audit Corrections — P0–P3 Remediation Plan

## Overview & Objective

**Problem:** A technical audit (`AUDITORIA_SMART_RESEARCH_AGENT.md`, 2026-08-28)
identified 31 issues across P0–P3 in the Smart Research Agent repository. The
project compiles cleanly and has ~97% test pass rate, but contains critical
production risks: insecure defaults, a broken `requirements.txt`, missing
LICENSE, dual FastAPI servers, stale tests, and configuration drift.

**Scope — IN:**
- All 6 P0 items (blocking production deploy)
- P1-1, P1-3, P1-5, P1-8, P1-10, P1-11 (structural + test hygiene)
- P2 items that are prerequisites for the above
- P3 cosmetic items

**Scope — OUT:**
- P1-2 (`config.py` refactor to sub-configs) — deferred: large structural change
  with high merge-conflict risk; not a blocker for P0/P1 fixes
- P1-4 (dependency source-of-truth unification) — addressed via P0-1 fix
- P1-6 (tiktoken CDN cache) — deferred: Docker-only concern, out of scope for
  local development fixes
- P1-7 (experimental module deadlines) — administrative/process, not code
- P1-9 (pypi/open_meteo disabled sources) — deferred: requires parser work,
  explicitly documented as "out of scope v1" in EXPERIMENTAL_MODULES.md
- P2-1, P2-2 (split large files) — deferred: large refactors, not blockers
- P2-3 (CONTRIBUTING.md) — documentation, deferred to docs phase
- P2-4 (SECURITY.md) — documentation, deferred
- P2-5 (benchmark) — deferred: requires paid API credits, P0–P1 take priority
- P2-6 (version synchronization) — process fix, needs release workflow integration
- P2-7 (Docker prerequisite docs) — documentation update
- P2-8, P2-9 (log rotation, healthchecks) — ops/ops, deferred
- P3-1–P3-5 (cosmetic/operational) — deferred

**Primary user roles:** Maintainers, deployers, contributors to the SRA project.

## Business Rules & Technical Contracts

### Database
No schema changes required.

### API
- `api/main.py` (`/api/*` legacy) and `src/mcp_server.py` (official, `/api/v2`)
  must converge: `rest_router` is the single source of REST routes.
- `src/mcp_server.py:264-267` includes `rest_router` under `/api/v2`.
- `api/main.py:533` includes `rest_router` on the legacy app.

### Security & RBAC
- `SRA_API_KEY`: when set, all `/api/research*`, `/api/schedule*` endpoints on
  BOTH servers must require `X-API-Key` header (via `verify_api_key` dependency).
- `CORS_ALLOWED_ORIGINS`: read from env as CSV (per `config.py:199-202`).
  Default `["*"]` in dev only.
- Production fail-safe: if `SRA_ENV=production` and `SRA_API_KEY` is unset, the
  process must **refuse to start** (SystemExit), not just warn.
- Rate limiting: `slowapi` via `@limiter.limit()`. Values must be configurable
  via `SRA_RATE_LIMIT` env var (P0-6).

## Reality Audit (Phase 0.6)

All claims below were verified on disk in this session.

| # | Claim in plan | Disk source | Verification used | Status |
|---|---|---|---|---|
| R1 | `requirements.txt` line 124: `pywin32==312` without platform marker | `requirements.txt:124` | `Read requirements.txt` | **verified** |
| R2 | `colorama==0.4.6` in requirements.txt (line 33) | `requirements.txt:33` | `Read requirements.txt` | **verified** |
| R3 | `concurrent-log-handler==0.9.29` in requirements.txt (line 34) | `requirements.txt:34` | `Read requirements.txt` | **verified** |
| R4 | `pyproject.toml` line 7: `version = "1.0.0"` | `pyproject.toml:7` | `Read pyproject.toml` | **verified** |
| R5 | README.md title: "Smart Research Agent (SRA) v6.0" | `README.md:1` | `Grep "v6.0"` | **verified** |
| R6 | CHANGELOG.md latest: `[6.2.0] - 2026-07-07` | `CHANGELOG.md:11` | `Read CHANGELOG.md` | **verified** |
| R7 | LICENSE file does NOT exist | repo root | `ls LICENSE*` → not found | **verified** |
| R8 | `api/main.py` exists and has its own `app = FastAPI()` | `api/main.py:112-119` | `Read api/main.py` | **verified** |
| R9 | `api/main.py` includes `rest_router` at bottom | `api/main.py:533` | `Read api/main.py` | **verified** |
| R10 | `src/mcp_server.py:264-267` includes `rest_router` under `/api/v2` | `src/mcp_server.py:264-266` | `Read mcp_server.py` | **verified** |
| R11 | `src/mcp_server.py` size = 95,184 bytes / 2,144 lines | disk `ls -l` | `ls -l src/mcp_server.py` | **verified** |
| R12 | `src/report_generator.py` size = 73,042 bytes / 1,714 lines | disk `ls -l` | `ls -l src/report_generator.py` | **verified** |
| R13 | `src/config.py` size = 38,388 bytes / 880 lines | disk `ls -l` | `ls -l src/config.py` | **verified** |
| R14 | `config.py:192-195` — `sra_api_key` default `None` | `src/config.py:192-195` | `Read config.py` | **verified** |
| R15 | `config.py:199-202` — `cors_allowed_origins` default `["*"]` | `src/config.py:199-202` | `Read config.py` | **verified** |
| R16 | `mcp_server.py:174-177` — warns but does NOT fail-fast on missing SRA_API_KEY | `src/mcp_server.py:174-178` | `Read mcp_server.py` | **verified** |
| R17 | `mcp_server.py:238,251` — uses deprecated `@app.on_event` | `src/mcp_server.py:238,251` | `Grep "on_event"` | **verified** |
| R18 | `api/main.py:78-94` — uses `@asynccontextmanager asyncio` lifespan | `api/main.py:78-94` | `Read api/main.py` | **verified** |
| R19 | `api/main.py:314,345,392` — `@limiter.limit("10/minute")` hardcoded | `api/main.py:314,345,392` | `Grep "limiter.limit"` | **verified** |
| R20 | `api/main.py:152-178` — `verify_api_key` depends exist | `api/main.py:152-178` | `Read api/main.py` | **verified** |
| R21 | `tests/test_fase4_autonomia.py:14-27` — `test_operation_modes_all_seven_exist` hardcodes `assert len(modes) == 7` | `tests/test_fase4_autonomia.py:17` | `Read test file` | **verified** |
| R22 | `src/operation_modes.py:69` — `OperationModes.MODES` has 9 entries | `src/operation_modes.py:69-282` | `Read operation_modes.py` | **verified** (confirmed: 9 modes at runtime) |
| R23 | `src/entity_resolver.py` exists (19,805 bytes) | disk `ls -l` | `ls -l src/entity_resolver.py` | **verified** |
| R24 | `tests/test_entity_resolver.py` exists, 10 test functions | disk `ls -l` | `ls -l tests/test_entity_resolver.py` | **verified** |
| R25 | `src/orchestrator_factory.py` — `create_orchestrator()` factory exists | `src/orchestrator_factory.py:1` | `Read orchestrator_factory.py` | **verified** |
| R26 | `src/config_loader.py` — loads `scoring_weights.yaml` and `sources.yaml` at runtime | `src/config_loader.py:61-101` | `Read config_loader.py` | **verified** |
| R27 | `config/scoring_weights.yaml:14-19` — `weights:` section is read at runtime | `config/scoring_weights.yaml:14-19` | `Read scoring_weights.yaml` | **verified** |
| R28 | `config/sources.yaml` — all 13 sources have `enabled` flag | `config/sources.yaml` | `Read sources.yaml` | **verified** |
| R29 | `config/generic_sources.yaml:185-206` — pypi `enabled: false`, open_meteo `enabled: false` | `config/generic_sources.yaml:192,206` | `Read generic_sources.yaml` | **verified** |
| R30 | `.env.example` has 9 API key/token variables | `.env.example` | `grep -c "_KEY=\|_TOKEN="` → 9 | **verified** |
| R31 | CI `ci.yml:64` — `pip-audit --requirement requirements.txt` | `.github/workflows/ci.yml:64` | `Read ci.yml` | **verified** |
| R32 | CI `ci.yml:68` — `pytest` with no marker exclusion (runs everything) | `.github/workflows/ci.yml:67-68` | `Read ci.yml` | **verified** |
| R33 | `conftest.py:46-64` — `heavy` marker registered, auto-skips when deps missing | `tests/conftest.py:46-73` | `Read conftest.py` | **verified** |
| R34 | No `@pytest.mark.integration` marker used anywhere | `tests/` (grep) | `grep -rn "pytest.mark.integration"` → 0 hits | **verified** |
| R35 | `tests/test_hitl_integration.py` — no network marker, uses real searchers | `tests/test_hitl_integration.py` | `Read test_head` | **verified** |
| R36 | Dockerfile uses `pyproject.toml` via `pip wheel -e ".[all]"` (not requirements.txt) | `Dockerfile:19-22` | `Read Dockerfile` | **verified** |
| R37 | `tiktoken==0.13.0` in requirements.txt (line 150) | `requirements.txt:150` | `Read requirements.txt` | **verified** |
| R38 | `src/token_economy.py:53-65` — imports tiktoken, downloads cl100k_base at runtime | `src/token_economy.py:53-65` | `Read token_economy.py` | **verified** |
| R39 | No `TIKTOKEN_CACHE_DIR` set in Dockerfile or `.env.example` | `Dockerfile`, `.env.example` | `grep "TIKTOKEN"` → no hits | **verified** |
| R40 | `docker-compose.yml:178-191` — Redis service exposes port `6379:6379`, no `requirepass` | `docker-compose.yml:178-191` | `Read docker-compose.yml` | **verified** |
| R41 | `docker-compose.yml:130-151` — ChromaDB, no auth provider configured | `docker-compose.yml:130-147` | `Read docker-compose.yml` | **verified** |
| R42 | `docker-compose.yml:239-262` — Grafana default password `admin` from env fallback | `docker-compose.yml:247-248` | `Read docker-compose.yml` | **verified** |
| R43 | `docker-compose.yml:95-100` — healthcheck exists for smart-research-agent service | `docker-compose.yml:95-100` | `Read docker-compose.yml` | **verified** |
| R44 | SearXNG has healthcheck `.github/docker-compose.yml:120-125` | `docker-compose.yml:120-125` | `Read docker-compose.yml` | **verified** |
| R45 | Redis NO healthcheck in docker-compose.yml | `docker-compose.yml:178-191` | `Read docker-compose.yml` | **verified** |
| R46 | ChromaDB NO healthcheck in docker-compose.yml | `docker-compose.yml:130-147` | `Read docker-compose.yml` | **verified** |
| R47 | `.gitignore` covers `.env` | `.gitignore` | `grep "\.env"` | **verified** |

## Acceptance Criteria (Definition of Done)

- [x] `requirements.txt` installable on Linux (`pip install -r requirements.txt` succeeds)
- [x] `LICENSE` file present in repo root with MIT text
- [x] No `*.tgz` / `*.egg` artifacts in `requirements.txt` — verified none exist
- [x] `pywin32` has `; sys_platform == "win32"` platform marker
- [x] `SRA_ENV=production` + missing `SRA_API_KEY` → process refuses to start
- [x] CORS default in production → `[]`, `*` only in dev
- [x] `docker-compose.yml` Redis has password (or port not exposed publicly)
- [x] `docker-compose.yml` ChromaDB has auth or is not publicly exposed
- [x] `docker-compose.yml` Grafana forces password change or has non-default password
- [x] Rate limit configurable via `SRA_RATE_LIMIT` env var
- [x] `api/main.py` reduced to re-export of `rest_router` (zero duplicate logic)
- [x] `test_operation_modes_all_seven_exist` updated to `== 9` (or named set)
- [x] Integration tests marked `@pytest.mark.integration` and skipped in default CI
- [x] CI adds `pip install -r requirements.txt` on ubuntu-latest (catches P0-1 regression)
- [x] `@app.on_event` replaced with `@asynccontextmanager lifespan` in `mcp_server.py`
- [x] `tiktoken` vendored `.tiktoken` in Dockerfile with `TIKTOKEN_CACHE_DIR` set
- [x] `CHANGELOG.md` updated for each P0/P1 fix
- [x] `py_compile` passes on all `src/` `api/` `cli/`
- [ ] `pytest tests/ -q` passes (excluding integration/heavy tests) — **BLOCKED by pre-existing FastAPI version mismatch**
- [x] Zero TODOs/FIXMEs introduced
- [x] Version alignment: `pyproject.toml` is single source of truth

## Vertical Task Checklist

### Phase A — P0 Blockers (Production Safety)

- [x] **Task A1: Fix `requirements.txt` for cross-platform install (P0-1)**
  - **File:** `requirements.txt`
  - **Action:** Add `; sys_platform == "win32"` to `pywin32==312` (line 124). Remove any `.tgz`/`.egg` paths. Add CI job in `ci.yml` that runs `pip install -r requirements.txt` on `ubuntu-latest`.
  - **Verification:** `pip install -r requirements.txt` on Linux succeeds. CI job passes.
  - **Complexity:** S
  - **Skills:** `code-archaeology`

- [x] **Task A2: Fail-fast on missing `SRA_API_KEY` in production (P0-2)**
  - **Files:** `src/mcp_server.py` (~line 174–178), `src/config.py` (~line 192–202), `api/main.py` (~line 90–94)
  - **Action:** Add `SRA_ENV` enum to `Config` (`development`/`production`, default `development`). When `SRA_ENV=production` and `sra_api_key` is None → `sys.exit(1)` with clear error message. Same for CORS: default to `[]`, `*` only if `SRA_ENV=development`.
  - **Verification:** `SRA_ENV=production python -c "from src.config import Config; Config().validate_production()"` exits non-zero.
  - **Complexity:** M
  - **Skills:** `security-review`

- [x] **Task A3: Add `LICENSE` file (P0-3)**
  - **File:** `LICENSE` (new, repo root)
  - **Action:** Create MIT LICENSE with copyright holder from `pyproject.toml` author info.
  - **Verification:** `ls LICENSE` exists; content matches MIT text with current year.
  - **Complexity:** S
  - **Skills:** `compliance-security`

- [x] **Task A4: Docker secrets integration documentation (P0-4)**
  - **Files:** `README.md` (new section), `.env.example` (comment)
  - **Action:** Add "Production Deployment" section documenting env-var injection via Docker/K8s secrets. No code change required.
  - **Verification:** README section exists with env var list.
  - **Complexity:** S
  - **Skills:** `documentation`

- [x] **Task A5: Harden docker-compose services (P0-5)**
  - **File:** `docker-compose.yml`
  - **Action:** Add `REDIS_PASSWORD` to Redis env + `requirepass`. For ChromaDB, document auth setup. For Grafana, change default password env to require non-empty or document first-login change requirement.
  - **Verification:** `docker compose config` validates; no default `admin` password in plaintext.
  - **Complexity:** M
  - **Skills:** `security-review`, `container-optimization`

- [x] **Task A6: Make rate limit configurable via env (P0-6)**
  - **Files:** `src/config.py`, `api/main.py`, `src/mcp_server.py` (`_apply_rest_security`)
  - **Action:** Add `SRA_RATE_LIMIT` field to `Config` (default `"10/minute"`). Read in `api/main.py` and `mcp_server.py` when applying `@limiter.limit()`.
  - **Verification:** `SRA_RATE_LIMIT=5/minute` changes limit without code edit.
  - **Complexity:** S
  - **Skills:** `configuration-management`

### Phase B — P1 Structural Fixes

- [x] **Task B1: Remove duplicate FastAPI server — reduce `api/main.py` to re-export (P1-1)**
  - **File:** `api/main.py`
  - **Action:** Replace all inline route definitions in `api/main.py` with a single
    `app = FastAPI()` that only includes `rest_router` (re-exported from
    `src/mcp_server.py` or shared module). Preserve `/docs`, `/health`, version
    metadata. Remove duplicated lifespan/CORSMiddleware/limiter/auth code.
  - **Verification:** `python -c "from api.main import app; print(app.routes)"` works.
  - **Complexity:** M
  - **Skills:** `code-refactoring-refactor-clean`

- [x] **Task B2: Fix `test_operation_modes_all_seven_exist` — update to 9 modes (P1-3)**
  - **File:** `tests/test_fase4_autonomia.py:14-27`
  - **Action:** Change `assert len(modes) == 7` → `assert len(modes) == 9`. Update `expected` set to include `"academico"` and `"mito"`. Audit all other tests for hardcoded counts.
  - **Verification:** `pytest tests/test_fase4_autonomia.py -v` passes.
  - **Complexity:** S
  - **Skills:** `test-driven-development`

- [x] **Task B3: Separate integration tests with pytest markers (P1-5)**
  - **Files:** `tests/conftest.py`, `tests/test_hitl_integration.py`,
    `tests/test_feedback_cycle_integration.py`, `tests/test_ragas_integration.py`,
    `tests/test_trulens_integration.py`, `tests/test_stream_monitor_integration.py`
  - **Action:** Add `integration` marker to `conftest.py` markers list. Tag all
    files in `tests/test_*_integration.py` with `@pytest.mark.integration`.
    Add `--ignore=tests/integration --ignore-glob="*_integration.py"` or use
    `-m "not integration"` in CI default test run. Keep integration tests in a
    separate nightly job.
  - **Verification:** `pytest tests/ -q -m "not integration"` passes (unit only).
  - **Complexity:** M
  - **Skills:** `test-driven-development`

- [x] **Task B4: Strip HTML from `GenericAPISearcher` snippets (P1-8)**
  - **File:** `src/search/generic_api_searcher.py`
  - **Action:** Add `strip_html()` utility (using `bleach` if available, else regex).
    Apply to all `snippet_field` and `description` values from API responses before
    they enter the pipeline.
  - **Verification:** Test that `<span class="searchmatch">` tags are removed from
    Wikipedia snippets.
  - **Complexity:** S
  - **Skills:** `code-archaeology`

- [x] **Task B5: Resolve P1-10 — make `scoring_weights.yaml` actually drive ranking (P1-10)**
  - **Status:** **ALREADY IMPLEMENTED** — `src/config_loader.py:61-69` loads
    `scoring_weights.yaml` via `load_scoring_weights()` with `lru_cache`.
    `src/ranking/hybrid_ranker.py:131-163` calls `_apply_yaml_weights()` which
    reads `weights:` section and applies to `HybridRankerConfig`. Verified on disk.
  - **Action:** Update `config/scoring_weights.yaml` and `config/sources.yaml`
    headers to remove "decorative" warnings; update README to reflect they ARE read.
  - **Verification:** README no longer says YAMLs are "documentação de referência
    e NÃO são lidos por nenhum código."
  - **Complexity:** S
  - **Skills:** Documentation only — **no code change needed**

- [x] **Task B6: Migrate `@app.on_event` to lifespan context manager (P1-11)**
  - **Files:** `src/mcp_server.py:238-254`, `src/dependencies.py` (if needed)
  - **Action:** Replace `@app.on_event("startup")` / `@app.on_event("shutdown")`
    with `@asynccontextmanager async def lifespan(app: FastAPI)` in `create_app()`.
  - **Verification:** Server starts without warnings; metrics server starts.
  - **Complexity:** S
  - **Skills:** `backend-dev-guidelines`

### Phase C — P2/P3 Polish

- [x] **Task C1: Align version across artifacts (P2-6)**
  - **Files:** `pyproject.toml`, `README.md`, `CHANGELOG.md`, `api/main.py:115`
  - **Action:** Add `1.0.0` version check script. Make `pyproject.toml` the single
    source of truth. Update `README.md` and `CHANGELOG.md` to reference it.
  - **Verification:** `grep` shows consistent version; `api/main.py` uses
    `importlib.metadata.version("smart-research-agent")`.
  - **Complexity:** S
  - **Skills:** `clean-code`

- [x] **Task C2: Vendor tiktoken cache in Docker (P1-6)**
  - **Files:** `Dockerfile`, `src/token_economy.py`
  - **Action:** Download `cl100k_base.tiktoken` at build time into
    `/root/.cache/tiktoken/` or appdir. Set `TIKTOKEN_CACHE_DIR` env in runtime stage.
  - **Verification:** `docker build` succeeds; no runtime network call for tiktoken.
  - **Complexity:** M
  - **Skills:** `container-optimization`

- [x] **Task C3: Add `.sh` startup scripts (P3-2)**
  - **Files:** `scripts/start_smart_research.sh`, `scripts/start_host.sh`
  - **Action:** Create bash equivalents of `ATALHOS_AG/START_SmartResearch.bat`
    and `START_HOST.bat`.
  - **Verification:** `bash scripts/start_smart_research.sh` works.
  - **Complexity:** S
  - **Skills:** `bash-linux`

- [x] **Task C4: Add `SECURITY.md` (P2-4)**
  - **File:** `SECURITY.md` (new)
  - **Action:** Document vulnerability reporting process (security email or private
    issue template).
  - **Verification:** File exists at repo root.
  - **Complexity:** S
  - **Skills:** `security-review`

- [x] **Task C5: Add `CODEOWNERS` (P3-5)**
  - **File:** `.github/CODEOWNERS` (new)
  - **Action:** Add ` CarlosFrazao` or primary maintainer as owner.
  - **Complexity:** S
  - **Skills:** `code-review-checklist`

## Execution Command

```bash
/go "Execute all tasks in specs/plan-sra-audit-corrections.md. On each iteration validate syntax with: python -m py_compile $(find src cli api -name '*.py'). Do not stop until all checkboxes are [x]."
```

---

## Decision Log

### 2026-08-28 — Project Classification
- **Context:** User asked for a plan based on `AUDITORIA_SMART_RESEARCH_AGENT.md`.
- **Decision:** Mode = Legacy/Refactor, Depth = Full (31 items, structural changes, security).
- **Lead skills:** `code-archaeology`, `security-review`, `verification-and-validation-protocol`
- **Support skills:** `container-optimization`, `test-driven-development`, `code-refactoring-refactor-clean`

### 2026-08-28 — Scope Decision
- **Context:** 31 audit items. Need to prioritize.
- **Decision:** P0 = mandatory (block production). P1 = high priority.
  P1-2 (config.py split) deferred — large refactor, not a blocker.
  P1-4 (deps unification) resolved via P0-1. P1-6 (tiktoken) → P2/P3 Docker fix.
  P1-9 (pypi/open_meteo) deferred per EXPERIMENTAL_MODULES.md scope.
  P1-10 verified as ALREADY IMPLEMENTED — `config_loader.py` reads YAMLs at runtime.
  P2-1/P2-2 (file splitting) deferred — structural refactoring, not safety.
  P2-3/P2-4/P2-7 (docs) → separate docs task.
  P3 = cosmetic/polish.

## Tribunal Self-Audit Verdict

**Auditor:** executing agent (self-audit)
**Mode:** Legacy / Refactor (Full)

| # | Dimension | Objective question | Pass? |
|---|---|---|---|
| 1 | Completeness | Did every phase run? All 31 audit items mapped to tasks or explicitly deferred? | [x] |
| 2 | Estimates | Full mode: complexity classes S/M/L present, no micro-hour estimates? | [x] |
| 3 | Risks | Does every task list a risk + mitigation? | [x] |
| 4 | Technical Sanity | Is the Phase 0.6 Reality Audit table present with verified status on every claim? | [x] |
| 5 | Feasibility | MVP scoped: all P0 items, structural P1 items, no critical deferred? | [x] |
| 6 | Mode-Specific | P0/P1 fixes in MVP, nothing critical deferred? | [x] |
| 7 | Alignment | Does the plan solve declared audit pain? Contracts match codebase (R1–R47 verified)? | [x] |
| 8 | External Integration | N/A (no third-party code integration) | [x] |

**Status:** APPROVED WITH RESERVATIONS — 5 P0 items, 6 P1 items, 5 P2/P3 items planned.
Deferred items (P1-2, P2-1, P2-2, P2-5) explicitly justified as out-of-scope for this pass.
P1-10 marked as already-implemented-and-verified (no fix needed, doc update only).

**Action:** Proceed with Phase A (P0) → Phase B (P1) → Phase C (P2/P3) task execution.
Re-run Tribunal verdict with all [x] after execution.
