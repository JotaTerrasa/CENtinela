# Stack tecnológico de CENtinela

## Resumen para CTO

CENtinela está construido como una aplicación Python 3.12 modular, con
Streamlit en la capa de experiencia, LangGraph para el workflow agéntico,
SQLite y ChromaDB para persistencia local y una factoría que desacopla el
producto de Codex, OpenAI API, Ollama y vLLM. El stack prioriza una réplica rápida
del MVP, trazabilidad de evidencia y coste operativo visible.

Las versiones que aparecen como `==` son las dependencias directas fijadas en
[`requirements.txt`](requirements.txt) y
[`requirements-dev.txt`](requirements-dev.txt). No existe todavía un lockfile
con el grafo transitivo completo; para producción se recomienda generar y firmar
uno, acompañarlo de SBOM y promover imágenes por digest.

La arquitectura y el estado real de cada capacidad se documentan en
[ARQUITECTURA.md](ARQUITECTURA.md) y
[MATRIZ_CUMPLIMIENTO.md](MATRIZ_CUMPLIMIENTO.md).

## 1. Vista del stack por capas

| Capa | Tecnología implementada | Responsabilidad |
|---|---|---|
| Experiencia | Streamlit 1.61.1, pandas 2.3.3 | Login, dashboard, alertas, informe, Chat RAG, métricas y arquitectura. |
| Dominio | Python 3.12 | Reglas regulatorias, normalización, citas, fallbacks y contratos tipados. |
| Orquestación | LangGraph 1.2.11 | `Planner -> Scraper -> Executor -> Evaluator`, con estado y barreras explícitas. |
| Integración LLM | Codex CLI, OpenAI SDK 2.54.0 y contratos OpenAI-compatible | Generación, Judge, filtrado opcional, RAG y embeddings remotos. |
| RAG | ChromaDB 0.6.3 y embeddings hash locales de 1.536 dimensiones | Indexación persistente, búsqueda coseno, señal léxica y procedencia. |
| Datos transaccionales | SQLite mediante `sqlite3` de Python | Usuarios, noticias, alertas, ejecuciones, pasos, llamadas, informes y memoria. |
| Ingesta | Requests, Beautiful Soup, Feedparser y python-dateutil | HTTP, HTML, RSS/Atom, sitemaps, fechas y normalización de fuentes públicas. |
| Configuración | Pydantic 2.13.4 y pydantic-settings 2.15.0 | Variables de entorno tipadas, secretos, validación de rutas, modelos y endpoints. |
| Observabilidad | Callback de LangChain y SQLite | Tokens reportados, latencia, modelo, estado, coste USD y conversión CLP. |
| Empaquetado | Docker y Docker Compose | Imagen no privilegiada, volúmenes y perfiles Codex/Ollama CPU/GPU. |
| Calidad | pytest, Ruff, pip-audit, compileall y GitHub Actions | Pruebas, estilo, vulnerabilidades, compilación y validación de Compose. |

## 2. Dependencias directas de runtime

| Componente | Versión fijada | Uso efectivo en CENtinela | Razón de elección | Evolución recomendada |
|---|---:|---|---|---|
| `streamlit` | `1.61.1` | Aplicación web multipágina en [`app.py`](app.py) | Permite entregar y demostrar el flujo completo sin mantener un frontend separado. | Separar API y frontend si aumentan concurrencia, control de sesión o requisitos de accesibilidad. |
| `pandas` | `2.3.3` | Tablas de noticias, métricas y normalización de fechas en UI | Adecuado para el volumen del MVP y su presentación tabular. | Ejecutar agregaciones operativas en PostgreSQL/warehouse cuando crezca el corpus. |
| `pydantic` | `2.13.4` | Modelos de pricing, configuración y validadores | Contratos tipados y validación temprana de configuración insegura. | Mantener; añadir versionado formal de esquemas de eventos y APIs. |
| `pydantic-settings` | `2.15.0` | Carga de `.env` y variables de entorno | Centraliza configuración por entorno y usa `SecretStr`. | Integrar secret manager mediante identidad de workload. |
| `langchain-core` | `1.5.4` | Interfaces de callback y `LLMResult` | Se usa la capa mínima necesaria para observabilidad compatible; no se depende de cadenas opacas. | Mantener el contrato estrecho o migrar a OpenTelemetry si se desacopla LangChain. |
| `langgraph` | `1.2.11` | Máquina de estados compilada del informe diario | Topología explícita, testeable y preparada para checkpoints futuros. | Añadir checkpointer persistente y ejecución asíncrona por workers. |
| `openai` | `2.54.0` | Responses API, Chat Completions y embeddings | Un SDK soporta OpenAI y endpoints compatibles con contratos normalizados. | Encapsular tras gateway y probar cada actualización contra el conjunto dorado. |
| `chromadb` | `0.6.3` | `PersistentClient` embebido y búsqueda HNSW coseno | Réplica local sin servicio adicional y metadata de procedencia por chunk. | Vector store gestionado o Chroma separado, con backup/restauración y métricas. |
| `posthog` | `5.4.0` | Compatibilidad de la dependencia Chroma 0.6.3 | Evita una incompatibilidad de firma; la telemetría anónima de Chroma está desactivada. | Eliminar del runtime si la evolución de Chroma deja de requerirlo. |
| `openai-codex` | `0.144.4` | Proporciona el binario Codex empaquetado en la imagen | Hace reproducible el perfil Codex cuando no existe un binario en `PATH`. | No usar una sesión humana como identidad de servicio; elegir API o gateway con credenciales de workload. |
| `requests` | `2.34.2` | Sesión HTTP, pooling, timeouts y adaptadores de reintento | Cliente síncrono robusto y conocido para siete fuentes de bajo volumen. | Workers asíncronos o conectores oficiales si la frecuencia y el número de fuentes crecen. |
| `beautifulsoup4` | `4.15.0` | Parsers HTML por organismo | Tolerancia práctica a HTML heterogéneo de portales públicos. | Priorizar APIs/RSS contractuales y añadir snapshots de regresión por fuente. |
| `feedparser` | `6.0.14` | RSS y Atom | Reduce lógica propia para canales oficiales estructurados. | Mantener donde el feed sea estable y monitorizar cambios de esquema. |
| `python-dateutil` | `2.9.0.post0` | Fechas regulatorias heterogéneas | Parsing flexible con normalización posterior a UTC/Chile. | Reglas estrictas por fuente cuando existan contratos de fecha estables. |
| `tenacity` | `9.1.4` | Dependencia declarada, sin importación directa en el runtime actual | Quedó disponible para resiliencia; hoy Requests/urllib3 y el SDK aplican sus propias políticas acotadas. | Eliminarla si sigue sin uso o reservarla para retries de workers con política central y jitter. |

### Decisión específica de ChromaDB

`chromadb==0.6.3` se mantiene embebido y no se expone como servidor. La versión
queda fuera del rango vulnerable documentado en el repositorio para
CVE-2026-45829. Además, `anonymized_telemetry=False` y un volumen específico
evitan abrir o modificar silenciosamente índices legacy incompatibles. La
decisión y sus límites están desarrollados en [SECURITY.md](SECURITY.md).

## 3. Herramientas de desarrollo y calidad

| Componente | Versión/revisión | Función en el repositorio | Recomendación de producción |
|---|---:|---|---|
| Python | `3.12` | Runtime de Docker y CI; Ruff usa target `py312`. | Fijar patch y digest de imagen, con calendario de actualización. |
| `pytest` | `9.0.3` | Suite unitaria, integración con dobles y pruebas de frontend. | Añadir pruebas de carga, restauración y evaluación regulatoria continua. |
| `ruff` | `0.15.4` | Lint con ancho de línea 100. | Mantener como gate obligatorio. |
| `pip-audit` | `2.10.0` | Auditoría de dependencias Python directas/transitivas instaladas. | Complementar con SBOM, firma y escaneo de imagen. |
| `compileall` | Python 3.12 | Verificación sintáctica de módulos. | Mantener como gate rápido, no como sustituto de tests. |
| GitHub Actions | `checkout@v4`, `setup-python@v5` | CI en push y pull request. | Fijar actions por SHA y aplicar permisos mínimos. |
| Docker Compose | Versión aportada por el host | Desarrollo y validación de tres perfiles. | Sustituir en cloud por plataforma orquestada e IaC versionada. |

La pipeline en [`.github/workflows/ci.yml`](.github/workflows/ci.yml) instala
`requirements-dev.txt`, compila, ejecuta Ruff, `pip-audit`, pytest y valida los
perfiles Compose base, Ollama CPU y Ollama NVIDIA.

## 4. Sistema operativo, contenedores y plataforma

| Elemento | Fijación actual | Perfil runtime | Controles actuales | Alternativa de producción |
|---|---|---|---|---|
| Imagen Python | `python:3.12-slim` | Todos los perfiles Docker | UID `10001`, health check HTTP y directorios con permisos explícitos | Imagen interna por digest, mínima, firmada y con SBOM. |
| Paquete SO | `libgomp1`, sin versión fijada | Streamlit Cloud mediante [`packages.txt`](packages.txt) | Solo dependencia de runtime nativa | Repositorio base controlado y paquetes fijados por snapshot. |
| Servicio CENtinela | Imagen construida desde [`Dockerfile`](Dockerfile) | Codex, OpenAI, Ollama o vLLM | Rootfs read-only, `no-new-privileges`, `cap_drop: ALL`, PID limit y `tmpfs` | Despliegue stateless, probes, cuotas y policy de admisión. |
| Ollama | `ollama/ollama:0.32.5` con digest `sha256:4dea9fb511947e24a84237bb636b0203abcb2ff0d3fbc7b4ff865deb91362131` | Compose local CPU/GPU | API publicada solo en `127.0.0.1`, volumen de pesos y health check | vLLM/gateway privado o servicio Ollama aislado, sin exposición directa. |
| Streamlit Community Cloud | Plataforma gestionada, versión no fijada por el repo | Replay público | `PUBLIC_DEMO_MODE="true"`, sin secretos ni acciones externas | Entorno corporativo con identidad, logs, red y SLO gobernados. |
| SQLite | Versión incluida en Python 3.12 | MVP interactivo de una instancia | WAL, claves foráneas, busy timeout y transacciones | PostgreSQL gestionado con migraciones, PITR y cifrado. |

El digest completo de Ollama está en
[`docker-compose.ollama.yml`](docker-compose.ollama.yml). Los pesos se descargan
por tag durante el bootstrap; por tanto, el servidor está fijado por digest pero
los artefactos de modelo todavía no lo están. Producción debe promocionar pesos
por digest desde un registro aprobado.

## 5. Persistencia y modelo de datos

### SQLite implementado

[`core/database.py`](core/database.py) crea de forma idempotente:

| Tabla | Contenido | Propiedad de seguridad/operación |
|---|---|---|
| `users` | Identidad local, hash, sal, iteraciones, estado y rol admin | PBKDF2-HMAC-SHA256, sal de 16 bytes y comparación constante. |
| `news` | Documento normalizado, URL, contenido, fechas, temas y metadata | URL única, hash de contenido y marca de fallback. |
| `alerts` | Nombre, palabras clave, fuentes y estado | FK de usuario y unicidad por usuario/nombre. |
| `executions` | Workflow, estado, tokens, costes, latencia y metadata | Registro agregado por ejecución. |
| `execution_steps` | Nodo, modelo, estado y métricas | Trazabilidad Planner/Scraper/Executor/Evaluator. |
| `llm_calls` | Run, modelo, uso, coste, latencia y metadata económica | Inserción idempotente por identificador. |
| `reports` | Informe, citas, Judge y fecha | Asociado a usuario y ejecución. |
| `daily_memory` | Memoria diaria por usuario | Alimentada únicamente por informes aprobados. |

El valor predeterminado de `password_pbkdf2_iterations` es `600000`. La
autenticación local es deliberadamente un control de MVP; no sustituye OIDC,
MFA ni RBAC corporativo.

### ChromaDB implementado

- Colección: `centinela_regulatory`.
- Distancia: coseno mediante metadata `hnsw:space=cosine`.
- Chunking: 1.200 caracteres con solape de 160.
- Upsert idempotente por documento/chunk y hash de contenido.
- Metadata: fuente, URL original, título, fecha, temas, hash e identidad del
  embedding.
- Consulta: filtro por identidad del embedding, deduplicación por URL, score
  vectorial más señal léxica y representación de organismos nombrados.
- Fallback: búsqueda léxica local si falla la consulta vectorial.

## 6. Proveedores, modelos y routing

### Contrato de proveedor

Los consumidores usan una interfaz común para `invoke`, `invoke_json` y
`health`. La implementación normaliza texto, salida estructurada, modelo,
identificador, latencia, metadata y uso. Las superficies son:

| Proveedor | Superficie | Facturación registrada | Estado de soporte |
|---|---|---|---|
| Codex | CLI `codex exec` con eventos JSONL | Suscripción; coste por llamada no atribuible | Implementado para ejecución individual autenticada con ChatGPT. |
| OpenAI | Responses API y `/v1/embeddings` | API por tokens y pricing configurado | Implementado; requiere `OPENAI_API_KEY`. |
| Ollama | Chat Completions y `/v1/embeddings` compatibles | Coste API 0; cómputo separado | Implementado y empaquetado en Compose CPU/GPU. |
| vLLM | Chat Completions y `/v1/embeddings` compatibles | Coste API 0; cómputo separado | Adaptador implementado; infraestructura no incluida. |

### Routing configurado por rol

Los valores siguientes son defaults del repositorio, no una afirmación de
equivalencia de calidad entre modelos.

| Rol | Codex | OpenAI API | Ollama | vLLM |
|---|---|---|---|---|
| Planner | Configurado `gpt-5.6-luna`; el nodo usa plan determinista en el perfil Codex | `gpt-4o-mini` | `qwen3.5:9b` | `Qwen/Qwen3.5-9B` |
| Filtro de noticias | Configurado `gpt-5.6-luna`; el nodo conserva selección determinista en Codex | `gpt-4o-mini` | `qwen3.5:9b` | `Qwen/Qwen3.5-9B` |
| Respuesta RAG | `gpt-5.6-luna` mediante el rol de filtro | `gpt-4o-mini` | `qwen3.5:9b` | `Qwen/Qwen3.5-9B` |
| Executor / informe | `gpt-5.6-sol` | `gpt-4o` | `qwen3.5:9b` | `Qwen/Qwen3.5-9B` |
| Evaluator / Judge | `gpt-5.6-terra` | `gpt-4o-mini` | `qwen3.5:9b` | `Qwen/Qwen3.5-9B` |
| Embeddings | `local-hash-1536` por defecto | `text-embedding-3-small` | `qwen3-embedding:0.6b` | `Qwen/Qwen3-Embedding-0.6B` |

El requisito adicional de routing fijado para esta implementación queda
preservado en la ruta OpenAI: `gpt-4o-mini` para planificación/filtrado y
`gpt-4o` para redacción final. El PDF oficial es agnóstico respecto de las
herramientas y no prescribe esos modelos. El Judge también usa `gpt-4o-mini`.
Para GPT-4o/4o-mini el adaptador omite `reasoning.effort`, porque esa familia no
acepta ese parámetro en la superficie utilizada.

Los roles pueden seleccionar proveedores distintos mediante
`PLANNER_PROVIDER`, `FILTER_PROVIDER`, `REPORT_PROVIDER` y `JUDGE_PROVIDER`. Un
perfil híbrido podría, por ejemplo, generar con OpenAI y mantener embeddings en
un endpoint privado; debe probarse como una configuración independiente.

### Routing y límites por valor

| Control | Implementación |
|---|---|
| Modelo económico | Planner, filtro, Judge y RAG usan el rol de bajo coste; el modelo de mayor capacidad se reserva para el informe. |
| Ventana de scraping | 1-30 días, 8 elementos por fuente por defecto y máximo validado de 20 en el plan. |
| Snapshot | Reutiliza documentos de SQLite recuperados en los últimos 30 minutos. |
| Evidencia del Executor | Máximo 18 documentos y 1.400 caracteres por documento. |
| Recuperación RAG | `top_k=5` por defecto, máximo 20; candidatos acotados a 50. |
| Revisiones | Una revisión de citas en Executor y una sustitución extractiva en Evaluator. |
| Embeddings locales | Hashing determinista sin token API, descarga de pesos ni secreto. |

## 7. Embeddings y RAG

El perfil predeterminado utiliza `LocalHashEmbeddings`, con palabras, bigramas
y trigramas de caracteres, hashing BLAKE2b firmado y normalización L2. Su
identidad es `centinela-local-hash-v1-1536d`. Es reproducible y adecuado para
términos regulatorios exactos, pero no sustituye un embedding neuronal en
paráfrasis complejas.

Los adaptadores remotos envían lotes a `/v1/embeddings` y propagan el uso
reportado al mismo callback de observabilidad. Cambiar proveedor o modelo cambia
la identidad del espacio y evita consultar chunks generados con otra versión.
La promoción de un embedding neuronal debe basarse en Recall@k/nDCG sobre un
conjunto chileno, no solo en inspección visual.

## 8. Observabilidad y modelo económico

[`core/observability.py`](core/observability.py) implementa
`CostTrackingCallback`, derivado de `BaseCallbackHandler` de LangChain. Registra
por llamada:

- `prompt_tokens` y `completion_tokens` reportados por el backend;
- modelo, proveedor, `run_id`, `parent_run_id` y estado;
- latencia de llamada;
- coste exacto API en USD y CLP cuando existe pricing;
- metadata de suscripción o estimación de cómputo self-hosted.

El tipo de coste no se mezcla:

| Modo | `cost_usd` de API | Interpretación |
|---|---:|---|
| OpenAI API | Calculado con tokens y tabla configurada | Coste atribuible por llamada. |
| Codex | `0` por compatibilidad del esquema | N/A por llamada; incluido en suscripción, no gratuito. |
| Ollama/vLLM | `0` | Sin tarifa API; la infraestructura se estima aparte si se configura USD/hora. |
| Backend sin `usage` | `0` y `token_usage_status=not_reported` | Coste no calculable; no significa consumo nulo. |

La conversión contractual es fija: `1 USD = 940 CLP`. Los precios configurados
por millón de tokens son `0,15/0,60 USD` para entrada/salida de GPT-4o mini,
`2,50/10,00 USD` para GPT-4o y `0,02 USD` de entrada para
`text-embedding-3-small`. Esta tabla se contrastó el 14 de agosto de 2026 con
las fichas oficiales de [GPT-4o](https://developers.openai.com/api/docs/models/gpt-4o),
[GPT-4o mini](https://developers.openai.com/api/docs/models/gpt-4o-mini) y
[text-embedding-3-small](https://developers.openai.com/api/docs/models/text-embedding-3-small).
Son valores versionados de configuración, no una promesa de tarifa futura, y
deben reconciliarse antes de cada release y de cualquier uso financiero real.

## 9. Seguridad del stack

| Riesgo | Control implementado | Refuerzo de producción |
|---|---|---|
| Secreto en configuración o error | `SecretStr`, `public_dict` sin claves y sanitización de errores | Secret manager, DLP y rotación. |
| Credenciales en URL | Validador exige URL HTTP(S) absoluta sin userinfo, query ni fragment | Endpoint catalog y policy de egress. |
| Prompt injection documental | Contenido delimitado como dato no confiable y herramientas de modelo sin acceso libre | Filtros adversariales y evaluación continua. |
| URL alucinada | Citas construidas/validadas contra catálogo y `source_ids` en RAG | Firma de snapshots y auditoría inmutable. |
| Informe de baja calidad | Regla local más LLM-as-Judge; fallo no aprueba | Judge independiente y revisión humana formal. |
| Contenedor comprometido | Usuario no root, rootfs read-only, sin capabilities y `no-new-privileges` | Imagen firmada, NetworkPolicy y runtime sandbox. |
| Ollama expuesto | Bind en loopback | Red privada, gateway autenticado y mTLS. |
| Datos locales robados | Hash de contraseñas y secretos fuera del repo | Cifrado gestionado, SSO y controles de acceso de plataforma. |

El modelo de amenazas completo está en [SECURITY.md](SECURITY.md).

## 10. Perfiles de ejecución

| Perfil | Cómo se activa | Capacidades | Restricción principal |
|---|---|---|---|
| Replay público | `PUBLIC_DEMO_MODE="true"` | Seis vistas y artefactos históricos en solo lectura | No scrapea, no genera, no indexa, no usa SQLite y no acepta secretos. |
| Codex interactivo | `AI_PROVIDER=codex` y sesión ChatGPT válida | Informe, Judge y RAG con cuota de la sesión | No es identidad de servicio cloud ni tiene coste por llamada atribuible. |
| OpenAI gestionado | `AI_PROVIDER=openai` y `OPENAI_API_KEY` | Routing GPT-4o mini/GPT-4o y embeddings opcionales | Coste por token y gobierno contractual del proveedor. |
| Ollama local | Override [`docker-compose.ollama.yml`](docker-compose.ollama.yml) | Inferencia y embeddings locales, CPU o NVIDIA | Bootstrap por tag, capacidad y concurrencia limitadas. |
| vLLM privado | `AI_PROVIDER=vllm` y endpoint compatible | Contrato de inferencia privada preparado | No hay manifiesto de infraestructura vLLM en el repositorio. |

## 11. IA utilizada y gobierno

OpenAI Codex se utilizó como herramienta de ingeniería asistida durante el
desarrollo: código, refactorización, pruebas, revisión de seguridad,
documentación y validación. Dentro del producto, la IA se utiliza para redacción,
Judge, RAG y, en perfiles HTTP, planificación/filtrado. Las URLs nunca dependen
del conocimiento del modelo.

La declaración completa, incluidos responsabilidad, privacidad y controles, se
encuentra en [AI_USAGE.md](AI_USAGE.md). Solo se han utilizado fuentes públicas
regulatorias; el repositorio no contiene datos internos de Grenergy ni
credenciales de proveedores.

## 12. Capacidades deliberadamente no implementadas

Para evitar confundir un MVP defendible con una plataforma productiva, el
repositorio **no** declara implementados:

- PostgreSQL, migraciones gestionadas, backup/PITR ni operación multi-AZ;
- SSO/OIDC, MFA, RBAC corporativo o aprovisionamiento de identidades;
- scheduler, cola, DLQ, workers asíncronos o autoscaling;
- Kubernetes, Terraform o una plataforma vLLM desplegada;
- notificaciones externas por email, Slack o push;
- OpenTelemetry y un backend externo de métricas/logs/trazas;
- modelo o embedding abierto promovido mediante benchmark dorado;
- alta disponibilidad o SLO medidos en producción.

Estas capacidades forman el objetivo descrito en
[ARQUITECTURA.md](ARQUITECTURA.md) y
[CLOUD_ARCHITECTURE.md](CLOUD_ARCHITECTURE.md), no el estado actual.
