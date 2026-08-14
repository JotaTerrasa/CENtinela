# Matriz de cumplimiento de la prueba técnica

## Alcance y criterio de lectura

Esta matriz traza el enunciado oficial entregado en
`Prueba técnica_AI Arquitect.pdf` contra el código y los artefactos del
repositorio. No interpreta como requisito la arquitectura objetivo de
producción: solo marca como cumplido aquello que puede verificarse en la entrega.

| Estado | Criterio |
|---|---|
| **Cumplido** | Implementación y evidencia localizables en el repositorio. |
| **Cumplido en modo interactivo** | Funciona al ejecutar la aplicación con persistencia y proveedor; la demo cloud solo reproduce evidencia histórica. |
| **Parcial** | Se cubre el núcleo del requisito, con una limitación material declarada. |
| **Extra** | Capacidad valorable, pero no exigible por el enunciado. |

## 1. Funcionalidades requeridas

| ID | Requisito del PDF | Estado | Implementación | Evidencia verificable / límite |
|---|---|---|---|---|
| F-01 | Cubrir como mínimo CEN, CNE, Ministerio de Energía, SEC, SEA y tramitación legislativa de Senado/Cámara. | **Cumplido** | Registro de siete conectores con URLs, parsers, categorías y allowlist de hosts. | [`scrapers/chile_regulatory.py`](scrapers/chile_regulatory.py), `SOURCE_REGISTRY`; pruebas en [`tests/test_scraper_parsers.py`](tests/test_scraper_parsers.py) y [`tests/test_scraper_fallback.py`](tests/test_scraper_fallback.py). |
| F-02 | Fuentes escalables y contrastadas. | **Parcial** | Adaptadores encapsulados, captura concurrente, deduplicación, aislamiento de fallos y cobertura multi-organismo. | Cubre las siete fuentes mínimas del PDF. La ampliación productiva debe priorizar Diario Oficial, BCN/LeyChile, Panel de Expertos, SMA y otras fuentes sectoriales; tampoco existe todavía un motor semántico de contraste entre organismos. |
| F-03 | Dashboard con insights relevantes para el negocio. | **Cumplido** | Radar, lectura ejecutiva, publicaciones/citas, coincidencias de alertas, cobertura y tabla con enlaces. | [`app.py`](app.py), `render_dashboard`; capturas en [`docs/demo/screenshots/02-dashboard.png`](docs/demo/screenshots/02-dashboard.png). |
| F-04 | Panel de alertas personalizables por usuario y palabras clave. | **Cumplido en modo interactivo** | CRUD por usuario, multiselect de palabras clave y fuentes; las reglas priorizan evidencia e informe. | [`app.py`](app.py), `render_alerts`; [`core/database.py`](core/database.py), tabla `alerts`. El replay cloud muestra una simulación UI no persistente. |
| F-05 | Chat RAG ad-hoc en lenguaje natural con citas. | **Cumplido en modo interactivo** | Chroma, recuperación híbrida, respuesta estructurada por `source_ids`, construcción local de citas y fallback extractivo. | [`rag/vector_engine.py`](rag/vector_engine.py); pruebas en [`tests/test_rag_vector_engine.py`](tests/test_rag_vector_engine.py). La consulta nueva está bloqueada en el replay público. |
| F-06 | Método de autenticación de usuarios. | **Cumplido en modo interactivo** | Registro/login local, PBKDF2-HMAC-SHA256 con sal, comparación constante y aislamiento por usuario. | [`core/database.py`](core/database.py) y `render_auth` en [`app.py`](app.py). Es autenticación de MVP, no SSO/OIDC. |
| F-07 | Información contrastable y trazabilidad de fuentes. | **Cumplido** | Fuente, URL original, fecha, extracto, hashes y metadata se conservan desde captura hasta informe/RAG; las URLs se validan contra catálogo. | [`agent/tools.py`](agent/tools.py), `validate_report_citations`; [`rag/vector_engine.py`](rag/vector_engine.py). Una cita acredita procedencia, no corrección jurídica. |
| F-08 | Optimizar recursos y tokens/créditos. | **Cumplido** | Routing por rol, plan/filtro deterministas en Codex, snapshot de 30 minutos, límites de evidencia, `top_k`, embeddings locales y revisiones acotadas. | [`agent/graph.py`](agent/graph.py), [`core/config.py`](core/config.py), [`DECISIONES_TECNICAS.md`](DECISIONES_TECNICAS.md). La optimización debe recalibrarse con carga real. |

## 2. Alcance del MVP esperado

| ID | Requisito del PDF | Estado | Implementación y evidencia |
|---|---|---|---|
| M-01 | Plataforma funcionando con las funcionalidades requeridas y datos reales del sector chileno. | **Cumplido en modo interactivo** | El scraper consume fuentes públicas reales y el flujo persiste/indexa el resultado. [`docs/demo/validation-summary.json`](docs/demo/validation-summary.json) conserva una aceptación con siete fuentes recuperadas. La URL pública es un replay histórico, no captura en vivo. |
| M-02 | Dashboard, panel de alertas y Chat de IA. | **Cumplido** | Seis vistas integradas en [`app.py`](app.py); evidencia visual en [`docs/demo/screenshots/`](docs/demo/screenshots/). |
| M-03 | Orquestación multi-paso con framework justificado. | **Cumplido** | LangGraph compila `START -> planner -> scraper -> executor -> evaluator -> END`. | [`agent/graph.py`](agent/graph.py), `build_graph`; justificación en [`DECISIONES_TECNICAS.md`](DECISIONES_TECNICAS.md). |
| M-04 | Reportes descargables. | **Cumplido** | Descarga autocontenida en Markdown y JSON; escritura atómica de artefactos en el perfil interactivo. | `build_report_exports` en [`app.py`](app.py) y `_persist_report_artifacts` en [`agent/graph.py`](agent/graph.py). |
| M-05 | Observabilidad por reporte: pasos, llamadas, tokens y coste USD/CLP. | **Parcial** | Tablas `executions`, `execution_steps`, `llm_calls`; callback por llamada LLM y panel de detalle. | Tokens, latencia y coste del modelo son exactos cuando el backend informa `usage`. Los errores de scraping se registran por fuente, pero cada petición HTTP todavía no es una `tool_call` de primera clase con latencia y estado propios. |

## 3. Extras valorables

| ID | Extra del PDF | Estado | Implementación y evidencia |
|---|---|---|---|
| E-01 | Evaluación automática de calidad / LLM-as-Judge. | **Extra implementado** | Baseline determinista, Judge estructurado, score mínimo 70, una revisión acotada y rechazo fail-closed. [`agent/graph.py`](agent/graph.py), `evaluator_node`; [`agent/tools.py`](agent/tools.py), `deterministic_judgement`. |
| E-02 | Patrón Planner-Executor. | **Extra implementado** | Planificación, adquisición, ejecución y evaluación como nodos explícitos. [`ARQUITECTURA.md`](ARQUITECTURA.md). |
| E-03 | Memoria entre ejecuciones. | **Extra implementado** | `daily_memory` por usuario; el informe anterior se añade como contexto comparativo solo tras una aprobación. [`core/database.py`](core/database.py) y `run_daily_report` en [`agent/graph.py`](agent/graph.py). |
| E-04 | Funcionalidades adicionales relevantes. | **Extra implementado** | Multiproveedor, embeddings intercambiables, replay público sin secretos, Docker endurecido, evaluación de proveedor y evidencia de aceptación. [`STACK_TECNOLOGICO.md`](STACK_TECNOLOGICO.md), [`scripts/evaluate_provider.py`](scripts/evaluate_provider.py), [`docs/demo/`](docs/demo/). |

## 4. Pasos de trabajo solicitados

| Paso del PDF | Estado | Entregable |
|---|---|---|
| Diseñar flujo, pasos, herramientas, decisiones y condiciones. | **Cumplido** | Diagramas de contexto, componentes, secuencia, RAG, observabilidad y despliegue en [`ARQUITECTURA.md`](ARQUITECTURA.md). |
| Implementar herramientas contra fuentes públicas chilenas. | **Cumplido** | [`scrapers/chile_regulatory.py`](scrapers/chile_regulatory.py), sin noticias sintéticas en la ruta viva. |
| Componer fuentes, dashboard, alertas y chat. | **Cumplido** | [`app.py`](app.py), [`agent/`](agent/) y [`rag/`](rag/). |
| Instrumentar citas, tokens y coste. | **Cumplido** | [`core/observability.py`](core/observability.py), [`agent/tools.py`](agent/tools.py) y panel de observabilidad. |
| Redactar decisiones técnicas. | **Cumplido** | [`DECISIONES_TECNICAS.md`](DECISIONES_TECNICAS.md), [`CLOUD_ARCHITECTURE.md`](CLOUD_ARCHITECTURE.md), [`SECURITY.md`](SECURITY.md) y [`STACK_TECNOLOGICO.md`](STACK_TECNOLOGICO.md). |

## 5. Formato de entrega

| Requisito del PDF | Estado | Evidencia |
|---|---|---|
| Repositorio GitHub con código completo y organizado. | **Cumplido** | [JotaTerrasa/CENtinela](https://github.com/JotaTerrasa/CENtinela) y estructura modular documentada en [`README.md`](README.md). |
| Diagrama de arquitectura. | **Cumplido** | Siete diagramas Mermaid, incluidas las topologías actual y objetivo, en [`ARQUITECTURA.md`](ARQUITECTURA.md). |
| README con variables, instalación y ejecución. | **Cumplido** | [`README.md`](README.md), [`.env.example`](.env.example), [`Makefile`](Makefile) y perfiles Compose. |
| Documento de decisiones con límites y cambios para producción. | **Cumplido** | [`DECISIONES_TECNICAS.md`](DECISIONES_TECNICAS.md) y la separación actual/objetivo en [`ARQUITECTURA.md`](ARQUITECTURA.md). |
| Enlace o instrucciones para visualizar la interfaz. | **Cumplido** | [Demo pública en Streamlit](https://centinela-regulatory.streamlit.app/?embed=true) y ejecución local descrita en [`README.md`](README.md). |

## 6. Consideraciones finales del enunciado

| Consideración | Estado | Evidencia / declaración |
|---|---|---|
| Uso de IA permitido, indicando cuáles y cómo. | **Cumplido** | [`AI_USAGE.md`](AI_USAGE.md) declara Codex como herramienta de ingeniería y los proveedores usados dentro del producto. |
| Trabajar únicamente con datos públicos reales. | **Cumplido** | Las fuentes vivas son portales oficiales; el fallback recupera la misma URL pública. El replay se etiqueta como artefacto histórico y no se presenta como dato en vivo. |
| Coherencia, razonamiento y claridad sobre límites y fallos reales. | **Cumplido** | Límites en [`ARQUITECTURA.md`](ARQUITECTURA.md), riesgos en [`DECISIONES_TECNICAS.md`](DECISIONES_TECNICAS.md) y amenazas en [`SECURITY.md`](SECURITY.md). |
| Defensa presencial con demostración en vivo. | **Preparado** | [`GUION_DEMO.md`](GUION_DEMO.md), [`DEFENSA_CTO.md`](DEFENSA_CTO.md), [`CHECKLIST_ENTREGA.md`](CHECKLIST_ENTREGA.md) y vídeo incluido en la release. |

## 7. Cobertura de la rúbrica

| Criterio de evaluación | Peso en el PDF | Evidencia principal |
|---|---:|---|
| Funcionamiento del MVP | 30 % | Aplicación, Docker, demo pública, capturas y suite automatizada. |
| Criterio técnico y trade-offs | 30 % | LangGraph, routing, tokenomics, ADRs, límites y roadmap. |
| Calidad de ingeniería | 15 % | Modularidad, tests, CI, lint, auditoría de dependencias y hardening. |
| Comunicación | 15 % | README, arquitectura, stack, matriz, guion y defensa. |
| Visión de producto | 10 % | Alertas por usuario, memoria, multiproveedor y evolución cloud gobernada. |

## 8. Requisitos adicionales fijados para esta implementación

Estos controles proceden del alcance acordado para construir CENtinela y no se
atribuyen literalmente al PDF:

| Control | Estado | Evidencia |
|---|---|---|
| `gpt-4o-mini` para planificación/filtrado y `gpt-4o` para informe en OpenAI. | **Cumplido** | Defaults de [`core/config.py`](core/config.py) y tabla de routing en [`STACK_TECNOLOGICO.md`](STACK_TECNOLOGICO.md). |
| Callback LangChain con prompt/completion tokens y coste USD/CLP a 940. | **Cumplido** | `CostTrackingCallback` y `calculate_cost` en [`core/observability.py`](core/observability.py). |
| Flujo exacto Planner, Scraper, Executor, LLM-as-Judge, END. | **Cumplido** | Aristas compiladas en [`agent/graph.py`](agent/graph.py). |
| Cada afirmación material con `[Fuente | URL]`. | **Cumplido** | `validate_report_citations` y barrera fail-closed en [`agent/tools.py`](agent/tools.py). |

## Conclusión de cumplimiento

El repositorio cubre el alcance funcional y de ingeniería del MVP. Las
salvedades relevantes están declaradas: la demo pública es un replay de solo
lectura; el inventario vivo cubre las siete fuentes mínimas, pero no todas las
extensiones productivas identificadas; y las peticiones HTTP de scraping no son
aún spans de herramienta individuales. La transición a producción requiere
identidad corporativa, ejecución asíncrona, datos gestionados, observabilidad
externa y operación gobernada de modelos; ninguno de esos elementos se presenta
como ya desplegado.
