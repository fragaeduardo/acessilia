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
