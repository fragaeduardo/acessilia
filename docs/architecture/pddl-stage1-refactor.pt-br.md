# Acessília — Etapa 1 (PDDL Orchestrator/Planner): fronteiras em migração

> Branch de trabalho: `refactor/etapa-1-orchestrator-planner` no fork
> `fragaeduardo/acessilia`. Base imutável da organização:
> `develop@06d145302fd02fb73946aac2c343ee43ab362bcf`.
> Este documento descreve **o que já foi implementado**; a Etapa 1 ainda
> não está finalizada.

## Contratos preservados

- A API `PddlAccessibilityOrchestrator.executar(...)` e `_build_plan(...)`
  continua no mesmo local. Os imports legados são reexportados como aliases.
- Não houve alteração nos `ProcessingManifest`, `NominalPlan`,
  `ExecutionReport` nem nos domínios `domain_v2.2.pddl`/`domain_v2.3.pddl`.
- Apenas o Core cria o plano. O `PlannerAgent` original continua sendo o
  único compilador/solucionador PDDL, inclusive em modo `both`.
- `media.inspect`, extração, classificação de slides, enriquecimentos de
  imagem/fórmula/tabela e construção do PDDL mantêm sua ordem.
- A única rota para capabilities remotas continua sendo a Toolbox do
  ambiente. A refatoração não adiciona fallback de renderização, extração ou
  matemáticas locais.
- `human-review` não significa sucesso automatizado: a política de entrega
  e escalonamento é a original.

## Fronteiras atuais

| Pacote/módulo | Responsabilidade | Estado |
| --- | --- | --- |
| `backend/agents/pddl_orchestrator.py` | Facade de execução, bootstrap e orquestração ainda não extraída | Parcial |
| `backend/core/planning/coordinator.py` | Um processo de síntese PDDL, catálogo de métodos ausentes, inspeção de propostas/bindings | Integrado |
| `backend/core/planning/hierarchy/proposals.py` | Propostas com evidência/origem/dependências derivadas do manifesto existente | Integrado em modo de observação |
| `backend/core/planning/hierarchy/global_planner.py` | Deduplicação, referências e conflitos determinísticos de propostas | Integrado em modo de observação |
| `backend/core/planning/hierarchy/bindings.py` | Binding do método planejado a handlers efetivamente registrados | Integrado |
| `backend/core/execution/recovery.py` | Replan, obrigações críticas, mensagens de pendências e escalonamento | Extraído |
| `backend/core/execution/handlers/common.py` | Lookup de obrigação, erros comuns | Extraído |
| `backend/core/execution/handlers/mathematics.py` | Handlers MathML/verbalização pela Toolbox | Extraído |
| `backend/core/execution/handlers/deterministic.py` | Handlers determinísticos de tabela/código/LaTeX/texto | Extraído |
| `backend/core/manifest/table_reading.py` | Parsing e admissibilidade de tabelas sem chamar providers | Extraído |
| `backend/core/manifest/element_geometry.py` | Geometria normalizada e papel estrutural de elementos | Extraído |
| `backend/core/manifest/payload.py` | Payload canônico, páginas, avisos e blocos | Extraído |
| `backend/core/manifest/payload_lists.py` | Recuperação de listas alfabéticas, callouts e agrupamento | Extraído |
| `backend/core/manifest/payload_text.py` | Serialização em texto e normalização de links de metadados | Extraído |
| `backend/core/manifest/preprocessing/media_triage.py` | media.inspect, apresentações e fallback de imagem inteira | Extraído |
| `backend/core/manifest/preprocessing/page_limits.py` | Limites de páginas com preflight verificado e pós-extração obrigatório | Extraído |
| `backend/core/manifest/preprocessing/visual_signals.py` | Heurísticas já existentes de ruído OCR/placeholder | Extraído |

### Fluxo efetivo

```text
Orchestrator.executar
  ├─ media_triage._inspect_media → extractor (Toolbox)
  ├─ enrichments antigos (imagem/fórmula/tabela; em migração)
  ├─ toolbox_gate.decide (catálogo da Toolbox local)
  ├─ Orchestrator._build_plan
  │    └─ coordinator.build_nominal_plan
  │         ├─ proposals_from_manifest → consolidate_proposals
  │         ├─ PlannerAgent.plan ou PlannerAgent.compare (PDDL original)
  │         └─ inspect_execution_bindings (MethodRegistry real)
  ├─ ExecutorAgent.execute (dry-run ou real)
  ├─ recovery para replan e pendências
  └─ payload.build_pddl_structured_payload → texto e metadados
```

## Testes de regressão

A branch possui um CI **somente de validação**, em
`.github/workflows/refactor-stage1.yml`, acionado em pushes para a branch
de trabalho. Não há job de publicação/deployment. O workflow instala as
dependências como o CI canônico, executa `compileall` e uma amostra relevante
de testes de manifesto, PDDL, execução, tabelas e compatibilidade.

Execução no checkout completo, depois de instalar as dependências:

```bash
python -m compileall -q backend tests
python -m pytest -q -m "not e2e" \
  tests/test_planning_proposals.py \
  tests/test_planning_coordinator.py \
  tests/test_pddl_modularization_contract.py \
  tests/test_toolbox_availability_gate.py \
  tests/test_pddl_planning.py \
  tests/test_pddl_execution_contract.py \
  tests/test_pddl_orchestrator.py \
  tests/test_pddl_payload_lists_and_callouts.py \
  tests/test_table_ast.py \
  tests/test_manifest_characterization.py
```

As projeções de evidências usam índice por elemento, e o grafo de dependências
é verificado iterativamente para documentos com muitas obrigações, sem alterar
os passos emitidos pelo PDDL. A projeção tolera repetição em arrays legados,
sem mudar o manifesto enviado ao `PlannerAgent`.

O CI deste fork agora inclui a suíte completa `not e2e` do Core e as suítes
das bibliotecas `docstruct` e SDK, após a suíte focada.

Os golden outputs de payload e planner devem continuar iguais ao baseline
quando nenhuma política muda. A suíte focada não substitui E2E com a
Toolbox/homologação nem benchmarking de modelos.

## Pendências da Etapa 1

- Caracterizar E2E de PDF, apresentação e entradas falhas com Toolbox da
  mesma versão, em dry-run e execução real (1A).
- Extrair de forma conservadora enrichments de imagem, fórmulas, tabelas,
  inspeção de regiões, métodos de fusão/adjudicação e a facade final (1B/1J).
  Vários testes fazem monkeypatch nos imports do orquestrador: preservar
  esses pontos de substituição em cada extração.
- Evoluir `PlanningProposal` de projeção somente leitura para fontes
  universais/especializadas **somente quando existirem regras reais**,
  sem duplicar as obrigações nem chamadas a modelos (1C/1D/1E).
- **Já cobertos:** `mathml` e `latex-verbalizer` exigem `math.convert` e
  `math.verbalize` (mesmo `TOOLBOX_MATH_PROVIDER` do SDK); fixtures do gate
  verificam catálogo indisponível e provider incompatível. **Pendente:**
  auditoria completa de `METHOD_TOOLBOX_REQUIREMENTS` e E2E de catálogo
  desatualizado nos modos `observe/enforce/off` (1F/1G).
- Confirmar com ambiente de homologação o isolamento PROD/HOMOL,
  budgets, contrato VLM/Toolbox e paridade completa dos domínios e
  metadados serializados (1H/1I).

Não avançar para etapas posteriores, merge ou PR sem esses gates.


---

## Auditoria de encerramento do código da Etapa 1 — branch isolada

**Base:** `A11yDevs/acessilia@develop:06d1453`.
**Destino:** `fragaeduardo/acessilia@refactor/etapa-1-orchestrator-planner`.
**Regra:** sem PR, merge, deploy ou mutações na organização.

### 1A–1J: código e provas

| Item | Implementação | Validação possível nesta branch |
| --- | --- | --- |
| 1A — caracterização | `test_stage1_golden_parity.py`; golden tests existentes de PDDL/manifesto | Core completo, sem E2E |
| 1B — extração | `core/orchestration/pipeline.py`, `manifest/preprocessing/*`, `manifest/payload*.py`, `execution/handlers/*` | todos os testes Core não E2E |
| 1C — propostas | `hierarchy/proposals.py`, `global_planner.py`; índice de evidências, deduplicação e DAG iterativo | `test_planning_proposals.py` |
| 1D — analisadores | `planning/universal/{structure,text,image,table,formula_code}.py` e `hierarchy/registry.py`; slots `specialists/registry.py` **inativos** sem detector/binding/validador | `test_planning_analyzer_registry.py` |
| 1E — PDDL único | `planning/coordinator.py` delega ao `PlannerAgent` existente, sem outro solver | `test_planning_coordinator.py`; golden |
| 1F — binding | `hierarchy/bindings.py` usa `MethodRegistry`, `TOOLBOX_CAPABILITIES`, provider e output schema reais | `test_execution_binding_contracts.py` |
| 1G — catálogo | `toolbox_gate.py` com `observe/enforce/off` incluindo `math.convert` e `math.verbalize` | `test_toolbox_availability_gate.py`; remoto ainda pendente |
| 1H — versões | domínios v2.2/v2.3 e `PlannerAgent` não alterados | hash e passos do plano comparados ao original |
| 1I — ambientes/VLM | SDK e settings continuam selecionando os endpoints da Toolbox configurada; módulos extraídos não criam rota direta ao servidor VLM | testes de configuração; isolamento real de PROD/HOMOL pendente |
| 1J — modularização | handlers de visão, Unicode, texto, inspeção de regiões, adjudicação VLM, fusão dual/single, pré-processamento e saída separados; facade mantém wrappers injetáveis | `test_pddl_modularization_contract.py` e Core completo |

### Estrutura de fronteiras relevantes

```text
backend/
├── agents/pddl_orchestrator.py           # somente facade, registro e compatibilidade
└── core/
    ├── orchestration/
    │   ├── pipeline.py                   # sequenciamento original
    │   └── reporting.py                  # progresso e disponibilidade
    ├── planning/
    │   ├── coordinator.py                # planner PDDL existente, único
    │   ├── hierarchy/                    # proposals, registry, global_planner, bindings
    │   ├── universal/                    # structure, text, image, table, formula_code
    │   └── specialists/                  # slots com gates, sem provider fictício
    ├── manifest/
    │   ├── preprocessing/                # triage, media, fórmulas, imagens, tabelas,
    │   │                                 # regiões, retry, page limit e sinais
    │   ├── table_reading.py              # AST e validação
    │   ├── element_geometry.py           # bbox, papéis e overlays
    │   └── payload*.py                   # blocos, listas, texto e saída canônica
    └── execution/
        ├── recovery.py                   # replan e obrigações
        └── handlers/                     # math, text, Unicode, region, vision,
                                          # fusion_execution e fusion_selection
```

### Evidência de regressão offline

Execução completa no fork, commit **`7f55f319c094a43be1258da0f0bd8ec0757e837e`**:

- 236 testes focados aprovados;
- 2.329 testes do Core aprovados (60 E2E desmarcados);
- 980 testes de `docstruct` aprovados;
- 266 testes do SDK aprovados.

Log: https://github.com/fragaeduardo/acessilia/actions/runs/41766668018

Esses resultados **não substituem homologação real**. O gate remoto é
`.github/workflows/refactor-stage1-homolog.yml`, acionado manualmente
no fork, com `ACESSILIA_HOMOLOG_TOOLBOX_URL` e
`ACESSILIA_HOMOLOG_TOOLBOX_KEY` configurados como GitHub Actions Secrets.
O workflow falha se o serviço não estiver acessível ou os testes forem
ignorados; nunca consulta PROD como alternativa.

**Critério para declarar 100% validada:** executar sem falhas o gate de
homologação contra o par Core/Toolbox e a VLM de homologação, além de
comparar resultados reais de PDF, apresentação e falhas com o baseline 0.2.0.
Antes disso, afirmar "100%" seria incorreto.

