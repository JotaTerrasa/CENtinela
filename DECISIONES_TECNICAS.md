# Decisiones técnicas de CENtinela

## Resumen ejecutivo

CENtinela es un monolito modular preparado para demostrar de extremo a extremo
un proceso de inteligencia regulatoria: captura, normalización, persistencia,
recuperación, síntesis, evaluación y trazabilidad. Streamlit, SQLite y ChromaDB
reducen la infraestructura del MVP; los contratos Python permiten separar UI,
workers y almacenamiento cuando aparezca una necesidad real de escala.

La versión actual adopta una decisión deliberada: **todo el razonamiento
generativo pasa por Codex CLI autenticado con ChatGPT**. No existe una segunda
ruta por API key. Los embeddings también son locales. Esta elección facilita
una réplica privada con la cuenta Codex del operador y elimina secretos de API
del repositorio, pero desplaza el control económico desde precios unitarios de
API hacia cuota, disponibilidad y políticas del plan ChatGPT/Codex.

La separación conceptual permanece intacta:

- los scrapers producen evidencia con URL;
- el RAG recupera fragmentos sin enviar textos a un servicio de embeddings;
- LangGraph controla los pasos deterministas y qué modelo Codex cumple cada rol
  generativo;
- los validadores locales verifican citas;
- la observabilidad conserva el uso que reporta el CLI sin inventar costes.

## Objetivos de arquitectura

1. Producir información contrastable antes que texto persuasivo.
2. Conservar operativo el corpus, las alertas y el dashboard ante fallos de
   Codex o de una fuente.
3. Registrar tokens, latencia, estado, modelo y modo de facturación por llamada.
4. No presentar como coste real una cifra que la autenticación por suscripción
   no permite atribuir por turno.
5. Limitar herramientas, contexto, permisos y superficie de prompt injection.
6. Permitir evolución a producción sin reescribir contratos de evidencia.

No es objetivo del MVP sustituir criterio jurídico, emitir decisiones de
inversión, garantizar exhaustividad frente a portales sin SLA ni ofrecer una
frontera de seguridad multi-tenant.

## ADR-001: LangGraph para Planner-Executor

**Decisión.** Implementar un `StateGraph` tipado con topología fija:

```text
START -> planner -> scraper -> executor -> evaluator -> END
```

**Razón.** El proceso tiene estado compartido, cuatro responsabilidades y una
obligación de auditoría por paso. LangGraph hace explícitas y testeables las
transiciones. Frente a una cadena informal reduce estado implícito; frente a un
ReAct abierto evita bucles, herramientas arbitrarias, latencia y consumo no
acotado.

**Responsabilidades.**

- **Planner:** determina de forma local horizonte, términos y prioridad; no
  visita URLs, redacta ni consume una llamada generativa por defecto.
- **Scraper:** consulta el registro cerrado de organismos, normaliza, deduplica,
  persiste, indexa y filtra relevancia.
- **Executor:** redacta el informe únicamente sobre la evidencia capturada y el
  informe anterior acotado.
- **Evaluator:** combina barreras locales con LLM-as-Judge.

**Consecuencia.** El flujo termina después del Judge, incluso si rechaza el
informe. Un rechazo conserva borrador, diagnóstico y métricas dentro de la
ejecución para auditoría, pero no crea un informe distribuible, artefactos ni
memoria diaria. Una nueva ejecución es una decisión del usuario. En producción
se permitiría como máximo una revisión condicional, con presupuesto y motivo
auditados.

## ADR-002: Codex CLI como único runtime generativo

**Decisión.** Encapsular `codex exec` en `core/codex_client.py` y delegar la
autenticación a una sesión ChatGPT/Codex ya establecida. No leer credenciales
desde Python ni aceptar `OPENAI_API_KEY`.

El cliente:

- pasa el prompt por `stdin`, nunca como argumento del proceso;
- usa `--json` y procesa eventos JSONL;
- solicita `--ephemeral` para no persistir rollouts;
- selecciona por configuración inline el perfil mínimo `centinela_runtime` y
  valida su esquema con `--strict-config`, sin mezclarlo con `--sandbox`;
- ignora configuración y reglas personales para mantener el perfil del
  repositorio;
- usa un directorio de trabajo aislado;
- aplica timeout y errores sanitizados;
- admite JSON Schema para salidas estructuradas;
- expone una interfaz `invoke` compatible con los consumidores existentes.

El uso de `codex exec` para automatización está documentado en
[Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode). La
sesión guardada se reutiliza por defecto.

**Ventajas.**

- una sola identidad de IA y un solo mecanismo de autenticación;
- no hay API keys en `.env`, SQLite, logs ni imagen Docker;
- se heredan permisos y límites del workspace ChatGPT;
- el CLI entrega texto, JSON estructurado y telemetría de uso en un contrato
  uniforme.

**Trade-offs.**

- iniciar un proceso por turno añade latencia frente a un cliente HTTP
  persistente;
- disponibilidad, modelos y límites dependen del plan/workspace autenticado;
- una sesión personal no es una credencial de servicio ni una solución de
  multi-tenancy;
- el modo suscripción no proporciona un coste API unitario atribuible por
  llamada;
- el despliegue horizontal exige una estrategia empresarial de identidad,
  rotación y aislamiento, no compartir indiscriminadamente un volumen personal.

## ADR-003: routing Codex por valor y complejidad

**Decisión.** Configurar cada rol de manera explícita:

| Función | Modelo | Esfuerzo | Justificación |
|---|---|---:|---|
| Planificación | determinista | — | Reglas locales, sin llamada por defecto |
| Filtrado | determinista | — | Clasificación local del catálogo |
| Chat RAG | `gpt-5.6-luna` | `low` | Respuesta breve sobre evidencia recuperada |
| Redacción final | `gpt-5.6-sol` | `high` | Síntesis ejecutiva y seguimiento de citas |
| Judge | `gpt-5.6-terra` | `medium` | Rúbrica estructurada y crítica independiente |

Luna responde el RAG, Sol se reserva para el informe y Terra separa la
evaluación del redactor. Planner y filtro son deterministas para ahorrar cuota y
hacer reproducible la selección. Modelo y esfuerzo se pasan al CLI en cada
invocación generativa, por lo que no dependen de preferencias personales.

La reducción de consumo se apoya en deduplicación previa, límites por fuente,
catálogos truncados, una única redacción, `top_k` pequeño y ausencia de ciclos
automáticos. Scraping, alertas, hashing, validación de citas y ranking lexical
de contingencia son deterministas.

Los slugs configurados deben estar habilitados para la cuenta. Un cambio de
modelo requiere evaluación de calidad, disponibilidad y telemetría; no basta
con sustituir una cadena.

## ADR-004: embeddings locales versionados con ChromaDB

**Decisión.** Mantener `chromadb.PersistentClient`, pero sustituir embeddings
remotos por `local-hash-1536`. El runtime fija ChromaDB 0.6.3: conserva la API
embebida necesaria y queda fuera del intervalo vulnerable `>=1.0.0,<=1.5.9`
publicado para CVE-2026-45829. No se inicia ni publica el servidor HTTP de
Chroma; el único acceso es local desde el proceso no privilegiado.
`PersistentClient` desactiva explícitamente la telemetría de producto y se fija
`posthog==5.4.0`, última rama compatible con la llamada positional de Chroma
0.6.3. Un volumen creado por Chroma 1.5.9 no es compatible hacia atrás: antes de
aplicar el downgrade se conserva una copia o volumen anterior y se crea un
índice limpio desde SQLite/fuentes. No se modifica en sitio un índice 1.x.

El algoritmo normaliza Unicode y combina palabras, bigramas y trigramas de
caracteres. Cada feature se proyecta mediante BLAKE2b con signo a un vector de
1.536 dimensiones y se normaliza L2. La salida es determinista entre procesos,
no descarga modelos y no realiza llamadas externas.

Los metadatos conservan `source`, `url`, `source_url`, título, fecha, temas,
hash documental, índice de fragmento y versión del embedding. Una consulta
vectorial filtra por esa versión. Si un documento procede de un espacio antiguo,
se reindexa antes de considerarlo equivalente; así no se mezclan distancias
incompatibles.

La recuperación combina distancia vectorial y señal léxica, elimina fragmentos
duplicados por URL y reserva diversidad para los organismos mencionados de
forma explícita en la pregunta. Luna no redacta URLs libres: devuelve
afirmaciones estructuradas con `source_ids` de un catálogo cerrado y la
aplicación materializa localmente cada cita `[Fuente | URL]`. Este contrato
reduce tanto resultados repetidos como enlaces inventados.

**Ventajas.**

- cero secretos, descargas y coste remoto de embeddings;
- despliegue reproducible y rápido para un corpus regulatorio pequeño;
- buen comportamiento con acrónimos y vocabulario específico como BESS, PMGD,
  transmisión o precios de nudo;
- persistencia y trazabilidad de URL intactas.

**Límite.** Es hashing lexical enriquecido, no comprensión semántica neuronal.
Paráfrasis sin solapamiento pueden perder recall. Se conserva búsqueda lexical
de contingencia y la evolución debe decidirse con un conjunto dorado, no por
preferencia tecnológica. A escala, pgvector o un servicio administrado aporta
alta disponibilidad y aislamiento, pero no resuelve por sí mismo la calidad del
embedding.

## ADR-005: uso exacto y coste no atribuible

**Decisión.** Adaptar los callbacks existentes a los eventos de Codex. El
cliente lee `turn.completed.usage` y normaliza:

- `input_tokens` como `prompt_tokens`;
- `output_tokens` como `completion_tokens`;
- `cached_input_tokens`;
- `reasoning_output_tokens`;
- modelo, estado y latencia.

No se calculan tokens desde caracteres ni se completan huecos con estimaciones.
Si una versión del CLI no publica `usage`, el `CodexResult` lo representa como
no disponible; operativamente, un cero en el esquema debe revisarse y no
interpretarse como ausencia de consumo.

Para mantener compatibilidad con SQLite y el panel, se conservan `cost_usd` y
`cost_clp`. En autenticación ChatGPT/Codex ambos valen `0`, acompañados por:

```json
{
  "billing_mode": "subscription",
  "cost_attribution": "not_attributable",
  "attributable_cost_usd": 0.0
}
```

Esto significa que no existe un precio de API atribuible a esa llamada. No
significa que Codex sea gratuito: existe una suscripción o contrato, límites de
uso y coste operativo. `USD_TO_CLP=940` se mantiene por compatibilidad con el
contrato de datos del ejercicio, pero no debe utilizarse para inferir el coste
total de propiedad.

**Métrica económica correcta para este perfil.** Tokens por informe, latencia,
errores por límites, utilización de cuota y coste de suscripción asignado por
centro de coste. Una imputación por turno requeriría una política interna
explícita. El panel permite esa imputación interna opcional en USD/CLP, claramente
etiquetada; no debe presentarse como tarifa del proveedor.

## ADR-006: autenticación ChatGPT/Codex en Docker

**Decisión.** Instalar Codex CLI en la imagen y persistir `CODEX_HOME` en el
volumen dedicado `centinela-codex-auth`, montado en
`/home/centinela/.codex`. El operador completa:

```bash
docker compose exec centinela codex login --device-auth
```

El device flow es apropiado para un contenedor sin navegador. La
[documentación de autenticación](https://learn.chatgpt.com/docs/auth) exige
habilitar Device Code Login en la cuenta o workspace y tratar `auth.json` como
una contraseña.

**Controles.**

- el volumen no se copia en la imagen ni se versiona;
- solo se monta en el servicio `centinela` y en la ruta del usuario no
  privilegiado;
- la imagen crea datos, Chroma, workdir e informes para el UID 10001;
- debe permanecer escribible para renovación de tokens;
- cada generación fuerza `forced_login_method="chatgpt"` y falla si las
  credenciales activas pertenecen a otro método;
- el perfil inline `centinela_runtime` concede solo `:minimal` y lectura del
  workdir; deniega `/app`, la raíz del proyecto y `CODEX_HOME`;
- los comandos del modelo heredan un entorno vacío salvo un `PATH` mínimo, no
  tienen red ni escritura y no pueden solicitar elevación interactiva;
- `docker compose down` lo conserva;
- acceso al daemon Docker implica acceso potencial al volumen y debe limitarse;
- logout, revocación de la cuenta y borrado controlado del volumen forman parte
  de la baja operativa.

**No decidido para producción.** Un volumen con sesión personal no es un vault.
Una implantación corporativa debe evaluar access tokens empresariales para
automatización confiable, identidad dedicada, rotación, revocación, auditoría y
políticas de workspace. No debe exponerse este patrón en un servicio público ni
compartirse entre tenants.

## ADR-007: scraping resiliente de fuentes oficiales

**Decisión.** Mantener un registro cerrado de organismos y adaptadores. Cada
petición aplica timeout, reintentos acotados, User-Agent, normalización y
aislamiento de errores. La URL canónica es identidad de deduplicación y evidencia.

Ante cambios o bloqueos se prueban feeds y sitemaps oficiales. Como último
recurso del MVP, `r.jina.ai` lee la misma URL pública; el registro etiqueta el
fallback y conserva la URL oficial como fuente. Si también falla, la ejecución
continúa con los demás organismos y expone el error. Nunca se inyectan noticias
sintéticas.

BeautifulSoup y feeds ofrecen transparencia suficiente para el MVP. Playwright
solo se justifica para una fuente que requiera JavaScript, porque incrementa
imagen, latencia y mantenimiento. Producción requiere revisión de términos,
`robots.txt`, frecuencia, caché HTTP, backoff y límites por host.

## ADR-008: SQLite y autenticación local para el MVP

**Decisión.** Usar SQLite con WAL, claves foráneas y transacciones cortas.
Contraseñas con PBKDF2-HMAC-SHA256, sal aleatoria y comparación constante.
Alertas, informes y ejecuciones se asocian a `user_id`.

Es una decisión de reproducibilidad, no de escala. Streamlit Session State no
ofrece SSO, MFA, recuperación, SCIM, bloqueo por intentos ni revocación
corporativa. Producción debe migrar a PostgreSQL, SSO OIDC/SAML, cookies seguras,
RBAC, rate limiting y auditoría inmutable.

La identidad local de CENtinela no concede acceso a Codex. Ambas capas deben
administrarse y auditarse de forma independiente.

## ADR-009: Streamlit como interfaz del MVP

**Decisión.** Mantener una aplicación multipanel en un único `app.py` y módulos
de dominio separados.

Streamlit permite demostrar login, scraping, tabla, alertas, informe, chat,
descargas y observabilidad sin mantener dos stacks. Su modelo de rerun obliga a
acciones idempotentes y a no iniciar red durante imports. Cada informe crea sus
propios componentes para no mezclar trazas entre sesiones.

El siguiente umbral de escala justifica FastAPI, workers y frontend separado;
no se introduce esa complejidad antes de necesitar concurrencia, colas o una
API contractual.

## Trazabilidad y control de alucinaciones

La defensa se implementa en capas:

1. Solo entran documentos con organismo y URL HTTP(S).
2. El Planner no inventa destinos: las fuentes están registradas en código.
3. El Executor recibe un catálogo cerrado y delimitado como datos no confiables.
4. Toda afirmación material exige `[Fuente | URL]` en la misma línea.
5. Un validador comprueba sintaxis y pertenencia exacta al catálogo.
6. Terra evalúa relevancia, cobertura, claridad y trazabilidad.
7. La interfaz permite abrir la fuente primaria.
8. Ante fallo interno durante una ejecución ya autorizada puede construirse una
   salida extractiva para evaluación, pero la UI no inicia ni presenta una
   generación como IA sin una sesión Codex válida.

Una cita acredita procedencia, no verdad jurídica. El Judge tampoco certifica
vigencia o aplicabilidad. El especialista regulatorio permanece en el circuito.

## Memoria y temporalidad

SQLite conserva el último informe aprobado del usuario. El Executor recibe una
versión limitada para distinguir novedades, continuidad y ausencia de cambios.
Un informe rechazado queda en la traza de ejecución para auditoría, pero no se
distribuye ni entra en la memoria diaria. No se mantiene memoria conversacional
ilimitada porque aumentaría consumo, mezclaría evidencia obsoleta y dificultaría
rectificación.

La fecha civil usa `America/Santiago`; timestamps y latencias de auditoría se
persisten en UTC.

## Modos degradados

- Si falla una fuente, se conserva el resto y se muestra el fallo.
- Si Codex no está autenticado, datos, login, dashboard, scraping, alertas e
  índice local siguen operativos; la UI bloquea informe y RAG generativos y no
  presenta un fallback como si fuera una respuesta de IA.
- Si falla la consulta vectorial, se intenta recuperación lexical.
- Si Chroma no está disponible, las noticias permanecen en SQLite.
- Si falla el Judge, el informe no se presenta como aprobado por IA.
- Si no hay novedades, no se genera contenido de relleno.

Los errores se sanitizan para no almacenar prompts completos, tokens de acceso
ni cabeceras de autenticación.

## Evaluación propuesta

| Dimensión | Métrica | Umbral inicial |
|---|---|---:|
| Captura | fuentes disponibles / esperadas | >= 6/7 |
| Frescura | demora publicación-ingesta | p95 < 6 h |
| Retrieval | Recall@5 de documento relevante | >= 0,80 |
| Citas | afirmaciones con cita válida | 100 % |
| Groundedness | afirmaciones respaldadas según revisor | >= 0,95 |
| Relevancia | ítems útiles para activos objetivo | >= 0,80 |
| Consumo | tokens por informe por modelo | presupuesto interno |
| Disponibilidad IA | ejecuciones Codex completadas | >= 99 % en ventana acordada |
| Latencia | informe end-to-end | p95 acordado |

El Judge es una señal continua, no el benchmark único. Debe calibrarse contra
un conjunto dorado humano y versionar prompt, modelo, esfuerzo y rúbrica.

En este MVP, “llamadas a herramientas” se observa a dos niveles: LangGraph
registra el nodo Scraper como paso con estado, latencia, cobertura y
`capture_stats`, mientras cada documento conserva organismo, URL, método y
estado de recuperación. No existe aún una tabla independiente por petición HTTP
o intento de fallback. Producción debe añadir spans por host/URL con estado,
latencia, reintento y bytes, aplicando retención y sanitización para no convertir
la telemetría en un canal de datos sensibles.

## Validación real de aceptación — 13 de agosto de 2026

La entrega incluye en `docs/demo/` una muestra generada en el contenedor final.
No es una promesa de SLA ni un benchmark estadístico; sí demuestra el recorrido
completo con datos y telemetría reales:

| Prueba | Resultado observado |
|---|---:|
| Fuentes recuperadas | 7/7, sin errores de captura |
| Suite determinista | 88 pruebas superadas |
| Informe aprobado | 78/100, validación determinista correcta |
| Informe · tokens | 35.203 entrada + 3.702 salida |
| Informe · llamadas / latencia | 2 llamadas / 77,9 s end-to-end |
| RAG CNE + SEA | ambas fuentes presentes, URLs válidas |
| RAG · tokens / latencia | 12.463 entrada + 229 salida / 8,81 s |
| Atribución económica | N/A por suscripción; contadores USD/CLP = 0 |

Una primera ejecución del informe fue rechazada correctamente y no creó
artefactos distribuibles. Ese fallo permitió endurecer el catálogo del Judge:
ahora siempre recibe todos los documentos realmente citados, aunque el corpus
de evidencia general se trunque por presupuesto. La repetición fue aprobada sin
citas desconocidas ni líneas materiales sin cita. Esto ilustra el valor del
control fail-closed y de conservar trazas de ejecuciones rechazadas.

## Riesgos conocidos

1. Cambios de HTML, WAF y certificados de portales estatales.
2. Fechas inconsistentes y documentos PDF pendientes de extracción profunda.
3. Cita correcta con interpretación normativa equivocada.
4. Duplicados entre organismos y ramas legislativas.
5. Prompt injection dentro de contenido público.
6. Recall limitado del embedding local ante paráfrasis sin vocabulario común.
7. Expiración, revocación o límites de la sesión ChatGPT/Codex.
8. Modelos configurados no disponibles en un workspace concreto.
9. Exposición del volumen de autenticación a usuarios con acceso Docker.
10. Latencia y concurrencia por iniciar un proceso CLI por turno.
11. SQLite, Chroma local y Session State insuficientes para alta disponibilidad.
12. Campos económicos malinterpretados: cero atribuible no equivale a gratuito.

## Roadmap a producción

### Fase 1 — endurecimiento funcional

- Contratos de fuente y monitores sintéticos.
- Extracción de PDF/adjuntos, OCR y versionado documental.
- Scheduler idempotente, caché HTTP y dead-letter queue.
- Dataset dorado para retrieval, groundedness y citas.
- Presupuestos de tokens, timeout y circuit breakers por rol.
- Runbook de login, expiración, logout, rotación y revocación Codex.
- Prueba automatizada que verifique presencia de `turn.completed.usage`.

### Fase 2 — plataforma corporativa

- FastAPI, workers y colas administradas.
- PostgreSQL + pgvector, object storage y backups probados.
- SSO, RBAC, cifrado, WAF, rate limiting y SIEM.
- Identidad Codex dedicada o access token empresarial en runners confiables,
  sujeto a política del workspace.
- Volúmenes y namespaces separados por entorno y tenant.
- OpenTelemetry y objetivos de servicio.

### Fase 3 — producto regulatorio

- Taxonomía y knowledge graph de normas, activos y obligaciones.
- Detección de cambios semánticos y plazos accionables.
- Workflow humano revisar/aprobar/publicar con firma.
- Notificaciones por email, Teams o Slack.
- Multi-país y evaluación por jurisdicción.
- Feedback explícito para ranking sin entrenar sobre datos sensibles por defecto.

## Compatibilidad con el enunciado original

El enunciado oficial es tecnológicamente agnóstico. El perfil Codex-only mantiene
Planner-Executor, LLM-as-Judge, routing por valor, Chroma, citas y observabilidad,
y documenta con precisión dónde se usa generación, qué pasos son deterministas y
qué costes son atribuibles. No se presenta la suscripción como tarifa API.

## Decisiones que cambiarían con escala

| Señal | Cambio |
|---|---|
| Decenas de usuarios concurrentes | Separar API, workers y UI; migrar SQLite |
| Cientos de miles de fragmentos | pgvector o índice administrado evaluado |
| Varias réplicas | Identidad Codex de servicio y aislamiento por replica/tenant |
| Informes vinculantes | Aprobación humana, firma y control de versiones |
| Datos internos o personales | DLP, residencia, retención y acceso reforzado |

## Defensa de la solución

El valor de CENtinela no depende de producir más texto. Depende de convertir
siete canales dispersos en una cadena verificable y operable: evidencia,
recuperación, síntesis, evaluación y revisión humana. La arquitectura Codex-only
reduce secretos y simplifica la demo personal, a cambio de límites de
industrialización que se declaran y tienen un roadmap concreto.
