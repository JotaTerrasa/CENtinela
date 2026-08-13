# Decisiones técnicas de CENtinela

## Resumen para CTO

CENtinela es un MVP ejecutable de inteligencia regulatoria con cuatro garantías
centrales: evidencia oficial trazable, flujo agéntico explícito, fallo seguro de
la evaluación y observabilidad económica por llamada. La arquitectura separa el
dominio regulatorio del proveedor de inferencia y permite ejecutar la misma
aplicación con Codex, OpenAI API, Ollama o vLLM.

La solución demuestra el recorrido funcional completo solicitado. No se declara
lista para alta disponibilidad: SQLite, Chroma embebido, autenticación local y
ejecución síncrona son elecciones deliberadas del MVP. El paso a producción
exige identidad corporativa, persistencia gestionada, trabajos asíncronos,
secret manager, evaluación dorada y operación formal del proveedor/modelo.

## Principios

1. Una afirmación regulatoria sin evidencia verificable no se publica.
2. El contenido externo es dato no confiable, nunca una instrucción.
3. El Judge no sustituye la validación determinista ni la revisión humana.
4. Cambiar de proveedor no cambia la topología, las citas ni el contrato de
   observabilidad.
5. Coste API, suscripción y cómputo propio son magnitudes distintas.
6. Los fallos y modos degradados deben ser visibles, no maquillados.

## ADR-001: LangGraph como orquestador

**Estado:** aceptada.

**Decisión.** Implementar una máquina de estados compilada con la topología:

```text
START -> Planner -> Scraper -> Executor -> Evaluator -> END
```

**Motivación.** El problema no es una única conversación, sino un workflow con
estado, efectos laterales, rutas de fallo y una barrera de calidad. LangGraph
hace explícitos los nodos y las transiciones, permite inyectar clientes en tests
y deja un punto natural de evolución a checkpoints y workers.

**Controles.**

- No existen aristas ocultas ni bucles abiertos.
- En Codex, Planner y filtro son deterministas para evitar procesos CLI sobre
  decisiones fijas. OpenAI/Ollama/vLLM ejecutan esos roles con su modelo barato
  y conservan el mismo resultado determinista como fallback validado.
- Executor puede efectuar una única revisión si falla la barrera de citas.
- Evaluator puede sustituir un borrador rechazado por una versión extractiva y
  evaluarla una única vez.
- Un Judge fallido o no aprobatorio impide guardar el informe como completado.

**Alternativas descartadas.** Una cadena lineal simple reduce dependencias, pero
oculta mejor los estados intermedios y dificulta reintentos e instrumentación.
Un agente autónomo abierto añade flexibilidad a costa de control y
reproducibilidad, inadecuado para inteligencia regulatoria.

## ADR-002: contrato multiproveedor

**Estado:** aceptada; sustituye la decisión inicial Codex-only.

**Decisión.** Los consumidores dependen de un contrato estructural común:

```python
invoke(prompt, config=None, output_schema=None, model=None,
       reasoning_effort=None, timeout_seconds=None)
invoke_json(prompt, output_schema, **kwargs)
```

El resultado normaliza texto, JSON, modelo, identificador, latencia, metadata y
uso. La factoría selecciona:

- `CodexClient`, mediante `codex exec` y eventos JSONL;
- `OpenAIResponsesClient`, mediante Responses API;
- `OpenAICompatibleChatClient`, mediante Chat Completions para Ollama/vLLM;
- `OpenAICompatibleEmbeddings`, mediante `/v1/embeddings`.

**Por qué dos superficies HTTP.** Responses API es la interfaz nativa recomendada
para OpenAI y conserva razonamiento, uso y JSON Schema. Chat Completions tiene
mayor compatibilidad efectiva entre runtimes abiertos. Forzar una única
superficie habría reducido portabilidad o desaprovechado la API nativa.

**Seguridad de configuración.** Las URLs deben ser HTTP(S) absolutas y no pueden
incluir credenciales, query ni fragment. Las claves son `SecretStr`, se revelan
solo al construir el cliente y se excluyen de `public_dict`, errores y trazas.

**Límite.** “OpenAI-compatible” no garantiza equivalencia total. El contrato se
prueba, pero cada combinación servidor/modelo debe validar JSON Schema,
razonamiento y campos `usage`. Un único servidor vLLM suele cargar un modelo; un
gateway multimodelo o endpoints por rol son responsabilidad del despliegue.

## ADR-003: routing de modelos por valor

**Estado:** aceptada.

**Decisión.** Separar proveedor y modelo por rol. `AI_PROVIDER` es el default y
los overrides `*_PROVIDER` permiten routing híbrido.

| Rol | Codex | OpenAI API | Perfil abierto inicial |
|---|---|---|---|
| Planner/filtro/RAG | Luna | GPT-4o mini | Qwen3.5 9B |
| Informe | Sol | GPT-4o | Qwen3.5 9B demo; 27B candidato |
| Judge | Terra | GPT-4o mini | Qwen3.5 9B demo; Mistral Small 3.1 candidato |

El enunciado exige GPT-4o mini para planificación/filtrado y GPT-4o para la
redacción final en la ruta API; esos defaults están preservados. Codex utiliza
los tiers Luna/Terra/Sol equivalentes por valor. Ollama usa un solo modelo en la
demo para evitar cargar varios pesos; producción debe decidir por benchmark.

**Regla de promoción.** Un modelo abierto no se aprueba porque responda. Debe
superar un conjunto dorado chileno en:

- porcentaje de afirmaciones con cita válida;
- afirmaciones no respaldadas y severidad;
- cobertura de activos y organismos;
- JSON válido y estabilidad del Judge;
- Recall@k/nDCG del RAG;
- latencia p50/p95 y throughput;
- tokens, VRAM pico y coste amortizado por informe.

**Riesgo de sesgo.** Usar el mismo modelo como Executor y Judge reduce diversidad
de evaluación. Se acepta en el perfil Ollama de demo por capacidad; producción
debe probar un Judge distinto o un ensemble con controles deterministas.

## ADR-004: RAG local por defecto y embeddings intercambiables

**Estado:** aceptada.

**Decisión.** Mantener `local_hash` como default reproducible y permitir
embeddings OpenAI-compatible. ChromaDB persiste el vector y metadata de
procedencia. La identidad del espacio incluye proveedor/modelo; una versión
distinta fuerza reindexación.

**Ventajas de `local_hash`.** Cero secretos, descargas y coste remoto; alta
reproducibilidad; rendimiento razonable para vocabulario regulatorio exacto.

**Límites.** No captura toda la similitud semántica. Los embeddings neuronales
pueden mejorar paráfrasis, pero requieren recursos, gobierno de modelo y una
evaluación Recall@k. `qwen3-embedding:0.6b` es un candidato, no un resultado de
benchmark afirmado.

**Trazabilidad.** El modelo estructurado devuelve `source_ids`; la aplicación
construye las citas desde el catálogo. La URL no se acepta como texto libre del
modelo.

## ADR-005: observabilidad y tokenomics

**Estado:** aceptada.

**Decisión.** Un callback LangChain registra exactamente los contadores
reportados por el backend:

```text
prompt_tokens
completion_tokens
latency_seconds
model
provider
billing_mode
```

Para una tarifa de entrada `Pi` y salida `Po`, expresada en USD por millón:

```text
cost_usd = prompt_tokens / 1_000_000 * Pi
         + completion_tokens / 1_000_000 * Po
cost_clp = cost_usd * 940
```

No se estiman tokens por longitud. Si el proveedor no entrega `usage`, se
registra cero y se marca `token_usage_status=not_reported` para diagnóstico. Los
embeddings HTTP emiten sus tokens de entrada por lote; para
`text-embedding-3-small` se aplica el precio oficial configurado.

### Semántica económica

| Modo | `cost_usd` | Interpretación |
|---|---:|---|
| OpenAI API | calculado | tarifa por tokens atribuible |
| Codex | `0` en esquema | coste por llamada N/A, incluido en cuota/suscripción |
| Ollama/vLLM | `0` API | no hay tarifa API; existe coste de infraestructura |

`SELF_HOSTED_COMPUTE_USD_PER_HOUR` permite una estimación por tiempo de llamada.
Se guarda en metadata como coste de cómputo estimado y nunca se suma al coste API
exacto. No incluye necesariamente ociosidad, energía, almacenamiento ni
overhead; producción debe obtenerlos de la plataforma cloud.

**Pricing.** GPT-4o mini y GPT-4o tienen defaults explícitos para cumplir la
prueba. Nuevos modelos o tarifas deben versionarse y reconciliarse; un alias sin
precio no rompe una respuesta válida, pero queda `pricing_status=unknown`.

## ADR-006: citas y LLM-as-Judge fail-closed

**Estado:** aceptada.

Cada afirmación material debe terminar en la misma línea con
`[Fuente | URL]`. La validación local comprueba existencia de cita, URL
canonicalizada y pertenencia al catálogo. El Judge evalúa relevancia, cobertura,
claridad, respaldo y trazabilidad, pero no puede convertir una cita localmente
inválida en válida.

Un informe se persiste como completado solo cuando:

1. existe evidencia normalizada;
2. las citas superan la barrera determinista;
3. el Judge devuelve una evaluación estructurada;
4. `approved=true` y el score alcanza el umbral.

La cita acredita procedencia, no validez jurídica de la interpretación. La
distribución externa sigue requiriendo revisión de un especialista.

## ADR-007: scraping resiliente y sin evidencia sintética

**Estado:** aceptada.

Cada organismo tiene adaptadores HTML/RSS tolerantes a cambios y límites por
fuente. Los fallos se aíslan, se registran y no cancelan necesariamente el resto.
El proxy de lectura `r.jina.ai` solo recupera la misma URL pública; se marca como
fallback y la cita conserva el origen oficial.

No existen noticias simuladas en el recorrido vivo. Si no hay evidencia, el
estado lo muestra y el informe no inventa cobertura.

**Límite.** Un scraper HTML requiere mantenimiento ante rediseños, rate limits,
robots y cambios contractuales. Producción debe priorizar APIs/RSS oficiales,
guardar artefactos de captura y añadir observabilidad por petición.

## ADR-008: persistencia y autenticación del MVP

**Estado:** aceptada con deuda explícita.

SQLite y Chroma embebido permiten una réplica de una sola instancia sin
servicios gestionados. El login local usa PBKDF2 con sal, comparación constante
y aislamiento de registros por usuario.

No son decisiones de producción multi-réplica. El objetivo productivo es:

- PostgreSQL con migraciones, backups y point-in-time recovery;
- vector store con HA o reconstrucción probada desde documentos;
- SSO/OIDC, MFA, RBAC y provisión/desprovisión corporativa;
- secret manager y rotación;
- auditoría inmutable y política de retención.

La sesión Codex es distinta del usuario Streamlit. Permite la demo individual,
pero no debe utilizarse como identidad humana compartida de un servicio cloud.

## ADR-009: Docker y perfiles de despliegue

**Estado:** aceptada.

La imagen única incluye el CLI Codex y el SDK OpenAI. El servicio se ejecuta como
UID no privilegiado, filesystem raíz de solo lectura, capabilities eliminadas,
`no-new-privileges`, límite de procesos y `tmpfs` acotado.

`docker-compose.yml` conserva Codex. `docker-compose.ollama.yml` añade servidor,
health check, bootstrap deduplicado y volumen de pesos; el override NVIDIA es
separado. Ollama se enlaza solo a loopback y nunca debe exponerse directamente.

El bootstrap dinámico de pesos es adecuado para desarrollo, no para producción.
Cloud debe fijar imagen y revisión por digest, verificar licencia/SBOM y obtener
pesos desde un registro aprobado antes de servir tráfico.

## Estrategia de costes

1. Mantener Planner/filtro deterministas en Codex; en API/self-hosted usar el
   modelo barato exigido y fallar hacia el contrato determinista.
2. Usar modelos pequeños para alto volumen y reservar el modelo de mayor calidad
   para la síntesis que llega al analista.
3. Limitar documentos, caracteres, `top_k`, revisiones y tokens de salida.
4. Reutilizar snapshots recientes e indexar solo versiones documentales nuevas.
5. Medir por llamada y por informe, no solo por mes.
6. Comparar TCO self-hosted con API incluyendo utilización de GPU e inactividad.
7. Definir budgets y alertas por tenant antes de habilitar tráfico productivo.

## Pruebas y evidencia

La suite cubre configuración, secretos, callbacks, pricing, Codex CLI,
Responses API, Chat Completions, embeddings, grafo, Judge fail-closed, citas,
RAG, base de datos, parsers y helpers del frontend. Los contratos HTTP usan
dobles y no consumen cuota. Docker Compose se valida para los perfiles Codex,
Ollama CPU y Ollama NVIDIA.

`docs/demo/` contiene capturas y una ejecución Codex aprobada. Demuestra la ruta
Codex, no la equivalencia de calidad de los modelos abiertos. Esa comparación
requiere el benchmark dorado y hardware representativo.

## Riesgos abiertos

| Riesgo | Mitigación actual | Acción de producción |
|---|---|---|
| cambio de HTML oficial | parsers tolerantes y fallo por fuente | contratos/API, snapshots y alertas |
| prompt injection documental | delimitación y allowlist de citas | filtros, evaluación adversarial |
| alucinación con cita real | extracto visible y Judge | revisión humana y benchmark de grounding |
| Judge correlacionado | reglas deterministas | modelo/ensemble independiente |
| indisponibilidad LLM | health y estado degradado | cola, retry con jitter y fallback gobernado |
| endpoint Ollama expuesto | loopback | gateway, mTLS y NetworkPolicy |
| saturación GPU/KV cache | concurrencia conservadora | load test y autoscaling por cola/TTFT |
| secreto en logs | `SecretStr` y sanitización | DLP, redacción central y auditoría |
| SQLite multi-réplica | una instancia | PostgreSQL y migraciones |
| autenticación local | hash robusto | SSO/OIDC, MFA y RBAC |

## Roadmap a producción

### Fase 1 — piloto controlado

- Crear corpus dorado y umbrales de promoción.
- Ejecutar Ollama en una instancia con límites de concurrencia.
- Medir calidad, latencia, RAM/VRAM y coste por informe.
- Completar runbooks de modelos, fuentes y recuperación.

### Fase 2 — plataforma cloud

- Separar UI, workers de scraping y workers de generación mediante cola.
- Migrar a PostgreSQL y vector store con backup.
- Implantar SSO/OIDC, RBAC y secret manager.
- Servir vLLM tras gateway privado con TLS, cuotas y límites.
- Añadir OpenTelemetry, métricas de GPU, TTFT, p95 y alertas.

### Fase 3 — gobierno y resiliencia

- Evaluación continua en sombra y canary por versión de modelo/prompt.
- Registro de modelos, licencias, SBOM, firma y rollback.
- Auditoría inmutable, retención y clasificación de datos.
- Multi-AZ, pruebas de carga, restauración y objetivos RTO/RPO.
- Human-in-the-loop formal antes de distribuir un informe.

### SLO inicial propuesto para piloto

- frontend disponible al 99,5 % mensual;
- RAG p95 inferior a 15 s con modelo caliente;
- informe p95 inferior a 120 s;
- 100 % de afirmaciones materiales con cita localmente válida;
- JSON inválido inferior al 0,5 %;
- cero secretos en logs y artefactos.

Estos valores son hipótesis operativas y deben calibrarse con carga real.

## Conclusión

La arquitectura cumple el objetivo de la prueba sin encerrar el producto en un
único proveedor. Codex proporciona una demo de alta calidad con la cuenta del
operador; OpenAI API ofrece una ruta gestionada y facturable; Ollama facilita
portabilidad; vLLM permite una evolución cloud privada. La ventaja sostenible no
es el nombre del modelo, sino el contrato de evidencia, las barreras de calidad,
la observabilidad y la capacidad de comparar proveedores con la misma prueba.
